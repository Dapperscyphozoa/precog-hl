#!/usr/bin/env bash
set -euo pipefail

STATE_FILE="${STATE_FILE:-/tmp/smc_state.json}"
SMC_SOURCE_DIR="${SMC_SOURCE_DIR:-./smc_src}"
RENDER_SERVICE_ID="${RENDER_SERVICE_ID:-srv-d7ksm7d7vvec739n7f70}"
PROD_HOST="${PROD_HOST:-precog-i8c3.onrender.com}"
PROD_BASE="https://${PROD_HOST}"
BRANCH="smc-v1"
LOCAL_TEST="${LOCAL_TEST:-0}"

PHASES=(INIT FILES_DELETED FILES_WRITTEN TESTS_PASSED PUSHED DEPLOYING HEALTHY VERIFIED LIVE)

COPY_FILES=(
  smc_app.py
  smc_engine.py
  smc_execution.py
  smc_monitors.py
  smc_state.py
  smc_config.py
  smc_trade_log.py
  smc_skip_log.py
  smc_daily_rollup.py
  smc_fill_hook.py
  smc_pl_compat.py
)

DEPRECATED_GLOBS=(
  "precog.py"
  "confluence_*"
  "wall_*"
  "regime_*"
  "swing_fail_*"
  "*.mq4"
  "tests"
  "docs"
)

log() { printf '[%s] %s\n' "$(date -u +%FT%TZ)" "$*"; }

read_phase() {
  if [[ -f "$STATE_FILE" ]]; then
    python3 -c "import json,sys;print(json.load(open('$STATE_FILE')).get('phase','INIT'))"
  else
    echo "INIT"
  fi
}

write_phase() {
  local phase="$1"
  python3 - "$STATE_FILE" "$phase" <<'PY'
import json,sys,os,time
path, phase = sys.argv[1], sys.argv[2]
data = {}
if os.path.exists(path):
    try: data = json.load(open(path))
    except Exception: data = {}
history = data.get("history", [])
history.append({"phase": phase, "ts": int(time.time())})
data["phase"] = phase
data["history"] = history
json.dump(data, open(path, "w"), indent=2)
PY
}

phase_index() {
  local p="$1" i=0
  for x in "${PHASES[@]}"; do
    if [[ "$x" == "$p" ]]; then echo "$i"; return 0; fi
    i=$((i+1))
  done
  echo -1
}

pushover() {
  local title="$1" msg="$2"
  if [[ -z "${PUSHOVER_TOKEN:-}" || -z "${PUSHOVER_USER:-}" ]]; then
    log "pushover: skipped (no creds)"
    return 0
  fi
  curl -sS --max-time 15 \
    --form-string "token=${PUSHOVER_TOKEN}" \
    --form-string "user=${PUSHOVER_USER}" \
    --form-string "title=${title}" \
    --form-string "message=${msg}" \
    https://api.pushover.net/1/messages.json >/dev/null || log "pushover: send failed (non-fatal)"
}

ping_phase() { pushover "SMC cutover: $1" "phase=$1 branch=${BRANCH} host=${PROD_HOST}"; }

require() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    log "missing required env: $name"
    exit 1
  fi
}

phase_init() {
  log "INIT: validating environment (LOCAL_TEST=${LOCAL_TEST})"
  if [[ "$LOCAL_TEST" != "1" ]]; then
    require RENDER_API_TOKEN
    require PUSHOVER_TOKEN
    require PUSHOVER_USER
    require RENDER_SERVICE_ID
    require PROD_HOST
  fi
  if [[ ! -d "$SMC_SOURCE_DIR" ]]; then
    log "ERROR: SMC_SOURCE_DIR not found: $SMC_SOURCE_DIR"
    exit 1
  fi
  for f in "${COPY_FILES[@]}"; do
    if [[ ! -f "$SMC_SOURCE_DIR/$f" ]]; then
      log "ERROR: missing source file: $SMC_SOURCE_DIR/$f"
      exit 1
    fi
  done
  mkdir -p "$(dirname "$STATE_FILE")"
  write_phase INIT
  ping_phase INIT
}

phase_files_deleted() {
  log "FILES_DELETED: removing deprecated modules per spec globs"
  for pat in "${DEPRECATED_GLOBS[@]}"; do
    rm -rf $pat 2>/dev/null || true
  done
  find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
  write_phase FILES_DELETED
  ping_phase FILES_DELETED
}

phase_files_written() {
  log "FILES_WRITTEN: copying SMC modules from $SMC_SOURCE_DIR"
  for f in "${COPY_FILES[@]}"; do
    cp "$SMC_SOURCE_DIR/$f" "./$f"
  done
  write_phase FILES_WRITTEN
  ping_phase FILES_WRITTEN
}

