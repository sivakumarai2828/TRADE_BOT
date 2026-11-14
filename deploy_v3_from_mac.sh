#!/bin/bash
# Deploy TRADE_BOT_V3 to the Google Cloud VM from a Mac (replaces V2 in the
# same services). Mac twin of deploy_v3_from_windows.ps1.
#
#   ./deploy_v3_from_mac.sh            (or double-click DEPLOY_V3.command)
#
# 1. Pushes branch v3 to GitHub (backup + history)
# 2. Copies this folder to the VM as ~/TRADE_BOT_V3 (V2 folder is untouched)
# 3. Runs vm_deploy_v3.sh: copies V2's .env, adds V3 settings, checks
#    Telegram, switches the two services to V3, waits for a healthy
#    heartbeat, and rolls back to V2 automatically if it doesn't get one.
#
# Optional overrides (environment variables):
#   GCLOUD_ACCOUNT  gcloud account with access to the project (default: active one)
#   VM_USER         Linux user on the VM that owns ~/TRADE_BOT_V2 (default: konda)
#
# Your global gcloud config is NOT changed: project/account are passed per command.

set -euo pipefail

PROJECT="trade-bot-2025"
NAME="trade-bot-vm"
ZONE="us-central1-a"
VM_USER="${VM_USER:-konda}"
HERE="$(cd "$(dirname "$0")" && pwd)"

cyan()  { printf '\033[36m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
warn()  { printf '\033[33m%s\033[0m\n' "$*"; }
die()   { printf '\033[31m!! %s\033[0m\n' "$*"; exit 1; }

# gcloud may not be on PATH when launched by double-click
GCLOUD="$(command -v gcloud || true)"
for p in "$HOME/Downloads/google-cloud-sdk/bin/gcloud" "$HOME/google-cloud-sdk/bin/gcloud" \
         /usr/local/share/google-cloud-sdk/bin/gcloud /opt/homebrew/bin/gcloud; do
    [ -n "$GCLOUD" ] && break
    [ -x "$p" ] && GCLOUD="$p"
done
[ -n "$GCLOUD" ] || die "gcloud not found. Install the Google Cloud SDK first."

G=("$GCLOUD" --project="$PROJECT")
[ -n "${GCLOUD_ACCOUNT:-}" ] && G+=(--account="$GCLOUD_ACCOUNT")

cyan "=== [0/3] Checking access to $NAME in $PROJECT ==="
if ! status=$("${G[@]}" compute instances describe "$NAME" --zone="$ZONE" --format='value(status)' 2>&1); then
    echo "$status"
    die "Cannot reach $NAME with account '$("$GCLOUD" config get-value account 2>/dev/null)'.
   Log in with the account that owns $PROJECT, then re-run:
     gcloud auth login
     GCLOUD_ACCOUNT=<that account> ./deploy_v3_from_mac.sh"
fi
[ "$status" = "RUNNING" ] || die "$NAME is $status, not RUNNING."
echo "  $NAME is RUNNING"

cyan "=== [1/3] Pushing branch v3 to GitHub ==="
if [ -n "$(git -C "$HERE" status --porcelain --untracked-files=no)" ]; then
    warn "Uncommitted changes: they WILL be deployed, but are not in the GitHub push."
fi
git -C "$HERE" push -u origin v3

cyan "=== [2/3] Copying TRADE_BOT_V3 to the VM ==="
[ -f "$HERE/.env" ] && warn "Local .env found - it will NOT be copied; the VM builds its own from V2."
STAGE_ROOT="$(mktemp -d)"
trap 'rm -rf "$STAGE_ROOT"' EXIT
STAGE="$STAGE_ROOT/TRADE_BOT_V3"
rsync -a \
    --exclude .git --exclude __pycache__ --exclude .pytest_cache --exclude node_modules \
    --exclude .env --exclude .env.live --exclude '*.pyc' --exclude '*.db' --exclude .DS_Store \
    --exclude .claude --exclude .codex --exclude .vercel --exclude dist \
    --exclude 'deploy_*.log' --exclude 'TRADE_BOT_V1_archive_*.tar.gz' \
    "$HERE/" "$STAGE/"
"${G[@]}" compute scp --recurse --zone="$ZONE" "$STAGE" "${VM_USER}@${NAME}:/home/${VM_USER}/"

cyan "=== [3/3] Switching the VM to V3 ==="
"${G[@]}" compute ssh "${VM_USER}@${NAME}" --zone="$ZONE" \
    --command="sed -i 's/\r$//' ~/TRADE_BOT_V3/vm_deploy_v3.sh && chmod +x ~/TRADE_BOT_V3/vm_deploy_v3.sh && ~/TRADE_BOT_V3/vm_deploy_v3.sh"

echo ""
green "=== Open the control dashboard securely (SSH tunnel) ==="
echo "  gcloud compute ssh ${VM_USER}@${NAME} --project=$PROJECT --zone=$ZONE -- -N -L 8000:localhost:8000"
echo "  then browse to http://localhost:8000/control and paste the token printed above"
echo "Rollback: gcloud compute ssh ${VM_USER}@${NAME} --project=$PROJECT --zone=$ZONE --command='~/TRADE_BOT_V3/vm_deploy_v3.sh --rollback'"
