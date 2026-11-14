#!/bin/bash
# Double-click (Finder) to deploy TRADE_BOT_V3 to the Google Cloud VM.
# Full output is also saved to deploy_v3.log in this folder.
cd "$(dirname "$0")" || exit 1
./deploy_v3_from_mac.sh 2>&1 | tee deploy_v3.log
[ "${PIPESTATUS[0]}" -eq 0 ] || printf '\033[31mDEPLOY FAILED (see deploy_v3.log)\033[0m\n'
echo "=== END OF DEPLOY ==="
read -r -p "Press Enter to close..." _
