#!/usr/bin/env bash
# T3 local smoke: start the real server (scripts/run.sh) on a free loopback port with a throwaway
# data directory, exercise it, then tear it down and verify nothing is left.
# Usage: ./scripts/smoke.sh
# Needs: .venv (make bootstrap), lsof, curl, perl. Never touches <root>/data; creates and removes one temp dir.
# Output: one line per check, "[id] PASS|FAIL|SKIPPED <text>", then a summary; exits 1 on any FAIL.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY="$ROOT/.venv/bin/python"
DRIVER="$ROOT/tests/smoke/smoke_roundtrip.py"

PASS_N=0
FAIL_N=0
SKIP_N=0
SMOKE_ROOT=""
SERVER_PID=""
SERVER_PGID=""
SERVER_STOP_RC=""
PORT=""
LISTEN_OUT=""
LSOF_ERR=""

record() { # record STATUS ID TEXT...
  local status="$1" id="$2"
  shift 2
  printf '[%s] %-7s %s\n' "$id" "$status" "$*"
  case "$status" in
    PASS) PASS_N=$((PASS_N + 1)) ;;
    FAIL) FAIL_N=$((FAIL_N + 1)) ;;
    SKIPPED) SKIP_N=$((SKIP_N + 1)) ;;
  esac
}

ingest() { # ingest FILE: print a driver's output and count its check lines
  local file="$1" line
  while IFS= read -r line; do
    printf '%s\n' "$line"
    case "$line" in
      "["*"] PASS "*) PASS_N=$((PASS_N + 1)) ;;
      "["*"] FAIL "*) FAIL_N=$((FAIL_N + 1)) ;;
      "["*"] SKIPPED "*) SKIP_N=$((SKIP_N + 1)) ;;
    esac
  done < "$file"
}

run_driver() { # run_driver SUBCOMMAND ARGS...: ingest its lines; a nonzero exit without a FAIL line is a FAIL
  local out rc=0 before="$FAIL_N"
  out="$SMOKE_ROOT/driver-$1.out"
  "$PY" "$DRIVER" "$@" > "$out" 2>&1 || rc=$?
  ingest "$out"
  if [ "$rc" -ne 0 ] && [ "$FAIL_N" -eq "$before" ]; then
    record FAIL "$1" "driver exited $rc without reporting a failure"
  fi
}

# Sets LISTEN_OUT to the "host:port" names of TCP listeners on a port; returns 2 if lsof itself failed.
listeners_on_port() {
  local rc=0
  LISTEN_OUT="$(lsof -nP -iTCP:"$1" -sTCP:LISTEN -Fn 2> "$LSOF_ERR" | awk '/^n/ { print substr($0, 2) }')" || rc=$?
  if [ "$rc" -gt 1 ] || { [ "$rc" -eq 1 ] && [ -s "$LSOF_ERR" ]; }; then
    return 2
  fi
  return 0
}

process_state() { ps -o stat= -p "$1" 2> /dev/null | tr -d ' ' || true; }

stop_server() { # SIGTERM, escalate to SIGKILL on the whole group; sets SERVER_STOP_RC
  local waited=0 state
  kill -TERM "$SERVER_PID" 2> /dev/null || true
  while [ "$waited" -lt 150 ]; do
    state="$(process_state "$SERVER_PID")"
    case "$state" in "" | Z*) break ;; esac
    sleep 0.1
    waited=$((waited + 1))
  done
  state="$(process_state "$SERVER_PID")"
  case "$state" in
    "" | Z*) SERVER_STOP_RC="graceful" ;;
    *)
      kill -KILL -- "-$SERVER_PGID" 2> /dev/null || true
      SERVER_STOP_RC="killed"
      ;;
  esac
  local rc=0
  { wait "$SERVER_PID"; } 2> /dev/null || rc=$?
  SERVER_STOP_RC="$SERVER_STOP_RC (exit $rc)"
  case "$rc" in
    0 | 143) ;;
    *) [ "${SERVER_STOP_RC%% *}" = "killed" ] || SERVER_STOP_RC="unexpected $SERVER_STOP_RC" ;;
  esac
}

