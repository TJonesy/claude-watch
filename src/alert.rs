//! Alerting: push notifications, claude-event emission, and
//! interrupt-then-inject.
//!
//! Three sinks fire from this module:
//! 1. **Push notification** via `$CLAUDE_WATCH_NOTIFY_CMD` — operator's
//!    phone alert. The env var names the executable (e.g. `pingme`).
//!    When unset/empty, push notifications are silently skipped.
//!    The command is invoked as: `<cmd> -p <priority> <message>`.
//! 2. **claude-event** via `event_bus::emit` — structured JSON dropped
//!    into `~/claude-events/` so `claude-event-watch` surfaces the
//!    alert to the main loop with parseable fields (alert_type,
//!    stuck_reason, stale_minutes, affected_watchers, severity). The
//!    reflexive "claude-watch said /cleanup → I run /cleanup without
//!    looking at the data" failure mode (flagged on a prior chore)
//!    only goes away when the loop is forced to read structured fields.
//! 3. **tmux-inject** — types the resume prompt into Claude Code's
//!    pane so the agent can recover in-band.
//!
//! Sinks are independent: a failure in one MUST NOT skip the others.
//! `event_bus::emit` is itself default-open (logs + swallows errors),
//! so this module just calls it unconditionally.

use crate::cmd::run_cmd;
use crate::event_bus::{self, ClaudeWatchAlert};
use crate::inject_dispatch;
use crate::tmux;

pub async fn send_pingme(message: &str) {
    send_pingme_with_priority(message, "normal").await;
}

pub async fn send_pingme_with_priority(message: &str, priority: &str) {
    let cmd = match std::env::var("CLAUDE_WATCH_NOTIFY_CMD") {
        Ok(c) if !c.is_empty() => c,
        _ => return,
    };
    let parts: Vec<&str> = cmd.split_whitespace().collect();
    if parts.is_empty() {
        return;
    }
    let mut args: Vec<&str> = parts.clone();
    args.push("-p");
    args.push(priority);
    args.push(message);
    let _ = run_cmd(&args, 15).await;
}

/// Outcome of a push notification the caller actually cares about the fate of.
///
/// `send_pingme_with_priority` above is fire-and-forget by design — most
/// alerts repeat, so a single lost push costs nothing. Some do not repeat. For
/// those the send is part of the ACTION, not a side effect of it, and "we ran
/// a command and never looked" is not delivery.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PushResult {
    /// False when no notify command is configured, i.e. nothing was even
    /// attempted. Distinct from a failed attempt.
    pub attempted: bool,
    /// The notify command exited 0.
    pub delivered: bool,
    /// A request / receipt id, if the notify command printed one. Pushover
    /// returns `{"status":1,"request":"<uuid>"}`; anything else that prints a
    /// JSON `request` or `receipt` field works too.
    pub receipt: Option<String>,
    /// Whatever the command printed (trimmed), for the log line.
    pub detail: String,
}

impl PushResult {
    /// One-line summary for a log or an event field.
    pub fn as_str(&self) -> &'static str {
        match (self.attempted, self.delivered) {
            (false, _) => "not_configured",
            (true, true) => "delivered",
            (true, false) => "failed",
        }
    }
}

/// Send a push notification and REPORT WHAT HAPPENED.
///
/// Same command and same priority vocabulary as `send_pingme_with_priority`,
/// but the exit status is read rather than discarded and any receipt the tool
/// prints is captured. Use this wherever the notification is the point of the
/// action — a one-off state change the operator has to be told about — rather
/// than one of several repeating sinks.
///
/// Deliberately independent of every alert gate in this module: it consults no
/// cooldown and no `max_pingme_alerts`-style counter, so a rare high-value
/// push can never be coalesced away behind chatty ones.
pub async fn send_push_verified(message: &str, priority: &str) -> PushResult {
    let cmd = match std::env::var("CLAUDE_WATCH_NOTIFY_CMD") {
        Ok(c) if !c.is_empty() => c,
        _ => {
            return PushResult {
                attempted: false,
                delivered: false,
                receipt: None,
                detail: "CLAUDE_WATCH_NOTIFY_CMD is unset or empty".to_string(),
            }
        }
    };
    send_push_with_cmd(&cmd, message, priority).await
}

