#!/usr/bin/env bash
#
# CloudRecovery — monitor_anything.sh (Production Edition + Site-Down + DDoS + Ransomware + CloudSec)
# -----------------------------------------------------------------------------
# Universal interactive monitoring wizard for CloudRecovery.
# Streams strictly structured evidence to stdout (PTY) for AI correlation.
#
# Features
#   ✅ Hybrid: Docker + OpenShift/Kubernetes + systemd + URL + file tail + process
#   ✅ Site-Down Assistant (DNS/TLS/HTTP + quick hints)
#   ✅ Emergency DDoS monitor (observe-only)
#   ✅ NEW: Ransomware Integrity Watch (File patterns, CPU spikes, Auth logs)
#   ✅ NEW: Cloud Identity Guard (IMDS exposure, Env secrets, K8s tokens)
#   ✅ Strict logs: "ts | level=... | key=value ..."
#   ✅ Optional evidence push to Control Plane (agent-style) via env vars
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
  # keep evidence parseable: no whitespace/newlines/pipes in values
  echo "$1" | tr ' ' '_' | tr '\t' '_' | tr '\r' '_' | tr '\n' '_' | tr '|' '_' 
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

  # python3 is best-effort; if missing, message becomes empty string
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
    if command -v timeout >/dev/null 2>&1; then
      timeout 3 openssl s_client -connect "${host}:${port}" -servername "$host" </dev/null >/dev/null 2>&1 \
        && echo "ok" || echo "fail"
    else
      # fallback: no timeout available
      openssl s_client -connect "${host}:${port}" -servername "$host" </dev/null >/dev/null 2>&1 \
        && echo "ok" || echo "fail"
    fi
  else
    echo "unknown"
  fi
}

# -----------------------------
# Interactive selectors
# -----------------------------
choose_from_list() {
  # args: prompt_title, default_index, items...
  local title="$1"; shift
  local def="$1"; shift
  local items=("$@")
  local n="${#items[@]}"
  (( n > 0 )) || die "empty selection list for: $title"

  prompt "\n${title}\n"
  local i
  for i in "${!items[@]}"; do
    prompt "$(printf "  %2d) %s\n" "$((i+1))" "${items[$i]}")"
  done
  prompt "   0) Enter manually\n\n"

  local sel
  sel="$(read_default "Selection" "$def")"
  if [[ "$sel" == "0" ]]; then
    echo "__MANUAL__"
    return 0
  fi
  [[ "$sel" =~ ^[0-9]+$ ]] || die "invalid selection"
  (( sel >= 1 && sel <= n )) || die "selection out of range"
  echo "${items[$((sel-1))]}"
}

docker_list_running() {
  need_cmd docker || return 1
  docker ps --format '{{.Names}}|{{.Image}}|{{.Status}}' 2>/dev/null || true
}

k8s_list_namespaces() {
  local kube="$1"
  $kube get ns --no-headers 2>/dev/null | awk '{print $1}' || true
}

k8s_list_deployments() {
  local kube="$1"
  local ns="$2"
  $kube get deploy -n "$ns" --no-headers 2>/dev/null | awk '{print $1}' || true
}

k8s_list_pods() {
  local kube="$1"
  local ns="$2"
  $kube get pods -n "$ns" --no-headers 2>/dev/null | awk '{print $1" "$3}' || true
}

# -----------------------------
# NEW: DDoS / Site-Down helpers
# -----------------------------
top_remote_ips_from_accesslog() {
  # best-effort nginx/apache common log parsing (remote IP is first field)
  local file="$1"
  local n="${2:-15}"
  [[ -f "$file" ]] || { echo ""; return 0; }
  awk '{print $1}' "$file" 2>/dev/null | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' | sort | uniq -c | sort -nr | head -n "$n" || true
}

