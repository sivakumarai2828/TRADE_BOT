#!/bin/bash
# Double-click (Finder) to publish vercel_dashboard to https://trade-bot-v2-one.vercel.app
# Output is saved to deploy_dashboard.log in this folder.
cd "$(dirname "$0")" || exit 1
LOG="$PWD/deploy_dashboard.log"
cd vercel_dashboard || exit 1
export PATH="/usr/local/bin:/opt/homebrew/bin:$PATH"   # npx/vercel when launched from Finder
{
    if command -v vercel >/dev/null; then vercel --prod --yes; else npx --yes vercel --prod --yes; fi
} 2>&1 | tee "$LOG"
[ "${PIPESTATUS[0]}" -eq 0 ] || printf '\033[31mDASHBOARD DEPLOY FAILED (see deploy_dashboard.log)\033[0m\n'
echo "=== END OF DASHBOARD DEPLOY ==="
read -r -p "Press Enter to close..." _
