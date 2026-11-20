# freemobile-conso-mcp

<!-- mcp-name: io.github.drmeph/freemobile-conso-mcp -->

An [MCP](https://modelcontextprotocol.io) server that lets AI agents (Claude Desktop, Claude Code, any MCP client) read your **Free Mobile** account usage: data, calls, SMS and MMS in France and while roaming, out-of-plan charges, carbon footprint and billing cycle.

It's built for people living abroad on a Free plan with a roaming allowance (for example 35 GB abroad / 200+ GB in France) who want to ask *"how much roaming data do I have left?"* without logging into mobile.free.fr.

> Unofficial. Not affiliated with Free / Iliad. It scrapes your own subscriber dashboard with your own credentials, using the open-source [FreeMobileConso](https://github.com/CorentinMre/FreeMobileConso) library (MIT, by CorentinMre).

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

Every number comes with the raw string from the dashboard (`"used": "12.5Go"`, `"used_gb": 12.5`), so you can always check what the server parsed. Results are cached for 5 minutes, so a burst of tool calls only logs in once. Pass `refresh: true` to force a new login.

Example output from `get_roaming_usage` with `metric: "data"`:

```json
{
  "scope": "roaming",
  "data": {
    "used": "12.5Go", "used_gb": 12.5,
    "allowance": "35Go", "allowance_gb": 35.0,
    "remaining": "22.5Go", "remaining_gb": 22.5,
    "percent_used": 35.7,
    "overage": "0.00€", "overage_eur": 0.0
  },
  "billing_period": {
    "raw": "Suivi conso du 05/11/2026 au 04/12/2026",
    "start": "2026-11-05", "end": "2026-12-04", "next_renewal": "2026-12-05"
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
| `FREEMOBILE_DUMP_HTML` | no | A file path. When parsing fails, the raw dashboard HTML is written there so the selectors can be fixed. **The file contains your personal data.** |

Credentials are only sent to `mobile.free.fr` and are never logged.

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

FreeMobileConso was written in 2021 against the dashboard HTML of the time. If Free redesigns the dashboard, the selectors break. The server is built to tell you when that happens instead of returning wrong numbers:

- `health_check` (the tool) and `freemobile-conso-mcp --check` (the CLI) log in, parse the dashboard and inspect a set of canary fields: account name, billing period, and local/roaming data used and allowance.
- The `status` field is one of:
  - `ok`
  - `degraded`: parsed, but some canary fields are empty or no longer look like `12.3Go`
  - `layout_changed`: logged in, but the parser crashed
  - `auth_failed`: the login page came back
  - `network_error`
  - `not_configured`
- The usage tools add a `warnings` field when the fields they rely on come back empty.
- `--check` exits with `0` when the status is ok and `1` otherwise, so you can run it from cron or an uptime monitor:

```bash
# every morning at 08:00: alert if the scraper is broken
0 8 * * * FREEMOBILE_USERNAME=... FREEMOBILE_PASSWORD=... freemobile-conso-mcp --check >/dev/null || notify-send "Free Mobile MCP broken"
```

**`auth_failed` with correct credentials** usually means Free asked for an extra verification step on this login, such as an SMS or e-mail code or a captcha. FreeMobileConso can't complete that step. Log in once from a browser, then try again.

## Known limitations

- The data comes from scraping HTML, so it's only as reliable as the FreeMobileConso selectors. Run the health check.
- The plan name isn't parsed by FreeMobileConso. The server looks for it heuristically, and it may come back `null`.
- The dashboard only reports a carbon footprint for data used in France.
- FreeMobileConso adds up the out-of-plan amounts with `float()`. If Free starts showing amounts with French decimal commas (`1,50€`), the library raises an error, which shows up as `layout_changed`. This server's own parsers accept both `1.50` and `1,50`.
- FreeMobileConso uses BeautifulSoup's `findAll`, which is deprecated. A future major version of `beautifulsoup4` could break it.
- Only one line is supported per server instance. To track several lines, run one instance per line.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest
```

The tests run offline. The real FreeMobileConso parser runs against saved HTML fixtures in `tests/fixtures/`, which cover a working dashboard, the login page and a redesigned page. If Free changes its markup, save a sanitized copy of the new page with `FREEMOBILE_DUMP_HTML` and add it as a fixture.

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

MIT. FreeMobileConso is © CorentinMre, MIT.
