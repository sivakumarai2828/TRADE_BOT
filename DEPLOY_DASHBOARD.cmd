@echo off
rem Double-click to publish vercel_dashboard to https://trade-bot-v2-one.vercel.app
rem Output is saved to deploy_dashboard.log in this folder.
cd /d "%~dp0vercel_dashboard"
powershell -NoExit -ExecutionPolicy Bypass -Command "Start-Transcript -Path %~dp0deploy_dashboard.log -Force | Out-Null; if (Get-Command vercel -ErrorAction SilentlyContinue) { vercel --prod --yes 2>&1 | Out-Host } else { npx --yes vercel --prod --yes 2>&1 | Out-Host }; Write-Host === END OF DASHBOARD DEPLOY ===; Stop-Transcript | Out-Null"
