#!/usr/bin/env bash
#
# faultctl.sh — scripted, repeatable fault injection (SRS stretch req S4)
#
#   ./scripts/faultctl.sh list
#   ./scripts/faultctl.sh inject broken-commit|crashloop|latency
#   ./scripts/faultctl.sh heal
#   ./scripts/faultctl.sh status

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="manifests/demo-api"
APP_FILE="${APP_DIR}/app.py"
FAULTS_DIR="faults"

NAMESPACE="demo"
DEPLOYMENT="demo-api"
ARGOCD_NS="argocd"
ARGOCD_APP="demo-api"

ALERTMANAGER_NS="monitoring"
ALERTMANAGER_SVC="kube-prometheus-stack-alertmanager"
ALERTMANAGER_PORT="9093"
LOCAL_AM_PORT="19093"

SYNC_TIMEOUT=120
ROLLOUT_TIMEOUT=120
ALERT_TIMEOUT=300

scenario_file()  { case "$1" in
  broken-commit) echo "app.broken-commit.py" ;;
  crashloop)     echo "app.crashloop.py" ;;
  latency)       echo "app.latency.py" ;;
  healthy)       echo "app.healthy.py" ;;
  *)             return 1 ;;
esac; }

scenario_alert() { case "$1" in
  broken-commit) echo "DemoApiHighErrorRate" ;;
  crashloop)     echo "DemoApiCrashLooping" ;;
  latency)       echo "DemoApiHighLatency" ;;
  *)             return 1 ;;
esac; }

scenario_desc()  { case "$1" in
  broken-commit) echo "Unhandled KeyError in /work — pods stay up, emit HTTP 500s" ;;
  crashloop)     echo "Missing module at import time — pods crash and restart" ;;
  latency)       echo "Artificial 3s delay in /work — latency breach, no errors" ;;
esac; }

SCENARIOS=(broken-commit crashloop latency)

BOLD=$'\033[1m'; RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'
BLU=$'\033[34m'; DIM=$'\033[2m'; RST=$'\033[0m'

START_TS=0
timeline=()
elapsed() { echo $(( $(date +%s) - START_TS )); }
mark() {
  local t; t=$(elapsed)
  timeline+=("$(printf '%4ds  %s' "$t" "$1")")
  printf '%s[%3ds]%s %s%s%s\n' "$DIM" "$t" "$RST" "$GRN" "$1" "$RST"
}
step() { printf '\n%s==>%s %s%s%s\n' "$BLU" "$RST" "$BOLD" "$1" "$RST"; }
warn() { printf '%s!!%s %s\n' "$YEL" "$RST" "$1"; }
die()  { printf '%s!!%s %s\n' "$RED" "$RST" "$1" >&2; exit 1; }
print_timeline() {
  printf '\n%s---- timeline ----%s\n' "$BOLD" "$RST"
  printf '%s\n' "${timeline[@]}"
  printf '%s------------------%s\n' "$BOLD" "$RST"
}

preflight() {
  command -v kubectl >/dev/null || die "kubectl not found on PATH"
  command -v git     >/dev/null || die "git not found on PATH"
  command -v curl    >/dev/null || die "curl not found on PATH"
  kubectl cluster-info >/dev/null 2>&1 || die "cannot reach the cluster (VIP down?)"
  [[ -d "${REPO_DIR}/.git" ]] || die "not a git repo: ${REPO_DIR}"
  [[ -f "${REPO_DIR}/${APP_FILE}" ]] || die "missing ${APP_FILE}"
  [[ -d "${REPO_DIR}/${FAULTS_DIR}" ]] || die "missing ${FAULTS_DIR}/"
}

require_clean_tree() {
  cd "${REPO_DIR}"
  if [[ -n "$(git status --porcelain -- "${APP_DIR}")" ]]; then
    die "uncommitted changes in ${APP_DIR}. Commit or stash first."
  fi
}

