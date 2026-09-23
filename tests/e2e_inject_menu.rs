//! E2e test for answering the menu a submitted slash command opens.
//!
//! Stands up a real tmux pane running a small fake menu (a Python script that
//! draws a Claude-Code-shaped `❯ 1.` selection list, reads arrow keys and
//! Enter in raw mode, and records the choice) and drives
//! `inject_menu::settle_menu` against it. Exercises what unit tests over
//! captured text cannot: the render delay, one-verified-row-at-a-time
//! navigation through real keystrokes, and the menu closing after Enter.

use claude_watch::inject_menu::{settle_menu, MenuOutcome, MenuPolicy};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicU32, Ordering};
use std::time::Duration;

static TEST_COUNTER: AtomicU32 = AtomicU32::new(0);

struct TmuxSession {
    name: String,
}

impl Drop for TmuxSession {
    fn drop(&mut self) {
        let _ = Command::new("tmux")
            .args(["kill-session", "-t", &self.name])
            .output();
    }
}

/// The fake menu. argv: title, result file, delay-before-render (s), options…
/// Every key it receives is appended to the result file as `key:<name>`; the
/// final choice as `chose:<n>`. After a choice it redraws an idle `❯` prompt,
/// the way Claude Code closes a menu.
const FAKE_MENU: &str = r#"
import sys, os, time, tty, termios
title, out, delay = sys.argv[1], sys.argv[2], float(sys.argv[3])
opts = sys.argv[4:]
time.sleep(delay)
sel = 0
def draw():
    sys.stdout.write("\x1b[2J\x1b[H")
    sys.stdout.write("> /model something\r\n\r\n")
    sys.stdout.write(" " + title + "\r\n")
    sys.stdout.write(" Your next response will be slower\r\n\r\n")
    for i, o in enumerate(opts):
        cur = "❯" if i == sel else " "
        sys.stdout.write(" %s %d. %s\r\n" % (cur, i + 1, o))
    sys.stdout.flush()
def log(s):
    with open(out, "a") as f:
        f.write(s + "\n")
fd = sys.stdin.fileno()
old = termios.tcgetattr(fd)
tty.setraw(fd)
try:
    draw()
    while True:
        c = os.read(fd, 1)
        if c == b"\x1b":
            seq = os.read(fd, 2)
            if seq == b"[A":
                log("key:Up"); sel = max(0, sel - 1)
            elif seq == b"[B":
                log("key:Down"); sel = min(len(opts) - 1, sel + 1)
            draw()
        elif c in (b"\r", b"\n"):
            log("key:Enter"); log("chose:%d" % (sel + 1))
            break
        else:
            log("key:" + c.decode(errors="replace"))
finally:
    termios.tcsetattr(fd, termios.TCSADRAIN, old)
sys.stdout.write("\x1b[2J\x1b[H✓ Done\r\n\r\n❯ \r\n")
sys.stdout.flush()
time.sleep(60)
"#;

fn have(cmd: &str) -> bool {
    Command::new(cmd)
        .arg("-V")
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false)
        || Command::new(cmd)
            .arg("--version")
            .output()
            .map(|o| o.status.success())
            .unwrap_or(false)
}

/// Start the fake menu in a fresh tmux session; returns (guard, pane, result file).
fn start_menu(title: &str, delay: &str, opts: &[&str]) -> Option<(TmuxSession, String, PathBuf)> {
    if !have("tmux") || !have("python3") {
        eprintln!("skipping: tmux or python3 not available");
        return None;
    }
    let n = TEST_COUNTER.fetch_add(1, Ordering::SeqCst);
    let name = format!("cw-menu-{}-{}", std::process::id(), n);
    let dir = std::env::temp_dir().join(&name);
    std::fs::create_dir_all(&dir).ok()?;
    let script = dir.join("menu.py");
    std::fs::write(&script, FAKE_MENU).ok()?;
    let out = dir.join("result");
    let mut argv = vec![
        "python3".to_string(),
        script.display().to_string(),
        title.to_string(),
        out.display().to_string(),
        delay.to_string(),
    ];
    argv.extend(opts.iter().map(|s| s.to_string()));
    let quoted: Vec<String> = argv
        .iter()
        .map(|a| format!("'{}'", a.replace('\'', "'\\''")))
        .collect();
    let ok = Command::new("tmux")
        .args(["new-session", "-d", "-s", &name, "-x", "120", "-y", "30"])
        .arg(quoted.join(" "))
        .status()
        .map(|s| s.success())
        .unwrap_or(false);
    if !ok {
        eprintln!("skipping: could not start tmux session");
        return None;
    }
    let pane = format!("{}:0.0", name);
    Some((TmuxSession { name }, pane, out))
}

