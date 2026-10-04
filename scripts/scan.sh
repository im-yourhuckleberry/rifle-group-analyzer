#!/usr/bin/env bash
# Security scans: bandit over src/ and pip-audit over requirements.lock.
# Usage: ./scripts/scan.sh [--only bandit|pip-audit]
# Env:   SCAN_IGNORE_VULNS  comma-separated pip-audit vulnerability IDs to ignore (each ID is logged)
# Output: .claude/specs/rifle-group-analyzer/{bandit,pip-audit}-results.json
# Exit:  0 no High bandit findings and no known vulnerabilities
#        1 findings (bandit High severity, or any pip-audit vulnerability: PyPI data carries no severity)
#        2 usage error
#        3 a scan could not run completely (pip-audit without network, missing or empty lock, a checker
#          that failed unexpectedly); printed as NOT RUN, never a pass
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VENV_PY="$ROOT/.venv/bin/python"
RESULTS_DIR="$ROOT/.claude/specs/rifle-group-analyzer"

ONLY=""
case "${1:-}" in
  "") ;;
  --only)
    ONLY="${2:-}"
    case "$ONLY" in
      bandit | pip-audit) ;;
      *) echo "usage: scan.sh [--only bandit|pip-audit]" >&2; exit 2 ;;
    esac
    ;;
  *) echo "usage: scan.sh [--only bandit|pip-audit]" >&2; exit 2 ;;
esac

IGNORE_ARGS=()
if [ -n "${SCAN_IGNORE_VULNS:-}" ]; then
  set -f # no pathname expansion while splitting the ID list
  for vuln_id in $(printf '%s' "$SCAN_IGNORE_VULNS" | tr ',' ' '); do
    if ! printf '%s' "$vuln_id" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9._-]*$'; then
      echo "scan.sh: invalid vulnerability ID in SCAN_IGNORE_VULNS: '$vuln_id'" >&2
      exit 2
    fi
    echo "PIP-AUDIT: ignoring $vuln_id (SCAN_IGNORE_VULNS)"
    IGNORE_ARGS+=(--ignore-vuln "$vuln_id")
  done
  set +f
fi

for tool in python bandit pip-audit; do
  if [ ! -x "$ROOT/.venv/bin/$tool" ]; then
    echo "scan.sh: .venv/bin/$tool not found; run ./scripts/bootstrap.sh first" >&2
    exit 1
  fi
done
mkdir -p "$RESULTS_DIR"

STATUS=0
note_status() { # keep the most severe code: 3 (not run) > 1 (findings) > 0; any other code is NOT RUN
  case "$1" in
    0) ;;
    1) [ "$STATUS" -eq 3 ] || STATUS=1 ;;
    3) STATUS=3 ;;
    *)
      echo "scan.sh: unexpected checker exit code $1; treating as NOT RUN" >&2
      STATUS=3
      ;;
  esac
}

run_bandit() {
  local out="$RESULTS_DIR/bandit-results.json" rc=0
  rm -f "$out"
  "$ROOT/.venv/bin/bandit" -q -c pyproject.toml -r src -f json -o "$out" || rc=$?
  if [ "$rc" -gt 1 ] || [ ! -s "$out" ]; then
    echo "BANDIT: NOT RUN (bandit exit $rc, no result file)" >&2
    return 3
  fi
  local bandit_rc="$rc"
  rc=0
  "$VENV_PY" - "$out" "$bandit_rc" <<'PY' || rc=$?
import json
import sys


def main():
    result_path, bandit_rc = sys.argv[1], int(sys.argv[2])
    try:
        data = json.load(open(result_path, encoding="utf-8"))
        results = data["results"]
        scanned_lines = data["metrics"]["_totals"]["loc"]
        errors = data["errors"]
    except (OSError, ValueError, KeyError) as exc:
        print(f"BANDIT: NOT RUN (unreadable result file: {exc})", file=sys.stderr)
        return 3
    if errors or scanned_lines == 0:
        print(f"BANDIT: NOT RUN (errors={len(errors)}, scanned lines={scanned_lines})", file=sys.stderr)
        return 3
    if bandit_rc == 1 and not results:
        print("BANDIT: NOT RUN (bandit exit 1 but the result file lists no issues)", file=sys.stderr)
        return 3
    levels = ("HIGH", "MEDIUM", "LOW")
    counts = {level: sum(r["issue_severity"] == level for r in results) for level in levels}
    print(f"BANDIT: scanned {scanned_lines} lines; high={counts['HIGH']} medium={counts['MEDIUM']} low={counts['LOW']}")
    for r in results:
        if r["issue_severity"] in ("HIGH", "MEDIUM"):
            print(f"  {r['issue_severity']} {r['test_id']} {r['filename']}:{r['line_number']} {r['issue_text']}")
    return 1 if counts["HIGH"] else 0


try:
    sys.exit(main())
except Exception as exc:
    print(f"BANDIT: NOT RUN (unexpected error: {exc!r})", file=sys.stderr)
    sys.exit(3)
PY
  return "$rc"
}

