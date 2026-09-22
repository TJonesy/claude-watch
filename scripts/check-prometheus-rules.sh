#!/usr/bin/env bash
#
# check-prometheus-rules.sh — gate monitoring/prometheus/claude-watch.rules.yml
# in the repo that OWNS it.
#
# WHY THIS EXISTS
#
# These rules are consumed by whatever Prometheus deployment loads them, which
# is not necessarily this repo. Before this script, a semantics change here —
# a renamed annotation, a retuned threshold, a dropped label — merged with no
# check at all, and the breakage surfaced later as a red build in a DOWNSTREAM
# repo that had no commit of its own. A gate that reddens somebody else's
# pipeline for your change is noise; the break belongs on the pull request
# that caused it. So the assertions live here, next to the rules.
#
# WHAT IT RUNS
#
#   promtool check rules  — syntax, plus a positive rule COUNT.
#   promtool test rules   — THE ASSERTIONS, one invocation per suite under
#                           monitoring/prometheus/tests/.
#
# THE VACUOUS-PASS TRAP — READ BEFORE SIMPLIFYING ANYTHING BELOW
#
# `promtool test rules` EXITS 0 ON A SUITE THAT LOADED ZERO RULES. A
# `rule_files:` entry that matches nothing is not an error; promtool prints
#
#     WARNING: no file match pattern ../whatever.yml
#       SUCCESS
#
# and returns 0. Every `exp_alerts: []` expectation then holds trivially, so
# the suite proves nothing while reporting green. Measured on Prometheus
# v3.11.2, 2026-09-22. Trusting the exit code alone therefore ships a gate
# that CANNOT FAIL, so this script carries five explicit non-vacuity guards
# (G1–G5). Do not delete them because "promtool already checks that" — it
# does not.
#
#   G1  the rules file exists and is non-empty
#   G2  there is at least one test suite
#   G3  every suite's `rule_files:` entries resolve to real files
#   G4  `check rules` reports a NON-ZERO rule count, and no suite log carries
#       the "no file match pattern" warning (fatal even at exit 0)
#   G5  every alert and recording rule defined in the rules file is named by
#       at least one suite — a new rule with no test fails the build
#
# MODES
#
#   PROMTOOL_MODE=auto (default) — use a promtool on PATH if there is one,
#     otherwise fall back to the pinned container image. Never silently skips.
#   PROMTOOL_MODE=native — require promtool on PATH.
#   PROMTOOL_MODE=docker — run promtool out of the pinned prom/prometheus
#     image. Nothing to install locally.
#
# Test suites declare `rule_files:` RELATIVE TO THEMSELVES (`../claude-watch.rules.yml`),
# which promtool resolves against the test file's own directory, so the same
# suite runs unchanged in both modes with no path rewriting.
#
# Env knobs: PROM_IMAGE (docker-mode image pin), PROMTOOL_MODE.
# Exit: 0 all checks passed, 1 something failed.

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
prom_dir="${repo_root}/monitoring/prometheus"
rules_file="${prom_dir}/claude-watch.rules.yml"
tests_dir="${prom_dir}/tests"

PROM_IMAGE="${PROM_IMAGE:-prom/prometheus:v3.11.2}"
PROMTOOL_MODE="${PROMTOOL_MODE:-auto}"

fail=0
note() { printf '%s\n' "$*"; }
ok()   { printf '   ok   %s\n' "$*"; }
bad()  { printf '   FAIL %s\n' "$*" >&2; fail=1; }

# ---------------------------------------------------------------------------
# G1 — the rules file must exist and hold something.
# ---------------------------------------------------------------------------
note "==> G1 rules file present"
if [[ ! -s "${rules_file}" ]]; then
    bad "${rules_file} is missing or empty — there is nothing to validate"
    note ""
    note "check-prometheus-rules: FAILED"
    exit 1
fi
ok "${rules_file#"${repo_root}"/}"

# ---------------------------------------------------------------------------
# promtool invocation — the ONLY thing that differs between the modes. Both
# forms see the same paths under ${prom_dir}: docker mode mounts that
# directory at /rules, native mode passes the host path.
# ---------------------------------------------------------------------------
have_native=0
command -v promtool >/dev/null 2>&1 && have_native=1

case "${PROMTOOL_MODE}" in
auto)    [[ ${have_native} -eq 1 ]] && resolved_mode=native || resolved_mode=docker ;;
native)  resolved_mode=native ;;
docker)  resolved_mode=docker ;;
*)
    echo "check-prometheus-rules: unknown PROMTOOL_MODE=${PROMTOOL_MODE} (want auto|native|docker)" >&2
    exit 1
    ;;
esac

case "${resolved_mode}" in
native)
    if [[ ${have_native} -ne 1 ]]; then
        # A skipped check that reports green is the exact failure mode this
        # script exists to remove, so this is fatal rather than a warning.
        echo "check-prometheus-rules: promtool is not on PATH (PROMTOOL_MODE=${PROMTOOL_MODE})." >&2
        echo "  Install it, or re-run with PROMTOOL_MODE=docker." >&2
        exit 1
    fi
    rules_root="${prom_dir}"
    promtool_run() { promtool "$@"; }
    ;;
docker)
    command -v docker >/dev/null 2>&1 || {
        echo "check-prometheus-rules: neither promtool nor docker is available." >&2
        echo "  One of them is required; skipping would report a green gate that checked nothing." >&2
        exit 1
    }
    rules_root="/rules"
    promtool_run() {
        docker run --rm -v "${prom_dir}:/rules:ro" \
            --entrypoint promtool "${PROM_IMAGE}" "$@"
    }
    ;;
esac

note "==> promtool: $(promtool_run --version 2>&1 | head -1) [mode=${resolved_mode}]"