teardown() { # check 9: runs from the EXIT trap, whatever happened before
  if [ -n "$SERVER_PID" ]; then
    stop_server 2> /dev/null
    case "$SERVER_STOP_RC" in
      "graceful (exit 0)" | "graceful (exit 143)") record PASS 9.1 "server pid $SERVER_PID stopped by SIGTERM, $SERVER_STOP_RC" ;;
      *) record FAIL 9.1 "server pid $SERVER_PID did not stop cleanly: $SERVER_STOP_RC" ;;
    esac
  else
    record SKIPPED 9.1 "(no server was started) nothing to stop"
  fi
  if [ -n "$PORT" ]; then
    local curl_rc=0 listening=""
    if listeners_on_port "$PORT"; then listening="$LISTEN_OUT"; else listening="lsof failed"; fi
    curl --noproxy '*' -sS --max-time 3 -o /dev/null "http://127.0.0.1:$PORT/healthz" 2> /dev/null || curl_rc=$?
    if [ -z "$listening" ] && [ "$curl_rc" -eq 7 ]; then
      record PASS 9.2 "port $PORT is free: no listener, connect refused"
    else
      record FAIL 9.2 "port $PORT is not free (listeners: ${listening:-none}; curl exit $curl_rc, expected 7)"
    fi
  fi
  if [ -n "$SMOKE_ROOT" ]; then
    local pgid_arg="${SERVER_PGID:-0}" before="$FAIL_N" out rc=0
    out="$SMOKE_ROOT/driver-residue.out"
    "$PY" "$DRIVER" residue --root "$SMOKE_ROOT" --pgid "$pgid_arg" > "$out" 2>&1 || rc=$?
    ingest "$out"
    if [ "$rc" -ne 0 ] && [ "$FAIL_N" -eq "$before" ]; then
      record FAIL 9.3 "residue check exited $rc without reporting a failure"
    fi
    if [ "$rc" -ne 0 ] && [ -n "$SERVER_PGID" ] && kill -KILL -- "-$SERVER_PGID" 2> /dev/null; then
      echo "(killed the processes left in process group $SERVER_PGID)"
    fi
    if [ "$FAIL_N" -gt 0 ] && [ -f "$SMOKE_ROOT/server.log" ]; then
      echo "--- last lines of the server log ---"
      tail -n 20 "$SMOKE_ROOT/server.log"
    fi
    rm -rf "$SMOKE_ROOT"
    if [ -e "$SMOKE_ROOT" ]; then
      record FAIL 9.4 "temp dir $SMOKE_ROOT was not removed"
    else
      record PASS 9.4 "temp dir removed"
    fi
  fi
  if [ -n "$LSOF_ERR" ]; then rm -f "$LSOF_ERR"; fi
}

