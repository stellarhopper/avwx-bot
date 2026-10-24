#!/usr/bin/env bash
# One-time setup for a Raspberry Pi. Run it as your normal user, not root:
#
#   bash deploy/setup-pi.sh
#
# Safe to re-run: every step checks before it acts.

set -euo pipefail

RUN_USER="${SUDO_USER:-$USER}"
DEPLOY_DIR="${AVWX_DIR:-/home/$RUN_USER/avwx-bot}"
REPO_URL="${AVWX_REPO:-https://github.com/stellarhopper/avwx-bot.git}"
LOG_DIR=/var/log/avwx-bot
UNIT_DIR=/etc/systemd/system

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()  { printf '  ✓ %s\n' "$*"; }

if [ "$EUID" -eq 0 ]; then
    echo "Run this as your normal user — it will ask for sudo when it needs it."
    exit 1
fi

say "avwx-bot — Raspberry Pi setup"
echo "user:      $RUN_USER"
echo "directory: $DEPLOY_DIR"

# --- 1. system packages ----------------------------------------------------

say "[1/7] System packages"
sudo apt-get update -qq
# python3-venv for the bot's virtualenv, python3-paho-mqtt for the deployment
# listener (which deliberately runs outside that venv), git to fetch code.
# fonts-dejavu-core is a fallback only; the cards use the fonts in bot/fonts.
sudo apt-get install -y -qq python3 python3-venv python3-pip python3-paho-mqtt git fonts-dejavu-core
ok "installed"

# --- 2. log directory ------------------------------------------------------

say "[2/7] Log directory"
sudo mkdir -p "$LOG_DIR"
sudo chown "$RUN_USER:$RUN_USER" "$LOG_DIR"
ok "$LOG_DIR"

# --- 3. the code -----------------------------------------------------------

say "[3/7] Repository"
if [ ! -d "$DEPLOY_DIR/.git" ]; then
    git clone "$REPO_URL" "$DEPLOY_DIR"
    ok "cloned"
else
    git -C "$DEPLOY_DIR" pull --ff-only
    ok "up to date"
fi

# --- 4. virtualenv ---------------------------------------------------------

say "[4/7] Python environment"
if [ ! -x "$DEPLOY_DIR/.venv/bin/python" ]; then
    python3 -m venv "$DEPLOY_DIR/.venv"
fi
"$DEPLOY_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$DEPLOY_DIR/.venv/bin/pip" install --quiet -e "$DEPLOY_DIR[dev]"
ok "installed into $DEPLOY_DIR/.venv"

# --- 5. secrets ------------------------------------------------------------

say "[5/7] Credentials"

if [ ! -f "$DEPLOY_DIR/.env" ]; then
    read -rsp "  Discord bot token (paste, it won't echo): " token
    echo
    printf 'DISCORD_TOKEN=%s\n' "$token" > "$DEPLOY_DIR/.env"
    chmod 600 "$DEPLOY_DIR/.env"
    unset token
    ok "wrote .env"
else
    ok ".env already exists, leaving it alone"
fi

if [ ! -f "$DEPLOY_DIR/deploy/.env.mqtt" ]; then
    echo "  MQTT broker details (the Pi's login on the EMQX deployment):"
    read -rp  "    broker host: " mqtt_broker
    read -rp  "    username:    " mqtt_user
    read -rsp "    password:    " mqtt_pass
    echo
    cat > "$DEPLOY_DIR/deploy/.env.mqtt" <<EOF
MQTT_BROKER=$mqtt_broker
MQTT_PORT=8883
MQTT_USERNAME=$mqtt_user
MQTT_PASSWORD=$mqtt_pass
MQTT_TOPIC=avwx/deploy
EOF
    chmod 600 "$DEPLOY_DIR/deploy/.env.mqtt"
    unset mqtt_pass
    ok "wrote deploy/.env.mqtt"
else
    ok "deploy/.env.mqtt already exists, leaving it alone"
fi

chmod +x "$DEPLOY_DIR/deploy/deploy.sh" "$DEPLOY_DIR/deploy/mqtt_subscriber.py"

# --- 6. systemd ------------------------------------------------------------

say "[6/7] systemd units"

# The units are written for user `stellarhopper` in /home/stellarhopper/avwx-bot.
# Rewrite both if this Pi uses anything else, so the same files work anywhere.
install_unit() {
    local name="$1"
    sed -e "s|/home/stellarhopper/avwx-bot|$DEPLOY_DIR|g" \
        -e "s|^User=stellarhopper$|User=$RUN_USER|" \
        "$DEPLOY_DIR/deploy/$name" | sudo tee "$UNIT_DIR/$name" >/dev/null
    ok "$name"
}

install_unit avwx-bot.service
install_unit avwx-bot-deployer.service

sed -e "s|^stellarhopper |$RUN_USER |" -e "s|/home/stellarhopper/avwx-bot|$DEPLOY_DIR|g" \
    "$DEPLOY_DIR/deploy/avwx-bot-sudoers" | sudo tee /etc/sudoers.d/avwx-bot >/dev/null
sudo chmod 440 /etc/sudoers.d/avwx-bot
# A malformed sudoers file locks you out of sudo entirely, so check it and
# remove it if it doesn't parse rather than leaving it in place.
if ! sudo visudo -cf /etc/sudoers.d/avwx-bot >/dev/null; then
    sudo rm -f /etc/sudoers.d/avwx-bot
    echo "  ✗ sudoers file was invalid and has been removed — deployment will"
    echo "    not be able to restart the bot until this is fixed."
    exit 1
fi
ok "sudoers drop-in"

sudo systemctl daemon-reload
sudo systemctl enable avwx-bot avwx-bot-deployer >/dev/null
ok "enabled at boot"

# --- 7. go -----------------------------------------------------------------

say "[7/7] Starting"
sudo systemctl restart avwx-bot avwx-bot-deployer
sleep 3
sudo systemctl is-active --quiet avwx-bot \
    && ok "avwx-bot running" \
    || echo "  ✗ avwx-bot did not start — tail $LOG_DIR/error.log"
sudo systemctl is-active --quiet avwx-bot-deployer \
    && ok "avwx-bot-deployer running" \
    || echo "  ✗ avwx-bot-deployer did not start — tail $LOG_DIR/deployer.log"

say "Done"
cat <<EOF
Follow the bot:            tail -f $LOG_DIR/error.log
Follow deployments:        tail -f $LOG_DIR/deployer.log $LOG_DIR/deploy.log

Add these to the GitHub repo (Settings → Secrets and variables → Actions):
  MQTT_BROKER, MQTT_USERNAME, MQTT_PASSWORD

After that, every push to main deploys itself here.
EOF
