#!/usr/bin/env bash
# BuzzMarket one-time setup for a Raspberry Pi that runs its OWN Wi-Fi network.
#
#   cd ~/buzzword-polymarket && sudo ./deploy/install.sh
#
# What it does:
#   1. Installs qrencode (for join QR codes) while the Pi still has internet.
#   2. Creates your admin account if there isn't one yet.
#   3. Runs the app as a service on port 80 that starts on boot.
#   4. Sets up a nightly database backup (keeps 14 days).
#   5. Turns the Pi's Wi-Fi into a hotspot. Phones that join get the app
#      popped up automatically, and it lives at http://10.42.0.1
#
# Needs Raspberry Pi OS Bookworm or newer (uses NetworkManager).
# Step 5 disconnects the Pi from any Wi-Fi it's on, so run this from a
# keyboard/monitor or over an Ethernet SSH connection, not over Wi-Fi.
#
# Safe to re-run (e.g. to change the Wi-Fi name or password).
set -euo pipefail

HOTSPOT_IP="10.42.0.1"
CON_NAME="buzz-hotspot"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="${SUDO_USER:-}"

say()  { printf '\n\033[1;33m==> %s\033[0m\n' "$*"; }
die()  { printf '\n\033[1;31mError: %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run with sudo:  sudo ./deploy/install.sh"
[[ -n "$RUN_USER" && "$RUN_USER" != "root" ]] || die "Run with sudo from your normal user account (not as root directly)."
command -v python3 >/dev/null || die "python3 is missing."
command -v nmcli >/dev/null || die "NetworkManager (nmcli) not found. Use Raspberry Pi OS Bookworm (2023) or newer."
WIFI_IF="$(nmcli -t -f DEVICE,TYPE device | awk -F: '$2=="wifi"{print $1; exit}')"
[[ -n "$WIFI_IF" ]] || die "No Wi-Fi interface found."

# ------------------------------------------------------------------ questions
read -rp "Wi-Fi network name [BuzzMarket]: " SSID
SSID="${SSID:-BuzzMarket}"
while true; do
  read -rp "Wi-Fi password (8+ characters, leave blank for an open network): " WIFI_PASS
  [[ -z "$WIFI_PASS" || ${#WIFI_PASS} -ge 8 ]] && break
  echo "Must be at least 8 characters."
done
read -rp "Wi-Fi country code, e.g. US, GB, CA [US]: " COUNTRY
COUNTRY="${COUNTRY:-US}"

# ------------------------------------------------------- 1. packages (online)
say "Installing qrencode (for QR codes)"
if ! command -v qrencode >/dev/null; then
  apt-get install -y qrencode >/dev/null 2>&1 || (apt-get update >/dev/null 2>&1 && apt-get install -y qrencode >/dev/null 2>&1) \
    || echo "Couldn't install qrencode (no internet?). Skipping QR codes; everything else still works."
fi

# ------------------------------------------------------------ 2. admin account
cd "$APP_DIR"
if ! sudo -u "$RUN_USER" python3 - <<'PY'
import sys; from buzz import db
c = db.connect(); db.init(c)
sys.exit(0 if c.execute("SELECT 1 FROM users WHERE is_admin = 1").fetchone() else 1)
PY
then
  say "Create your admin account"
  sudo -u "$RUN_USER" python3 manage.py create-admin
else
  say "Admin account already exists, skipping"
fi

# ------------------------------------------------------------------ 3. service
say "Installing the buzzmarket service (port 80, starts on boot)"
cat > /etc/systemd/system/buzzmarket.service <<EOF
[Unit]
Description=BuzzMarket buzzword betting
After=network.target

[Service]
User=$RUN_USER
WorkingDirectory=$APP_DIR
Environment=BUZZ_PORT=80
Environment=BUZZ_QUIET=1
Environment=BUZZ_CAPTIVE_HOST=$HOTSPOT_IP
AmbientCapabilities=CAP_NET_BIND_SERVICE
ExecStart=/usr/bin/python3 manage.py serve
Restart=always
RestartSec=2
MemoryMax=150M

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable buzzmarket >/dev/null
systemctl restart buzzmarket

# ------------------------------------------------------------------ 4. backups
say "Nightly backup at 3:15am into $APP_DIR/backups (keeps 14 days)"
sudo -u "$RUN_USER" mkdir -p "$APP_DIR/backups"
cat > /etc/cron.d/buzzmarket <<EOF
15 3 * * * $RUN_USER cd $APP_DIR && python3 manage.py backup backups/buzz-\$(date +\\%F).db >/dev/null && find backups -name 'buzz-*.db' -mtime +14 -delete
EOF

# ---------------------------------------------------------------- QR codes
qr_escape() { printf '%s' "$1" | sed -e 's/[\\;,:"]/\\&/g'; }
if command -v qrencode >/dev/null; then
  if [[ -n "$WIFI_PASS" ]]; then
    WIFI_QR="WIFI:T:WPA;S:$(qr_escape "$SSID");P:$(qr_escape "$WIFI_PASS");;"
  else
    WIFI_QR="WIFI:T:nopass;S:$(qr_escape "$SSID");;"
  fi
  qrencode -t SVG -m 1 -o "$APP_DIR/static/qr-wifi.svg" "$WIFI_QR"
  qrencode -t SVG -m 1 -o "$APP_DIR/static/qr-site.svg" "http://$HOTSPOT_IP/"
  chown "$RUN_USER" "$APP_DIR"/static/qr-*.svg
fi

# ----------------------------------------------------------------- 5. hotspot
say "Turning $WIFI_IF into the '$SSID' hotspot"
command -v raspi-config >/dev/null && raspi-config nonint do_wifi_country "$COUNTRY" || true
command -v rfkill >/dev/null && rfkill unblock wifi || true

# Answer every DNS lookup with the Pi, so phones' connectivity checks land on
# the app and they show the "sign in to network" pop-up.
mkdir -p /etc/NetworkManager/dnsmasq-shared.d
echo "address=/#/$HOTSPOT_IP" > /etc/NetworkManager/dnsmasq-shared.d/buzzmarket.conf

nmcli connection delete "$CON_NAME" >/dev/null 2>&1 || true
nmcli connection add type wifi ifname "$WIFI_IF" con-name "$CON_NAME" autoconnect yes ssid "$SSID" \
  connection.autoconnect-priority 100 \
  802-11-wireless.mode ap 802-11-wireless.band bg 802-11-wireless.channel 6 \
  ipv4.method shared ipv4.addresses "$HOTSPOT_IP/24" ipv6.method disabled >/dev/null
if [[ -n "$WIFI_PASS" ]]; then
  nmcli connection modify "$CON_NAME" wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$WIFI_PASS" \
    wifi-sec.proto rsn wifi-sec.pairwise ccmp wifi-sec.group ccmp
fi
nmcli connection up "$CON_NAME" >/dev/null

sleep 2
if curl -fsS -o /dev/null "http://$HOTSPOT_IP/join"; then OK="yes"; else OK="no"; fi

say "Done!"
cat <<EOF
  Wi-Fi network : $SSID
  Wi-Fi password: ${WIFI_PASS:-(open network)}
  App address   : http://$HOTSPOT_IP
  Join poster   : http://$HOTSPOT_IP/join   (put this on screen in the meeting)
  App responding: $OK

Players join the Wi-Fi; the app should pop up on its own. If it doesn't,
they open http://$HOTSPOT_IP in their browser.

Handy commands:
  sudo systemctl status buzzmarket          # is the app running?
  journalctl -u buzzmarket -f               # app logs
  sudo nmcli connection down $CON_NAME      # turn the hotspot off (back to normal Wi-Fi)
  sudo nmcli connection up $CON_NAME        # turn it back on
  ./deploy/update.sh                        # update after 'git pull' (needs Ethernet for internet)
EOF
if command -v qrencode >/dev/null; then
  echo; echo "Wi-Fi QR code:"; qrencode -t ANSIUTF8 -m 1 "$WIFI_QR"
fi
