# Deploy TRADE_BOT_V2 to the Google Cloud VM using your installed gcloud SDK.
# Run from PowerShell:
#   cd C:\Users\konda\Downloads\Projects\Trade_App\TRADE_BOT_V2
#   .\deploy_from_windows.ps1
#
# What it does:
#   1. Switches gcloud to project trade-bot-2025 (VM: trade-bot-vm, us-central1-a)
#   2. Copies this folder to the VM
#   3. Runs vm_migrate.sh remotely: installs V2, stops/removes old bot
#      services + watchdog cron, archives old code, starts trade-bot-v2

$ErrorActionPreference = "Stop"

# Confirmed from Google Cloud console (July 2026)
$PROJECT = "trade-bot-2025"
$name = "trade-bot-vm"
$zone = "us-central1-a"

Write-Host "=== Setting project $PROJECT ===" -ForegroundColor Cyan
gcloud config set project $PROJECT
Write-Host "VM: $name (zone: $zone)" -ForegroundColor Green

Write-Host "=== Copying TRADE_BOT_V2 to VM ===" -ForegroundColor Cyan
# pscp on Windows can't expand "~" — use the explicit home path of the SSH user
$VM_USER = "konda"
gcloud compute scp --recurse "$PSScriptRoot" "${name}:/home/$VM_USER/" --zone=$zone

Write-Host "=== Running migration on VM ===" -ForegroundColor Cyan
# strip Windows line endings if any snuck in during transfer, then run
gcloud compute ssh $name --zone=$zone --command="sed -i 's/\r$//' ~/TRADE_BOT_V2/vm_migrate.sh ~/TRADE_BOT_V2/.env.example ~/TRADE_BOT_V2/trade-bot-v2.service && chmod +x ~/TRADE_BOT_V2/vm_migrate.sh && ~/TRADE_BOT_V2/vm_migrate.sh"

Write-Host ""
Write-Host "=== DONE ===" -ForegroundColor Green
Write-Host "Watch logs:  gcloud compute ssh $name --zone=$zone --command='sudo journalctl -u trade-bot-v2 -f'"
