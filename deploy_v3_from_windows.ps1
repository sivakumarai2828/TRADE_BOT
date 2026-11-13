# Deploy TRADE_BOT_V3 to the Google Cloud VM (replaces V2 in the same services).
# Run from PowerShell:
#   cd C:\Users\konda\Downloads\Projects\Trade_App\TRADE_BOT_V3
#   .\deploy_v3_from_windows.ps1
#
# 1. Pushes branch v3 to GitHub (backup + history)
# 2. Copies this folder to the VM as ~/TRADE_BOT_V3 (V2 folder is untouched)
# 3. Runs vm_deploy_v3.sh: copies V2's .env, adds V3 settings, checks
#    Telegram, switches the two services to V3, waits for a healthy
#    heartbeat, and rolls back to V2 automatically if it doesn't get one.

$ErrorActionPreference = "Stop"
$PROJECT = "trade-bot-2025"
$name = "trade-bot-vm"
$zone = "us-central1-a"
$VM_USER = "konda"

Write-Host "=== [1/3] Pushing branch v3 to GitHub ===" -ForegroundColor Cyan
git -C "$PSScriptRoot" push -u origin v3

Write-Host "=== [2/3] Copying TRADE_BOT_V3 to the VM ===" -ForegroundColor Cyan
gcloud config set project $PROJECT
if (Test-Path "$PSScriptRoot\.env") {
    Write-Host "Local .env found - it will NOT be copied; the VM builds its own from V2." -ForegroundColor Yellow
}
$stage = Join-Path $env:TEMP "TRADE_BOT_V3"
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
robocopy "$PSScriptRoot" $stage /E /XD .git __pycache__ .pytest_cache node_modules /XF .env *.pyc | Out-Null
gcloud compute scp --recurse $stage "${name}:/home/$VM_USER/" --zone=$zone
Remove-Item $stage -Recurse -Force

Write-Host "=== [3/3] Switching the VM to V3 ===" -ForegroundColor Cyan
gcloud compute ssh $name --zone=$zone --command="sed -i 's/\r$//' ~/TRADE_BOT_V3/vm_deploy_v3.sh && chmod +x ~/TRADE_BOT_V3/vm_deploy_v3.sh && ~/TRADE_BOT_V3/vm_deploy_v3.sh"

Write-Host ""
Write-Host "=== Open the control dashboard securely (SSH tunnel) ===" -ForegroundColor Green
Write-Host "  gcloud compute ssh $name --zone=$zone -- -N -L 8000:localhost:8000"
Write-Host "  then browse to http://localhost:8000/control and paste the token printed above"
Write-Host "Rollback: gcloud compute ssh $name --zone=$zone --command='~/TRADE_BOT_V3/vm_deploy_v3.sh --rollback'"
