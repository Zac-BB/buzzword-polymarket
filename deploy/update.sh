#!/usr/bin/env bash
# Pull the latest code and restart the app. The Pi needs internet for the
# pull: plug in Ethernet, since its Wi-Fi is busy being the hotspot.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
python3 manage.py backup "backups/buzz-before-update-$(date +%F-%H%M).db"
git pull --ff-only
sudo systemctl restart buzzmarket
sleep 1
systemctl --no-pager --lines=0 status buzzmarket
