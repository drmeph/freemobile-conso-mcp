"""Offline tests against a fake mobile.free.fr/account/v2.

The page fixtures are redacted copies of real v2 pages, built with
scripts/make_v2_fixtures.py: Free's markup and RSC rows, fake values.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import requests

from freemobile_conso_mcp import parsing, report, server
from freemobile_conso_mcp.client import BASE_URL, FreeMobileAccount, FreeMobileError, parse_pages

FIXTURES = Path(__file__).parent / "fixtures"
USAGE_HTML = (FIXTURES / "v2_consommation.html").read_text(encoding="utf-8")
HOME_HTML = (FIXTURES / "v2_home.html").read_text(encoding="utf-8")


def _resp(url: str, status: int = 200, body: str | dict = "") -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r.url = url
    if isinstance(body, dict):
        r._content = json.dumps(body).encode()
        r.headers["Content-Type"] = "application/json"
    else:
        r._content = body.encode()
        r.headers["Content-Type"] = "text/html; charset=utf-8"
    r.encoding = "utf-8"
    return r


class FakeFree:
    """Just enough of the v2 site: NextAuth credentials + OTP, two pages.

    Login state lives in the client's cookies, like the real site."""

    def __init__(self, password="secret", otp=None, usage=USAGE_HTML, home=HOME_HTML,
                 page_status=200, down=False, trusted_skips_otp=True):
        self.password, self.otp = password, otp
        self.usage, self.home = usage, home
        self.page_status, self.down = page_status, down
        self.trusted_skips_otp = trusted_skips_otp
        self.logins = 0  # password submissions (each one may send an SMS)
        self.otp_submissions = 0
        self.sms_sent = 0

    def session(self) -> "FakeSession":
        return FakeSession(self)


class FakeSession(requests.Session):
    def __init__(self, free: FakeFree):
        super().__init__()
        self.free = free

    def _token(self):
        return self.cookies.get("session-token")

    def get(self, url, **kwargs):
        f = self.free
        if f.down:
            raise requests.ConnectionError("boom")
        if url == f"{BASE_URL}/api/auth/csrf":
            return _resp(url, body={"csrfToken": "csrf-1"})
        if url == f"{BASE_URL}/api/auth/session":
            tok = self._token()
            if tok == "pending":
                return _resp(url, body={"user": {"type2FA": "sms", "otpId": 7}, "expires": "x"})
            return _resp(url, body={"user": {}, "expires": "x"} if tok == "ok" else {})
        page = url[len(BASE_URL):].strip("/")
        if self._token() != "ok":
            return _resp(f"{BASE_URL}/otp" if self._token() == "pending" else f"{BASE_URL}/login", body="<html>login</html>")
        if page == "consommation-mobile":
            return _resp(url, f.page_status, f.usage)
        if page == "":
            return _resp(url, f.page_status, f.home)
        return _resp(url, 404, "not found")

    def post(self, url, data=None, **kwargs):
        f = self.free
        if f.down:
            raise requests.ConnectionError("boom")
        assert url == f"{BASE_URL}/api/auth/callback/credentials"
        assert data["csrfToken"] == "csrf-1"
        err = {"url": f"{BASE_URL}/api/auth/error?error=CredentialsSignin"}
        if "codeOtp" in data:
            f.otp_submissions += 1
            if self._token() != "pending" or data["codeOtp"] != f.otp:
                return _resp(url, 401, err)
            self.cookies.set("session-token", "ok", domain="mobile.free.fr", path="/")
            if data.get("isTrusted") == "true":
                self.cookies.set("trusted-devices", "dev-1", domain="mobile.free.fr", path="/account")
            return _resp(url, body={"url": f"{BASE_URL}/otp"})
        f.logins += 1
        if data.get("password") != f.password:
            return _resp(url, body=err)
        trusted = self.cookies.get("trusted-devices") == "dev-1" and f.trusted_skips_otp
        if f.otp and not trusted:
            f.sms_sent += 1
            self.cookies.set("session-token", "pending", domain="mobile.free.fr", path="/")
        else:
            self.cookies.set("session-token", "ok", domain="mobile.free.fr", path="/")
        return _resp(url, body={"url": f"{BASE_URL}/login"})


@pytest.fixture
def state(tmp_path):
    return tmp_path / "state" / "session.json"


def account(free: FakeFree, state_path: Path, password: str = "secret") -> FreeMobileAccount:
    acct = FreeMobileAccount("12345678", password, cache_ttl=300, session_factory=free.session, state_path=state_path)
    acct.free = free
    return acct


@pytest.fixture
def acct(state):
    return account(FakeFree(), state)


# ------------------------------------------------------------------- parsing


@pytest.mark.parametrize(
    "raw,gb",
    [("48.27Go", 48.27), ("512Mo", 0.512), ("35 Go", 35.0), ("1,5Go", 1.5), ("0 Mo", 0.0), ("", None), ("Illimité", None)],
)
def test_parse_data(raw, gb):
    assert parsing.parse_data_gb(raw) == gb


