#!/usr/bin/env python3
"""Tests for the AST-based privilege-escalation gate matcher.

The sudo-blocking obligations historically detected ``sudo`` with a regex
tool_pattern (``Bash:\bsudo\b`` / ``mcp__host-bash__run_*:\bsudo\b``). A raw
``\bsudo\b`` matches the substring ANYWHERE in the command -- inside a quoted
string, an argument, a comment, or a heredoc body -- so ``grep 'sudo x'``,
``echo sudoers``, and a PR body mentioning sudo all false-positive DENIED.

The fix: two AST-aware tool_pattern forms backed by
``shell_ast.command_invokes`` (via ``obligations._command_invokes``):

  * ``Bashinvoke:<names>``                      (the in-container Bash tool)
  * ``mcp__host-bash__run_command:invoke:<names>`` / ``..._run_script:...``

They match iff a target name is a real command-POSITION word -- a head OR a
wrapper such as ``sudo`` in ``sudo apt-get`` (which the plain ``Bashcmd:``
form cannot see, because ``command_names`` strips ``sudo`` as a wrapper) --
and NOT when the name is quoted / argument / comment / heredoc data.
FAIL-CLOSED: an unparseable command falls back to a word-boundary match so a
real sudo hidden behind a broken construct still trips the gate.

Run::

    uv run --python 3.11 --with pytest \
        pytest tools/obligations/tests/test_command_invokes.py -v
"""

import importlib.machinery
import importlib.util
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
OBLIGATIONS = HERE.parent / "obligations"


