"""Offline tests: the real FreeMobileConso parser runs against saved HTML."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import requests
from FreeMobileConso import Client

from freemobile_conso_mcp import parsing, report, server
from freemobile_conso_mcp.client import FreeMobileAccount, FreeMobileError

FIXTURES = Path(__file__).parent / "fixtures"


def fake_factory(fixture: str | None = None, status: int = 200, exc: Exception | None = None):
    calls = {"n": 0}

    def factory(user, pwd):
        client = Client(user, pwd)

        def post(url, data=None, **kwargs):
            calls["n"] += 1
            if exc:
                raise exc
            resp = requests.Response()
            resp.status_code = status
            resp._content = (FIXTURES / fixture).read_bytes()
            resp.encoding = "utf-8"
            resp.url = url
            return resp

        client.session.post = post
        return client

    factory.calls = calls
    return factory


def account(fixture="dashboard.html", **kw) -> FreeMobileAccount:
    return FreeMobileAccount("12345678", "secret", cache_ttl=300, client_factory=fake_factory(fixture, **kw))


# ------------------------------------------------------------------- parsing


@pytest.mark.parametrize(
    "raw,gb",
    [("48.27Go", 48.27), ("512Mo", 0.512), ("35 Go", 35.0), ("1,5Go", 1.5), ("", None), ("Illimité", None)],
)
def test_parse_data(raw, gb):
    assert parsing.parse_data_gb(raw) == gb


def test_parse_misc():
    assert parsing.parse_euros("1.50€") == 1.5
    assert parsing.parse_euros("0,20 €") == 0.2
    assert parsing.parse_duration_seconds("1h02min10s") == 3730
    assert parsing.parse_duration_seconds("25min") == 1500
    assert parsing.parse_carbon_grams("1205gCO2e") == 1205.0
    assert parsing.parse_carbon_grams("1,2 kg CO2") == 1200.0
    p = parsing.billing_period("Suivi conso du 05/11/2026 au 04/12/2026")
    assert p == {
        "raw": "Suivi conso du 05/11/2026 au 04/12/2026",
        "start": "2026-11-05",
        "end": "2026-12-04",
        "next_renewal": "2026-12-05",
    }


# ------------------------------------------------------------------ reports


def test_local_usage():
    out = report.usage(account().get(), "local")
    assert out["data"]["used_gb"] == 48.27
    assert out["data"]["allowance_gb"] == 200.0
    assert out["data"]["remaining_gb"] == 151.73
    assert out["data"]["percent_used"] == 24.1
    assert out["calls"]["to_own_country_seconds"] == 3730
    assert out["sms"]["count"] == 37
    assert out["billing_period"]["next_renewal"] == "2026-12-05"
    assert "warnings" not in out


def test_roaming_usage_single_metric():
    out = report.usage(account().get(), "roaming", "data")
    assert set(out) >= {"scope", "data", "billing_period"}
    assert "calls" not in out
    assert out["data"]["allowance_gb"] == 35.0
    assert out["data"]["remaining_gb"] == 22.5


def test_overage_and_carbon_and_plan():
    snap = account().get()
    ov = report.overage(snap)
    assert ov["total_eur"] == 1.7
    assert ov["breakdown"]["roaming"]["calls"]["eur"] == 1.5
    assert report.carbon_footprint(snap)["grams_co2e"] == 1205.0
    plan = report.plan_info(snap)
    assert plan["plan_name"] == "Forfait Free 5G 200Go"
    assert plan["phone_number"] == "0612345678"
    assert plan["identifier"] == "12345678"


def test_cache_avoids_relogin():
    factory = fake_factory("dashboard.html")
    acct = FreeMobileAccount("u", "p", cache_ttl=300, client_factory=factory)
    acct.get()
    acct.get()
    assert factory.calls["n"] == 1
    acct.get(refresh=True)
    assert factory.calls["n"] == 2


# ------------------------------------------------------------- failure modes


@pytest.mark.parametrize(
    "kwargs,kind",
    [
        (dict(fixture="login.html"), "auth_failed"),
        (dict(fixture="redesigned.html"), "layout_changed"),
        (dict(fixture="dashboard.html", exc=requests.ConnectionError("boom")), "network_error"),
        (dict(fixture="login.html", status=503), "network_error"),
    ],
)
def test_error_classification(kwargs, kind):
    with pytest.raises(FreeMobileError) as err:
        account(**kwargs).get()
    assert err.value.kind == kind


def test_not_configured(monkeypatch):
    monkeypatch.delenv("FREEMOBILE_USERNAME", raising=False)
    monkeypatch.delenv("FREEMOBILE_PASSWORD", raising=False)
    with pytest.raises(FreeMobileError) as err:
        FreeMobileAccount().get()
    assert err.value.kind == "not_configured"


def test_health_ok_and_failures():
    assert server.run_health_check(account())["status"] == "ok"
    assert server.run_health_check(account("login.html"))["status"] == "auth_failed"
    assert server.run_health_check(account("redesigned.html"))["status"] == "layout_changed"


def test_health_degraded_on_empty_parse(tmp_path):
    # Same structure, but the selectors now hit empty elements.
    html = (FIXTURES / "dashboard.html").read_text().replace("48.27Go</span>", "</span>")
    (tmp_path / "empty.html").write_text(html)
    acct = FreeMobileAccount(
        "u", "p", client_factory=fake_factory(str(tmp_path / "empty.html"))
    )
    res = server.run_health_check(acct)
    assert res["status"] == "degraded"
    assert "local.internet.conso" in res["empty_fields"]
    assert report.usage(acct.get(), "local")["warnings"]


# ------------------------------------------------------------- MCP surface


def test_mcp_tools_end_to_end(monkeypatch):
    monkeypatch.setattr(server, "_account", account())

    async def run():
        tools = {t.name for t in await server.mcp.list_tools()}
        assert tools == {
            "get_local_usage",
            "get_roaming_usage",
            "get_overage",
            "get_carbon_footprint",
            "get_plan_info",
            "get_usage_summary",
            "health_check",
        }
        result = await server.mcp.call_tool("get_roaming_usage", {"metric": "data"})
        return result

    result = asyncio.run(run())
    text = json.dumps(result, default=str)
    assert "22.5" in text


def test_mcp_tool_error_is_reported(monkeypatch):
    monkeypatch.setattr(server, "_account", account("login.html"))

    async def run():
        return await server.mcp.call_tool("get_overage", {})

    try:
        result = asyncio.run(run())
    except Exception as exc:  # depending on SDK version, errors raise or come back flagged
        assert "auth_failed" in str(exc)
    else:
        assert "auth_failed" in json.dumps(result, default=str)
