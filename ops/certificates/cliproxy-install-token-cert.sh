#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

DOMAIN="${CLIPROXY_CERT_DOMAIN:-token.zasdas.com}"
CERT_DIR="${CLIPROXY_CERT_DIR:-/etc/nginx/ssl/${DOMAIN}}"
VERIFY_HOST="${CLIPROXY_CERT_VERIFY_HOST:-127.0.0.1}"
VERIFY_PORT="${CLIPROXY_CERT_VERIFY_PORT:-8443}"
MIN_VALIDITY_SECONDS="${CLIPROXY_CERT_MIN_VALIDITY_SECONDS:-86400}"
BACKUP_KEEP="${CLIPROXY_CERT_BACKUP_KEEP:-5}"

log() {
    printf '%s [cert-install] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"
}

fail() {
    log "ERROR: $*" >&2
    exit 1
}

if [[ "${EUID}" -ne 0 ]]; then
    fail "this script must run as root"
fi

if [[ "$#" -ne 2 ]]; then
    fail "usage: $0 FULLCHAIN KEY"
fi

SOURCE_FULLCHAIN="$1"
SOURCE_KEY="$2"

for command_name in openssl nginx systemctl install cmp date timeout sha256sum awk cut find sort sleep; do
    command -v "$command_name" >/dev/null 2>&1 || fail "required command is missing: ${command_name}"
done

[[ -s "$SOURCE_FULLCHAIN" ]] || fail "certificate file is missing or empty: ${SOURCE_FULLCHAIN}"
[[ -s "$SOURCE_KEY" ]] || fail "private key file is missing or empty: ${SOURCE_KEY}"

openssl x509 -in "$SOURCE_FULLCHAIN" -noout >/dev/null 2>&1 || fail "invalid certificate PEM"
openssl pkey -in "$SOURCE_KEY" -noout >/dev/null 2>&1 || fail "invalid private key PEM"
openssl x509 -in "$SOURCE_FULLCHAIN" -noout -checkhost "$DOMAIN" >/dev/null 2>&1 \
    || fail "certificate does not cover ${DOMAIN}"
openssl x509 -in "$SOURCE_FULLCHAIN" -noout -checkend "$MIN_VALIDITY_SECONDS" >/dev/null 2>&1 \
    || fail "certificate expires within ${MIN_VALIDITY_SECONDS} seconds"

cert_public_key_hash="$({ openssl x509 -in "$SOURCE_FULLCHAIN" -pubkey -noout \
    | openssl pkey -pubin -outform DER; } 2>/dev/null | sha256sum | awk '{print $1}')"
key_public_key_hash="$(openssl pkey -in "$SOURCE_KEY" -pubout -outform DER 2>/dev/null \
    | sha256sum | awk '{print $1}')"
[[ "$cert_public_key_hash" == "$key_public_key_hash" ]] || fail "certificate and private key do not match"

source_fingerprint="$(openssl x509 -in "$SOURCE_FULLCHAIN" -noout -fingerprint -sha256 | cut -d= -f2)"
source_not_after="$(openssl x509 -in "$SOURCE_FULLCHAIN" -noout -enddate | cut -d= -f2-)"

install -d -o root -g root -m 0755 "$CERT_DIR"

if [[ -s "${CERT_DIR}/fullchain.pem" && -s "${CERT_DIR}/key.pem" ]] \
    && cmp -s "$SOURCE_FULLCHAIN" "${CERT_DIR}/fullchain.pem" \
    && cmp -s "$SOURCE_KEY" "${CERT_DIR}/key.pem"; then
    log "already current: fingerprint=${source_fingerprint}, not_after=${source_not_after}"
    exit 0
fi

timestamp="$(date -u '+%Y%m%dT%H%M%SZ')"
backup_dir="${CERT_DIR}/backup-${timestamp}"
install -d -o root -g root -m 0700 "$backup_dir"
had_previous=0
if [[ -s "${CERT_DIR}/fullchain.pem" && -s "${CERT_DIR}/key.pem" ]]; then
    install -o root -g root -m 0644 "${CERT_DIR}/fullchain.pem" "${backup_dir}/fullchain.pem"
    install -o root -g root -m 0600 "${CERT_DIR}/key.pem" "${backup_dir}/key.pem"
    had_previous=1
fi

rollback() {
    log "rolling back certificate deployment"
    if [[ "$had_previous" -eq 1 ]]; then
        install -o root -g root -m 0644 "${backup_dir}/fullchain.pem" "${CERT_DIR}/fullchain.pem"
        install -o root -g root -m 0600 "${backup_dir}/key.pem" "${CERT_DIR}/key.pem"
    else
        rm -f "${CERT_DIR}/fullchain.pem" "${CERT_DIR}/key.pem"
    fi
    nginx -t >/dev/null 2>&1 || true
    systemctl reload nginx >/dev/null 2>&1 || true
}

install -o root -g root -m 0644 "$SOURCE_FULLCHAIN" "${CERT_DIR}/.fullchain.pem.new"
install -o root -g root -m 0600 "$SOURCE_KEY" "${CERT_DIR}/.key.pem.new"
mv -f "${CERT_DIR}/.fullchain.pem.new" "${CERT_DIR}/fullchain.pem"
mv -f "${CERT_DIR}/.key.pem.new" "${CERT_DIR}/key.pem"

if ! nginx -t; then
    rollback
    fail "nginx configuration validation failed"
fi

if ! systemctl reload nginx; then
    rollback
    fail "nginx reload failed"
fi

served_fingerprint=""
for attempt in {1..10}; do
    served_fingerprint="$(
        timeout 15 openssl s_client -connect "${VERIFY_HOST}:${VERIFY_PORT}" -servername "$DOMAIN" </dev/null 2>/dev/null \
            | openssl x509 -noout -fingerprint -sha256 2>/dev/null \
            | cut -d= -f2 \
            || true
    )"
    [[ "$served_fingerprint" == "$source_fingerprint" ]] && break
    [[ "$attempt" -lt 10 ]] && sleep 1
done
if [[ -z "$served_fingerprint" || "$served_fingerprint" != "$source_fingerprint" ]]; then
    rollback
    fail "nginx did not serve the expected certificate on ${VERIFY_HOST}:${VERIFY_PORT}"
fi

mapfile -t old_backups < <(
    find "$CERT_DIR" -mindepth 1 -maxdepth 1 -type d -name 'backup-*' -printf '%T@ %p\n' \
        | sort -rn \
        | awk -v keep="$BACKUP_KEEP" 'NR > keep {sub(/^[^ ]+ /, ""); print}'
)
for old_backup in "${old_backups[@]:-}"; do
    [[ -n "$old_backup" ]] && rm -rf -- "$old_backup"
done

log "installed: fingerprint=${source_fingerprint}, not_after=${source_not_after}"
