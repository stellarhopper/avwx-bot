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
| `/metar [airports]` | Current observation cards, with the raw text |
| `/taf [airports]` | Forecasts with a flight-category timeline. Airports without a TAF are skipped |
| `/brief [airports]` | Dashboard: conditions, TAF strips, AIRMETs and SIGMETs, NWS warnings, winds aloft, PIREPs, forecast discussion |
| `/winds [where]` | Winds and temperatures aloft for an airport's nearest forecast site, or a site such as `DEN` |
| `/pirep [airport] [radius] [hours]` | Recent pilot reports, with moderate-or-worse turbulence, icing and mountain wave listed first |
| `/alerts` | Severe weather affecting the server's area right now |
| `/mountain` | Mountain flying: pass status, mountain-wave risk, ridge-level winds, density altitude. Off until an admin switches it on |
| `/avwx config edit` | Private link to the configurator, filled in with this server's settings |
| `/avwx config import file:` | Apply a `config.json` from the configurator. Shows the changes and waits for **Apply** |
| `/avwx config export` | Download the current settings |
| `/avwx config show` | Summary of what's switched on |
| `/avwx config rollback` | Go back to the previous settings |
| `/avwx enable` / `disable` | Switch one feature on or off |
| `/avwx post-brief` | Post the morning brief now, to check the channel. Today's scheduled brief still goes out |
| `/avwx chat-status` | Whether the club chat runs here, what it has cost today and this month, and which aircraft it knows |

**Which airports a command uses:** the ones you type. If you type none, it uses
the airports of the brief or alert thread you're in. Failing that, it uses the
server's home airports. Three-letter US identifiers get a K added, so `bjc`
becomes `KBJC`.

**Each image comes with text:** the raw METAR or TAF goes in the message so it
can be searched and copied, and the image has alt text. Up to 12 airports at a
time, four cards to an image.

## Mountain flying

`/mountain` and the brief's mountain section cover the passes in a region file
in [`data/regions/`](data/regions/). The Front Range file has 16 passes, from
Rollins and Berthoud to Wolf Creek and Raton. For each pass the file lists:

- the weather station on the pass, where there is one
- the airports on either side
- the forecast site for winds aloft

The board then shows:

- **Pass status** (looks OK / caution / no-go). It's based on the station on
  the pass, mountain-obscuration and IFR G-AIRMETs over the pass, the approach
  airports, and the forecast wind at the first level at or above the pass:
  15 kt is caution, 25 kt is no-go.
- **Mountain-wave risk** (low / elevated / high). It comes from flow across the
  ridge at 12,000–18,000 ft: from the 240–320° sector at 25 kt or more, and
  high from 40 kt. It's also raised by Tango G-AIRMETs for wave, by turbulence
  or wave PIREPs, and by wave or rotor in the forecast discussion.
- **Ridge-level winds** at each winds-aloft site, and **density altitude** at
  the mountain airports.

The thresholds are rules of thumb from mountain-flying courses, collected at
the top of [`bot/mountain.py`](bot/mountain.py). They're there for awareness,
not as a go/no-go decision. To add a region, add another file in
`data/regions/`; the configurator picks it up automatically.

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

## Club chat

A flying club can talk to the bot about flights. @mention it in a chat channel:

> @avwx-bot KBJC, Rollins pass, Granby, Kremmling, SBS, KBJC tomorrow at 8 in
> N648F, landing at Granby and Steamboat. Max gross. What should I know?

In a text channel the bot opens a thread on that message. In an existing thread,
such as the morning brief's forum post, it answers in place. After that it
answers every message in the thread without being mentioned, until the thread
has been quiet for a few hours (`chat.idle_hours`). A new @mention picks it up
again. A mention with nothing else in it ("@avwx-bot ^") picks up the question
asked just before it, while the bot wasn't listening.

Not every message in a thread is for the bot. Ones that don't mention it go
past a quick check by a small model first (Claude Haiku), so chatter and thanks
cost a fraction of a cent and get no reply. Only questions reach the main model.

