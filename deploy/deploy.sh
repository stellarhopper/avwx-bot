#!/usr/bin/env bash
# Pull the latest main, install it, run the tests, and restart the bot.
#
# Invoked by the MQTT listener on every push to main, and safe to run by hand.
#
# The one rule: never restart the bot onto code that fails its tests. Servers
# rely on the morning brief and the severe-weather alerts turning up, so a
# failing test set rolls the checkout back to the commit that was running and
# leaves the process untouched.

set -euo pipefail

DEPLOY_DIR="${AVWX_DIR:-/home/stellarhopper/avwx-bot}"
REPO_URL="${AVWX_REPO:-https://github.com/stellarhopper/avwx-bot.git}"
BRANCH="${AVWX_BRANCH:-main}"
SERVICE="${AVWX_SERVICE:-avwx-bot}"
LOG_DIR="${AVWX_LOG_DIR:-/var/log/avwx-bot}"
LOG_FILE="$LOG_DIR/deploy.log"
VENV="$DEPLOY_DIR/.venv"

mkdir -p "$LOG_DIR" 2>/dev/null || true

log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG_FILE"
}

fail() {
    log "DEPLOY FAILED: $*"
    exit 1
}

# Fingerprint of this script as it was before the checkout moves under it.
script_hash() {
    sha256sum "$DEPLOY_DIR/deploy/deploy.sh" 2>/dev/null | cut -d' ' -f1
}
SELF_HASH="$(script_hash)"

log "=== deployment starting ==="

# --- fetch -----------------------------------------------------------------

if [ ! -d "$DEPLOY_DIR/.git" ]; then
    log "cloning $REPO_URL into $DEPLOY_DIR"
    git clone --branch "$BRANCH" "$REPO_URL" "$DEPLOY_DIR" || fail "clone failed"
    PREVIOUS=""
else
    cd "$DEPLOY_DIR"
    PREVIOUS="$(git rev-parse HEAD)"
    log "currently at ${PREVIOUS:0:12}"
    git fetch origin "$BRANCH" || fail "fetch failed (network? deploy key?)"
    # Hard reset rather than pull: the Pi is a mirror of main, not somewhere
    # anyone edits. .env is gitignored and the database lives in
    # /var/lib/avwx-bot, so both survive.
    git reset --hard "origin/$BRANCH" || fail "reset failed"
fi

cd "$DEPLOY_DIR"
CURRENT="$(git rev-parse HEAD)"
log "now at ${CURRENT:0:12} — $(git log -1 --pretty=%s)"

# --- run the *new* deploy script, not this one -----------------------------
#
# This script lives in the checkout it just replaced. Bash reads a script as it
# goes and buffers it, so everything below is whatever was on disk when this
# process started — the *previous* commit's version. A change to this file
# would therefore take effect one deploy late, and silently: the deploy reports
# success while having run the old steps. That is exactly how a corrected
# install line can look deployed and still do nothing.
#
# So if this commit changed the script, hand over to the version that came with
# it. AVWX_REEXEC stops that handover happening twice, and AVWX_PREVIOUS
# carries the commit that was actually running, so rollback still points at it
# after the re-exec has made HEAD look unchanged.
if [ -z "${AVWX_REEXEC:-}" ] && [ "$SELF_HASH" != "$(script_hash)" ]; then
    log "deploy.sh changed in this commit — re-running the new one"
    export AVWX_REEXEC=1 AVWX_PREVIOUS="$PREVIOUS"
    exec bash "$DEPLOY_DIR/deploy/deploy.sh" "$@"
fi

if [ -n "${AVWX_PREVIOUS:-}" ]; then
    PREVIOUS="$AVWX_PREVIOUS"   # the re-run's own fetch was a no-op
fi

if [ -n "$PREVIOUS" ] && [ "$PREVIOUS" = "$CURRENT" ]; then
    log "already up to date; reinstalling and restarting anyway"
fi

# --- roll back to the code that was running if anything below fails ---------

rollback() {
    if [ -n "$PREVIOUS" ] && [ "$PREVIOUS" != "$CURRENT" ]; then
        log "rolling the checkout back to ${PREVIOUS:0:12}"
        git -C "$DEPLOY_DIR" reset --hard "$PREVIOUS" >>"$LOG_FILE" 2>&1 || true
    fi
}

# --- install ---------------------------------------------------------------

if [ ! -x "$VENV/bin/python" ]; then
    log "creating venv at $VENV"
    python3 -m venv "$VENV" || fail "could not create the venv"
fi

log "installing dependencies"
if ! "$VENV/bin/pip" install --quiet --upgrade pip >>"$LOG_FILE" 2>&1; then
    log "warning: could not upgrade pip, continuing"
fi
if ! "$VENV/bin/pip" install --quiet -e ".[dev]" >>"$LOG_FILE" 2>&1; then
    rollback
    fail "dependency install failed (see $LOG_FILE)"
fi

# --- verify ----------------------------------------------------------------

log "running the test suite"
if ! "$VENV/bin/python" -m pytest -q >>"$LOG_FILE" 2>&1; then
    rollback
    fail "tests failed — the bot was NOT restarted and is still running ${PREVIOUS:0:12}"
fi
log "tests passed"

# --- systemd units ---------------------------------------------------------
#
# The units live in the repo, so a change to one ships like any other change.
# Install whichever differ from what systemd has, using exactly the commands
# the sudoers drop-in allows.
#
# The deployer's own unit is installed but never restarted from here: this
# script runs inside that service's cgroup, so restarting it would kill the
# deployment halfway through. Its change takes effect at its next restart.
#
# The files in git are written for /home/stellarhopper/avwx-bot. setup-pi.sh
# rewrites them for any other path; installing the git copies verbatim would
# undo that, so a checkout elsewhere skips the sync and says so.

units_changed=0
for unit in avwx-bot.service avwx-bot-deployer.service; do
    src="$DEPLOY_DIR/deploy/$unit"
    dst="/etc/systemd/system/$unit"
    [ -f "$src" ] || continue
    cmp -s "$src" "$dst" && continue
    if ! grep -q "^WorkingDirectory=$DEPLOY_DIR" "$src"; then
        log "warning: $unit changed, but it's written for another path — re-run deploy/setup-pi.sh to install it"
        continue
    fi
    log "installing changed unit $unit"
    sudo install -m 644 -o root -g root "$src" "$dst" || fail "could not install $unit"
    units_changed=1
    if [ "$unit" = "avwx-bot-deployer.service" ]; then
        log "note: the deployer's unit changed; it applies when avwx-bot-deployer next restarts"
    fi
done
if [ "$units_changed" -eq 1 ]; then
    sudo systemctl daemon-reload || fail "systemctl daemon-reload failed"
fi

# --- restart ---------------------------------------------------------------

log "restarting $SERVICE"
sudo systemctl restart "$SERVICE" || fail "systemctl restart failed"

# Give it a moment to either come up or crash on startup — a token error or a
# bad migration shows up within a couple of seconds, and reporting "deployed"
# for a process that died immediately is worse than reporting nothing.
sleep 5
if sudo systemctl is-active --quiet "$SERVICE"; then
    log "$SERVICE is running ${CURRENT:0:12}"
else
    log "$SERVICE is NOT running after restart — check: journalctl -u $SERVICE -n 50"
    exit 1
fi

log "=== deployment complete ==="
