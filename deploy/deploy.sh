#!/usr/bin/env bash
# Push local changes + redeploy to the Azure VM in one shot.
#   ./deploy/deploy.sh "commit message"
#
# Required: $AZURE_HOST=azureuser@<ip> in your env or first arg = host.

set -euo pipefail

COMMIT_MSG="${1:-update}"
HOST="${AZURE_HOST:-${2:-}}"

if [[ -z "${HOST}" ]]; then
  echo "AZURE_HOST not set. Either:"
  echo "  export AZURE_HOST=azureuser@20.235.xxx.xxx"
  echo "or pass it as the 2nd arg:"
  echo "  ./deploy/deploy.sh 'msg' azureuser@20.235.xxx.xxx"
  exit 1
fi

echo "==> pushing to GitHub"
git add -A
if ! git diff --cached --quiet; then
  git commit -m "${COMMIT_MSG}"
fi
git push

echo "==> deploying to ${HOST}"
ssh "${HOST}" 'bash -s' <<'REMOTE'
set -euo pipefail
sudo -u alphagrid bash -c '
  cd /home/alphagrid/hedgefund
  git pull --ff-only
  source venv/bin/activate
  pip install -r requirements.txt --quiet
'
sudo systemctl restart alphagrid alphagrid-api
echo "==> services restarted"
sudo systemctl status --no-pager alphagrid alphagrid-api | tail -10
REMOTE

echo "==> deploy complete"
