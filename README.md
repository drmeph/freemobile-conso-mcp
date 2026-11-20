# freemobile-conso-mcp

<!-- mcp-name: io.github.drmeph/freemobile-conso-mcp -->

An [MCP](https://modelcontextprotocol.io) server that lets AI agents (Claude Desktop, Claude Code, any MCP client) read your **Free Mobile** account usage: data, calls, SMS and MMS in France and while roaming, out-of-plan charges, carbon footprint and billing cycle.

It's built for people living abroad on a Free plan with a roaming allowance (for example 35 GB abroad / 200+ GB in France) who want to ask *"how much roaming data do I have left?"* without logging into mobile.free.fr.

> Unofficial. Not affiliated with Free / Iliad. It reads your own subscriber area (mobile.free.fr/account/v2) with your own credentials. Earlier versions used the [FreeMobileConso](https://github.com/CorentinMre/FreeMobileConso) library (MIT, by CorentinMre), which only supports the dashboard Free retired in 2026.

## Tools

All tools are read-only.

| Tool | What it returns |
|---|---|
| `get_local_usage` | Usage on the French network: data (used / allowance / remaining GB, % used), calls, SMS, MMS. Optional `metric` filter: `data`, `calls`, `sms`, `mms`, or `all` (the default). |
| `get_roaming_usage` | The same breakdown for usage abroad, counted against the roaming allowance. |
| `get_overage` | Out-of-plan charges ("hors forfait") in EUR: the total, plus a breakdown by local/roaming and by metric. |
| `get_carbon_footprint` | The carbon footprint of mobile data that the dashboard shows, in g CO2e. |
| `get_plan_info` | Plan name (best effort), account holder, line number, billing period and next renewal date. |
| `get_usage_summary` | A single call covering France and roaming data, total overage and renewal date. |
| `health_check` | Logs in fresh and checks that the dashboard still parses. See [Health check](#health-check). |

Every number comes with the raw string from the site (`"used": "12,5 Go"`, `"used_gb": 12.5`), so you can always check what the server parsed. Results are cached for 5 minutes, so a burst of tool calls fetches the pages once. Pass `refresh: true` to fetch them again. The login session is kept between runs, so most fetches don't log in at all.

Abroad, Free reports call time by direction rather than destination, so `get_roaming_usage` adds `outgoing`/`incoming` (and `_seconds`) to `calls`; `used` is their sum.

Example output from `get_roaming_usage` with `metric: "data"`:

```json
{
  "scope": "roaming",
  "data": {
    "used": "12,5 Go", "used_gb": 12.5,
    "allowance": "35 Go", "allowance_gb": 35.0,
    "remaining": "22,5 Go", "remaining_gb": 22.5,
    "percent_used": 35.7,
    "overage": "0,00€", "overage_eur": 0.0
  },
  "billing_period": {
    "raw": "Période du 05/11 au 05/12. Votre forfait sera remis à zéro le 5 décembre 2026",
    "start": "2026-11-05", "end": "2026-12-05", "next_renewal": "2026-12-05"
  },
  "fetched_seconds_ago": 0
}
```

## Install

You need Python 3.10 or later. The simplest setup uses [uv](https://docs.astral.sh/uv/), which installs and runs the server with `uvx`:

```bash
uvx freemobile-conso-mcp --check   # one-off health check, prints JSON
```

Or install it with pip:

```bash
pip install freemobile-conso-mcp
```

### Credentials

| Variable | Required | Meaning |
|---|---|---|
| `FREEMOBILE_USERNAME` | yes | Your Free Mobile *identifiant*: the 8-digit number you use at mobile.free.fr/account |
| `FREEMOBILE_PASSWORD` | yes | Your account password |
| `FREEMOBILE_CACHE_TTL` | no | How many seconds to cache a scrape (default `300`) |
| `FREEMOBILE_STATE_FILE` | no | Where the login session and Free's trusted-device cookie are kept (default `~/.local/state/freemobile-conso-mcp/session.json`, created with mode 0600). **Anyone who can read this file can open your subscriber area until the session expires.** |
| `FREEMOBILE_DUMP_HTML` | no | A file path. When parsing fails, the raw page HTML is written there so the parser can be fixed. **The file contains your personal data.** |

Credentials are only sent to `mobile.free.fr` and are never logged.

### One-time security code (2FA)

Free asks for a code sent by SMS (or e-mail) when you log in from a new device. Run this once in a terminal, with the same environment variables as the server:

```bash
FREEMOBILE_USERNAME=12345678 FREEMOBILE_PASSWORD='your-password' uvx freemobile-conso-mcp --login
```

Type the code when asked. The device is then remembered ("Se souvenir de cet appareil", about six months), and the server logs in without a code. If Free asks again later, the tools fail with `[otp_required]`. The server won't log in again by itself until you've run `--login`, so it never sends you a stream of codes. A code is reused if you rerun `--login` within 10 minutes; after that, Free sends a new one.

## Claude Desktop setup

Open the config file:

- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`

Add the server:

```json
{
  "mcpServers": {
    "freemobile": {
      "command": "uvx",
      "args": ["freemobile-conso-mcp"],
      "env": {
        "FREEMOBILE_USERNAME": "12345678",
        "FREEMOBILE_PASSWORD": "your-password"
      }
    }
  }
}
```

If you installed with pip instead of uv, set `"command": "freemobile-conso-mcp"` and `"args": []`. If Claude Desktop can't find the command, use its absolute path (`which freemobile-conso-mcp`).

Restart Claude Desktop, then ask something like *"How much roaming data do I have left on Free this month, and when does it reset?"*

### Claude Code

```bash
claude mcp add freemobile \
  -e FREEMOBILE_USERNAME=12345678 -e FREEMOBILE_PASSWORD='your-password' \
  -- uvx freemobile-conso-mcp
```

## Health check

The server scrapes Free's web pages, so a redesign can break it. It's built to tell you when that happens instead of returning wrong numbers:

- `health_check` (the tool) and `freemobile-conso-mcp --check` (the CLI) fetch the pages afresh, parse them and inspect a set of canary fields: account name, billing period, and local/roaming data used and allowance.
- The `status` field is one of:
  - `ok`
  - `degraded`: parsed, but some canary fields are empty or no longer look like `12.3Go`
  - `layout_changed`: logged in, but the usage data isn't where the parser expects it
  - `auth_failed`: Free rejected the identifiant/password or the security code
  - `otp_required`: Free wants a one-time code; run `freemobile-conso-mcp --login`
  - `network_error`
  - `not_configured`
- The usage tools add a `warnings` field when the fields they rely on come back empty.
- `--check` exits with `0` when the status is ok and `1` otherwise, so you can run it from cron or an uptime monitor:

```bash
# every morning at 08:00: alert if the scraper is broken
0 8 * * * FREEMOBILE_USERNAME=... FREEMOBILE_PASSWORD=... freemobile-conso-mcp --check >/dev/null || notify-send "Free Mobile MCP broken"
```

**`otp_required`** isn't a breakage: Free wants a one-time code for this login. See [One-time security code](#one-time-security-code-2fa).

## Known limitations

- The data comes from scraping the v2 pages (`/consommation-mobile` and the home page): usage figures from the page's embedded React Server Components data, the rest from the HTML. It's only as reliable as that structure. Run the health check.
- The site shows a single carbon footprint figure; it's reported under France usage, as before.
- Free shows the billing period without a year ("du 18/11 au 18/12"); the year is taken from the reset date it shows next to it.
- Only one line is supported per server instance. To track several lines, run one instance per line.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest
```

The tests run offline against a fake mobile.free.fr (login, one-time code, session expiry) that serves the fixtures in `tests/fixtures/`. Those are redacted, trimmed copies of real v2 pages: Free's markup and RSC data for the parts the parser reads, with fake identity and usage values.

To refresh them after a site change, save the pages with `scripts/probe_v2_login.py` (writes to `probe_out/`, which is gitignored and contains personal data), then run `python scripts/make_v2_fixtures.py`. It refuses to write a fixture if anything that looks personal (name, identifiant, phone numbers, e-mails, call log, invoices) is left.

## Publishing to the MCP Registry

The metadata for the [official MCP Registry](https://registry.modelcontextprotocol.io) is in [`server.json`](server.json). Directories such as Glama, LobeHub, mcp.so and mcpservers.org pick up registry entries automatically.

1. Bump the version in `pyproject.toml`, `src/freemobile_conso_mcp/__init__.py` and both `version` fields in `server.json`.
2. Publish to PyPI:
   ```bash
   python -m build && twine upload dist/*
   ```
   The registry checks that you own the PyPI package by looking for the `mcp-name: io.github.drmeph/freemobile-conso-mcp` comment at the top of this README.
3. Publish the metadata. Install `mcp-publisher` (see the [registry docs](https://github.com/modelcontextprotocol/registry)), then run:
   ```bash
   mcp-publisher login github     # proves you own the io.github.drmeph/* namespace
   mcp-publisher publish
   ```

## License

MIT. Earlier versions built on FreeMobileConso, © CorentinMre, MIT.