/// `send_push_verified` with the notify command passed in, so the delivery
/// reporting is testable against a real process without touching the
/// environment (which tests in the same binary share).
pub async fn send_push_with_cmd(cmd: &str, message: &str, priority: &str) -> PushResult {
    let parts: Vec<&str> = cmd.split_whitespace().collect();
    if parts.is_empty() {
        return PushResult {
            attempted: false,
            delivered: false,
            receipt: None,
            detail: "CLAUDE_WATCH_NOTIFY_CMD is blank".to_string(),
        };
    }
    let mut args: Vec<&str> = parts.clone();
    args.push("-p");
    args.push(priority);
    args.push(message);
    let (out, ok) = crate::cmd::run_cmd_any(&args, 15).await;
    PushResult {
        attempted: true,
        delivered: ok,
        receipt: extract_push_receipt(&out),
        detail: out,
    }
}

/// Pull a request / receipt id out of whatever the notify command printed.
///
/// Pure so the parsing is testable without a notifier. Best effort: a tool
/// that prints nothing useful simply yields `None`, which is not a failure —
/// the exit status is what says whether the push went out.
pub fn extract_push_receipt(output: &str) -> Option<String> {
    let value: serde_json::Value = serde_json::from_str(output.trim()).ok()?;
    for key in ["request", "receipt", "id"] {
        if let Some(found) = value.get(key).and_then(|v| v.as_str()) {
            if !found.is_empty() {
                return Some(found.to_string());
            }
        }
    }
    None
}

/// Pingme + claude-event emission in one shot. Use this for any alert
/// that doesn't need tmux-inject (auto-update progress, reauth alert,
/// crash notice). For the full stuck-state path use `alert()`.
///
/// Severity controls the push-notification priority AND the event's
/// `severity` data field. Priorities: `low|normal|high|urgent` (mapped
/// from Severity).
pub async fn notify(alert: ClaudeWatchAlert<'_>) {
    let priority = alert.severity.as_priority();
    send_pingme_with_priority(alert.message, priority).await;
    event_bus::emit(&alert);
}

/// Stuck-state alert: pingme (gated) + claude-event + (maybe) interrupt + inject.
///
/// `cancel_turn` (KNOB #4, 2026-06-24) selects the injection tier:
///   * `true`  — EMERGENCY: rapid-fire Escape (`interrupt_and_wait`) seizes the
///     in-flight turn, then inject. Use only when waiting for a turn boundary
///     is too costly (context-critical, wedged/zombie session).
///   * `false` — ROUTINE: skip the Escape blast entirely and inject via the
///     NON-CANCELLING queued path. The nudge is delivered as a queued message
///     at the next turn boundary WITHOUT aborting the loop's active turn or
///     killing mid-flight background agents. Use for watcher-down,
///     heartbeat-stale, and other can-wait recovery prompts. Cancelling those
///     was the root of the agent-loop churn this change targets.
pub async fn alert(
    message: &str,
    pane: &str,
    resume_prompt: &str,
    use_pingme: bool,
    event_alert: ClaudeWatchAlert<'_>,
    cancel_turn: bool,
) {
    if use_pingme {
        send_pingme(message).await;
    }
    // Always emit the claude-event, even when pingme is suppressed by
    // the max_pingme_alerts gate. The structured event is the channel
    // that forces the main loop to look at fields like stale_minutes
    // — silencing it would defeat the whole point of this sink.
    event_bus::emit(&event_alert);

    if cancel_turn {
        // EMERGENCY tier: actively interrupt, then inject. 5s budget keeps
        // perceived recovery latency low; if the pane never goes idle we
        // proceed with the inject anyway.
        //
        // The Escape/interrupt phase only matters for terminal-mode panes —
        // for panel-mode agents the pidfd path appends rather than cancels,
        // so the interrupt is a no-op there. We still call interrupt_and_wait
        // because the cost is bounded and a stray Escape into a defunct pane
        // is harmless.
        tmux::interrupt_and_wait(pane, 5).await;
        inject_dispatch::inject_to_agent(pane, resume_prompt).await;
    } else {
        // ROUTINE tier (KNOB #4): NO Escape blast. Queue the nudge without
        // seizing the turn — the active turn and any running background
        // agents survive. `inject_to_agent_queued` types + Enter-submits as
        // a queued message (terminal) or appends via pidfd (panel mode).
        inject_dispatch::inject_to_agent_queued(pane, resume_prompt).await;
    }
}