run_pip_audit() {
  local out="$RESULTS_DIR/pip-audit-results.json" rc=0 cache
  rm -f "$out"
  if [ ! -s requirements.lock ]; then
    echo "PIP-AUDIT: NOT RUN (requirements.lock is missing or empty)" >&2
    return 3
  fi
  cache="$(mktemp -d)"
  "$ROOT/.venv/bin/pip-audit" --requirement requirements.lock --require-hashes --disable-pip \
    --strict --progress-spinner off --cache-dir "$cache" --format json --output "$out" \
    ${IGNORE_ARGS[@]+"${IGNORE_ARGS[@]}"} 2> "$cache/stderr.txt" || rc=$?
  if [ "$rc" -gt 1 ] || [ ! -s "$out" ]; then
    tail -n 2 "$cache/stderr.txt" >&2
    rm -rf "$cache"
    echo "PIP-AUDIT: NOT RUN (pip-audit exit $rc, no result file; network required for vulnerability lookups)" >&2
    return 3
  fi
  rm -rf "$cache"
  local audit_rc="$rc"
  rc=0
  "$VENV_PY" - "$out" requirements.lock "$audit_rc" <<'PY' || rc=$?
import json
import re
import sys


class NotRun(Exception):
    pass


def normalize(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def main():
    result_path, lock_path, audit_rc = sys.argv[1], sys.argv[2], int(sys.argv[3])
    lock_text = open(lock_path, encoding="utf-8").read()
    pins = [normalize(n) for n in re.findall(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==", lock_text, re.M)]
    if not pins:
        raise NotRun("no pinned packages found in requirements.lock")
    try:
        deps = json.load(open(result_path, encoding="utf-8"))["dependencies"]
        names = [normalize(d["name"]) for d in deps]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise NotRun(f"unreadable result file: {exc}") from exc
    skipped = [d["name"] for d in deps if "skip_reason" in d]
    unrecognized = [d["name"] for d in deps if "skip_reason" not in d and not isinstance(d.get("vulns"), list)]
    if skipped or unrecognized:
        raise NotRun(f"skipped: {skipped}; entries without a vulns list: {unrecognized}")
    if len(names) != len(pins) or set(names) != set(pins):
        raise NotRun(
            f"audited {len(names)} packages but the lock pins {len(pins)}; "
            f"unaudited: {sorted(set(pins) - set(names))}; unexpected: {sorted(set(names) - set(pins))}"
        )
    vulnerable = [d for d in deps if d["vulns"]]
    if not vulnerable and audit_rc != 0:
        raise NotRun(f"pip-audit exit {audit_rc} but the result file records no vulnerabilities")
    print(f"PIP-AUDIT: audited {len(deps)} pinned packages; vulnerable packages={len(vulnerable)}")
    for d in vulnerable:
        for v in d["vulns"]:
            fixes = ",".join(v.get("fix_versions", [])) or "none"
            print(f"  {d['name']}=={d['version']} {v.get('id', '?')} fix={fixes}")
    return 1 if vulnerable else 0


try:
    sys.exit(main())
except NotRun as exc:
    print(f"PIP-AUDIT: NOT RUN ({exc})", file=sys.stderr)
    sys.exit(3)
except Exception as exc:
    print(f"PIP-AUDIT: NOT RUN (unexpected error: {exc!r})", file=sys.stderr)
    sys.exit(3)
PY
  return "$rc"
}

if [ "$ONLY" != "pip-audit" ]; then
  rc=0
  run_bandit || rc=$?
  note_status "$rc"
fi
if [ "$ONLY" != "bandit" ]; then
  rc=0
  run_pip_audit || rc=$?
  note_status "$rc"
fi

echo "scan.sh: exit $STATUS (0 clean, 1 findings, 3 not run)"
exit "$STATUS"
