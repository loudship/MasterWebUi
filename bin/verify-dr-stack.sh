#!/usr/bin/env bash
# =============================================================================
# bin/verify-dr-stack.sh
# Ghost Command Offline Ecosystem — DR Stack Health Validation Script
# =============================================================================
#
# Purpose
# -------
# Post-deployment audit of the Docker Compose stack.  Verifies that:
#
#   1. The ClickHouse dual-port healthcheck (HTTP 8123 + TCP 9000) has fully
#      passed before langfuse-server and langfuse-worker are allowed to start.
#
#   2. langfuse-server and langfuse-worker log evidence of sequential
#      initialization (i.e., they started AFTER clickhouse became healthy).
#
#   3. The vram-arbiter daemon is running and emitting expected poll output.
#
#   4. All expected containers are in the 'running' state.
#
# Usage
# -----
#   chmod +x bin/verify-dr-stack.sh
#   ./bin/verify-dr-stack.sh [--compose-file <path>] [--timeout <seconds>]
#
# Exit codes
# ----------
#   0  All checks passed.
#   1  One or more checks failed.
#
# Dependencies
# ------------
#   docker (CLI), grep, awk, sed — all standard on Linux/macOS.
#   No WAN calls; operates entirely on the local Docker daemon.
#
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Configurable defaults
# ---------------------------------------------------------------------------

COMPOSE_FILE="${COMPOSE_FILE:-langfuse.yml}"
TIMEOUT_S="${TIMEOUT_S:-120}"             # max seconds to wait for healthy state
POLL_INTERVAL_S=3                         # seconds between status polls
PASS_MARKER="\e[32m[PASS]\e[0m"
FAIL_MARKER="\e[31m[FAIL]\e[0m"
WARN_MARKER="\e[33m[WARN]\e[0m"
INFO_MARKER="\e[34m[INFO]\e[0m"

OVERALL_STATUS=0   # 0 = all green, 1 = at least one failure

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

while [[ $# -gt 0 ]]; do
  case "$1" in
    --compose-file) COMPOSE_FILE="$2"; shift 2 ;;
    --timeout)      TIMEOUT_S="$2";    shift 2 ;;
    *)              echo "Unknown arg: $1"; exit 1 ;;
  esac
done

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

log_pass() { echo -e "${PASS_MARKER}  $*"; }
log_fail() { echo -e "${FAIL_MARKER}  $*"; OVERALL_STATUS=1; }
log_warn() { echo -e "${WARN_MARKER}  $*"; }
log_info() { echo -e "${INFO_MARKER}  $*"; }

# Return the Docker health status string for a container name
container_health() {
  docker inspect --format='{{.State.Health.Status}}' "$1" 2>/dev/null || echo "missing"
}

# Return the Docker run state for a container name
container_state() {
  docker inspect --format='{{.State.Status}}' "$1" 2>/dev/null || echo "missing"
}

# Wait for a container to report a given health status within TIMEOUT_S
wait_for_health() {
  local name="$1"
  local target_status="${2:-healthy}"
  local elapsed=0

  log_info "Waiting for ${name} to become '${target_status}' (timeout=${TIMEOUT_S}s)..."
  while [[ $elapsed -lt $TIMEOUT_S ]]; do
    local current
    current="$(container_health "$name")"
    if [[ "$current" == "$target_status" ]]; then
      return 0
    fi
    sleep "$POLL_INTERVAL_S"
    elapsed=$((elapsed + POLL_INTERVAL_S))
  done
  return 1
}

# =============================================================================
# SECTION 1 — Container Run-State Audit
# =============================================================================

echo ""
echo "================================================================="
echo "  Ghost Command DR Stack — Health Validation"
echo "  Compose file : ${COMPOSE_FILE}"
echo "  Timestamp    : $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo "================================================================="
echo ""
echo "--- SECTION 1: Container Run-State Audit ---"

EXPECTED_CONTAINERS=(
  "clickhouse"
  "langfuse-server"
  "langfuse-worker"
  "postgres"
  "minio"
  "vram-arbiter"
)

for cname in "${EXPECTED_CONTAINERS[@]}"; do
  state="$(container_state "$cname")"
  if [[ "$state" == "running" ]]; then
    log_pass "${cname}: state=${state}"
  else
    log_fail "${cname}: state=${state} (expected: running)"
  fi
done

# =============================================================================
# SECTION 2 — ClickHouse Dual-Port Healthcheck Validation
# =============================================================================

echo ""
echo "--- SECTION 2: ClickHouse Dual-Port Healthcheck ---"