conntrack_top() {
  # Linux only; shows top destination ports in conntrack as a quick DDoS hint
  # Requires conntrack tool (often installed on servers)
  if command -v conntrack >/dev/null 2>&1; then
    conntrack -L 2>/dev/null | awk '
      {
        for(i=1;i<=NF;i++){
          if($i ~ /^dport=/){split($i,a,"="); print a[2]}
        }
      }' | sort | uniq -c | sort -nr | head -n 10 || true
  fi
}

ss_synrecv_count() {
  # Linux only: SYN-RECV states can indicate SYN flood
  if command -v ss >/dev/null 2>&1; then
    ss -Hn state syn-recv 2>/dev/null | wc -l | tr -d ' ' || echo "0"
  else
    echo "0"
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
# Menu (requested prompt lines + new modes)
# -----------------------------
prompt $'\nSelect monitoring target:\n'
prompt $'  1) 🐳 Docker Container (Status + Logs)\n'
prompt $'  2) ☸️  OpenShift / Kubernetes (Pods + Events + Optional Rollout)\n'
prompt $'  3) ⚙️  Systemd Service (Active State + Recent Journal)\n'
prompt $'  4) 🌐 URL Health (HTTP Code + Latency + DNS/TLS hint)\n'
prompt $'  5) 📄 Log File Tail (Local file)\n'
prompt $'  6) 🔍 Process by name (ps snapshot)\n'
prompt $'  7) 🆘 Site-Down Assistant (DNS/TLS/HTTP + K8s/Docker quick checks)\n'
prompt $'  8) 🛡️ Emergency DDoS Monitor (observe-only: traffic clues + top IPs + SYN hints)\n'
prompt $'  9) 🦠 Ransomware & Integrity Watch (Extension scan + CPU spikes + Auth logs)\n'
prompt $' 10) 🔑 Cloud Identity & Security Hygiene (IMDS + Env Secrets + K8s Tokens)\n\n'

MODE="$(read_default 'Selection' '7')"
INTERVAL_RAW="$(read_default 'Polling interval seconds' '10')"
INTERVAL="${INTERVAL_RAW// /}"
[[ "$INTERVAL" =~ ^[0-9]+$ ]] || die "interval must be an integer"

log_line "INFO" "msg='monitor_configured' mode=${MODE} interval_s=${INTERVAL}"

# -----------------------------
# MODE 1: Docker (interactive list)
# -----------------------------
docker_monitor() {
  need_cmd docker || die "docker not found"

  local list line chosen container log_tail
  mapfile -t list < <(docker_list_running | sed '/^$/d' || true)

  if ((${#list[@]} > 0)); then
    chosen="$(choose_from_list "Choose a running Docker container to monitor" "1" "${list[@]}")"
    if [[ "$chosen" == "__MANUAL__" ]]; then
      container="$(read_default 'Container name or ID' 'my-app')"
    else
      container="${chosen%%|*}"
    fi
  else
    log_line "WARN" "mode=docker msg='no_running_containers_found' hint='docker ps empty'"
    container="$(read_default 'Container name or ID' 'my-app')"
  fi

  log_tail="$(read_default 'docker logs --tail N' '80')"
  [[ "$log_tail" =~ ^[0-9]+$ ]] || die "log tail must be an integer"

  log_line "INFO" "mode=docker msg='watch_start' container=$(sanitize_kv "$container")"

  while ((_STOP == 0)); do
    if docker inspect "$container" >/dev/null 2>&1; then
      local running status exit_code started finished image restartc health
      running="$(docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null || echo "unknown")"
      status="$(docker inspect -f '{{.State.Status}}' "$container" 2>/dev/null || echo "unknown")"
      exit_code="$(docker inspect -f '{{.State.ExitCode}}' "$container" 2>/dev/null || echo "unknown")"
      started="$(docker inspect -f '{{.State.StartedAt}}' "$container" 2>/dev/null || echo "unknown")"
      finished="$(docker inspect -f '{{.State.FinishedAt}}' "$container" 2>/dev/null || echo "unknown")"
      image="$(docker inspect -f '{{.Config.Image}}' "$container" 2>/dev/null || echo "unknown")"
      restartc="$(docker inspect -f '{{.RestartCount}}' "$container" 2>/dev/null || echo "unknown")"
      health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container" 2>/dev/null || echo "unknown")"

      local lvl="INFO"
      [[ "$running" == "false" || "$status" == "exited" ]] && lvl="WARN"
      [[ "$exit_code" != "0" && "$exit_code" != "unknown" ]] && lvl="ERROR"
      [[ "$health" == "unhealthy" ]] && lvl="ERROR"

      log_line "$lvl" "mode=docker container=$(sanitize_kv "$container") image=$(sanitize_kv "$image") status=${status} running=${running} exit_code=${exit_code} restart_count=$(sanitize_kv "$restartc") health=$(sanitize_kv "$health") started_at=$(sanitize_kv "$started") finished_at=$(sanitize_kv "$finished")"
      emit_add "docker_state" "$([[ "$lvl" == "INFO" ]] && echo info || ([[ "$lvl" == "WARN" ]] && echo warning || echo critical))" \
        "docker container state" \
        "{\"container\":\"$(sanitize_kv "$container")\",\"image\":\"$(sanitize_kv "$image")\",\"status\":\"$status\",\"running\":\"$running\",\"exit_code\":\"$exit_code\",\"restart_count\":\"$(sanitize_kv "$restartc")\",\"health\":\"$(sanitize_kv "$health")\"}" || true
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
# MODE 2: OpenShift / Kubernetes (interactive namespace/deploy selection)
# -----------------------------
k8s_monitor() {
  local kube ns show_events rollout_name

  kube="$(detect_k8s_cli)" || die "neither oc nor kubectl found"

  # Namespace selection
  local ns_list chosen_ns manual_ns
  mapfile -t ns_list < <(k8s_list_namespaces "$kube" | sed '/^$/d' || true)
  if ((${#ns_list[@]} > 0)); then
    chosen_ns="$(choose_from_list "Choose namespace to monitor (or manual)" "1" "${ns_list[@]}")"
    if [[ "$chosen_ns" == "__MANUAL__" ]]; then
      ns="$(read_default 'Namespace (or ALL)' 'ALL')"
    else
      ns="$chosen_ns"
      # allow ALL as quick option
      local all_ans
      all_ans="$(read_default "Monitor ALL namespaces instead? (y/n)" "n")"
      [[ "$all_ans" =~ ^[Yy]$ ]] && ns="ALL"
    fi
  else
    ns="$(read_default 'Namespace (or ALL)' 'ALL')"
  fi

  show_events="$(read_default 'Show events? (y/n)' 'y')"

  # Optional rollout deployment selection (only if single namespace)
  rollout_name="skip"
  if [[ "$ns" != "ALL" ]]; then
    local dep_ans dep_list chosen_dep
    dep_ans="$(read_default 'Watch a specific Deployment rollout? (y/n)' 'n')"
    if [[ "$dep_ans" =~ ^[Yy]$ ]]; then
      mapfile -t dep_list < <(k8s_list_deployments "$kube" "$ns" | sed '/^$/d' || true)
      if ((${#dep_list[@]} > 0)); then
        chosen_dep="$(choose_from_list "Choose a Deployment to watch (or manual)" "1" "${dep_list[@]}")"
        if [[ "$chosen_dep" == "__MANUAL__" ]]; then
          rollout_name="$(read_default 'Deployment name' 'my-deploy')"
        else
          rollout_name="$chosen_dep"
        fi
      else
        rollout_name="$(read_default 'Deployment name (none listed; type manually or skip)' 'skip')"
      fi
    fi
  fi

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
      out="$(ps -eo pid,ppid,comm,%cpu,%mem,etime,args 2>/dev/null | grep -F -i -- "$pname" | grep -v grep || true)"
    else
      out="$(ps -ax -o pid,ppid,comm,%cpu,%mem,etime,args 2>/dev/null | grep -F -i -- "$pname" | grep -v grep || true)"
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
# MODE 7: Site-Down Assistant (DNS/TLS/HTTP + optional Docker/K8s hints)
# -----------------------------
site_down_assistant() {
  prompt "\nSite-Down Assistant: this is READ-ONLY triage. It emits explicit triggers for the AI.\n\n" >&2

  local url host port scheme
  url="$(read_default 'Primary URL to validate (health endpoint preferred)' 'https://example.com/health')"
  read -r host port scheme <<<"$(url_host_port_scheme "$url")"

  local check_k8s check_docker
  check_k8s="$(read_default 'Also check Kubernetes/OpenShift? (y/n)' 'y')"
  check_docker="$(read_default 'Also check Docker container? (y/n)' 'n')"

  # optional targets
  local docker_target=""
  if [[ "$check_docker" =~ ^[Yy]$ ]]; then
    need_cmd docker || log_line "WARN" "mode=site_down msg='docker_not_available'"
    if command -v docker >/dev/null 2>&1; then
      local list chosen
      mapfile -t list < <(docker_list_running | sed '/^$/d' || true)
      if ((${#list[@]} > 0)); then
        chosen="$(choose_from_list "Choose Docker container for quick state check" "1" "${list[@]}")"
        if [[ "$chosen" == "__MANUAL__" ]]; then
          docker_target="$(read_default 'Container name or ID' 'my-app')"
        else
          docker_target="${chosen%%|*}"
        fi
      else
        docker_target="$(read_default 'Container name or ID (none listed)' 'my-app')"
      fi
    fi
  fi

  local kube="" ns=""
  if [[ "$check_k8s" =~ ^[Yy]$ ]]; then
    kube="$(detect_k8s_cli 2>/dev/null || true)"
    if [[ -n "$kube" ]]; then
      ns="$(read_default 'Namespace for quick K8s check (or ALL)' 'ALL')"
    else
      log_line "WARN" "mode=site_down msg='k8s_cli_not_found' hint='install oc or kubectl'"
    fi
  fi

  log_line "INFO" "mode=site_down msg='watch_start' url=$(sanitize_kv "$url") host=$(sanitize_kv "$host")"

  while ((_STOP == 0)); do
    local dns tls
    dns="$(dns_check "$host")"
    tls="unknown"
    [[ "$scheme" == "https" ]] && tls="$(tls_hint "$host" "$port")"

    local res code total latency_ms ip
    res="$(curl -sS -L -o /dev/null -w "code=%{http_code} time_total=%{time_total} remote_ip=%{remote_ip}" "$url" 2>&1 || true)"
    code="$(echo "$res" | sed -n 's/.*code=\([0-9]\+\).*/\1/p' | head -n1)"
    total="$(echo "$res" | sed -n 's/.*time_total=\([0-9.]\+\).*/\1/p' | head -n1)"
    ip="$(echo "$res" | sed -n 's/.*remote_ip=\([^ ]\+\).*/\1/p' | head -n1)"

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

    local lvl="INFO"
    if [[ -z "${code:-}" || "$code" == "000" ]]; then
      lvl="ERROR"
      code="000"
    elif (( code >= 500 )); then
      lvl="ERROR"
    elif (( code >= 400 )); then
      lvl="WARN"
    fi

    # Explicit triggers to help the AI
    local trigger="ok"
    if [[ "$dns" == "fail" ]]; then trigger="dns_fail"; fi
    if [[ "$dns" == "ok" && "$scheme" == "https" && "$tls" == "fail" ]]; then trigger="tls_fail"; fi
    if [[ "$code" == "000" ]]; then trigger="connect_fail"; fi
    if (( code >= 500 )); then trigger="http_5xx"; fi
    if (( code >= 400 && code < 500 )); then trigger="http_4xx"; fi

    log_line "$lvl" "mode=site_down msg='probe' trigger=${trigger} url=$(sanitize_kv "$url") dns=${dns} tls=${tls} http_code=${code} latency_ms=${latency_ms} remote_ip=$(sanitize_kv "${ip:-unknown}")"
    emit_add "site_down_probe" "$([[ "$lvl" == "INFO" ]] && echo info || ([[ "$lvl" == "WARN" ]] && echo warning || echo critical))" \
      "site down probe" \
      "{\"url\":\"$(sanitize_kv "$url")\",\"trigger\":\"$trigger\",\"dns\":\"$dns\",\"tls\":\"$tls\",\"code\":\"$code\",\"latency_ms\":$latency_ms}" || true

    # Optional quick Docker state check
    if [[ -n "$docker_target" ]]; then
      if docker inspect "$docker_target" >/dev/null 2>&1; then
        local st run ex hc
        run="$(docker inspect -f '{{.State.Running}}' "$docker_target" 2>/dev/null || echo "unknown")"
        st="$(docker inspect -f '{{.State.Status}}' "$docker_target" 2>/dev/null || echo "unknown")"
        ex="$(docker inspect -f '{{.State.ExitCode}}' "$docker_target" 2>/dev/null || echo "unknown")"
        hc="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$docker_target" 2>/dev/null || echo "unknown")"
        local dlvl="INFO"
        [[ "$run" == "false" || "$st" == "exited" || "$hc" == "unhealthy" ]] && dlvl="WARN"
        [[ "$ex" != "0" && "$ex" != "unknown" ]] && dlvl="ERROR"
        log_line "$dlvl" "mode=site_down msg='docker_hint' container=$(sanitize_kv "$docker_target") status=$(sanitize_kv "$st") running=$(sanitize_kv "$run") exit_code=$(sanitize_kv "$ex") health=$(sanitize_kv "$hc")"
      else
        log_line "WARN" "mode=site_down msg='docker_hint_container_not_found' container=$(sanitize_kv "$docker_target")"
      fi
    fi

    # Optional quick K8s check: emit only "bad pods" summaries
    if [[ -n "$kube" ]]; then
      if [[ "$ns" == "ALL" ]]; then
        # Count problematic pods across all namespaces (fast summary)
        local bad
        bad="$($kube get pods -A --no-headers 2>/dev/null | awk '$4 ~ /CrashLoopBackOff|ImagePullBackOff|ErrImagePull|Pending/ {c++} END{print c+0}' || echo 0)"
        if (( bad > 0 )); then
          log_line "WARN" "mode=site_down msg='k8s_hint_bad_pods' namespace=ALL bad_pods=${bad}"
        else
          log_line "INFO" "mode=site_down msg='k8s_hint_ok' namespace=ALL bad_pods=0"
        fi
      else
        local bad
        bad="$($kube get pods -n "$ns" --no-headers 2>/dev/null | awk '$3 ~ /CrashLoopBackOff|ImagePullBackOff|ErrImagePull|Pending/ {c++} END{print c+0}' || echo 0)"
        if (( bad > 0 )); then
          log_line "WARN" "mode=site_down msg='k8s_hint_bad_pods' namespace=$(sanitize_kv "$ns") bad_pods=${bad}"
        else
          log_line "INFO" "mode=site_down msg='k8s_hint_ok' namespace=$(sanitize_kv "$ns") bad_pods=0"
        fi
      fi
    fi

    emit_flush || true
    sleep_or_stop "$INTERVAL" || break
  done

  log_line "INFO" "mode=site_down msg='watch_stop' url=$(sanitize_kv "$url")"
}

# -----------------------------
# MODE 8: Emergency DDoS Monitor (observe-only)
# -----------------------------
ddos_monitor() {
  prompt "\nEmergency DDoS Monitor: OBSERVE-ONLY. It emits triggers and summaries.\n" >&2
  prompt "Tip: point it at your origin access log to identify top talkers.\n\n" >&2

  [[ "$(uname -s)" == "Linux" ]] || log_line "WARN" "mode=ddos msg='non_linux' hint='some network hints (ss/conntrack) require Linux'"

  local url host port scheme
  url="$(read_default 'Public URL to probe (edge/origin)' 'https://example.com/')"
  read -r host port scheme <<<"$(url_host_port_scheme "$url")"

  local access_log=""
  local want_log
  want_log="$(read_default 'Parse access log for top IPs? (y/n)' 'n')"
  if [[ "$want_log" =~ ^[Yy]$ ]]; then
    access_log="$(read_default 'Access log path (nginx/apache) e.g. /var/log/nginx/access.log' '/var/log/nginx/access.log')"
  fi

  local rate_ms
  rate_ms="$(read_default 'DDoS probe threshold latency ms (warn above)' '1200')"
  [[ "$rate_ms" =~ ^[0-9]+$ ]] || die "threshold must be integer"

  log_line "INFO" "mode=ddos msg='watch_start' url=$(sanitize_kv "$url") host=$(sanitize_kv "$host") threshold_ms=${rate_ms}"

  while ((_STOP == 0)); do
    # 1) Quick HTTP probe (gives a symptom line)
    need_cmd curl || die "curl not found"
    local res code total latency_ms ip
    res="$(curl -sS -L -o /dev/null -w "code=%{http_code} time_total=%{time_total} remote_ip=%{remote_ip}" "$url" 2>&1 || true)"
    code="$(echo "$res" | sed -n 's/.*code=\([0-9]\+\).*/\1/p' | head -n1)"
    total="$(echo "$res" | sed -n 's/.*time_total=\([0-9.]\+\).*/\1/p' | head -n1)"
    ip="$(echo "$res" | sed -n 's/.*remote_ip=\([^ ]\+\).*/\1/p' | head -n1)"

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

    local dns tls
    dns="$(dns_check "$host")"
    tls="unknown"
    [[ "$scheme" == "https" ]] && tls="$(tls_hint "$host" "$port")"

    # 2) Network hints (Linux best-effort)
    local synrecv="0"
    synrecv="$(ss_synrecv_count || echo 0)"
    local conn_top=""
    conn_top="$(conntrack_top | tr '\n' ';' | sed 's/;*$//' || true)"

    # 3) Access log top talkers
    local top_ips=""
    if [[ -n "$access_log" ]]; then
      top_ips="$(top_remote_ips_from_accesslog "$access_log" 15 | tr '\n' ';' | sed 's/;*$//' || true)"
    fi

    # 4) Decide “trigger”
    local lvl="INFO"
    local trigger="normal"
    if [[ "$code" == "000" || -z "${code:-}" ]]; then
      lvl="ERROR"; trigger="site_down"
    elif (( code >= 500 )); then
      lvl="ERROR"; trigger="http_5xx_spike"
    elif (( latency_ms >= rate_ms )); then
      lvl="WARN"; trigger="latency_spike"
    fi
    if [[ "$dns" == "fail" ]]; then
      lvl="ERROR"; trigger="dns_fail"
    fi
    if [[ "$scheme" == "https" && "$tls" == "fail" ]]; then
      lvl="ERROR"; trigger="tls_fail"
    fi
    if [[ "$synrecv" =~ ^[0-9]+$ ]] && (( synrecv > 2000 )); then
      # heuristic: many syn-recv can indicate SYN flood (tune per host)
      [[ "$lvl" == "INFO" ]] && lvl="WARN"
      trigger="syn_flood_suspected"
    fi

    log_line "$lvl" "mode=ddos msg='snapshot' trigger=${trigger} url=$(sanitize_kv "$url") dns=${dns} tls=${tls} http_code=${code:-000} latency_ms=${latency_ms} remote_ip=$(sanitize_kv "${ip:-unknown}") syn_recv_count=${synrecv} conntrack_top_dports=$(sanitize_kv "${conn_top:-none}") top_ips=$(sanitize_kv "${top_ips:-none}")"
    emit_add "ddos_snapshot" "$([[ "$lvl" == "INFO" ]] && echo info || ([[ "$lvl" == "WARN" ]] && echo warning || echo critical))" \
      "ddos snapshot" \
      "{\"url\":\"$(sanitize_kv "$url")\",\"trigger\":\"$trigger\",\"dns\":\"$dns\",\"tls\":\"$tls\",\"code\":\"${code:-000}\",\"latency_ms\":$latency_ms,\"syn_recv\":$synrecv,\"conntrack_top\":\"$(sanitize_kv "${conn_top:-}")\",\"top_ips\":\"$(sanitize_kv "${top_ips:-}")\"}" || true

    # Emit explicit “recommended next checks” lines (AI-friendly)
    if [[ "$trigger" != "normal" ]]; then
      log_line "INFO" "mode=ddos msg='next_checks' hint='check_edge_waf_events, rate_limits, bot_score, origin_protection, autoscaling, k8s_hpa, lb_health, top_urls'"
    fi

    emit_flush || true
    sleep_or_stop "$INTERVAL" || break
  done

  log_line "INFO" "mode=ddos msg='watch_stop' url=$(sanitize_kv "$url")"
}

# -----------------------------
# MODE 9: Ransomware & Integrity Watch
# -----------------------------
ransomware_monitor() {
  prompt "\nRansomware Watch: Monitors for encryption activity, suspicious notes, and high CPU.\n" >&2
  prompt "NOTE: Heuristic only. Does not replace Endpoint Protection.\n\n" >&2

  local watch_dir
  watch_dir="$(read_default 'Directory to scan for recent changes' '/var/www')"
  local auth_log
  auth_log="$(read_default 'Path to auth.log/secure (for sudo monitoring)' '/var/log/auth.log')"

  log_line "INFO" "mode=ransomware msg='watch_start' dir=$(sanitize_kv "$watch_dir") auth_log=$(sanitize_kv "$auth_log")"

  while ((_STOP == 0)); do
    # 1. Check for suspicious extensions (shallow scan for performance)
    # Common ransomware extensions: .enc, .locked, .crypt, .ryuk, .fuck, .000
    local sus_files
    sus_files="$(find "$watch_dir" -maxdepth 3 -type f \( -name "*.enc" -o -name "*.locked" -o -name "*.crypt" -o -name "*.ryuk" -o -name "*_READ_ME.txt" -o -name "*_DECRYPT_*.txt" \) -print -quit 2>/dev/null || true)"

    if [[ -n "$sus_files" ]]; then
      log_line "CRITICAL" "mode=ransomware msg='suspicious_file_found' trigger='known_extension' file=$(sanitize_kv "$sus_files")"
      emit_add "ransomware_alert" "critical" "suspicious file extension found" "{\"file\":\"$(sanitize_kv "$sus_files")\"}" || true
    else
      log_line "INFO" "mode=ransomware msg='file_scan_clean' dir=$(sanitize_kv "$watch_dir")"
    fi

    # 2. Check for crypto-miner signature (High CPU + User Load)
    # Heuristic: Load > 2 and User CPU > 80% might indicate mining if unexpected
    local cpu_user
    if command -v top >/dev/null 2>&1; then
      # Grep user cpu from top (Linux/Batch mode)
      cpu_user="$(top -bn1 | grep "Cpu(s)" | sed "s/.*, *\([0-9.]*\)%* id.*/\1/" | awk '{print 100 - $1}' 2>/dev/null || echo 0)"
      # Integer comparison in bash
      if [[ "${cpu_user%.*}" -gt 85 ]]; then
        # If high CPU, list top process
        local top_proc
        top_proc="$(ps -eo comm,%cpu --sort=-%cpu | head -n 2 | tail -n 1 | tr -d ' ')"
        log_line "WARN" "mode=ransomware msg='high_cpu_detected' user_cpu=${cpu_user}% top_proc=$(sanitize_kv "$top_proc") hint='check_for_crypto_miner_or_encryption_process'"
      fi
    fi

    # 3. Monitor Auth Log for rapid Sudo failures or mass usage
    if [[ -f "$auth_log" ]]; then
      log_line "DEBUG" "mode=ransomware msg='auth_log_tail'"
      # Look for "sudo" commands or "failed password"
      tail -n 10 "$auth_log" 2>/dev/null | grep -E "COMMAND|Failed password" | while read -r line; do
         printf "%s | level=WARN | mode=ransomware | type=auth_event | msg=%s\n" "$(ts)" "$(sanitize_kv "$line")"
      done || true
    fi

    emit_flush || true
    sleep_or_stop "$INTERVAL" || break
  done
  log_line "INFO" "mode=ransomware msg='watch_stop'"
}

# -----------------------------
# MODE 10: Cloud Identity & Security Hygiene
# -----------------------------
cloud_identity_monitor() {
  prompt "\nCloud Identity Guard: Audits 'Keys to the Kingdom' risks.\n" >&2
  
  log_line "INFO" "mode=cloud_sec msg='audit_start'"

  while ((_STOP == 0)); do
    # 1. IMDS Exposure Check (AWS/GCP/Azure Metadata)
    # Attackers steal credentials from here if accessible via SSRF
    local imds_aws imds_gcp
    imds_aws="$(curl -s --max-time 2 http://169.254.169.254/latest/meta-data/iam/security-credentials/ 2>/dev/null || true)"
    if [[ -n "$imds_aws" ]]; then
       log_line "CRITICAL" "mode=cloud_sec msg='aws_imds_exposed' risk='credential_theft_possible' hint='block_imds_access_or_require_token'"
    fi
    
    imds_gcp="$(curl -s -H "Metadata-Flavor: Google" --max-time 2 http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token 2>/dev/null || true)"
    if [[ -n "$imds_gcp" ]]; then
       log_line "CRITICAL" "mode=cloud_sec msg='gcp_metadata_exposed' risk='service_account_token_exposed'"
    fi

    # 2. Dangerous Environment Variables
    # Look for AWS keys, Kubeconfigs, Passwords in current env
    local risky_env
    risky_env="$(env | grep -E "AWS_SECRET|KUBECONFIG|PASSWORD|TOKEN" | cut -d= -f1 | tr '\n' ',' || true)"
    if [[ -n "$risky_env" ]]; then
       log_line "WARN" "mode=cloud_sec msg='risky_env_vars_detected' vars=$(sanitize_kv "$risky_env") hint='secrets_should_not_be_in_plaintext_env'"
    fi

    # 3. Kubernetes Service Account Token Mount
    # Checks if the pod has automountServiceAccountToken=true (default)
    if [[ -f "/var/run/secrets/kubernetes.io/serviceaccount/token" ]]; then
       log_line "INFO" "mode=cloud_sec msg='k8s_sa_token_present' path='/var/run/secrets/kubernetes.io/serviceaccount/token' hint='verify_rbac_least_privilege'"
       # Check if we can list secrets (Active Check)
       if command -v kubectl >/dev/null 2>&1; then
          if kubectl auth can-i list secrets >/dev/null 2>&1; then
             log_line "CRITICAL" "mode=cloud_sec msg='k8s_rbac_too_permissive' permission='list_secrets' risk='cluster_compromise'"
          fi
       fi
    fi

    # 4. Root User Check
    if [[ "$(id -u)" == "0" ]]; then
       log_line "WARN" "mode=cloud_sec msg='running_as_root' risk='container_breakout_easier'"
    fi

    emit_flush || true
    sleep_or_stop "$INTERVAL" || break
  done
  log_line "INFO" "mode=cloud_sec msg='audit_stop'"
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
  7) site_down_assistant ;;
  8) ddos_monitor ;;
  9) ransomware_monitor ;;
  10) cloud_identity_monitor ;;
  *) die "invalid selection: $MODE" ;;
esac

emit_flush || true
log_line "INFO" "msg='monitor_wizard_exit' code=0"