The answers come from Claude, but **the numbers come from the bot's own code**.
Claude only works out what's being asked and calls the bot's tools:

- **Route:** turns identifiers, fixes, pass names and town names into points.
  "Granby" between two Colorado airports is Granby, CO. When a name could mean
  two places, the bot asks.
- **Plan:** a nav log (courses, wind correction, ground speed, times, fuel per
  flight with the club's reserve), weather at each airport at its arrival time,
  hazards and alerts along the route, notable PIREPs, and any pass crossed.
- **Weight and balance**, and **takeoff and landing distances** from the POH
  tables in the aircraft's profile. Past the edge of a table, the result is
  marked as a lower bound rather than extrapolated.
- **Manuals:** search, and read pages, including scanned ones.

Members still get a proper briefing, so replies are short. They lead with what's
outside normal parameters (IFR at arrival, a crosswind over the demonstrated
limit, a pass at caution, short fuel) and summarise the rest in a few lines.

It sticks to flying and the club. Member messages and tool results are treated
as information, never instructions. Every tool is read-only, with no web access
and no way to post anywhere, so injected text has nothing to reach.

**Setting it up:**

1. Put `ANTHROPIC_API_KEY` and `CHAT_GUILD_IDS` in `.env`. Only the listed
   servers can use the chat, since every answer costs API credit.
2. Turn on **Message Content Intent** under Bot in the Developer Portal. Under
   100 servers, it's just a toggle. While you're there, turn off **Public Bot**
   so only you can add it to servers.
3. In the configurator, switch **Chat with Claude** on and pick the channels.
   Daily caps per member and per server are there too.
4. Add the aircraft (below). `/avwx chat-status` shows what loaded.

### Aircraft profiles

Each aircraft is a directory under `CLUB_DIR` (on the Pi,
`/var/lib/avwx-bot/club`), outside the checkout because deployment resets it:

```
club/
  club.toml                    name, reserve_minutes, rules
  aircraft/N648F/
    aircraft.toml              the profile
    manuals/poh.pdf, ...       POH, supplements, avionics guides
```

`aircraft.toml` holds the empty weight and arm, weight limits, loading
stations, usable fuel, the CG envelope, V-speeds, cruise TAS and fuel flow by
altitude and power, the performance tables, and a list of manuals with a
section-to-page index. Copy every number from the aircraft's own documents and
have someone check them. A profile with a missing value is rejected, not
guessed at, and `/avwx chat-status` says why.
[`tests/fixtures/club/`](tests/fixtures/club/) has a complete example, with
made-up numbers. A performance table is a grid over whichever axes the POH
uses (`weight_lb`, `pressure_alt_ft`, `temp_c`, `headwind_kt`), plus
corrections such as "10% per 25 °F above standard". Changes are picked up
without a restart.

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
  - Create Public Threads, for the club chat in text channels

The club chat needs the **Message Content** privileged intent, because members
reply in a chat thread without @mentioning the bot. The bot asks for it only when
`ANTHROPIC_API_KEY` and `CHAT_GUILD_IDS` are set. Discord refuses to connect a
bot that asks for an intent that isn't switched on in the Developer Portal, so
switch it on there before setting those. The bot reads nothing outside an open
chat thread.

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
| `bot/route.py`, `bot/flightwx.py` | Route resolution, and the weather for a planned flight |
| `bot/fleet.py` | Loading the club's aircraft profiles |
| `bot/chat/` | The club chat: system prompt, tools, and the conversation loop |
| `schema/` | The config schema, shared by the bot and the site |
| `data/regions/` | Regional data such as mountain passes and winds-aloft sites. One JSON file per region |
| `data/airports_us.json` | Public-use US airports by name and town, for routes like "Granby". Rebuilt by `scripts/airports.py` |
| `site/` | The configurator. Plain HTML and JS, no build step |
| `deploy/` | Pi setup, systemd units and the MQTT deployer |