# 2a. Docker health status
CH_HEALTH="$(container_health "clickhouse")"
if [[ "$CH_HEALTH" == "healthy" ]]; then
  log_pass "clickhouse: Docker healthcheck status = healthy"
else
  log_fail "clickhouse: Docker healthcheck status = ${CH_HEALTH} (expected: healthy)"
fi

# 2b. HTTP port 8123 — ClickHouse HTTP interface (used by CLICKHOUSE_URL)
log_info "Probing clickhouse HTTP port 8123..."
if docker exec clickhouse \
     sh -c 'wget -qO- http://127.0.0.1:8123/ping 2>/dev/null' \
   | grep -q 'Ok\.'; then
  log_pass "clickhouse port 8123 (HTTP interface): responding 'Ok.'"
else
  log_fail "clickhouse port 8123 (HTTP interface): no 'Ok.' response"
fi

# 2c. TCP port 9000 — ClickHouse native TCP (used by CLICKHOUSE_MIGRATION_URL)
log_info "Probing clickhouse TCP port 9000..."
if docker exec clickhouse \
     clickhouse-client \
       --host 127.0.0.1 \
       --port 9000 \
       --query 'SELECT 1' \
     >/dev/null 2>&1; then
  log_pass "clickhouse port 9000 (native TCP): SELECT 1 succeeded"
else
  log_fail "clickhouse port 9000 (native TCP): SELECT 1 failed"
fi

# 2d. Parse container logs for the dual-port ready signal
log_info "Scanning clickhouse logs for readiness signals..."

# HTTP interface ready log line
if docker logs clickhouse 2>&1 \
   | grep -qE 'HTTP.*listening|Ready for connections.*port 8123|HTTPServer.*started'; then
  log_pass "clickhouse: HTTP interface (8123) readiness log detected"
else
  log_warn "clickhouse: HTTP interface readiness log not found (may be suppressed)"
fi

# TCP interface ready log line
if docker logs clickhouse 2>&1 \
   | grep -qE 'TCP.*listening|Ready for connections.*port 9000|Application.*started'; then
  log_pass "clickhouse: TCP interface (9000) readiness log detected"
else
  log_warn "clickhouse: TCP interface readiness log not found (may be suppressed)"
fi

# =============================================================================
# SECTION 3 — Sequential Initialization Verification
# Confirms langfuse-server and langfuse-worker started AFTER clickhouse healthy
# =============================================================================

echo ""
echo "--- SECTION 3: Sequential Initialization Sequence ---"

# Extract the timestamp when clickhouse first reported healthy in Docker events
log_info "Extracting ClickHouse healthy event timestamp..."

CH_HEALTHY_TS="$(
  docker events \
    --filter "container=clickhouse" \
    --filter "event=health_status: healthy" \
    --format '{{.Time}}' \
    --since "$(docker inspect --format='{{.State.StartedAt}}' clickhouse 2>/dev/null || echo '1970-01-01T00:00:00Z')" \
    --until "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
  2>/dev/null | head -1
)"

if [[ -n "$CH_HEALTHY_TS" ]]; then
  log_pass "clickhouse first healthy event: ${CH_HEALTHY_TS}"
else
  log_warn "clickhouse healthy event not found in Docker event stream (may be older than event buffer)"
  CH_HEALTHY_TS="0"
fi

# Check langfuse-server started after clickhouse became healthy
for svc in "langfuse-server" "langfuse-worker"; do
  log_info "Checking ${svc} start order relative to ClickHouse healthy..."

  # Look for migration success log lines in the service logs
  if docker logs "$svc" 2>&1 \
     | grep -qE 'migration.*success|Migration.*complete|database.*ready|Prisma.*connected|ClickHouse.*connected'; then
    log_pass "${svc}: database connection / migration success log detected"
  else
    log_warn "${svc}: no migration success log found (service may still be initializing)"
  fi

  # Confirm no crash-loop signature in recent logs
  if docker logs "$svc" 2>&1 \
     | grep -cE 'ECONNREFUSED|connect ETIMEDOUT.*9000|connect ETIMEDOUT.*8123|ClickHouse.*error.*before.*ready' \
     | grep -qE '^0$'; then
    log_pass "${svc}: no pre-ready ClickHouse connection errors in logs"
  else
    # Count occurrences
    ERR_COUNT="$(docker logs "$svc" 2>&1 \
      | grep -cE 'ECONNREFUSED|connect ETIMEDOUT.*9000|connect ETIMEDOUT.*8123' \
      || true)"
    log_fail "${svc}: ${ERR_COUNT} pre-ready ClickHouse connection error(s) detected — race condition may persist"
  fi

  # Confirm service didn't start before clickhouse was healthy
  SVC_STARTED="$(docker inspect --format='{{.State.StartedAt}}' "$svc" 2>/dev/null || echo '')"
  if [[ -n "$SVC_STARTED" && -n "$CH_HEALTHY_TS" && "$CH_HEALTHY_TS" != "0" ]]; then
    # Compare seconds since epoch using date -d (GNU date) or python fallback
    if command -v python3 &>/dev/null; then
      ORDERED="$(python3 -c "