AM_PF_PID=""
am_port_forward() {
  kubectl -n "${ALERTMANAGER_NS}" port-forward \
    "svc/${ALERTMANAGER_SVC}" "${LOCAL_AM_PORT}:${ALERTMANAGER_PORT}" \
    >/dev/null 2>&1 &
  AM_PF_PID=$!
  for _ in {1..20}; do
    curl -sf "http://127.0.0.1:${LOCAL_AM_PORT}/-/ready" >/dev/null 2>&1 && return 0
    sleep 0.5
  done
  warn "could not reach Alertmanager — alert watch disabled"
  return 1
}
am_cleanup() {
  [[ -n "${AM_PF_PID}" ]] && kill "${AM_PF_PID}" 2>/dev/null || true
  AM_PF_PID=""
}
trap am_cleanup EXIT

am_firing() {
  curl -sf "http://127.0.0.1:${LOCAL_AM_PORT}/api/v2/alerts?active=true&silenced=false&inhibited=false" \
    2>/dev/null | grep -q "\"alertname\":\"$1\""
}
am_active_names() {
  curl -sf "http://127.0.0.1:${LOCAL_AM_PORT}/api/v2/alerts?active=true" 2>/dev/null \
    | tr ',' '\n' | grep '"alertname"' | cut -d'"' -f4 | sort -u
}

argocd_refresh() {
  kubectl -n "${ARGOCD_NS}" annotate application "${ARGOCD_APP}" \
    argocd.argoproj.io/refresh=hard --overwrite >/dev/null
}
argocd_field() {
  kubectl -n "${ARGOCD_NS}" get application "${ARGOCD_APP}" \
    -o jsonpath="{$1}" 2>/dev/null || true
}
wait_for_sync() {
  local want="$1" deadline=$(( $(date +%s) + SYNC_TIMEOUT ))
  while (( $(date +%s) < deadline )); do
    local rev status health
    rev="$(argocd_field .status.sync.revision)"
    status="$(argocd_field .status.sync.status)"
    health="$(argocd_field .status.health.status)"
    if [[ "${rev}" == "${want}"* && "${status}" == "Synced" && "${health}" == "Healthy" ]]; then
      return 0
    fi
    printf '\r  %ssync=%-10s health=%-10s rev=%.7s%s' \
      "$DIM" "${status:-?}" "${health:-?}" "${rev:-unknown}" "$RST"
    sleep 3
  done
  printf '\n'
  return 1
}

apply_variant() {
  local scen="$1" subject="$2" variant
  variant="$(scenario_file "${scen}")" || die "unknown scenario: ${scen}"
  local src="${REPO_DIR}/${FAULTS_DIR}/${variant}"
  [[ -f "${src}" ]] || die "missing variant: ${FAULTS_DIR}/${variant}"

  cd "${REPO_DIR}"
  cp "${src}" "${APP_FILE}"

  if git diff --quiet -- "${APP_FILE}"; then
    warn "${APP_FILE} already matches ${variant}"
    git rev-parse HEAD
    return 0
  fi

  git add "${APP_FILE}"
  git commit -q -m "${subject}"
  git push -q origin HEAD 2>/dev/null \
    || die "git push failed. Run 'gh auth setup-git' and retry."
  git rev-parse HEAD
}

