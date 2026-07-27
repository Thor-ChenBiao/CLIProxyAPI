#!/usr/bin/env bash
set -Eeuo pipefail

ACME_SH="${CLIPROXY_ACME_SH:-/root/.acme.sh/acme.sh}"
ACME_HOME="${CLIPROXY_ACME_HOME:-/.acme.sh}"
DEPLOY_SCRIPT="${CLIPROXY_CERT_DEPLOY_SCRIPT:-/usr/local/sbin/cliproxy-deploy-token-cert.sh}"
LOCK_FILE="${CLIPROXY_CERT_RENEW_LOCK_FILE:-/run/lock/cliproxy-cert-renew.lock}"

log() {
    printf '%s [cert-renew] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"
}

if [[ "${EUID}" -ne 0 ]]; then
    log "ERROR: this script must run as root" >&2
    exit 1
fi

[[ -x "$ACME_SH" ]] || { log "ERROR: acme.sh is missing: ${ACME_SH}" >&2; exit 1; }
[[ -x "$DEPLOY_SCRIPT" ]] || { log "ERROR: deploy script is missing: ${DEPLOY_SCRIPT}" >&2; exit 1; }

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    log "ERROR: another certificate renewal is running" >&2
    exit 1
fi

log "running acme.sh renewal check"
"$ACME_SH" --cron --home "$ACME_HOME"

# A renewal invokes this deployment through acme.sh's reload command. Running it
# again here is intentional: it also repairs node drift when no renewal was due.
log "checking cluster certificate convergence"
"$DEPLOY_SCRIPT"

log "renewal check completed"