finish() {
  local status=$?
  trap - EXIT INT TERM HUP
  teardown
  if [ "$status" -ne 0 ] && [ "$FAIL_N" -eq 0 ]; then
    record FAIL abort "the script stopped early (exit $status) before every check ran"
  fi
  printf 'SMOKE: %d PASS, %d FAIL, %d SKIPPED\n' "$PASS_N" "$FAIL_N" "$SKIP_N"
  if [ "$FAIL_N" -ne 0 ]; then
    echo "SMOKE: FAILED"
    exit 1
  fi
  echo "SMOKE: PASSED"
  exit 0
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

if [ "$#" -ne 0 ]; then
  echo "usage: scripts/smoke.sh (no arguments)" >&2
  exit 2
fi
for tool in lsof curl perl; do
  command -v "$tool" > /dev/null || { record FAIL 0 "required tool '$tool' not found"; exit 1; }
done
[ -x "$PY" ] || { record FAIL 0 "$PY not found; run ./scripts/bootstrap.sh first"; exit 1; }

# 1. Free port and a throwaway data directory; the EXIT trap above does the teardown.
SMOKE_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/rga-smoke.XXXXXX")"
LSOF_ERR="$SMOKE_ROOT/lsof.err"
DATA_DIR="$SMOKE_ROOT/data"
PORT="$("$PY" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
trap -p EXIT > "$SMOKE_ROOT/trap.out"
if listeners_on_port "$PORT" && [ -z "$LISTEN_OUT" ] && [ -d "$SMOKE_ROOT" ] && grep -q finish "$SMOKE_ROOT/trap.out"; then
  record PASS 1 "port $PORT is free; temp dir $SMOKE_ROOT (RGA_DATA_DIR=$DATA_DIR); EXIT trap installed"
else
  record FAIL 1 "port $PORT or temp dir $SMOKE_ROOT not usable (listeners: ${LISTEN_OUT:-unknown})"
fi

# 2. Negative start: a non-loopback host must exit non-zero within 5 s and never listen.
NEG_PORT="$("$PY" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
neg_rc=0
RGA_HOST=0.0.0.0 RGA_PORT="$NEG_PORT" RGA_DATA_DIR="$SMOKE_ROOT/negative-data" RGA_LOG_LEVEL=info \
  perl -e 'alarm shift; exec @ARGV' 5 "$ROOT/scripts/run.sh" > "$SMOKE_ROOT/negative.log" 2>&1 || neg_rc=$?
neg_problems=""
[ "$neg_rc" -ne 0 ] || neg_problems="$neg_problems exited 0;"
[ "$neg_rc" -ne 142 ] || neg_problems="$neg_problems still running after 5 s (alarm);"
grep -q 'RGA_HOST' "$SMOKE_ROOT/negative.log" || neg_problems="$neg_problems no message naming RGA_HOST;"
if grep -q 'Uvicorn running' "$SMOKE_ROOT/negative.log"; then neg_problems="$neg_problems it started;"; fi
if listeners_on_port "$NEG_PORT"; then
  [ -z "$LISTEN_OUT" ] || neg_problems="$neg_problems listening on $NEG_PORT;"
else
  neg_problems="$neg_problems lsof failed;"
fi
[ ! -e "$SMOKE_ROOT/negative-data" ] || neg_problems="$neg_problems data dir was created;"
if [ -z "$neg_problems" ]; then
  record PASS 2 "RGA_HOST=0.0.0.0 exited $neg_rc within 5 s, named RGA_HOST, never listened on $NEG_PORT, created no data dir"
else
  record FAIL 2 "RGA_HOST=0.0.0.0 start:$neg_problems exit $neg_rc"
fi

# 3. Start the real server in its own process group and wait for /healthz.
set -m
RGA_DATA_DIR="$DATA_DIR" RGA_HOST=127.0.0.1 RGA_PORT="$PORT" RGA_LOG_LEVEL=info \
  "$ROOT/scripts/run.sh" > "$SMOKE_ROOT/server.log" 2>&1 < /dev/null &
SERVER_PID=$!
set +m
SERVER_PGID="$(ps -o pgid= -p "$SERVER_PID" 2> /dev/null | tr -d ' ')" || SERVER_PGID=""
if [ "$SERVER_PGID" = "$SERVER_PID" ]; then
  record PASS 3.1 "server started via scripts/run.sh: pid $SERVER_PID, process group $SERVER_PGID"
else
  record FAIL 3.1 "server pid $SERVER_PID is in process group '$SERVER_PGID', not its own"
fi
BASE_URL="http://127.0.0.1:$PORT"
before_health="$FAIL_N"
run_driver healthz --base-url "$BASE_URL" --pid "$SERVER_PID" --timeout 15

if [ "$FAIL_N" -ne "$before_health" ]; then
  for pair in "4.1:bind check" "4.2:LAN reachability" "5:denials" "6:round trip" "7:on-disk checks" "8:no egress" "10:share defaults"; do
    record FAIL "${pair%%:*}" "${pair#*:}: not run (the server is not healthy)"
  done
else
  # 4. Bind check: only 127.0.0.1 may listen; a LAN address must not answer.
  if listeners_on_port "$PORT" && [ "$LISTEN_OUT" = "127.0.0.1:$PORT" ]; then
    record PASS 4.1 "lsof LISTEN on port $PORT shows only 127.0.0.1:$PORT"
  else
    record FAIL 4.1 "lsof LISTEN on port $PORT shows '${LISTEN_OUT:-nothing}', expected only 127.0.0.1:$PORT"
  fi
  lan_ips="$(ifconfig | awk '$1 == "inet" && $2 != "127.0.0.1" { print $2 }')"
  if [ -z "$lan_ips" ]; then
    record SKIPPED 4.2 "(no LAN IP) LAN-CHECK: SKIPPED (no LAN IP)"
  else
    lan_problems=""
    lan_tried=""
    for lan_ip in $lan_ips; do
      lan_rc=0
      curl --noproxy '*' -sS --max-time 3 -o /dev/null "http://$lan_ip:$PORT/healthz" 2> /dev/null || lan_rc=$?
      case "$lan_rc" in
        7 | 28) lan_tried="$lan_tried $lan_ip(curl $lan_rc)" ;;
        *) lan_problems="$lan_problems $lan_ip answered or failed oddly (curl exit $lan_rc);" ;;
      esac
    done
    if [ -z "$lan_problems" ]; then
      record PASS 4.2 "port $PORT is unreachable on every non-loopback IPv4 address:$lan_tried"
    else
      record FAIL 4.2 "the server is reachable beyond loopback:$lan_problems"
    fi
  fi

  # 5. Denials.
  run_driver denials --base-url "$BASE_URL"

  # 6. Round trip from rifle to finalized group.
  run_driver roundtrip --base-url "$BASE_URL" --work-dir "$SMOKE_ROOT/work" --summary-out "$SMOKE_ROOT/summary.json"

  # 7. On-disk state.
  DB_FILE="$DATA_DIR/rga.sqlite3"
  if [ -f "$DB_FILE" ]; then
    record PASS 7.1 "database file exists: $DB_FILE"
    if command -v sqlite3 > /dev/null; then
      integrity="$(sqlite3 "$DB_FILE" 'PRAGMA integrity_check;' 2>&1)" || integrity="sqlite3 failed: $integrity"
    else
      integrity="$("$PY" -c 'import sqlite3, sys; print(sqlite3.connect(sys.argv[1]).execute("PRAGMA integrity_check").fetchone()[0])' "$DB_FILE" 2>&1)" || integrity="python sqlite3 failed: $integrity"
    fi
    if [ "$integrity" = "ok" ]; then
      record PASS 7.2 "PRAGMA integrity_check = ok"
    else
      record FAIL 7.2 "PRAGMA integrity_check = $integrity"
    fi
  else
    record FAIL 7.1 "database file $DB_FILE does not exist"
    record FAIL 7.2 "integrity check not run (no database file)"
  fi
  if [ -s "$SMOKE_ROOT/summary.json" ] && [ "$(cat "$SMOKE_ROOT/summary.json")" != "{}" ]; then
    sha="$("$PY" -c 'import json, sys; print(json.load(open(sys.argv[1]))["sha256"])' "$SMOKE_ROOT/summary.json")"
    ext="$("$PY" -c 'import json, sys; print(json.load(open(sys.argv[1]))["ext"])' "$SMOKE_ROOT/summary.json")"
    fixture="$("$PY" -c 'import json, sys; print(json.load(open(sys.argv[1]))["fixture"])' "$SMOKE_ROOT/summary.json")"
    stored="$DATA_DIR/photos/$sha.$ext"
    photo_count="$(find "$DATA_DIR/photos" -type f 2> /dev/null | wc -l | tr -d ' ')"
    if [ -f "$stored" ] && cmp -s "$stored" "$fixture" && [ "$photo_count" = "1" ]; then
      record PASS 7.3 "photo stored as photos/$sha.$ext, byte-identical to the upload; it is the only file in photos/"
    else
      record FAIL 7.3 "photo file photos/$sha.$ext missing or different, or photos/ holds $photo_count files (expected 1)"
    fi
  else
    record FAIL 7.3 "photo file not checked: the round trip did not upload a photo"
  fi

  # 8. No egress: while a loopback connection is open, nothing the server holds may reach a non-loopback address.
  exec 3<> "/dev/tcp/127.0.0.1/$PORT"
  printf 'GET /healthz HTTP/1.1\r\nHost: 127.0.0.1:%s\r\nConnection: keep-alive\r\n\r\n' "$PORT" >&3
  sleep 0.3
  lsof_rc=0
  sockets="$(lsof -nP -a -p "$SERVER_PID" -i 2> "$LSOF_ERR")" || lsof_rc=$?
  exec 3>&- 3<&-
  if [ "$lsof_rc" -gt 1 ] || { [ "$lsof_rc" -eq 1 ] && [ -s "$LSOF_ERR" ]; }; then
    record FAIL 8 "lsof -a -p $SERVER_PID -i failed (exit $lsof_rc)"
  else
    established="$(printf '%s\n' "$sockets" | grep -c 'ESTABLISHED' || true)"
    foreign="$(printf '%s\n' "$sockets" | grep -- '->' | grep -v -e '->127\.0\.0\.1:' -e '->\[::1\]:' -e '->localhost:' || true)"
    if [ "$established" -ge 1 ] && [ -z "$foreign" ]; then
      record PASS 8 "pid $SERVER_PID holds $established loopback connection(s) and none to a non-loopback address"
    elif [ -n "$foreign" ]; then
      record FAIL 8 "pid $SERVER_PID has connection(s) to a non-loopback address: $foreign"
    else
      record FAIL 8 "the probe connection was not visible in lsof, so the egress check proved nothing"
    fi
  fi

  # 10. Stored share-card defaults (checks 1-9 keep their numbers; this one runs before the teardown).
  run_driver settings --base-url "$BASE_URL"
fi

exit 0
