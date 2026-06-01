#!/usr/bin/env bash
set -euo pipefail
 
# ================== CONFIG ==================
 
ROOT_DIR="/var/www/your_user/data/data_project"
 
SCRIPTS_DIR="$ROOT_DIR/main_pipeline"
 
PY="/opt/python/python-3.8.8/bin/python"
 
LOG_DIR="$ROOT_DIR/log"
mkdir -p "$LOG_DIR"
 
PIPE_LOG="$LOG_DIR/pipeline.log"
LOCK_FILE="/tmp/project_pipeline.lock"
MAX_LOG_SIZE_BYTES=5242880
LOG_TAIL_SIZE_BYTES=2621440
FAILED_STEPS=0
 
# ================== HELPERS ==================
 
rotate_log_if_needed() {
  local FILE="$1"
  if [ -f "$FILE" ]; then
    local SIZE
    SIZE=$(wc -c < "$FILE" 2>/dev/null || echo 0)
    if [ "$SIZE" -ge "$MAX_LOG_SIZE_BYTES" ]; then
      tail -c "$LOG_TAIL_SIZE_BYTES" "$FILE" > "${FILE}.tmp" && mv "${FILE}.tmp" "$FILE"
    fi
  fi
}
 
log() {
  echo "$(date '+%F %T') [$1] $2" >> "$PIPE_LOG"
}
 
run_step_soft() {
  local STEP_NAME="$1"
  shift
 
  log "INFO" "START $STEP_NAME (soft)"
  local STEP_START
  STEP_START=$(date +%s)
 
  if "$@" >> "$PIPE_LOG" 2>&1; then
    local STEP_END DURATION
    STEP_END=$(date +%s)
    DURATION=$((STEP_END - STEP_START))
    log "INFO" "END $STEP_NAME OK (${DURATION}s)"
  else
    local CODE=$?
    local STEP_END DURATION
    STEP_END=$(date +%s)
    DURATION=$((STEP_END - STEP_START))
    FAILED_STEPS=$((FAILED_STEPS + 1))
    log "WARN" "SOFT_FAIL $STEP_NAME code=$CODE (${DURATION}s) - continue"
    return 0
  fi
}
 
# ================== PRECHECKS ==================
 
rotate_log_if_needed "$PIPE_LOG"
 
if [ ! -x "$PY" ]; then
  log "WARN" "Python not found or not executable: $PY"
else
  "$PY" -V >> "$PIPE_LOG" 2>&1 || true
fi
 
for REQUIRED_FILE in \
  "$SCRIPTS_DIR/sync_staff.py" \
  "$SCRIPTS_DIR/mpb_deals.py" \
  "$SCRIPTS_DIR/company_dash.py" \
  "$SCRIPTS_DIR/gosbase_win.py"
do
  if [ ! -f "$REQUIRED_FILE" ]; then
    log "WARN" "script not found: $REQUIRED_FILE"
  fi
done
 
log "INFO" "ROOT_DIR=$ROOT_DIR SCRIPTS_DIR=$SCRIPTS_DIR PY=$PY"
 
# ================== LOCK ==================
 
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "WARN" "Pipeline already running - exit"
  exit 0
fi
trap 'rm -f "$LOCK_FILE"' EXIT
 
PIPE_START=$(date +%s)
log "INFO" "PIPELINE START"
 
# ================== STEPS ==================
 
run_step_soft "sync_staff.py" \
  "$PY" "$SCRIPTS_DIR/sync_staff.py"
 
run_step_soft "mpb_deals.py" \
  "$PY" "$SCRIPTS_DIR/mpb_deals.py"
 
run_step_soft "company_dash.py" \
  "$PY" "$SCRIPTS_DIR/company_dash.py"
 
run_step_soft "gosbase_win.py" \
  "$PY" "$SCRIPTS_DIR/gosbase_win.py"
 
PIPE_END=$(date +%s)
PIPE_DURATION=$((PIPE_END - PIPE_START))
 
log "INFO" "PIPELINE END OK (${PIPE_DURATION}s) failed_steps=$FAILED_STEPS"