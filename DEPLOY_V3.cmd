@echo off
rem Double-click to deploy TRADE_BOT_V3 to the Google Cloud VM.
rem Full output is also saved to deploy_v3.log in this folder.
cd /d "%~dp0"
powershell -NoExit -ExecutionPolicy Bypass -Command "Start-Transcript -Path '%~dp0deploy_v3.log' -Force | Out-Null; try { & '%~dp0deploy_v3_from_windows.ps1' } catch { Write-Host ('DEPLOY FAILED: ' + $_) -ForegroundColor Red } finally { Write-Host '=== END OF DEPLOY ==='; Stop-Transcript | Out-Null }"