def test_parse_misc():
    assert parsing.parse_euros("1.50€") == 1.5
    assert parsing.parse_euros("0,20 €") == 0.2
    assert parsing.parse_duration_seconds("1h02min10s") == 3730
    assert parsing.parse_duration_seconds("8min 39s") == 519
    assert parsing.parse_duration_seconds("25min") == 1500
    assert parsing.parse_carbon_grams("1205gCO2e") == 1205.0
    assert parsing.parse_carbon_grams("920g CO2e") == 920.0
    assert parsing.parse_carbon_grams("1,2 kg CO2") == 1200.0


def test_billing_period_legacy_format():
    p = parsing.billing_period("Suivi conso du 05/11/2026 au 04/12/2026")
    assert p == {
        "raw": "Suivi conso du 05/11/2026 au 04/12/2026",
        "start": "2026-11-05",
        "end": "2026-12-04",
        "next_renewal": "2026-12-05",
    }


def test_billing_period_v2_format():
    raw = "Période du 18/11 au 18/12. Votre forfait sera remis à zéro le 18 décembre 2026"
    assert parsing.billing_period(raw) == {
        "raw": raw, "start": "2026-11-18", "end": "2026-12-18", "next_renewal": "2026-12-18",
    }
    # A period that straddles the new year.
    p = parsing.billing_period("Période du 20/12 au 20/01. Votre forfait sera remis à zéro le 20 janvier 2027")
    assert (p["start"], p["end"], p["next_renewal"]) == ("2026-12-20", "2027-01-20", "2027-01-20")


def test_parse_v2_pages():
    d = parse_pages(USAGE_HTML, HOME_HTML)
    assert d["nameAcount"] == "Jean Dupont"
    assert d["identifier"] == "12345678"
    assert d["number"] == "0612345678"
    assert d["plan"] == "Forfait Free 5G+"
    assert d["local"]["internet"] == {
        "conso": "48,27 Go", "consoMax": "350 Go", "remaining": "301,73 Go",
        "excludingPackage": "0,00€", "carbonFootprint": "1205g CO2e",
    }
    assert d["roaming"]["call"]["outgoing"] == "20min 5s"
    assert d["roaming"]["SMS"]["excludingPackage"] == "0,20€"


# ------------------------------------------------------------------ reports


def test_local_usage(acct):
    out = report.usage(acct.get(), "local")
    assert out["data"]["used_gb"] == 48.27
    assert out["data"]["allowance_gb"] == 350.0
    assert out["data"]["remaining_gb"] == 301.73
    assert out["data"]["percent_used"] == 13.8
    assert out["calls"]["used_seconds"] == 4360
    assert out["calls"]["to_own_country_seconds"] == 3730
    assert out["calls"]["to_international_seconds"] == 630
    assert out["calls"]["allowance"] == "Illimité"
    assert out["sms"]["count"] == 37
    assert out["mms"]["count"] == 2
    assert out["billing_period"]["next_renewal"] == "2026-12-05"
    assert "warnings" not in out


def test_roaming_usage(acct):
    out = report.usage(acct.get(), "roaming")
    assert out["data"]["allowance_gb"] == 35.0
    assert out["data"]["remaining_gb"] == 22.5
    assert out["data"]["percent_used"] == 35.7
    assert out["calls"]["used_seconds"] == 1505
    assert out["calls"]["outgoing_seconds"] == 1205
    assert out["calls"]["incoming_seconds"] == 300
    assert out["calls"]["overage_eur"] == 1.5
    only_data = report.usage(acct.get(), "roaming", "data")
    assert "calls" not in only_data and "data" in only_data


def test_overage_carbon_plan_summary(acct):
    snap = acct.get()
    ov = report.overage(snap)
    assert ov["total_eur"] == 1.7
    assert ov["breakdown"]["roaming"]["calls"]["eur"] == 1.5
    assert ov["breakdown"]["roaming"]["sms"]["eur"] == 0.2
    assert ov["breakdown"]["local"]["data"]["eur"] == 0.0
    assert report.carbon_footprint(snap)["grams_co2e"] == 1205.0
    plan = report.plan_info(snap)
    assert plan == {**plan, "plan_name": "Forfait Free 5G+", "account_holder": "Jean Dupont",
                    "identifier": "12345678", "phone_number": "0612345678"}
    s = report.summary(snap)
    assert s["roaming_data"]["remaining_gb"] == 22.5
    assert s["overage_total_eur"] == 1.7


# ------------------------------------------------------- login and sessions


def test_cache_avoids_relogin(acct):
    acct.get()
    acct.get()
    assert acct.free.logins == 1
    acct.get(refresh=True)  # the saved session is still valid: no new login
    assert acct.free.logins == 1


def test_session_persists_across_instances(state):
    free = FakeFree()
    account(free, state).get()
    assert state.exists() and oct(state.stat().st_mode & 0o777) == "0o600"
    account(free, state).get()
    assert free.logins == 1


def test_state_file_is_per_account(state):
    free = FakeFree()
    account(free, state).get()
    other = FreeMobileAccount("87654321", "secret", session_factory=free.session, state_path=state)
    other.get()
    assert free.logins == 2


