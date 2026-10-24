# Raspberry Pi deployment

Push to `main`, and the Pi updates itself. This is the same setup as the
pinball and folklore bots on the same Pi, and it shares their broker.

```
git push → GitHub Actions → tests → MQTT broker → Pi → tests → restart
```

**Nothing listens on the Pi.** It keeps one outbound TLS connection open to the
broker. There's no port forwarding, no dynamic DNS and no inbound SSH, so it
works behind CGNAT and keeps working if the Pi moves to another network.

## First-time setup

### On the Pi

```bash
git clone https://github.com/stellarhopper/avwx-bot.git ~/avwx-bot
bash ~/avwx-bot/deploy/setup-pi.sh
```

The script:

- installs packages and builds the venv
- asks for the Discord token and the MQTT login, and writes both with `chmod 600`
- installs and enables the two systemd units and a sudoers drop-in
- starts everything

It's safe to run again.

### On GitHub

**Secrets:** go to Settings → Secrets and variables → Actions and add these three:

| Secret | Value |
| --- | --- |
| `MQTT_BROKER` | e.g. `abc123.ala.us-east-1.emqxsl.com` |
| `MQTT_USERNAME` | the shared `github-deploy` login |
| `MQTT_PASSWORD` | its password |

Or from a terminal, where each command asks for its value:

```bash
gh secret set MQTT_BROKER   -R stellarhopper/avwx-bot
gh secret set MQTT_USERNAME -R stellarhopper/avwx-bot
gh secret set MQTT_PASSWORD -R stellarhopper/avwx-bot
```

**Pages:** go to Settings → Pages and set **Source** to **GitHub Actions**. This publishes the
configurator from `site/`.

### The broker

The broker is the shared EMQX Cloud Serverless deployment. Each bot keeps to its own
`<bot>/…` topics, and each machine has its own login.

| Login | Used by | Allowed |
| --- | --- | --- |
| `github-deploy` | GitHub Actions in every bot's repo | publish `+/deploy`, which already covers `avwx/deploy` |
| the Pi's avwx login | this deployer (`deploy/.env.mqtt`) | subscribe `avwx/deploy` |

The client ID is `avwx-pi-deployer`. It has to be unique on the shared broker.

## What a deployment does

When the trigger arrives, `deploy.sh` runs on the Pi:

1. Runs `git fetch` and `git reset --hard origin/main`. The Pi is a mirror of `main`, not
   somewhere anyone edits code.
2. If that commit changed `deploy.sh` itself, it **re-runs the new copy**. See
   below for why.
3. Runs `pip install -e ".[dev]"` into `.venv`.
4. **Runs the test suite.**
5. Installs any systemd unit in `deploy/` that differs from the installed copy,
   then runs `daemon-reload`.
6. Only then runs `sudo systemctl restart avwx-bot`. It waits five seconds and checks
   the process is still alive before it reports success.

**If step 3 or 4 fails, the checkout is rolled back to the commit that was
running and the bot is not restarted.** Servers rely on the morning brief and
alerts arriving, so the running process is only replaced by code that passes its tests.

### Why step 2 exists

`deploy.sh` lives inside the checkout that it replaces. Bash reads a script as
it runs, so without the re-run, every line after the `git reset` would come from
the *previous* commit. A change to this script would then take effect one
deployment late, and the log would still say it succeeded.

### Why the deployer is never restarted by a deployment

`deploy.sh` runs inside the deployer service's cgroup. Restarting that service
would kill the deployment halfway through. If a commit changes
`avwx-bot-deployer.service`, step 5 installs it and logs a note. The change
applies the next time the deployer restarts, either on reboot or by hand.

## Where things live

| Path | What |
| --- | --- |
| `~/avwx-bot` | the checkout, rewritten by every deployment |
| `~/avwx-bot/.venv` | the bot's virtualenv |
| `~/avwx-bot/.env` | the Discord token (chmod 600, gitignored) |
| `~/avwx-bot/deploy/.env.mqtt` | the broker login (chmod 600, gitignored) |
| `/var/lib/avwx-bot/avwx.db` | **every server's configuration**, and alert and brief history |
| `/var/log/avwx-bot/` | `output.log`, `error.log`, `deployer.log` and `deploy.log` |

The database is deliberately kept **outside** the checkout, because deployment
runs `git reset --hard` on the checkout.

## The two services

| Unit | What it does |
| --- | --- |
| `avwx-bot.service` | runs the bot (`python -m bot`) and restarts it if it fails |
| `avwx-bot-deployer.service` | holds the MQTT connection and runs `deploy.sh` when a trigger arrives |

The deployer runs on the **system** Python, using `python3-paho-mqtt` from apt.
It doesn't use the bot's venv, so a broken deployment can't take down the thing
that would fix it.

Its only privileges come from the sudoers drop-in at `/etc/sudoers.d/avwx-bot`:
restarting and checking `avwx-bot`, `daemon-reload`, and installing exactly the
two unit files. There are no wildcards.

## Day-to-day

```bash
# follow the bot
tail -f /var/log/avwx-bot/error.log

# follow deployments
tail -f /var/log/avwx-bot/deploy.log

# deploy by hand, without a push
bash ~/avwx-bot/deploy/deploy.sh

# restart / stop
sudo systemctl restart avwx-bot
sudo systemctl stop avwx-bot avwx-bot-deployer
```

To trigger a deployment from any machine with `mosquitto-clients`:

```bash
mosquitto_pub -h "$MQTT_BROKER" -p 8883 -t avwx/deploy -m manual \
  -u "$MQTT_USERNAME" -P "$MQTT_PASSWORD" --capath /etc/ssl/certs/
```

## Backups

The database is a single file in WAL mode. Copy it with SQLite rather than `cp`:

```bash
sqlite3 /var/lib/avwx-bot/avwx.db ".backup /home/$USER/avwx-backup.db"
```

Every server's configuration can also be exported from Discord with
`/avwx config export`.

## When it doesn't work

**The bot won't start.** Read `tail -n 50 /var/log/avwx-bot/error.log`. The
cause is almost always the token in `~/avwx-bot/.env`.

**The deployer won't connect.** Read `tail -n 50 /var/log/avwx-bot/deployer.log`.
If `deploy/.env.mqtt` is incomplete, the log names the missing variable. The port must
be 8883; on 1883 the password would cross the internet unencrypted.

**A push didn't deploy.** Check the Actions tab first. The `notify-pi` job only
runs if the tests pass, so a failing test suite stops the deployment before the
Pi is told about it. That's intended. A push that only changes `site/` or
Markdown files doesn't deploy to the Pi at all.

**It deployed, but the bot is running old code.** Look for `tests failed` in
`/var/log/avwx-bot/deploy.log`. The checkout was rolled back on purpose.
