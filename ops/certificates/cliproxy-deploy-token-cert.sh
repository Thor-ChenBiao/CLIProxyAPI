#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

STAGE_DIR="${CLIPROXY_CERT_STAGE_DIR:-/var/lib/cliproxy-certificate}"
FULLCHAIN="${STAGE_DIR}/fullchain.pem"
KEY_FILE="${STAGE_DIR}/key.pem"
LOCAL_INSTALLER="${CLIPROXY_CERT_LOCAL_INSTALLER:-/usr/local/sbin/cliproxy-install-token-cert.sh}"
REMOTE_HOST="${CLIPROXY_CERT_REMOTE_HOST:-node-b}"
REMOTE_USER="${CLIPROXY_CERT_REMOTE_USER:-ec2-user}"
REMOTE_INSTALLER="${CLIPROXY_CERT_REMOTE_INSTALLER:-/usr/local/sbin/cliproxy-install-token-cert.sh}"
SSH_CONFIG="${CLIPROXY_CERT_SSH_CONFIG:-/home/ec2-user/.ssh/config}"
SSH_KEY="${CLIPROXY_CERT_SSH_KEY:-/home/ec2-user/.ssh/cluster-key}"
KNOWN_HOSTS="${CLIPROXY_CERT_KNOWN_HOSTS:-/home/ec2-user/.ssh/known_hosts}"
LOCK_FILE="${CLIPROXY_CERT_DEPLOY_LOCK_FILE:-/run/lock/cliproxy-cert-deploy.lock}"

log() {
    printf '%s [cert-deploy] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"
}

fail() {
    log "ERROR: $*" >&2
    exit 1
}

if [[ "${EUID}" -ne 0 ]]; then
    fail "this script must run as root"
fi

for value in "$REMOTE_HOST" "$REMOTE_USER"; do
    [[ "$value" =~ ^[A-Za-z0-9._@-]+$ ]] || fail "unsafe SSH value: ${value}"
done

for command_name in flock ssh scp install date; do
    command -v "$command_name" >/dev/null 2>&1 || fail "required command is missing: ${command_name}"
done

[[ -x "$LOCAL_INSTALLER" ]] || fail "local installer is missing: ${LOCAL_INSTALLER}"
[[ -s "$FULLCHAIN" ]] || fail "staged certificate is missing: ${FULLCHAIN}"
[[ -s "$KEY_FILE" ]] || fail "staged private key is missing: ${KEY_FILE}"
[[ -r "$SSH_CONFIG" ]] || fail "SSH config is not readable: ${SSH_CONFIG}"
[[ -r "$SSH_KEY" ]] || fail "SSH key is not readable: ${SSH_KEY}"
[[ -r "$KNOWN_HOSTS" ]] || fail "known_hosts is not readable: ${KNOWN_HOSTS}"

exec 9>"$LOCK_FILE"
flock -n 9 || fail "another certificate deployment is running"

ssh_options=(
    -F "$SSH_CONFIG"
    -i "$SSH_KEY"
    -o BatchMode=yes
    -o IdentitiesOnly=yes
    -o StrictHostKeyChecking=yes
    -o "UserKnownHostsFile=${KNOWN_HOSTS}"
    -o ConnectTimeout=15
)

remote_stage="/home/${REMOTE_USER}/.cache/cliproxy-cert-sync/$(date -u '+%Y%m%dT%H%M%SZ')-$$"
cleanup_remote() {
    ssh "${ssh_options[@]}" "$REMOTE_HOST" "rm -rf -- '${remote_stage}'" >/dev/null 2>&1 || true
}
trap cleanup_remote EXIT

log "copying staged certificate to ${REMOTE_HOST}"
remote_deploy_succeeded=1
if ! {
    ssh "${ssh_options[@]}" "$REMOTE_HOST" "install -d -m 0700 '${remote_stage}'" \
        && scp "${ssh_options[@]}" -p "$FULLCHAIN" "$KEY_FILE" "${REMOTE_HOST}:${remote_stage}/" \
        && log "deploying to ${REMOTE_HOST} first" \
        && ssh "${ssh_options[@]}" "$REMOTE_HOST" \
            "sudo -n '${REMOTE_INSTALLER}' '${remote_stage}/fullchain.pem' '${remote_stage}/key.pem'"
}; then
    remote_deploy_succeeded=0
    log "ERROR: deployment to ${REMOTE_HOST} failed; local deployment will continue" >&2
fi

log "deploying to local node"
"$LOCAL_INSTALLER" "$FULLCHAIN" "$KEY_FILE"

if [[ "$remote_deploy_succeeded" -ne 1 ]]; then
    fail "local certificate is current, but ${REMOTE_HOST} still needs repair"
fi

log "cluster certificate deployment completed"