def test_otp_required_then_login_flow(state):
    free = FakeFree(otp="123456")
    acct = account(free, state)
    with pytest.raises(FreeMobileError) as err:
        acct.get()
    assert err.value.kind == "otp_required"
    assert "--login" in err.value.message
    assert free.sms_sent == 1

    # The server must not log in again (and trigger more SMS) on its own.
    for _ in range(3):
        with pytest.raises(FreeMobileError) as err:
            account(free, state).get(refresh=True)
        assert err.value.kind == "otp_required"
    assert free.sms_sent == 1 and free.logins == 1

    # `--login` reuses the pending login rather than asking for a new code.
    said = []
    assert server.run_login(account(free, state), ask=lambda _: "123 456", say=said.append) == 0
    assert free.sms_sent == 1 and free.otp_submissions == 1
    assert "status = ok" in said[-1]

    acct = account(free, state)
    assert report.summary(acct.get())["roaming_data"]["remaining_gb"] == 22.5

    # Session lost (e.g. expired): the trusted-device cookie skips the code.
    saved = json.loads(state.read_text())
    saved["cookies"] = [c for c in saved["cookies"] if c["name"] != "session-token"]
    state.write_text(json.dumps(saved))
    account(free, state).get()
    assert free.logins == 2 and free.sms_sent == 1


def test_wrong_otp_is_auth_failed_and_not_retried(state):
    free = FakeFree(otp="123456")
    with pytest.raises(FreeMobileError):
        account(free, state).get()
    said = []
    assert server.run_login(account(free, state), ask=lambda _: "000000", say=said.append) == 1
    assert "auth_failed" in said[-1]
    assert free.otp_submissions == 1


def test_login_without_pending_code_starts_fresh(state):
    free = FakeFree(otp="123456")
    said = []
    assert server.run_login(account(free, state), ask=lambda _: "123456", say=said.append) == 0
    assert "by SMS" in said[0]
    assert free.sms_sent == 1


# ------------------------------------------------------------- failure modes


@pytest.mark.parametrize(
    "free,kind",
    [
        (FakeFree(password="other"), "auth_failed"),
        (FakeFree(usage="<html><body>Nouvelle page</body></html>"), "layout_changed"),
        (FakeFree(down=True), "network_error"),
        (FakeFree(page_status=503), "network_error"),
        (FakeFree(otp="123456"), "otp_required"),
    ],
)
def test_error_classification(free, kind, state):
    with pytest.raises(FreeMobileError) as err:
        account(free, state).get()
    assert err.value.kind == kind


def test_bad_password_does_not_leak_into_message(state):
    with pytest.raises(FreeMobileError) as err:
        account(FakeFree(password="other"), state, password="hunter2").get()
    assert "hunter2" not in err.value.message
    assert "CredentialsSignin" in err.value.message


def test_not_configured(monkeypatch, state):
    monkeypatch.delenv("FREEMOBILE_USERNAME", raising=False)
    monkeypatch.delenv("FREEMOBILE_PASSWORD", raising=False)
    with pytest.raises(FreeMobileError) as err:
        FreeMobileAccount(state_path=state).get()
    assert err.value.kind == "not_configured"


def test_health_ok_and_failures(state, tmp_path):
    assert server.run_health_check(account(FakeFree(), state))["status"] == "ok"
    assert server.run_health_check(account(FakeFree(password="x"), tmp_path / "a"))["status"] == "auth_failed"
    assert server.run_health_check(account(FakeFree(otp="1"), tmp_path / "b"))["status"] == "otp_required"
    bad = FakeFree(usage="<html><body>Nouvelle page</body></html>")
    assert server.run_health_check(account(bad, tmp_path / "c"))["status"] == "layout_changed"


def test_health_degraded_on_empty_parse(state):
    # Same structure, but the data gauge no longer carries the figures.
    usage = USAGE_HTML.replace("48,27 Go / 350 Go", "")
    acct = account(FakeFree(usage=usage), state)
    res = server.run_health_check(acct)
    assert res["status"] == "degraded"
    assert "local.internet.conso" in res["empty_fields"]
    assert report.usage(acct.get(), "local")["warnings"]


# ------------------------------------------------------------- MCP surface


def test_mcp_tools_end_to_end(monkeypatch, acct):
    monkeypatch.setattr(server, "_account", acct)

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
        return await server.mcp.call_tool("get_roaming_usage", {"metric": "data"})

    text = json.dumps(asyncio.run(run()), default=str)
    assert "22.5" in text


@pytest.mark.parametrize("free,kind", [(FakeFree(password="x"), "auth_failed"), (FakeFree(otp="1"), "otp_required")])
def test_mcp_tool_error_is_reported(monkeypatch, state, free, kind):
    monkeypatch.setattr(server, "_account", account(free, state))

    async def run():
        return await server.mcp.call_tool("get_overage", {})

    try:
        result = asyncio.run(run())
    except Exception as exc:  # depending on SDK version, errors raise or come back flagged
        assert kind in str(exc)
    else:
        assert kind in json.dumps(result, default=str)
