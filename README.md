# avwx-bot

Aviation weather for Discord servers. Pilots can look things up with slash
commands, and the bot can post automatically: a morning brief, and short-notice
alerts when severe weather is about to arrive. Mountain-flying information
(pass conditions, ridge-level winds, wave risk, density altitude) is built in
for the Colorado Front Range.

All data comes from free government sources, [aviationweather.gov] and
[api.weather.gov], so there are no API keys. METARs and TAFs work worldwide.
Hazards, alerts and the mountain features cover the US.

> Not an official weather briefing. Use it for situational awareness, and get a
> proper briefing before you fly.

[aviationweather.gov]: https://aviationweather.gov/data/api/
[api.weather.gov]: https://www.weather.gov/documentation/services-web-api

## Commands

| Command | What it does |
| --- | --- |
| `/metar [airports]` | Current observation card for up to 4 airports, with the raw text |
| `/taf [airports]` | Forecast with a flight-category timeline |
| `/brief [airports]` | Dashboard: conditions, TAF strips, AIRMETs and SIGMETs, NWS warnings, winds aloft, PIREPs, forecast discussion |
| `/winds [where]` | Winds and temperatures aloft for an airport's nearest forecast site, or a site such as `DEN` |
| `/pirep [airport] [radius] [hours]` | Recent pilot reports, with moderate-or-worse turbulence, icing and mountain wave listed first |
| `/alerts` | Severe weather affecting the server's area right now |
| `/avwx config edit` | Private link to the configurator, filled in with this server's settings |
| `/avwx config import file:` | Apply a `config.json` from the configurator. Shows the changes and waits for **Apply** |
| `/avwx config export` | Download the current settings |
| `/avwx config show` | Summary of what's switched on |
| `/avwx config rollback` | Go back to the previous settings |
| `/avwx enable` / `disable` | Switch one feature on or off |
| `/avwx post-brief` | Post the morning brief now, to check the channel. Today's scheduled brief still goes out |

**Which airports a command uses:** the ones you type. If you type none, it uses
the airports of the brief or alert thread you're in. Failing that, it uses the
server's home airports. Three-letter US identifiers get a K added, so `bjc`
becomes `KBJC`.

**Each image comes with text:** the raw METAR or TAF goes in the message so it
can be searched and copied, and the image has alt text.

## Configuring a server

Settings are made in a static page, the configurator, at
<https://stellarhopper.github.io/avwx-bot/>. There's no web server behind it.

1. Run `/avwx config edit`. You need **Manage Server**, or a role the config
   lists as an admin role. The reply only you can see has a link to the page.
   Your server's current settings and its channel and role names are packed
   into the part of the link after `#`, which browsers never send to the server.
2. Change what you like and press **Download config.json**.
3. Run `/avwx config import` and attach the file. The bot validates it against
   [`schema/config.schema.json`](schema/config.schema.json), the same schema the
   page uses. It also checks that the channels exist and that it can post there,
   and that the airports are real. Then it lists exactly what will change.
   Nothing happens until you press **Apply**.

The brief and alerts can each post to a **text channel**, where they appear as
ordinary messages, or to a **forum**, where each one becomes its own post.

## Automatic posts

**Morning brief.** The brief is posted at a set local time on the days you
choose. It's skipped if the bot was down for more than an hour past that time,
and it never posts twice in one day. Each section is fetched separately, so a
source that's down costs only its own section, and the image says which
section is missing. In a forum, the raw METARs, TAFs and the full forecast
discussion follow inside the post.

**Severe-weather alerts.** The bot checks every 3 minutes and posts when any of
these happens within the configured radius:

- an NWS warning of a kind you've chosen is issued (tornado, severe
  thunderstorm, high wind and so on)
- a convective SIGMET or a Center Weather Advisory touches the area
- a METAR at one of the area's airports reports a thunderstorm, a funnel
  cloud, or gusts above your threshold

What happens after the first post:

- **Updates edit the original post** instead of making a new one. NWS warnings
  are matched by their VTEC event number, which stays the same as the NWS
  updates a warning.
- In a forum, each update is also added as a reply in the thread.
- When the hazard ends, the post is marked as ended. In a forum, the thread is
  also locked if the bot has Manage Threads.

Settings that control the alerts:

- **Role ping:** an optional role is mentioned in each new alert.
- **Quiet hours:** turn the role ping off overnight. Alerts are still posted.
- **Cooldown:** limits how often METAR, SIGMET and CWA alerts can make a new
  post. NWS warnings are never held back by it.

To keep the commands to a dedicated bot channel, go to **Server Settings →
Integrations → avwx-bot** and set channel permissions there. That's Discord's own
control, so it applies before the bot ever sees the command.

## Adding the bot to a server

Create an application at <https://discord.com/developers/applications>, add a
bot, and invite it with:

- **Scopes:** `bot` and `applications.commands`
- **Permissions:** View Channel, Send Messages, Send Messages in Threads,
  Embed Links, Attach Files, Read Message History
- **Optional permissions:**
  - Mention @everyone, @here and All Roles, if alerts should ping a role that
    isn't otherwise mentionable
  - Manage Threads, to lock expired alert posts in a forum

No privileged intents are needed.

## Running it

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env        # add DISCORD_TOKEN, and DEV_GUILD_ID for a test server
.venv/bin/python -m bot
```

The tests run offline against saved API responses in `tests/fixtures/`:

```bash
.venv/bin/python -m pytest -q
```

Some tests also run the configurator's JavaScript under Node, to check that the
page and the bot agree on the link format and on what counts as a valid
config. Those tests are skipped if Node isn't installed.

To work on the configurator locally:

```bash
python scripts/build_site.py && python -m http.server -d _site 8000
# and set CONFIG_SITE_URL=http://localhost:8000/ in .env
```

### Deployment

The bot runs on a Raspberry Pi. Every push to `main` deploys itself over MQTT,
the same way as this author's other bots. The tests run on the Pi before every
restart, and a failure rolls back. See [`deploy/README.md`](deploy/README.md).

## Layout

| Path | What's there |
| --- | --- |
| `bot/sources/` | API clients: `awc.py` (aviationweather.gov), `nws.py` (api.weather.gov), the winds-aloft parser, and a shared HTTP cache |
| `bot/wx/` | Flight category, density altitude, geometry and formatting. No network |
| `bot/render/` | Pillow cards. Each returns PNG bytes |
| `bot/cogs/` | The slash commands and background loops |
| `bot/guildcfg.py` | Config validation, defaults, diffs and the link format |
| `schema/` | The config schema, shared by the bot and the site |
| `data/regions/` | Regional data such as mountain passes and winds-aloft sites. One JSON file per region |
| `site/` | The configurator. Plain HTML and JS, no build step |
| `deploy/` | Pi setup, systemd units and the MQTT deployer |
