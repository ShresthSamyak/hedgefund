# AlphaGrid — Azure deployment

Single Ubuntu 22.04 VM. All services run on the same box: scheduler+agents,
FastAPI backend, PostgreSQL, Redis, Nginx. The Next.js frontend lives on
Vercel (free) and points at this VM's public IP.

```
Browser  →  alphagrid.vercel.app  (Next.js, free)
              ↓ REST + WebSocket
            Azure VM (Nginx :80 → uvicorn :8000)
                        ↓
                  PostgreSQL + Redis
                        ↑
              alphagrid.service (8 agents + 4-speed runtime)
```

## One-time bootstrap

```bash
# 1) Create the VM in Azure portal: Ubuntu 22.04, Standard_B2s, allow port 22 + 80.
# 2) From the VM:
sudo apt update && sudo apt install -y git
git clone https://github.com/ShresthSamyak/hedgefund.git /tmp/hedgefund
sudo REPO_URL=https://github.com/ShresthSamyak/hedgefund.git \
     bash /tmp/hedgefund/deploy/setup.sh

# 3) Fill in API keys
sudo -u alphagrid nano /home/alphagrid/hedgefund/.env

# 4) Restart so services pick up the new .env
sudo systemctl restart alphagrid alphagrid-api

# 5) Verify
curl http://localhost/health
sudo journalctl -u alphagrid -f
```

## Frontend (Vercel)

```bash
cd web
vercel              # one-time, links the repo
# In Vercel project settings, set env vars:
#   NEXT_PUBLIC_API_URL = http://<your-azure-ip>
#   NEXT_PUBLIC_WS_URL  = ws://<your-azure-ip>
# Every git push -> redeploy.
```

## Daily ops

```bash
# Logs
sudo journalctl -u alphagrid -f         # main scheduler
sudo journalctl -u alphagrid-api -f     # dashboard backend

# Timers
sudo systemctl list-timers              # confirm snapshot + weekly are scheduled
sudo systemctl status alphagrid-snapshot.timer
sudo systemctl status alphagrid-weekly.timer

# Manual healthcheck (always before going live)
sudo -u alphagrid bash -c '
  cd /home/alphagrid/hedgefund && source venv/bin/activate &&
  python -m tools.healthcheck'

# Read latest reports
ls /home/alphagrid/hedgefund/reports/
sudo cat /home/alphagrid/hedgefund/reports/snapshots.jsonl | tail -n1 | jq
```

## Push updates

```bash
# From your local machine (Windows PowerShell or git-bash)
export AZURE_HOST=azureuser@20.235.xxx.xxx
./deploy/deploy.sh "improved momentum filter"
```

## Cost containment

Azure Portal → Cost Management + Billing → Budgets → Create:

```
Amount:  USD 50/month
Alert at: 80%  → email
Alert at: 100% → email
```

`Standard_B2s` (2 vCPU, 4 GB) runs ~$30/month. Well inside typical Azure
credit grants. Don't auto-shutdown the VM — the scheduler must stay up.