def _load_obligations():
    spec = importlib.util.spec_from_loader(
        "obligations_cli",
        importlib.machinery.SourceFileLoader("obligations_cli", str(OBLIGATIONS)),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


obl = _load_obligations()
m = obl._tool_pattern_matches

# The escalation gate spec used in production once the manifests are repointed.
BASH = "Bashinvoke:sudo,doas"
HB_CMD = "mcp__host-bash__run_command:invoke:sudo,doas"
HB_SCRIPT = "mcp__host-bash__run_script:invoke:sudo,doas"


def _run_command(cmd: str) -> str:
    return json.dumps({"command": cmd})


def _run_script(script: str, interpreter: str = "bash") -> str:
    return json.dumps({"interpreter": interpreter, "script": script})


# --------------------------------------------------------------------------
# Bashinvoke: in-container Bash tool
# --------------------------------------------------------------------------

# must-BLOCK: sudo is an actual command / wrapper invocation.

def test_block_sudo_apt_get():
    assert m(BASH, "Bash", "sudo apt-get install -y jq") is True


def test_block_pipe_into_sudo():
    assert m(BASH, "Bash", "foo | sudo bar") is True


def test_block_and_sudo():
    assert m(BASH, "Bash", "x && sudo y") is True


def test_block_semicolon_sudo():
    assert m(BASH, "Bash", "echo hi ; sudo rm -rf /x") is True


def test_block_substitution_sudo():
    assert m(BASH, "Bash", "msg=$(sudo id)") is True


def test_block_bare_sudo():
    assert m(BASH, "Bash", "sudo -v") is True


def test_block_env_prefixed_sudo():
    assert m(BASH, "Bash", "env FOO=1 sudo apt-get update") is True
    assert m(BASH, "Bash", "FOO=1 sudo apt-get update") is True


def test_block_nohup_wrapped_sudo():
    assert m(BASH, "Bash", "nohup sudo tee /etc/hosts") is True


def test_block_abs_path_sudo():
    assert m(BASH, "Bash", "/usr/bin/sudo apt-get") is True


def test_block_doas():
    assert m(BASH, "Bash", "doas pkg_add curl") is True


# must-PASS: sudo appears only as a string / arg / comment / heredoc.

def test_pass_grep_sudo_string():
    assert m(BASH, "Bash", "grep 'sudo x' file") is False


def test_pass_sudo_as_argument():
    assert m(BASH, "Bash", "echo sudo apt-get") is False


def test_pass_double_quoted_sudo():
    assert m(BASH, "Bash", 'echo "run sudo now"') is False


def test_pass_sudoers_substring():
    assert m(BASH, "Bash", "cat /etc/sudoers.d/foo") is False
    assert m(BASH, "Bash", "grep -r sudoers /etc") is False


def test_pass_write_text_containing_sudoers():
    assert m(BASH, "Bash", "echo 'add NOPASSWD to sudoers' >> notes.txt") is False


def test_pass_heredoc_body_sudo():
    assert m(
        BASH, "Bash", "cat <<'EOF'\nremember: sudo apt-get install\nEOF"
    ) is False


def test_pass_queue_add_mentioning_sudo():
    assert m(
        BASH, "Bash", "session-task queue add 'must not run sudo apt-get'"
    ) is False


def test_pass_plain_command():
    assert m(BASH, "Bash", "apt-get update") is False


def test_bashinvoke_only_bash_tool():
    assert m(BASH, "Read", "sudo apt-get") is False


def test_bashinvoke_failsafe_unparseable_with_sudo():
    # Unterminated quote => ShellParseError => word-boundary fallback: the raw
    # string DOES contain sudo as a word => match (fail-closed, safe).
    assert m(BASH, "Bash", "sudo 'unterminated") is True
    # ...and does NOT match when sudo is absent from the raw unparseable string.
    assert m(BASH, "Bash", "echo 'unterminated") is False


# --------------------------------------------------------------------------
# host-bash invoke: run_command / run_script bodies
# --------------------------------------------------------------------------

def test_hostbash_run_command_block_real_sudo():
    assert m(HB_CMD, "mcp__host-bash__run_command",
             _run_command("sudo apt-get install -y jq")) is True


def test_hostbash_run_command_block_piped_sudo():
    assert m(HB_CMD, "mcp__host-bash__run_command",
             _run_command("cat f | sudo tee /etc/x")) is True


def test_hostbash_run_command_pass_sudo_in_body_arg():
    # sudo only inside a --body / commit message -> NOT an invocation.
    assert m(HB_CMD, "mcp__host-bash__run_command",
             _run_command('git commit -m "document the sudo carve-out"')) is False
    assert m(HB_CMD, "mcp__host-bash__run_command",
             _run_command("grep -n sudoers /etc/sudoers")) is False


def test_hostbash_run_script_block_real_sudo():
    assert m(HB_SCRIPT, "mcp__host-bash__run_script",
             _run_script("set -e\nsudo systemctl restart nginx\n")) is True


def test_hostbash_run_script_pass_heredoc_mention():
    script = (
        "gh pr create --base main --body \"$(cat <<'EOF'\n"
        "This PR edits the sudo-obligation code.\n"
        "EOF\n"
        ")\""
    )
    assert m(HB_SCRIPT, "mcp__host-bash__run_script",
             _run_script(script)) is False


def test_hostbash_invoke_wrong_tool_name():
    assert m(HB_CMD, "Bash", _run_command("sudo apt-get")) is False


def test_hostbash_invoke_raw_text_fallback():
    # A raw (non-JSON) command_string still gets AST invocation matching.
    assert m(HB_CMD, "mcp__host-bash__run_command", "sudo apt-get") is True
    assert m(HB_CMD, "mcp__host-bash__run_command", "echo sudo") is False


# --------------------------------------------------------------------------
# backward-compat: existing forms unaffected by the new dispatch
# --------------------------------------------------------------------------

def test_bashcmd_still_strips_sudo_wrapper():
    # Bashcmd: continues to expose the WRAPPED head (sudo stripped) -- the new
    # Bashinvoke: form is the one that sees sudo itself.
    assert m("Bashcmd:apt-get", "Bash", "sudo apt-get install x") is True
    assert m("Bashcmd:sudo", "Bash", "sudo apt-get install x") is False


def test_hostbash_regex_rest_still_body_wide():
    # A regex spec (not invoke:, not a bare name) stays a body-wide re.search
    # -- the pre-fix behavior, preserved for backward compat. Here the raw
    # ``\bsudo\b`` matches ``sudo`` even though it is only an ARGUMENT to
    # echo (exactly the false positive the invoke: form fixes); the point is
    # that a REGEX rest still takes the old body-wide path, unchanged.
    assert m(r"mcp__host-bash__run_command:\bsudo\b",
             "mcp__host-bash__run_command",
             _run_command("echo sudo now")) is True
    # ...while the invoke: form does NOT fire on that same arg-only mention.
    assert m(HB_CMD, "mcp__host-bash__run_command",
             _run_command("echo sudo now")) is False
