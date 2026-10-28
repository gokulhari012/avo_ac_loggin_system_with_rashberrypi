#!/usr/bin/env bash
# Install the temperature node on Raspberry Pi OS (Bookworm or newer, Python >= 3.10).
#
#   sudo bash scripts/setup_raspberry_pi.sh
#
# Layout created:
#   /opt/temperature-node/            application (copy of this repository's node + docs)
#   /opt/temperature-node/venv/       Python virtual environment
#   /etc/temperature-node/config.yaml local configuration (edit this)
#   /etc/temperature-node/node.env    TEMP_NODE_TOKEN=... (secret, mode 640)
#   /var/lib/temperature-node/        local SQLite buffer and remote config state
#   /var/log/temperature-node/        rotating log files
# Re-running the script upgrades the application and keeps configuration and data.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then echo "Run with sudo." >&2; exit 1; fi
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP=/opt/temperature-node
ETC=/etc/temperature-node
STATE=/var/lib/temperature-node
LOGS=/var/log/temperature-node
USER_NAME=tempnode

echo "== Temperature node installer =="
python3 -c 'import sys; assert sys.version_info >= (3, 10), "Python 3.10+ required"' \
  || { echo "Python 3.10 or newer is required (Raspberry Pi OS Bookworm or newer)." >&2; exit 1; }
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip rsync >/dev/null

id -u "$USER_NAME" >/dev/null 2>&1 || useradd --system --home "$STATE" --shell /usr/sbin/nologin "$USER_NAME"
usermod -a -G dialout "$USER_NAME"   # access to /dev/ttyUSB* and /dev/serial0

mkdir -p "$APP" "$ETC" "$STATE" "$LOGS"
rsync -a --delete --exclude '.venv' --exclude 'state' --exclude '__pycache__' --exclude 'config.yaml' \
  "$REPO/raspberry_pi_node/" "$APP/raspberry_pi_node/"
rsync -a "$REPO/docs/" "$APP/docs/"

[[ -d "$APP/venv" ]] || python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install --quiet --upgrade pip
"$APP/venv/bin/pip" install --quiet -r "$APP/raspberry_pi_node/requirements.txt"

if [[ ! -f "$ETC/config.yaml" ]]; then
  cp "$APP/raspberry_pi_node/config.example.yaml" "$ETC/config.yaml"
  echo "Created $ETC/config.yaml - EDIT node.id, server.url and the serial settings."
fi
if [[ ! -f "$ETC/node.env" ]]; then
  printf '# Node API token shown once by the server when the node was registered\nTEMP_NODE_TOKEN=\n' > "$ETC/node.env"
fi
chown -R root:"$USER_NAME" "$ETC"
chmod 750 "$ETC"; chmod 640 "$ETC/config.yaml" "$ETC/node.env"
chown -R "$USER_NAME":"$USER_NAME" "$STATE" "$LOGS"
chmod 750 "$STATE" "$LOGS"

install -m 644 "$APP/raspberry_pi_node/temperature-node.service" /etc/systemd/system/temperature-node.service
systemctl daemon-reload
systemctl enable temperature-node.service >/dev/null

# Make sure the clock gets synchronised (the Pi has no battery-backed clock).
systemctl enable --now systemd-timesyncd.service >/dev/null 2>&1 || true

echo
echo "Checking configuration..."
TOKEN_LINE="$(grep -E '^TEMP_NODE_TOKEN=' "$ETC/node.env" || true)"
if (cd "$APP/raspberry_pi_node" && sudo -u "$USER_NAME" env "${TOKEN_LINE:-TEMP_NODE_TOKEN=}" \
      "$APP/venv/bin/python" -m src.main --config "$ETC/config.yaml" --check-config); then
  systemctl restart temperature-node.service
  echo "Service started.  Follow it with:  journalctl -u temperature-node -f"
else
  echo "Configuration not valid yet. Edit $ETC/config.yaml and $ETC/node.env, then:"
  echo "  cd $APP/raspberry_pi_node && sudo -u $USER_NAME $APP/venv/bin/python -m src.main --config $ETC/config.yaml --check-config"
  echo "  sudo systemctl restart temperature-node"
fi
echo
echo "Hardware validation (read-only) before production:"
echo "  sudo systemctl stop temperature-node"
echo "  cd $APP/raspberry_pi_node && sudo -u $USER_NAME $APP/venv/bin/python -m src.validate_controller --port /dev/ttyUSB0 --slave 1"
