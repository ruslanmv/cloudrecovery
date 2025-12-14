#!/usr/bin/env bash
#
# CloudRecovery — monitor_anything.sh (Production Edition)
# -----------------------------------------------------------------------------
# Universal interactive monitoring wizard for CloudRecovery.
# Streams strictly structured evidence to stdout (PTY) for AI correlation.
#
# Features
#   ✅ Hybrid: Docker + OpenShift/Kubernetes + systemd + URL + file tail + process
#   ✅ Strict logs: "ts | level=... | key=value ..."
#   ✅ Read-only by default
#   ✅ Optional evidence push to Control Plane (agent-style) via env vars
#   ✅ Graceful shutdown
#
# Run in CloudRecovery UI:
#   chmod +x scripts/monitor_anything.sh
#   cloudrecovery ui --cmd ./scripts/monitor_anything.sh --host 127.0.0.1 --port 8787
#
# Optional evidence push:
#   export CLOUDRECOVERY_CONTROL_PLANE="https://cloudrecovery.example.com"
#   export CLOUDRECOVERY_AGENT_TOKEN="REPLACE"
#   export CLOUDRECOVERY_AGENT_ID="monitor-wizard-1"
#   export CLOUDRECOVERY_EMIT_EVIDENCE="1"
#
# -----------------------------------------------------------------------------
set -euo pipefail

# -----------------------------
# Prompt helpers (requested style)
# -----------------------------
prompt() { printf "%b" "$1" >&2; }  # print to stderr to keep stdout evidence clean

# -----------------------------
# Logging helpers (strict format)
# -----------------------------
ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

log_line() {
  local level="$1"; shift
  printf "%s | level=%s | %s\n" "$(ts)" "$level" "$*"
}

die() {
  log_line "CRITICAL" "msg='fatal' err=\"$(printf "%s" "$*" | tr '\n' ' ' | tr '\r' ' ')\""
  exit 1
}

need_cmd() {
  local c="$1"
  if ! command -v "$c" >/dev/null 2>&1; then
    log_line "ERROR" "msg='missing_command' cmd=${c}"
    return 1
  fi
  return 0
}

read_default() {
  local prompt_txt="$1"
  local def="$2"
  local var=""
  prompt "${prompt_txt} [${def}]: "
  read -r var
  echo "${var:-$def}"
}

sanitize_kv() {
  # keep evidence parseable: no whitespace/newlines in values
  echo "$1" | tr ' ' '_' | tr '\t' '_' | tr '\r' '_' | tr '\n' '_'
}

# -----------------------------
# Graceful shutdown
# -----------------------------
_STOP=0
on_sig() {
  _STOP=1
  log_line "INFO" "msg='signal_received' signal=$1 action='stopping'"
}
trap 'on_sig SIGINT' INT
trap 'on_sig SIGTERM' TERM

sleep_or_stop() {
  local seconds="$1"
  local i=0
  while (( i < seconds )); do
    ((_STOP == 1)) && return 1
    sleep 1
    ((i++))
  done
  return 0
}

# -----------------------------
# Evidence push (optional)
# -----------------------------
EMIT="${CLOUDRECOVERY_EMIT_EVIDENCE:-0}"
CP_URL="${CLOUDRECOVERY_CONTROL_PLANE:-}"
AGENT_TOKEN="${CLOUDRECOVERY_AGENT_TOKEN:-}"
AGENT_ID="${CLOUDRECOVERY_AGENT_ID:-monitor-wizard}"
EMIT_MAX="${CLOUDRECOVERY_EMIT_BATCH_MAX:-25}"
EMIT_BUF=()

emit_enabled() {
  [[ "$EMIT" == "1" && -n "$CP_URL" && -n "$AGENT_TOKEN" ]]
}