# ---------------------------------------------------------------------------
# G2 — there must BE test suites. A glob that matches nothing is not a pass.
# ---------------------------------------------------------------------------
note "==> G2 test suites present"
mapfile -t test_files < <(cd "${tests_dir}" 2>/dev/null && ls -1 -- *.yml 2>/dev/null | sort || true)
if [[ ${#test_files[@]} -eq 0 ]]; then
    bad "no *.yml under ${tests_dir#"${repo_root}"/} — nothing would be asserted"
    note ""
    note "check-prometheus-rules: FAILED"
    exit 1
fi
ok "${#test_files[@]} suites"

# ---------------------------------------------------------------------------
# G3 — every rule_files: entry in every suite must resolve to a real file.
#
# This is the guard that turns the vacuous pass into a hard failure BEFORE
# promtool is ever asked. Entries are relative to the suite's own directory,
# which is how promtool itself resolves them.
# ---------------------------------------------------------------------------
note "==> G3 every suite's rule_files: resolve"
for tf in "${test_files[@]}"; do
    entries="$(awk '
        /^rule_files:/ {f=1; next}
        /^[a-zA-Z_]+:/ {f=0}
        f && /^[[:space:]]*-[[:space:]]*/ {
            sub(/^[[:space:]]*-[[:space:]]*/, "")
            sub(/[[:space:]]*#.*$/, "")
            sub(/[[:space:]]+$/, "")
            if (length($0)) print
        }' "${tests_dir}/${tf}")"
    if [[ -z "${entries}" ]]; then
        bad "${tf}: no rule_files: entries — the suite asserts against nothing"
        continue
    fi
    while IFS= read -r entry; do
        case "${entry}" in
        /*) bad "${tf}: rule_files entry '${entry}' is ABSOLUTE; suites must stay checkout-relative" ; continue ;;
        esac
        [[ -e "${tests_dir}/${entry}" ]] \
            || bad "${tf}: rule_files '${entry}' does not resolve (suite would load ZERO rules and still report SUCCESS)"
    done <<< "${entries}"
done
[[ ${fail} -eq 0 ]] && ok "all rule_files resolve"

# ---------------------------------------------------------------------------
# promtool check rules — syntax, plus a positive rule COUNT.
# ---------------------------------------------------------------------------
note "==> promtool check rules"
check_rules_out="$(promtool_run check rules "${rules_root}/claude-watch.rules.yml" 2>&1)" && rc=0 || rc=$?
printf '%s\n' "${check_rules_out}" | sed 's/^/   /'
if [[ ${rc} -ne 0 ]]; then
    bad "promtool check rules exited ${rc}"
else
    # G4 — "SUCCESS: 0 rules found" is syntactically valid and semantically
    # empty, so a positive count is part of the contract.
    if printf '%s\n' "${check_rules_out}" | grep -qE 'SUCCESS: 0 rules found'; then
        bad "the rules file reported 0 rules — valid YAML, zero coverage"
    elif ! printf '%s\n' "${check_rules_out}" | grep -qE 'SUCCESS: [0-9]+ rules found'; then
        bad "check rules printed no rule count at all — cannot confirm anything loaded"
    fi
fi

# ---------------------------------------------------------------------------
# G5 — coverage. Every alert and recording rule defined in the file must be
# named by at least one suite. Without this, adding a rule with no test is
# invisible: the existing suites still pass, and the new rule ships unasserted.
# ---------------------------------------------------------------------------
note "==> G5 every rule is named by a suite"
mapfile -t rule_names < <(
    grep -oE '^[[:space:]]*-[[:space:]]*(alert|record):[[:space:]]*[^[:space:]]+' "${rules_file}" \
        | sed -E 's/.*(alert|record):[[:space:]]*//' | sort -u
)
if [[ ${#rule_names[@]} -eq 0 ]]; then
    bad "no alert/record rules parsed out of ${rules_file#"${repo_root}"/} — refusing to call that covered"
else
    uncovered=()
    for rn in "${rule_names[@]}"; do
        grep -qFR -- "${rn}" "${tests_dir}" || uncovered+=("${rn}")
    done
    if [[ ${#uncovered[@]} -gt 0 ]]; then
        bad "no suite mentions: ${uncovered[*]} — add assertions under ${tests_dir#"${repo_root}"/}"
    else
        ok "${#rule_names[@]} rules, all referenced"
    fi
fi

# ---------------------------------------------------------------------------
# promtool test rules — THE ASSERTIONS. The check this whole file exists for.
# One suite per invocation so the failing file is named rather than buried in
# a merged stream.
# ---------------------------------------------------------------------------
note "==> promtool test rules (${#test_files[@]} suites)"
for tf in "${test_files[@]}"; do
    log="$(promtool_run test rules "${rules_root}/tests/${tf}" 2>&1)" && rc=0 || rc=$?
    if [[ ${rc} -ne 0 ]]; then
        bad "${tf} (exit ${rc})"
        printf '%s\n' "${log}" | sed 's/^/        /' >&2
        continue
    fi
    # G4 (cont.) — promtool exited 0, but did it actually LOAD anything? A
    # "no file match pattern" warning means the suite asserted against an
    # empty rule set. Exit 0 there is a lie; treat the warning as fatal.
    if printf '%s\n' "${log}" | grep -q 'no file match pattern'; then
        bad "${tf} passed VACUOUSLY — promtool loaded no rules:"
        printf '%s\n' "${log}" | sed 's/^/        /' >&2
        continue
    fi
    ok "${tf}"
done

note ""
if [[ ${fail} -ne 0 ]]; then
    note "check-prometheus-rules: FAILED"
    exit 1
fi
note "check-prometheus-rules: all checks passed"
