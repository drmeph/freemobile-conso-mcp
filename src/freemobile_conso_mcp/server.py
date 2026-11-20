"""MCP server: Free Mobile account usage as tools.

Run with ``freemobile-conso-mcp`` (stdio transport). Credentials come from the
FREEMOBILE_USERNAME / FREEMOBILE_PASSWORD environment variables. If Free asks
for a one-time code, run ``freemobile-conso-mcp --login`` once in a terminal.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any, Literal

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import __version__, report
from .client import FreeMobileAccount, FreeMobileError

log = logging.getLogger("freemobile_conso_mcp")

INSTRUCTIONS = """\
Read-only access to a Free Mobile (France) account's current-cycle usage, \
scraped from the subscriber area at mobile.free.fr.

- "local" means usage on the French network; "roaming" means usage abroad \
(EU/roaming destinations, counted against the plan's roaming allowance).
- Data amounts are reported in GB (1 GB = 1000 MB, matching Free's "Go").
- Results are cached for a few minutes; pass refresh=true only if the user \
explicitly needs the very latest figures (each refresh is a fresh login).
- If a tool errors or returns warnings, call health_check to find out whether \
it is a credentials problem or a dashboard layout change.
- An [otp_required] error means Free wants a one-time security code: ask the \
user to run `freemobile-conso-mcp --login` in a terminal and enter the code \
they receive. Retrying the tool will not help until they have.
"""

mcp = MCPServer(
    name="freemobile-conso",
    title="Free Mobile usage",
    description="Free Mobile (France) data, calls, SMS, roaming, overage and carbon footprint.",
    instructions=INSTRUCTIONS,
    version=__version__,
    website_url="https://github.com/drmeph/freemobile-conso-mcp",
)

_account: FreeMobileAccount | None = None


def account() -> FreeMobileAccount:
    global _account
    if _account is None:
        _account = FreeMobileAccount()
    return _account


async def _snapshot(refresh: bool):
    try:
        return await anyio.to_thread.run_sync(lambda: account().get(refresh=refresh))
    except FreeMobileError as exc:
        raise ToolError(f"[{exc.kind}] {exc.message}") from exc


READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True)

Metric = Literal["all", "data", "calls", "sms", "mms"]


@mcp.tool(title="Usage in France", annotations=READ_ONLY)
async def get_local_usage(metric: Metric = "all", refresh: bool = False) -> dict[str, Any]:
    """Current billing-cycle usage on the French network: mobile data (used /
    allowance / remaining in GB, % used), call time, SMS and MMS counts, each
    with any out-of-plan charge.

    Args:
        metric: Restrict to one metric ("data", "calls", "sms", "mms") or "all".
        refresh: Bypass the short cache and log in again.
    """
    return report.usage(await _snapshot(refresh), "local", metric)


@mcp.tool(title="Usage abroad (roaming)", annotations=READ_ONLY)
async def get_roaming_usage(metric: Metric = "all", refresh: bool = False) -> dict[str, Any]:
    """Current billing-cycle usage while abroad (roaming): data used against the
    roaming allowance (e.g. 35 GB on the 5G plan), remaining GB, call time, SMS
    and MMS, each with any out-of-plan charge.

    Args:
        metric: Restrict to one metric ("data", "calls", "sms", "mms") or "all".
        refresh: Bypass the short cache and log in again.
    """
    return report.usage(await _snapshot(refresh), "roaming", metric)


@mcp.tool(title="Out-of-plan charges", annotations=READ_ONLY)
async def get_overage(refresh: bool = False) -> dict[str, Any]:
    """Charges billed on top of the monthly package ("hors forfait") for the
    current cycle: total in EUR plus a breakdown by local/roaming and by
    data/calls/SMS/MMS.
    """
    return report.overage(await _snapshot(refresh))


@mcp.tool(title="Carbon footprint", annotations=READ_ONLY)
async def get_carbon_footprint(refresh: bool = False) -> dict[str, Any]:
    """Estimated carbon footprint of this cycle's mobile data use, as shown on
    the Free Mobile dashboard (grams CO2e).
    """
    return report.carbon_footprint(await _snapshot(refresh))


@mcp.tool(title="Plan and billing cycle", annotations=READ_ONLY)
async def get_plan_info(refresh: bool = False) -> dict[str, Any]:
    """Account and plan details: plan name (best effort), account holder, line
    number, current billing period and the date the quotas next renew.
    """
    return report.plan_info(await _snapshot(refresh))


@mcp.tool(title="Quota summary", annotations=READ_ONLY)
async def get_usage_summary(refresh: bool = False) -> dict[str, Any]:
    """One-call overview for "how much data do I have left?": France and
    roaming data (used / remaining / % used), total overage and renewal date.
    """
    return report.summary(await _snapshot(refresh))


@mcp.tool(
    title="Health check",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
)
async def health_check() -> dict[str, Any]:
    """Log in afresh and verify the dashboard still parses.

    status is one of: ok, degraded (parsed, but known fields came back empty
    or malformed: the dashboard probably changed), layout_changed, auth_failed,
    otp_required (Free wants a one-time code: the user must run
    `freemobile-conso-mcp --login`), network_error, not_configured. Never
    raises; use it to diagnose other tools' errors.
    """
    return await anyio.to_thread.run_sync(lambda: run_health_check(account()))


def run_health_check(acct: FreeMobileAccount) -> dict[str, Any]:
    result: dict[str, Any] = {"server_version": __version__, "library": _library_version()}
    try:
        snap = acct.get(refresh=True)
    except FreeMobileError as exc:
        log.error("Health check failed [%s]: %s", exc.kind, exc.message)
        return {**result, "status": exc.kind, "ok": False, "detail": exc.message}

    empty = report.empty_fields(snap)
    malformed = report.unparseable_numbers(snap)
    if empty or malformed:
        detail = (
            "Logged in and parsed, but some fields are empty or malformed. The "
            "Free Mobile dashboard has probably changed; figures may be wrong."
        )
        log.warning("Health check degraded: empty=%s malformed=%s", empty, malformed)
        return {
            **result,
            "status": "degraded",
            "ok": False,
            "detail": detail,
            "empty_fields": empty,
            "malformed_fields": malformed,
        }
    local = (snap.data.get("local") or {}).get("internet") or {}
    return {
        **result,
        "status": "ok",
        "ok": True,
        "detail": "Logged in and all canary fields parsed.",
        "canary": {"local_data_used": local.get("conso"), "billing_period": snap.data.get("date")},
    }


def _library_version() -> str:
    # Formerly the FreeMobileConso version; the scraper is now built in.
    return "built-in (mobile.free.fr/account/v2)"


def run_login(acct: FreeMobileAccount, ask=input, say=print) -> int:
    """Interactive login: enter the one-time code Free sends, once."""
    try:
        channel = acct.start_login()
        if channel:
            where = {"sms": "by SMS", "email": "by e-mail"}.get(channel, f"({channel})")
            say(f"Free Mobile sent a security code {where}.")
            code = ask("Security code: ")
            acct.submit_otp(code, trust_device=True)
        res = run_health_check(acct)
    except FreeMobileError as exc:
        say(f"Login failed [{exc.kind}]: {exc.message}")
        return 1
    except (EOFError, KeyboardInterrupt):
        say("\nCancelled. Run --login again within 10 minutes to enter the same code.")
        return 1
    say(f"Logged in: health check status = {res['status']}. Session saved to {acct.state_path}.")
    return 0 if res["ok"] else 1


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="freemobile-conso-mcp",
        description="MCP server exposing Free Mobile account usage. Runs over stdio by default.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Run the health check once, print JSON and exit (0 = ok, 1 = problem). "
        "Handy for cron or uptime monitors.",
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help="Log in interactively and enter the one-time code Free sends by SMS/e-mail. "
        "The session and trusted-device cookies are saved, so the server doesn't need a code "
        "again until Free forgets the device.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    # stdout is the MCP channel: logs must go to stderr.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")

    if args.login:
        sys.exit(run_login(account()))

    if args.check:
        res = run_health_check(account())
        print(json.dumps(res, indent=2, ensure_ascii=False))
        sys.exit(0 if res["ok"] else 1)

    mcp.run()


if __name__ == "__main__":
    main()