emit_add() {
  # args: kind severity message payload_json
  local kind="$1"
  local severity="$2"
  local message="$3"
  local payload_json="$4"

  emit_enabled || return 0

  local msg_json
  msg_json="$(printf '%s' "$message" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))' 2>/dev/null || echo "\"\"")"

  local ev
  ev=$(cat <<EOF
{"ts":"$(ts)","source":"monitor_wizard","kind":"${kind}","severity":"${severity}","message":${msg_json},"payload":${payload_json},"agent_id":"${AGENT_ID}"}
EOF
)
  EMIT_BUF+=("$ev")

  if ((${#EMIT_BUF[@]} >= EMIT_MAX)); then
    emit_flush || true
  fi
}

emit_flush() {
  emit_enabled || return 0
  ((${#EMIT_BUF[@]} == 0)) && return 0

  need_cmd curl || return 1

  local body
  body=$(printf '{"events":[%s]}' "$(IFS=,; echo "${EMIT_BUF[*]}")")

  if curl -fsS -X POST "${CP_URL%/}/api/agent/evidence" \
      -H "Authorization: Bearer ${AGENT_TOKEN}" \
      -H "X-CloudRecovery-Agent: ${AGENT_ID}" \
      -H "Content-Type: application/json" \
      --data "$body" >/dev/null 2>&1; then
    log_line "DEBUG" "msg='evidence_emitted' count=${#EMIT_BUF[@]} dest=$(sanitize_kv "$CP_URL")"
    EMIT_BUF=()
    return 0
  fi

  log_line "WARN" "msg='evidence_emit_failed' count=${#EMIT_BUF[@]} dest=$(sanitize_kv "$CP_URL")"
  # prevent unbounded growth
  if ((${#EMIT_BUF[@]} > EMIT_MAX * 4)); then
    EMIT_BUF=("${EMIT_BUF[@]: -$EMIT_MAX}")
  fi
  return 1
}

# -----------------------------
# Detect Kubernetes CLI (oc vs kubectl)
# -----------------------------
detect_k8s_cli() {
  if command -v oc >/dev/null 2>&1; then
    echo "oc"
  elif command -v kubectl >/dev/null 2>&1; then
    echo "kubectl"
  else
    return 1
  fi
}

# -----------------------------
# URL helpers (DNS/TLS hint)
# -----------------------------
url_host_port_scheme() {
  # outputs: host port scheme
  local url="$1"
  local scheme hostport host port
  scheme="$(printf "%s" "$url" | awk -F:// '{print $1}')"
  hostport="$(printf "%s" "$url" | awk -F:// '{print $2}' | awk -F/ '{print $1}')"
  host="$(printf "%s" "$hostport" | awk -F: '{print $1}')"
  port="$(printf "%s" "$hostport" | awk -F: '{print $2}')"
  if [[ -z "$port" ]]; then
    if [[ "$scheme" == "https" ]]; then port="443"; else port="80"; fi
  fi
  printf "%s %s %s\n" "$host" "$port" "$scheme"
}

dns_check() {
  local host="$1"
  if command -v getent >/dev/null 2>&1; then
    getent ahosts "$host" >/dev/null 2>&1 && echo "ok" || echo "fail"
  elif command -v nslookup >/dev/null 2>&1; then
    nslookup "$host" >/dev/null 2>&1 && echo "ok" || echo "fail"
  elif command -v dig >/dev/null 2>&1; then
    dig +short "$host" | grep -q . && echo "ok" || echo "fail"
  else
    echo "unknown"
  fi
}

tls_hint() {
  local host="$1"
  local port="$2"
  # best-effort: if openssl exists, attempt SNI handshake quickly
  if command -v openssl >/dev/null 2>&1; then
    timeout 3 openssl s_client -connect "${host}:${port}" -servername "$host" </dev/null >/dev/null 2>&1 \
      && echo "ok" || echo "fail"
  else
    echo "unknown"
  fi
}

# -----------------------------
# Header
# -----------------------------
HOST="$(hostname 2>/dev/null || echo unknown)"
USER_NAME="$(id -un 2>/dev/null || echo unknown)"
log_line "INFO" "msg='monitor_wizard_start' host=$(sanitize_kv "$HOST") user=$(sanitize_kv "$USER_NAME") pid=$$"

if emit_enabled; then
  log_line "INFO" "msg='emit_mode_enabled' agent_id=$(sanitize_kv "$AGENT_ID") control_plane=$(sanitize_kv "$CP_URL")"
else
  log_line "INFO" "msg='emit_mode_disabled' hint='set CLOUDRECOVERY_EMIT_EVIDENCE=1 and CLOUDRECOVERY_CONTROL_PLANE+CLOUDRECOVERY_AGENT_TOKEN to push evidence'"
fi

# -----------------------------
# Menu (requested prompt lines)
# -----------------------------
prompt $'\nSelect monitoring target:\n'
prompt $'  1) 🐳 Docker Container (Status + Logs)\n'
prompt $'  2) ☸️  OpenShift / Kubernetes (Pods + Events + Optional Rollout)\n'
prompt $'  3) ⚙️  Systemd Service (Active State + Recent Journal)\n'
prompt $'  4) 🌐 URL Health (HTTP Code + Latency + DNS/TLS hint)\n'
prompt $'  5) 📄 Log File Tail (Local file)\n'
prompt $'  6) 🔍 Process by name (ps snapshot)\n\n'

MODE="$(read_default 'Selection' '2')"
INTERVAL_RAW="$(read_default 'Polling interval seconds' '10')"
INTERVAL="${INTERVAL_RAW// /}"
[[ "$INTERVAL" =~ ^[0-9]+$ ]] || die "interval must be an integer"

log_line "INFO" "msg='monitor_configured' mode=${MODE} interval_s=${INTERVAL}"

# -----------------------------
# MODE 1: Docker
# -----------------------------
docker_monitor() {
  need_cmd docker || die "docker not found"
  local container log_tail
  container="$(read_default 'Container name or ID' 'my-app')"
  log_tail="$(read_default 'docker logs --tail N' '80')"
  [[ "$log_tail" =~ ^[0-9]+$ ]] || die "log tail must be an integer"

  log_line "INFO" "mode=docker msg='watch_start' container=$(sanitize_kv "$container")"

  while ((_STOP == 0)); do
    if docker inspect "$container" >/dev/null 2>&1; then
      local running status exit_code started finished
      running="$(docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null || echo "unknown")"
      status="$(docker inspect -f '{{.State.Status}}' "$container" 2>/dev/null || echo "unknown")"
      exit_code="$(docker inspect -f '{{.State.ExitCode}}' "$container" 2>/dev/null || echo "unknown")"
      started="$(docker inspect -f '{{.State.StartedAt}}' "$container" 2>/dev/null || echo "unknown")"
      finished="$(docker inspect -f '{{.State.FinishedAt}}' "$container" 2>/dev/null || echo "unknown")"

      local lvl="INFO"
      [[ "$running" == "false" || "$status" == "exited" ]] && lvl="WARN"
      [[ "$exit_code" != "0" && "$exit_code" != "unknown" ]] && lvl="ERROR"

      log_line "$lvl" "mode=docker container=$(sanitize_kv "$container") status=${status} running=${running} exit_code=${exit_code} started_at=$(sanitize_kv "$started") finished_at=$(sanitize_kv "$finished")"
      emit_add "docker_state" "$([[ "$lvl" == "INFO" ]] && echo info || ([[ "$lvl" == "WARN" ]] && echo warning || echo critical))" \
        "docker container state" \
        "{\"container\":\"$(sanitize_kv "$container")\",\"status\":\"$status\",\"running\":\"$running\",\"exit_code\":\"$exit_code\"}" || true
    else
      log_line "ERROR" "mode=docker container=$(sanitize_kv "$container") msg='container_not_found'"
      emit_add "docker_state" "critical" "container not found" "{\"container\":\"$(sanitize_kv "$container")\"}" || true
    fi

    log_line "DEBUG" "mode=docker container=$(sanitize_kv "$container") msg='logs_begin' tail=${log_tail}"
    docker logs --tail "$log_tail" "$container" 2>&1 | while IFS= read -r line; do
      printf "%s | level=DEBUG | mode=docker | container=%s | log=%s\n" \
        "$(ts)" "$(sanitize_kv "$container")" "$(sanitize_kv "$line")"
    done || true
    log_line "DEBUG" "mode=docker container=$(sanitize_kv "$container") msg='logs_end'"

    emit_flush || true
    sleep_or_stop "$INTERVAL" || break
  done

  log_line "INFO" "mode=docker msg='watch_stop' container=$(sanitize_kv "$container")"
}

# -----------------------------
# MODE 2: OpenShift / Kubernetes
# -----------------------------
k8s_monitor() {
  local kube ns show_events rollout_name
  kube="$(detect_k8s_cli)" || die "neither oc nor kubectl found"
  ns="$(read_default 'Namespace (or ALL)' 'ALL')"
  show_events="$(read_default 'Show events? (y/n)' 'y')"
  rollout_name="$(read_default 'Optional deployment to watch (or skip)' 'skip')"

  log_line "INFO" "mode=k8s msg='watch_start' cli=${kube} ns=$(sanitize_kv "$ns") show_events=${show_events} rollout=$(sanitize_kv "$rollout_name")"

  while ((_STOP == 0)); do
    log_line "INFO" "mode=k8s msg='pods_snapshot_begin' ns=$(sanitize_kv "$ns")"

    if [[ "$ns" == "ALL" ]]; then
      $kube get pods -A --no-headers 2>&1 | awk '
        {
          ns=$1; pod=$2; ready=$3; status=$4; restarts=$5; age=$6;
          lvl="INFO";
          if (status ~ /CrashLoopBackOff|ImagePullBackOff|ErrImagePull/) lvl="ERROR";
          printf "%s | level=%s | mode=k8s | resource=pod | namespace=%s pod=%s ready=%s status=%s restarts=%s age=%s\n",
            "'"$(ts)"'", lvl, ns, pod, ready, status, restarts, age
        }'
    else
      $kube get pods -n "$ns" --no-headers 2>&1 | awk -v NS="$ns" '
        {
          pod=$1; ready=$2; status=$3; restarts=$4; age=$5;
          lvl="INFO";
          if (status ~ /CrashLoopBackOff|ImagePullBackOff|ErrImagePull/) lvl="ERROR";
          printf "%s | level=%s | mode=k8s | resource=pod | namespace=%s pod=%s ready=%s status=%s restarts=%s age=%s\n",
            "'"$(ts)"'", lvl, NS, pod, ready, status, restarts, age
        }'
    fi
    log_line "INFO" "mode=k8s msg='pods_snapshot_end' ns=$(sanitize_kv "$ns")"

    if [[ "$show_events" =~ ^[Yy]$ ]]; then
      log_line "INFO" "mode=k8s msg='events_snapshot_begin' ns=$(sanitize_kv "$ns")"
      if [[ "$ns" == "ALL" ]]; then
        $kube get events -A --sort-by='.lastTimestamp' 2>&1 | tail -n 30 | while IFS= read -r line; do
          printf "%s | level=INFO | mode=k8s | resource=event | line=%s\n" "$(ts)" "$(sanitize_kv "$line")"
        done
      else
        $kube get events -n "$ns" --sort-by='.lastTimestamp' 2>&1 | tail -n 30 | while IFS= read -r line; do
          printf "%s | level=INFO | mode=k8s | resource=event | namespace=%s | line=%s\n" "$(ts)" "$(sanitize_kv "$ns")" "$(sanitize_kv "$line")"
        done
      fi
      log_line "INFO" "mode=k8s msg='events_snapshot_end' ns=$(sanitize_kv "$ns")"
    fi

    if [[ "$rollout_name" != "skip" && "$ns" != "ALL" ]]; then
      local out lvl
      out="$($kube rollout status "deploy/${rollout_name}" -n "$ns" --timeout=5s 2>&1 || true)"
      lvl="WARN"
      echo "$out" | grep -qi "successfully rolled out" && lvl="INFO"
      log_line "$lvl" "mode=k8s msg='rollout_status' namespace=$(sanitize_kv "$ns") deploy=$(sanitize_kv "$rollout_name") detail=$(sanitize_kv "$out")"
    fi

    emit_add "k8s_snapshot" "info" "k8s snapshot" "{\"cli\":\"$kube\",\"namespace\":\"$(sanitize_kv "$ns")\"}" || true
    emit_flush || true
    sleep_or_stop "$INTERVAL" || break
  done

  log_line "INFO" "mode=k8s msg='watch_stop' cli=${kube} ns=$(sanitize_kv "$ns")"
}

# -----------------------------
# MODE 3: systemd
# -----------------------------
systemd_monitor() {
  [[ "$(uname -s)" == "Linux" ]] || die "systemd monitor requires Linux"
  command -v systemctl >/dev/null 2>&1 || die "systemctl not found"

  local svc lines
  svc="$(read_default 'systemd service name' 'nginx')"
  lines="$(read_default 'journal lines' '80')"
  [[ "$lines" =~ ^[0-9]+$ ]] || die "journal lines must be integer"

  log_line "INFO" "mode=systemd msg='watch_start' service=$(sanitize_kv "$svc")"

  while ((_STOP == 0)); do
    local active failed sub lvl
    active="$(systemctl is-active "$svc" 2>/dev/null || echo "unknown")"
    failed="$(systemctl is-failed "$svc" 2>/dev/null || echo "unknown")"
    sub="$(systemctl show -p SubState --value "$svc" 2>/dev/null || echo "unknown")"

    lvl="INFO"
    [[ "$active" != "active" ]] && lvl="WARN"
    [[ "$failed" == "failed" ]] && lvl="ERROR"

    log_line "$lvl" "mode=systemd service=$(sanitize_kv "$svc") active=${active} failed=${failed} substate=$(sanitize_kv "$sub")"
    emit_add "systemd_state" "$([[ "$lvl" == "INFO" ]] && echo info || ([[ "$lvl" == "WARN" ]] && echo warning || echo critical))" \
      "systemd state" \
      "{\"service\":\"$(sanitize_kv "$svc")\",\"active\":\"$active\",\"failed\":\"$failed\"}" || true

    log_line "DEBUG" "mode=systemd service=$(sanitize_kv "$svc") msg='journal_begin' lines=${lines}"
    journalctl -u "$svc" -n "$lines" --no-pager 2>&1 | while IFS= read -r line; do
      printf "%s | level=DEBUG | mode=systemd | service=%s | log=%s\n" "$(ts)" "$(sanitize_kv "$svc")" "$(sanitize_kv "$line")"
    done || true
    log_line "DEBUG" "mode=systemd service=$(sanitize_kv "$svc") msg='journal_end'"

    emit_flush || true
    sleep_or_stop "$INTERVAL" || break
  done

  log_line "INFO" "mode=systemd msg='watch_stop' service=$(sanitize_kv "$svc")"
}

# -----------------------------
# MODE 4: URL synthetics (HTTP + latency + DNS/TLS hint)
# -----------------------------
url_monitor() {
  need_cmd curl || die "curl not found"
  local url host port scheme
  url="$(read_default 'URL to check' 'https://example.com/health')"
  read -r host port scheme <<<"$(url_host_port_scheme "$url")"

  log_line "INFO" "mode=url msg='watch_start' url=$(sanitize_kv "$url") host=$(sanitize_kv "$host") port=${port} scheme=$(sanitize_kv "$scheme")"

  while ((_STOP == 0)); do
    local dns tls
    dns="$(dns_check "$host")"
    tls="unknown"
    if [[ "$scheme" == "https" ]]; then
      tls="$(tls_hint "$host" "$port")"
    fi

    local res code total ip sslv latency_ms lvl sev
    res="$(curl -sS -L -o /dev/null \
      -w "code=%{http_code} time_total=%{time_total} remote_ip=%{remote_ip} ssl_verify=%{ssl_verify_result}" \
      "$url" 2>&1 || true)"

    code="$(echo "$res" | sed -n 's/.*code=\([0-9]\+\).*/\1/p' | head -n1)"
    total="$(echo "$res" | sed -n 's/.*time_total=\([0-9.]\+\).*/\1/p' | head -n1)"
    ip="$(echo "$res" | sed -n 's/.*remote_ip=\([^ ]\+\).*/\1/p' | head -n1)"
    sslv="$(echo "$res" | sed -n 's/.*ssl_verify=\([0-9]\+\).*/\1/p' | head -n1)"

    latency_ms="-1"
    if [[ -n "${total:-}" ]]; then
      latency_ms="$(python3 - <<PY 2>/dev/null || echo -1
t="${total}"
try:
  print(int(float(t)*1000))
except:
  print(-1)
PY
)"
    fi

    lvl="INFO"
    if [[ -z "${code:-}" || "$code" == "000" ]]; then
      lvl="ERROR"
      code="000"
    elif (( code >= 500 )); then
      lvl="ERROR"
    elif (( code >= 400 )); then
      lvl="WARN"
    fi

    sev="$([[ "$lvl" == "INFO" ]] && echo info || ([[ "$lvl" == "WARN" ]] && echo warning || echo critical))"

    log_line "$lvl" "mode=url url=$(sanitize_kv "$url") host=$(sanitize_kv "$host") dns=${dns} tls=${tls} http_code=${code} latency_ms=${latency_ms} remote_ip=$(sanitize_kv "${ip:-unknown}") ssl_verify=$(sanitize_kv "${sslv:-unknown}")"
    emit_add "url_check" "$sev" "url check" "{\"url\":\"$(sanitize_kv "$url")\",\"host\":\"$(sanitize_kv "$host")\",\"dns\":\"$dns\",\"tls\":\"$tls\",\"code\":\"$code\",\"latency_ms\":$latency_ms}" || true

    emit_flush || true
    sleep_or_stop "$INTERVAL" || break
  done

  log_line "INFO" "mode=url msg='watch_stop' url=$(sanitize_kv "$url")"
}

# -----------------------------
# MODE 5: file tail
# -----------------------------
file_monitor() {
  local fp lines
  fp="$(read_default 'Log file path' '/var/log/syslog')"
  lines="$(read_default 'tail -n lines' '80')"
  [[ "$lines" =~ ^[0-9]+$ ]] || die "tail lines must be integer"
  [[ -f "$fp" ]] || die "file not found: $fp"

  log_line "INFO" "mode=file msg='watch_start' file=$(sanitize_kv "$fp") lines=${lines}"

  while ((_STOP == 0)); do
    log_line "DEBUG" "mode=file file=$(sanitize_kv "$fp") msg='tail_begin' lines=${lines}"
    tail -n "$lines" "$fp" 2>&1 | while IFS= read -r line; do
      printf "%s | level=DEBUG | mode=file | file=%s | log=%s\n" "$(ts)" "$(sanitize_kv "$fp")" "$(sanitize_kv "$line")"
    done || true
    log_line "DEBUG" "mode=file file=$(sanitize_kv "$fp") msg='tail_end'"

    emit_add "file_tail" "info" "file tail snapshot" "{\"file\":\"$(sanitize_kv "$fp")\",\"lines\":$lines}" || true
    emit_flush || true
    sleep_or_stop "$INTERVAL" || break
  done

  log_line "INFO" "mode=file msg='watch_stop' file=$(sanitize_kv "$fp")"
}

# -----------------------------
# MODE 6: process by name
# -----------------------------
process_monitor() {
  need_cmd ps || die "ps not found"
  local pname
  pname="$(read_default 'Process name substring (e.g. nginx, java, python)' 'nginx')"

  log_line "INFO" "mode=process msg='watch_start' query=$(sanitize_kv "$pname")"

  while ((_STOP == 0)); do
    local out=""
    if ps -eo pid= >/dev/null 2>&1; then
      out="$(ps -eo pid,ppid,comm,%cpu,%mem,etime,args 2>/dev/null | grep -i "$pname" | grep -v grep || true)"
    else
      out="$(ps -ax -o pid,ppid,comm,%cpu,%mem,etime,args 2>/dev/null | grep -i "$pname" | grep -v grep || true)"
    fi

    if [[ -z "$out" ]]; then
      log_line "WARN" "mode=process msg='no_matches' query=$(sanitize_kv "$pname")"
      emit_add "process_snapshot" "warning" "no process matches" "{\"query\":\"$(sanitize_kv "$pname")\"}" || true
    else
      log_line "INFO" "mode=process msg='snapshot_begin' query=$(sanitize_kv "$pname")"
      while IFS= read -r line; do
        printf "%s | level=INFO | mode=process | line=%s\n" "$(ts)" "$(sanitize_kv "$line")"
      done <<< "$out"
      log_line "INFO" "mode=process msg='snapshot_end' query=$(sanitize_kv "$pname")"
      emit_add "process_snapshot" "info" "process snapshot" "{\"query\":\"$(sanitize_kv "$pname")\"}" || true
    fi

    emit_flush || true
    sleep_or_stop "$INTERVAL" || break
  done

  log_line "INFO" "mode=process msg='watch_stop' query=$(sanitize_kv "$pname")"
}

# -----------------------------
# Dispatch
# -----------------------------
case "$MODE" in
  1) docker_monitor ;;
  2) k8s_monitor ;;
  3) systemd_monitor ;;
  4) url_monitor ;;
  5) file_monitor ;;
  6) process_monitor ;;
  *) die "invalid selection: $MODE" ;;
esac

emit_flush || true
log_line "INFO" "msg='monitor_wizard_exit' code=0"