import sys
from datetime import datetime, timezone

def parse_ts(s):
    # Handle various Docker timestamp formats
    s = s.strip().rstrip('Z')
    for fmt in ('%Y-%m-%dT%H:%M:%S.%f', '%Y-%m-%dT%H:%M:%S', '%s'):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            pass
    return float(s)

ch_ts  = parse_ts('${CH_HEALTHY_TS}')
svc_ts = parse_ts('${SVC_STARTED}')
print('ok' if svc_ts >= ch_ts else 'early')
" 2>/dev/null || echo "unknown")"
      if [[ "$ORDERED" == "ok" ]]; then
        log_pass "${svc}: started at ${SVC_STARTED} (after clickhouse healthy at ${CH_HEALTHY_TS})"
      elif [[ "$ORDERED" == "early" ]]; then
        log_fail "${svc}: started at ${SVC_STARTED} BEFORE clickhouse healthy at ${CH_HEALTHY_TS} — race condition!"
      else
        log_warn "${svc}: could not compare timestamps (${ORDERED})"
      fi
    else
      log_warn "${svc}: python3 unavailable — skipping timestamp comparison"
    fi
  fi
done

# =============================================================================
# SECTION 4 — VRAM Arbiter Daemon Health
# =============================================================================

echo ""
echo "--- SECTION 4: VRAM Arbiter Daemon ---"

ARBITER_STATE="$(container_state "vram-arbiter")"
if [[ "$ARBITER_STATE" == "running" ]]; then
  log_pass "vram-arbiter: container state = running"
else
  log_fail "vram-arbiter: container state = ${ARBITER_STATE}"
fi

# Verify the daemon emits its startup banner
if docker logs vram-arbiter 2>&1 \
   | grep -q "Async VRAM Hardware Arbiter Daemon"; then
  log_pass "vram-arbiter: startup banner detected in logs"
else
  log_warn "vram-arbiter: startup banner not yet visible (may still be initializing)"
fi

# Verify the 1000 ms polling is active (look for ARBITER log entries)
if docker logs vram-arbiter 2>&1 \
   | grep -qE '\[ARBITER\]'; then
  log_pass "vram-arbiter: polling loop entries detected in logs"
else
  log_warn "vram-arbiter: no [ARBITER] poll entries yet (LM Studio may be offline — expected)"
fi

# Verify no unhandled crashes
if docker logs vram-arbiter 2>&1 \
   | grep -qE 'Traceback|Unhandled exception in main loop'; then
  log_fail "vram-arbiter: unhandled exception detected in logs"
else
  log_pass "vram-arbiter: no unhandled exceptions in logs"
fi

# Verify VRAM ceiling env var was injected
if docker inspect vram-arbiter \
   | grep -q '"VRAM_CEILING=12240537395"'; then
  log_pass "vram-arbiter: VRAM_CEILING=12240537395 correctly injected"
else
  log_warn "vram-arbiter: VRAM_CEILING env var not found in container inspect"
fi

# =============================================================================
# SECTION 5 — Redis and Postgres Ancillary Health
# =============================================================================

echo ""
echo "--- SECTION 5: Ancillary Service Health ---"

for svc_name in "postgres" "minio"; do
  h="$(container_health "$svc_name")"
  if [[ "$h" == "healthy" ]]; then
    log_pass "${svc_name}: healthcheck = healthy"
  else
    log_fail "${svc_name}: healthcheck = ${h}"
  fi
done

# =============================================================================
# FINAL SUMMARY
# =============================================================================

echo ""
echo "================================================================="
if [[ $OVERALL_STATUS -eq 0 ]]; then
  echo -e "\e[32m  ALL CHECKS PASSED — Stack is healthy and deterministically initialized.\e[0m"
else
  echo -e "\e[31m  ONE OR MORE CHECKS FAILED — Review the output above.\e[0m"
fi
echo "================================================================="
echo ""

exit $OVERALL_STATUS