phase_tests_passed() {
  log "TESTS_PASSED: smoke test (LIVE_TRADING=0, mock alert)"
  export LIVE_TRADING=0
  export LONG_ONLY=1
  export FIXED_NOTIONAL_USD=50
  export MAX_OPEN_POSITIONS=20
  export ENTRY_TIF=Alo
  export PORT="${PORT:-18080}"

  if [[ ! -d /var/data ]] || [[ ! -w /var/data ]]; then
    if command -v sudo >/dev/null 2>&1; then
      sudo mkdir -p /var/data && sudo chown -R "$(id -u):$(id -g)" /var/data
    else
      log "ERROR: /var/data not writable and sudo unavailable"
      exit 1
    fi
  fi
  : > /var/data/smc_trades.csv

  python3 -m pip install --quiet --disable-pip-version-check -r requirements.txt 2>/tmp/smc_pip.log || {
    log "ERROR: pip install failed; see /tmp/smc_pip.log"
    tail -40 /tmp/smc_pip.log || true
    exit 1
  }

  python3 -c "import smc_app" 2>/tmp/smc_import.log || {
    log "ERROR: smc_app import failed"
    cat /tmp/smc_import.log
    exit 1
  }

  python3 smc_app.py >/tmp/smc_app.out 2>&1 &
  APP_PID=$!
  trap 'kill $APP_PID 2>/dev/null || true' EXIT

  for i in $(seq 1 60); do
    if curl -sS --max-time 2 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done

  payload='{"alert_id":"TEST-1","coin":"FAKE","side":"BUY","sweep_wick":1,"ob_top":50000,"sl_price":49000,"tp2":52000,"atr14":100,"rr_to_tp2":2.0}'
  curl -sS --max-time 10 -X POST -H 'content-type: application/json' \
    -d "$payload" "http://127.0.0.1:${PORT}/smc/alert" >/dev/null

  sleep 2

  if ! grep -q "ALERT_RECV" /var/data/smc_trades.csv 2>/dev/null; then
    log "ERROR: ALERT_RECV row not found in /var/data/smc_trades.csv"
    cat /tmp/smc_app.out | tail -40 || true
    exit 1
  fi

  kill $APP_PID 2>/dev/null || true
  trap - EXIT
  write_phase TESTS_PASSED
  ping_phase TESTS_PASSED
}

phase_pushed() {
  log "PUSHED: committing and pushing smc-v1"
  git add -A
  if git diff --cached --quiet; then
    log "no changes to commit"
  else
    git commit -m "SMC cutover: delete legacy + write SMC v1 modules"
  fi
  git push -u origin "$BRANCH"
  write_phase PUSHED
  ping_phase PUSHED
}

phase_deploying() {
  log "DEPLOYING: swapping Render branch + triggering redeploy"
  curl -sS --fail --max-time 30 \
    -X PATCH \
    -H "Authorization: Bearer ${RENDER_API_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "{\"branch\":\"${BRANCH}\"}" \
    "https://api.render.com/v1/services/${RENDER_SERVICE_ID}" >/dev/null
  curl -sS --fail --max-time 30 \
    -X POST \
    -H "Authorization: Bearer ${RENDER_API_TOKEN}" \
    -H "Content-Type: application/json" \
    -d '{"clearCache":"do_not_clear"}' \
    "https://api.render.com/v1/services/${RENDER_SERVICE_ID}/deploys" >/dev/null
  write_phase DEPLOYING
  ping_phase DEPLOYING
}

phase_healthy() {
  log "HEALTHY: polling /health for ws_fresh=true (5min max)"
  local deadline=$(( $(date +%s) + 300 ))
  while (( $(date +%s) < deadline )); do
    body=$(curl -sS --max-time 10 "${PROD_BASE}/health" || true)
    if [[ -n "$body" ]]; then
      fresh=$(printf '%s' "$body" | python3 -c "import json,sys;d=json.load(sys.stdin);print(str(d.get('ws_fresh',False)).lower())" 2>/dev/null || echo "false")
      if [[ "$fresh" == "true" ]]; then
        write_phase HEALTHY
        ping_phase HEALTHY
        return 0
      fi
    fi
    sleep 10
  done
  log "ERROR: ws_fresh did not become true within 5 minutes"
  exit 1
}

phase_verified() {
  log "VERIFIED: GET /smc/status assertions"
  body=$(curl -sS --fail --max-time 15 "${PROD_BASE}/smc/status")
  printf '%s' "$body" | python3 - <<'PY'
import json, sys
d = json.loads(sys.stdin.read())
us = d.get("universe_size", 0)
btc = d.get("btc_trend_up", None)
orphans = d.get("orphan_positions", [])
orphan_count = len(orphans) if isinstance(orphans, list) else int(orphans or 0)
assert us >= 46, f"universe_size={us} (<46)"
assert btc is not None, "btc_trend_up not populated"
assert orphan_count == 2, f"expected 2 orphan positions, got {orphan_count}"
print(f"OK universe_size={us} btc_trend_up={btc} orphans={orphan_count}")
PY
  write_phase VERIFIED
  ping_phase VERIFIED
}

phase_live() {
  log "LIVE: cutover complete"
  pushover "SMC LIVE" "branch=${BRANCH} host=${PROD_HOST} cutover complete"
  write_phase LIVE
}

run_phase() {
  case "$1" in
    INIT)           phase_init ;;
    FILES_DELETED)  phase_files_deleted ;;
    FILES_WRITTEN)  phase_files_written ;;
    TESTS_PASSED)   phase_tests_passed ;;
    PUSHED)         phase_pushed ;;
    DEPLOYING)      phase_deploying ;;
    HEALTHY)        phase_healthy ;;
    VERIFIED)       phase_verified ;;
    LIVE)           phase_live ;;
    *)              log "unknown phase: $1"; exit 1 ;;
  esac
}

main() {
  local current next_idx
  current="$(read_phase)"
  log "current phase: $current"

  if [[ "$current" == "LIVE" ]]; then
    log "already LIVE — nothing to do"
    exit 0
  fi

  next_idx=$(phase_index "$current")
  if [[ "$next_idx" -lt 0 ]]; then
    log "invalid state, restarting from INIT"
    next_idx=0
  fi

  local stop_at="${STOP_AT:-LIVE}"
  for (( i=next_idx; i<${#PHASES[@]}; i++ )); do
    run_phase "${PHASES[$i]}"
    if [[ "${PHASES[$i]}" == "$stop_at" ]]; then
      log "stopping at $stop_at (STOP_AT)"
      exit 0
    fi
  done
}

main "$@"
