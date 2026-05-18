#!/usr/bin/env bash
# Bootstrap a fresh Ubuntu 22.04 VM for AlphaGrid.
# Run once after `ssh azureuser@<ip>`.
#
#   curl -sSL https://raw.githubusercontent.com/<you>/hedgefund/main/deploy/setup.sh | bash
#
# Or if you've already cloned the repo:
#   sudo bash deploy/setup.sh
#
# Idempotent — safe to re-run.

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/ShresthSamyak/hedgefund.git}"
APP_USER="alphagrid"
APP_HOME="/home/${APP_USER}"
APP_DIR="${APP_HOME}/hedgefund"
PG_DB="alphagrid"
PG_USER="alphagrid"
PG_PASSWORD="${PG_PASSWORD:-$(openssl rand -hex 16)}"

log() { echo "==> $*" >&2; }

require_root() {
  if [[ $EUID -ne 0 ]]; then
    echo "must run as root: sudo bash $0" >&2
    exit 1
  fi
}

apt_install() {
  log "installing system packages"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y \
    python3.11 python3.11-venv python3-pip \
    redis-server \
    postgresql postgresql-contrib \
    nginx git curl jq build-essential pkg-config \
    libpq-dev
  systemctl enable --now redis-server postgresql nginx
}

create_app_user() {
  if id "${APP_USER}" &>/dev/null; then
    log "user ${APP_USER} already exists"
  else
    log "creating user ${APP_USER}"
    adduser --disabled-password --gecos "" "${APP_USER}"
  fi
}

setup_postgres() {
  log "ensuring postgres role + database"
  sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='${PG_USER}'" | grep -q 1 || \
    sudo -u postgres psql -c "CREATE USER ${PG_USER} WITH PASSWORD '${PG_PASSWORD}';"
  sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='${PG_DB}'" | grep -q 1 || \
    sudo -u postgres psql -c "CREATE DATABASE ${PG_DB} OWNER ${PG_USER};"
  sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE ${PG_DB} TO ${PG_USER};"
  log "postgres ready: postgresql://${PG_USER}:****@localhost/${PG_DB}"
}

clone_repo() {
  if [[ -d "${APP_DIR}/.git" ]]; then
    log "repo already cloned at ${APP_DIR}"
  else
    log "cloning ${REPO_URL}"
    sudo -u "${APP_USER}" git clone "${REPO_URL}" "${APP_DIR}"
  fi
}

setup_venv() {
  log "creating venv + installing requirements"
  sudo -u "${APP_USER}" bash -c "
    cd '${APP_DIR}' &&
    python3.11 -m venv venv &&
    ./venv/bin/pip install --upgrade pip &&
    ./venv/bin/pip install -r requirements.txt &&
    ./venv/bin/pip install psycopg2-binary
  "
}

write_env_template() {
  if [[ -f "${APP_DIR}/.env" ]]; then
    log ".env already present — skipping"
    return
  fi
  log "writing .env template (fill in API keys!)"
  cat > "${APP_DIR}/.env" <<EOF
PAPER_MODE=true
ALPHAGRID_DB_URL=postgresql://${PG_USER}:${PG_PASSWORD}@localhost/${PG_DB}
REDIS_URL=redis://127.0.0.1:6379/0
TIMEZONE=Asia/Kolkata

# Fill these in via 'sudo nano /home/${APP_USER}/hedgefund/.env'
ANGEL_API_KEY=
ANGEL_CLIENT_CODE=
ANGEL_PASSWORD=
ANGEL_TOTP_SECRET=
ANGEL_USE_SANDBOX=true

BINANCE_API_KEY=
BINANCE_API_SECRET=
BINANCE_TESTNET=true

TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
HUMAN_APPROVAL_REQUIRED=true

ANTHROPIC_API_KEY=
GOOGLE_API_KEY=
LLM_PROVIDER=anthropic
LLM_MODEL=claude-haiku-4-5-20251001
EOF
  chown "${APP_USER}:${APP_USER}" "${APP_DIR}/.env"
  chmod 600 "${APP_DIR}/.env"
}

install_systemd_units() {
  log "installing systemd units"
  cp "${APP_DIR}/deploy/systemd/"*.service /etc/systemd/system/
  cp "${APP_DIR}/deploy/systemd/"*.timer /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable alphagrid.service alphagrid-api.service \
    alphagrid-snapshot.timer alphagrid-weekly.timer
}

install_nginx() {
  log "installing nginx site"
  cp "${APP_DIR}/deploy/nginx/alphagrid" /etc/nginx/sites-available/alphagrid
  ln -sf /etc/nginx/sites-available/alphagrid /etc/nginx/sites-enabled/alphagrid
  rm -f /etc/nginx/sites-enabled/default
  nginx -t
  systemctl reload nginx
}

start_services() {
  log "starting services"
  systemctl restart alphagrid.service alphagrid-api.service
  systemctl start alphagrid-snapshot.timer alphagrid-weekly.timer
  systemctl status --no-pager alphagrid.service alphagrid-api.service || true
}

main() {
  require_root
  apt_install
  create_app_user
  setup_postgres
  clone_repo
  setup_venv
  write_env_template
  install_systemd_units
  install_nginx
  start_services

  echo
  echo "AlphaGrid bootstrap complete."
  echo "  PG password: ${PG_PASSWORD} (already in /home/${APP_USER}/hedgefund/.env)"
  echo "  Fill in API keys: sudo -u ${APP_USER} nano ${APP_DIR}/.env"
  echo "  Restart after editing: sudo systemctl restart alphagrid alphagrid-api"
  echo "  Logs: sudo journalctl -u alphagrid -f"
  echo "  Dashboard API: http://<this-vm-ip>/api/health"
}

main "$@"