fn result(path: &Path) -> String {
    std::fs::read_to_string(path).unwrap_or_default()
}

fn policy(answer: Option<u32>) -> MenuPolicy {
    MenuPolicy {
        answer,
        wait_secs: None,
        auto_answer: true,
    }
}

/// The reported bug: `/model` submitted, the confirmation renders a beat
/// later, and nothing answers it. Now the allowlisted confirmation is
/// answered with Enter on the "Yes, switch to" row — no digit typed.
#[tokio::test]
async fn model_confirmation_is_auto_answered_after_it_renders() {
    let Some((_g, pane, out)) = start_menu(
        "Switch model?",
        "1.0",
        &["Yes, switch to Fable 5", "No, go back"],
    ) else {
        return;
    };
    let outcome = settle_menu(&pane, "/model claude-fable-5[1m]", &policy(None)).await;
    assert!(
        matches!(
            outcome,
            MenuOutcome::Answered {
                answer: 1,
                auto: true,
                ..
            }
        ),
        "got {:?}",
        outcome
    );
    assert_eq!(result(&out), "key:Enter\nchose:1\n");
}

/// `--answer N` moves the cursor with verified Down presses, then Enter.
#[tokio::test]
async fn explicit_answer_navigates_to_the_named_row() {
    let Some((_g, pane, out)) = start_menu("Pick one", "0.3", &["Alpha", "Beta", "Gamma"]) else {
        return;
    };
    let outcome = settle_menu(&pane, "/something", &policy(Some(3))).await;
    assert!(
        matches!(
            outcome,
            MenuOutcome::Answered {
                answer: 3,
                auto: false,
                ..
            }
        ),
        "got {:?}",
        outcome
    );
    assert_eq!(result(&out), "key:Down\nkey:Down\nkey:Enter\nchose:3\n");
}

/// An unknown menu without `--answer` is reported and NOT touched.
#[tokio::test]
async fn unknown_menu_is_reported_not_answered() {
    let Some((_g, pane, out)) =
        start_menu("Do you want to delete everything?", "0.3", &["Yes", "No"])
    else {
        return;
    };
    let outcome = settle_menu(&pane, "/model opus", &policy(None)).await;
    match outcome {
        MenuOutcome::Unanswered { menu } => {
            assert_eq!(menu.options.len(), 2);
            assert_eq!(menu.selected, 1);
        }
        other => panic!("expected Unanswered, got {:?}", other),
    }
    tokio::time::sleep(Duration::from_millis(500)).await;
    assert_eq!(result(&out), "", "no keystroke may reach an unknown menu");
}

/// `--answer` naming a row the menu does not have presses nothing.
#[tokio::test]
async fn answer_out_of_range_presses_nothing() {
    let Some((_g, pane, out)) = start_menu("Pick one", "0.2", &["Alpha", "Beta"]) else {
        return;
    };
    let outcome = settle_menu(&pane, "/something", &policy(Some(5))).await;
    assert!(
        matches!(outcome, MenuOutcome::AnswerFailed { answer: 5, .. }),
        "got {:?}",
        outcome
    );
    tokio::time::sleep(Duration::from_millis(500)).await;
    assert_eq!(result(&out), "");
}