cmd_inject() {
  local scen="${1:-}"
  [[ -n "${scen}" ]] || die "usage: faultctl.sh inject <scenario>"
  scenario_file "${scen}" >/dev/null || die "unknown scenario: ${scen}"

  preflight
  require_clean_tree
  local alert; alert="$(scenario_alert "${scen}")"
  START_TS=$(date +%s)

  printf '\n%sFault injection: %s%s\n' "$BOLD" "${scen}" "$RST"
  printf '%s%s%s\n' "$DIM" "$(scenario_desc "${scen}")" "$RST"
  printf '%sExpecting alert: %s%s\n' "$DIM" "${alert}" "$RST"

  step "Committing broken code to Git"
  local sha; sha="$(apply_variant "${scen}" "fault(${scen}): inject demo fault via faultctl")"
  mark "pushed ${sha:0:7} to origin"

  step "Forcing ArgoCD reconciliation"
  argocd_refresh
  if wait_for_sync "${sha}"; then
    printf '\n'; mark "ArgoCD reported Synced + Healthy at ${sha:0:7}"
  else
    warn "ArgoCD did not reach Synced within ${SYNC_TIMEOUT}s"
  fi

  step "Waiting for pod rollout"
  if kubectl -n "${NAMESPACE}" rollout status "deploy/${DEPLOYMENT}" \
       --timeout="${ROLLOUT_TIMEOUT}s" >/dev/null 2>&1; then
    mark "new pods running with faulty code"
  else
    mark "rollout did not converge (expected for '${scen}')"
  fi

  step "Watching Alertmanager for ${alert}"
  if am_port_forward; then
    local deadline=$(( $(date +%s) + ALERT_TIMEOUT )) seen=0
    while (( $(date +%s) < deadline )); do
      if am_firing "${alert}"; then seen=1; break; fi
      printf '\r  %swaiting... %ss elapsed%s' "$DIM" "$(elapsed)" "$RST"
      sleep 5
    done
    printf '\n'
    if (( seen )); then
      mark "${alert} FIRING — webhook delivered to sre-copilot"
    else
      warn "${alert} did not fire within ${ALERT_TIMEOUT}s"
      warn "active alerts right now:"; am_active_names | sed 's/^/    /'
    fi
  fi

  print_timeline
  printf '\nRevert with: %s./scripts/faultctl.sh heal%s\n\n' "$BOLD" "$RST"
}

cmd_heal() {
  preflight
  START_TS=$(date +%s)

  step "Restoring healthy app.py"
  local sha; sha="$(apply_variant healthy "fix: restore healthy demo-api via faultctl")"
  mark "pushed ${sha:0:7} to origin"

  step "Forcing ArgoCD reconciliation"
  argocd_refresh
  wait_for_sync "${sha}" && { printf '\n'; mark "ArgoCD Synced + Healthy"; }

  step "Waiting for pod rollout"
  kubectl -n "${NAMESPACE}" rollout status "deploy/${DEPLOYMENT}" \
    --timeout="${ROLLOUT_TIMEOUT}s" >/dev/null && mark "healthy pods running"

  step "Waiting for alerts to resolve"
  if am_port_forward; then
    local deadline=$(( $(date +%s) + ALERT_TIMEOUT ))
    while (( $(date +%s) < deadline )); do
      [[ -z "$(am_active_names)" ]] && { mark "all alerts resolved"; break; }
      printf '\r  %sstill active: %s%s' "$DIM" "$(am_active_names | tr '\n' ' ')" "$RST"
      sleep 5
    done
    printf '\n'
  fi

  print_timeline
}

cmd_status() {
  preflight
  printf '\n%sArgoCD%s\n' "$BOLD" "$RST"
  printf '  app=%s sync=%s health=%s rev=%.7s\n' \
    "${ARGOCD_APP}" "$(argocd_field .status.sync.status)" \
    "$(argocd_field .status.health.status)" "$(argocd_field .status.sync.revision)"

  printf '\n%sPods%s\n' "$BOLD" "$RST"
  kubectl -n "${NAMESPACE}" get pods -o wide | sed 's/^/  /'

  printf '\n%sActive alerts%s\n' "$BOLD" "$RST"
  if am_port_forward; then
    local names; names="$(am_active_names)"
    [[ -n "${names}" ]] && echo "${names}" | sed 's/^/  /' || printf '  (none)\n'
  fi
  printf '\n'
}

cmd_list() {
  printf '\n%sAvailable fault scenarios%s\n\n' "$BOLD" "$RST"
  for s in "${SCENARIOS[@]}"; do
    printf '  %s%-16s%s %s\n' "$BOLD" "$s" "$RST" "$(scenario_desc "$s")"
    printf '  %-16s %sexpects alert: %s%s\n\n' '' "$DIM" "$(scenario_alert "$s")" "$RST"
  done
  printf 'Run:  ./scripts/faultctl.sh inject <scenario>\n'
  printf 'Undo: ./scripts/faultctl.sh heal\n\n'
}

case "${1:-}" in
  inject) shift; cmd_inject "$@" ;;
  heal)   cmd_heal ;;
  status) cmd_status ;;
  list|"") cmd_list ;;
  *) die "unknown command: $1  (inject|heal|status|list)" ;;
esac