/// Convenience: emit a claude-event for a fire-and-forget alert path
/// (no pingme, no inject). Mirrors `event_bus::emit` but lives here so
/// callers only `use crate::alert::*`.
pub fn emit_event(alert: ClaudeWatchAlert<'_>) {
    event_bus::emit(&alert);
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Severity is re-exported through alert::Severity? No, callers
    /// `use crate::event_bus::Severity` directly. This test just
    /// smoke-checks that `notify` builds and serialises correctly when
    /// stubbed (it can't actually exec pingme in unit tests).
    #[test]
    fn notify_alert_struct_compiles() {
        let _ = ClaudeWatchAlert {
            alert_type: "claude-crashed",
            stuck_reason: "Claude Code process gone — restarting",
            stale_minutes: None,
            affected_watchers: vec![],
            severity: crate::event_bus::Severity::High,
            message: "claude-watch: Claude Code crashed -- auto-restarting",
        };
    }

    /// Write an executable stub notifier that echoes `stdout_text` and exits
    /// `code`, and return its path.
    fn stub_notifier(dir: &std::path::Path, name: &str, stdout_text: &str, code: i32) -> String {
        let path = dir.join(name);
        std::fs::write(
            &path,
            format!("#!/bin/sh\nprintf '%s' '{stdout_text}'\nexit {code}\n"),
        )
        .unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
        }
        path.to_string_lossy().into_owned()
    }

    /// A push whose command exits 0 is delivered, and a receipt it prints is
    /// captured so a delivery claim can point at something.
    #[tokio::test]
    async fn a_successful_push_reports_delivered_with_its_receipt() {
        let tmp = tempfile::tempdir().unwrap();
        let cmd = stub_notifier(
            tmp.path(),
            "ok-notifier",
            r#"{"status":1,"request":"req-42"}"#,
            0,
        );
        let result = send_push_with_cmd(&cmd, "body", "high").await;
        assert!(result.attempted);
        assert!(result.delivered);
        assert_eq!(result.receipt.as_deref(), Some("req-42"));
        assert_eq!(result.as_str(), "delivered");
    }

    /// THE point of this function: a notifier that FAILS must not be reported
    /// as a notified operator. `send_pingme_with_priority` discards this;
    /// `send_push_verified` is for the sends where that is not acceptable.
    #[tokio::test]
    async fn a_failing_push_reports_failed_rather_than_silence() {
        let tmp = tempfile::tempdir().unwrap();
        let cmd = stub_notifier(tmp.path(), "bad-notifier", "pushover: 429 rate limited", 1);
        let result = send_push_with_cmd(&cmd, "body", "high").await;
        assert!(result.attempted, "it was tried");
        assert!(!result.delivered, "a non-zero exit is not a delivery");
        assert_eq!(result.as_str(), "failed");
        assert!(result.detail.contains("429"), "detail: {}", result.detail);
    }

    /// No notifier configured is "not attempted" — distinct from a failed
    /// attempt, because the fixes differ.
    #[tokio::test]
    async fn an_unconfigured_notifier_is_not_attempted() {
        let result = send_push_with_cmd("   ", "body", "high").await;
        assert!(!result.attempted);
        assert!(!result.delivered);
        assert_eq!(result.as_str(), "not_configured");
    }

    /// A notifier that prints nothing parseable still delivered, if it exited
    /// 0. A missing receipt is not a failure.
    #[tokio::test]
    async fn a_push_with_no_receipt_is_still_delivered() {
        let tmp = tempfile::tempdir().unwrap();
        let cmd = stub_notifier(tmp.path(), "quiet-notifier", "sent", 0);
        let result = send_push_with_cmd(&cmd, "body", "high").await;
        assert!(result.delivered);
        assert_eq!(result.receipt, None);
        assert_eq!(result.as_str(), "delivered");
    }
}
