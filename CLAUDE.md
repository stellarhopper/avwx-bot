# avwx-bot

A Discord bot for aviation weather, written in Python with discord.py. It runs on
a Raspberry Pi 5 next to the pinball and folklore bots, and deploys over MQTT
on every push to main (see deploy/README.md). README.md has the features and
the layout.

## Conventions

- Every query and every piece of stored state is scoped by `guild_id`. A
  server's configuration is stored as one JSON document (`guild_config`), and
  `bot/guildcfg.py:effective()` lays it over the schema's defaults.
- `schema/config.schema.json` is the only definition of the settings. Changing
  it means changing `site/app.js` (the form) too. `tests/test_site_codec.py`
  checks that the JS validator agrees with `jsonschema`.
- The link format (`encode_fragment` / `decode_fragment`) is shared with
  `site/codec.js`. Keep both in step; the Node-driven tests catch drift.
- Discord IDs in configs are strings, because they don't fit in a JavaScript number.
- All HTTP goes through `bot/sources/http.py:Http`, which caches by URL with a
  TTL per product. Never cache anything per guild there.
- Rendering is CPU-bound. Run renderers through `asyncio.to_thread` from
  command handlers.
- Detect channel kinds by `channel.type`, not `isinstance`. That way
  announcement channels count as text, and the test fakes in `tests/fakes.py`
  work.
- Tests run offline against `tests/fixtures/`, which are real API responses.
  When the code needs a new field, refresh a fixture from the live API rather
  than inventing one.
- Comments explain *why*, the same as in the sibling repos.

## Commands

```bash
.venv/bin/python -m pytest -q                       # full suite, under 2 s
.venv/bin/python -m bot.render.preview preview/     # render every card from fixtures
python scripts/build_site.py && python -m http.server -d _site 8000
```

## Secrets

`~/.emqx-logins.env` holds broker passwords. Never read it. The Discord token
is in `.env` (gitignored), and the Pi's MQTT login is in `deploy/.env.mqtt`
(gitignored).
