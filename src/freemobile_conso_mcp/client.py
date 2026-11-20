"""Cached, failure-aware client for Free Mobile's v2 subscriber area.

Since late 2026 the subscriber area at https://mobile.free.fr/account/v2/ is a
Next.js app behind NextAuth, with SMS/e-mail two-factor authentication. This
module:

* logs in through NextAuth's credentials flow (CSRF token, then
  ``/api/auth/callback/credentials``), and submits the one-time code through
  the same endpoint (``codeOtp``) when Free asks for one;
* keeps the session cookies, including Free's ``trusted-devices`` cookie, in a
  private state file, so later logins skip the SMS step and most fetches reuse
  the existing session without logging in again;
* never triggers more than one SMS on its own: once Free asks for a code,
  every fetch fails with ``otp_required`` until the code is entered with
  ``freemobile-conso-mcp --login``;
* reads the usage page (``/consommation-mobile``) and the home page, and
  rebuilds the dict shape FreeMobileConso used to return, so ``report`` and
  the tool outputs stay the same;
* caches the result for a few minutes and reports failures with a typed,
  machine-readable ``kind``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup, Tag

log = logging.getLogger("freemobile_conso_mcp")

ENV_USERNAME = "FREEMOBILE_USERNAME"
ENV_PASSWORD = "FREEMOBILE_PASSWORD"
ENV_CACHE_TTL = "FREEMOBILE_CACHE_TTL"
ENV_DUMP_HTML = "FREEMOBILE_DUMP_HTML"
ENV_STATE_FILE = "FREEMOBILE_STATE_FILE"
DEFAULT_CACHE_TTL = 300  # seconds

BASE_URL = "https://mobile.free.fr/account/v2"
USAGE_PAGE = "consommation-mobile"
HOME_PAGE = ""
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
# A code Free sent is only worth typing in while the login that asked for it
# is recent; past this, `--login` starts over (and Free sends a new code).
OTP_PENDING_REUSE_SECONDS = 600


class FreeMobileError(Exception):
    """Raised when usage data can't be fetched.

    ``kind`` is one of:
      - ``not_configured``: credentials missing from the environment
      - ``network_error``: mobile.free.fr unreachable / HTTP error
      - ``auth_failed``: Free rejected the identifiant/password (or the
        one-time code), or the session was refused after logging in
      - ``otp_required``: Free wants a one-time code (sent by SMS or e-mail)
        for this login; run ``freemobile-conso-mcp --login`` once in a
        terminal to enter it
      - ``layout_changed``: logged in, but the pages no longer contain the
        data where this client expects it
    """

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


OTP_HELP = (
    "Free Mobile is asking for a one-time security code ({channel}) for this login. "
    "Run `freemobile-conso-mcp --login` in a terminal, with the same "
    f"{ENV_USERNAME}/{ENV_PASSWORD} (and {ENV_STATE_FILE}, if set), and type the "
    "code it asks for. The device is then remembered and later logins don't need "
    "a code. This server won't log in again until that's done, so it doesn't send "
    "you more codes."
)


@dataclass
class Snapshot:
    """One successful scrape of the subscriber area."""

    data: dict[str, Any]
    html: str
    fetched_at: float = field(default_factory=time.time)

    @property
    def age_seconds(self) -> float:
        return time.time() - self.fetched_at


def default_state_path() -> Path:
    env = os.environ.get(ENV_STATE_FILE)
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return Path(base) / "freemobile-conso-mcp" / "session.json"


def _new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "fr-FR,fr;q=0.9"})
    return s


class FreeMobileAccount:
    def __init__(
        self,
        username: str | None = None,
        password: str | None = None,
        cache_ttl: float | None = None,
        session_factory: Callable[[], requests.Session] = _new_session,
        state_path: str | Path | None = None,
    ):
        self.username = username if username is not None else os.environ.get(ENV_USERNAME, "")
        self.password = password if password is not None else os.environ.get(ENV_PASSWORD, "")
        if cache_ttl is None:
            try:
                cache_ttl = float(os.environ.get(ENV_CACHE_TTL, DEFAULT_CACHE_TTL))
            except ValueError:
                cache_ttl = DEFAULT_CACHE_TTL
        self.cache_ttl = cache_ttl
        self.state_path = Path(state_path) if state_path is not None else default_state_path()
        self._session_factory = session_factory
        self._lock = threading.Lock()
        self._snapshot: Snapshot | None = None

    # ------------------------------------------------------------------ fetch

    def get(self, refresh: bool = False) -> Snapshot:
        with self._lock:
            snap = self._snapshot
            if snap is not None and not refresh and snap.age_seconds < self.cache_ttl:
                return snap
            self._snapshot = self._fetch()
            return self._snapshot

    def _fetch(self) -> Snapshot:
        self._require_credentials()
        state = self._load_state()
        if state.get("otp_pending"):
            # Logging in again would make Free send yet another code.
            raise FreeMobileError(
                "otp_required", OTP_HELP.format(channel=_channel(state.get("otp_type")))
            )

        session = self._session(state)
        try:
            pages = self._fetch_pages(session)
            if pages is None:  # no session, or it expired: log in again
                self._login(session)
                pages = self._fetch_pages(session)
                if pages is None:
                    raise FreeMobileError(
                        "auth_failed",
                        "Free Mobile accepted the login but kept sending back the "
                        "login page. Log in once at https://mobile.free.fr/account/v2/ "
                        "from a browser to check the account, then retry.",
                    )
        except requests.RequestException as exc:
            raise FreeMobileError("network_error", f"Could not reach mobile.free.fr: {exc}") from exc
        # Saved only on success: a failed login must not overwrite a pending-code state.
        self._save_session(session)

        usage_html, home_html = pages
        try:
            data = parse_pages(usage_html, home_html)
        except Exception as exc:
            self._dump_html(usage_html + "\n<!-- ===== home ===== -->\n" + home_html)
            raise FreeMobileError(
                "layout_changed",
                "Logged in, but the usage page could not be parsed "
                f"({type(exc).__name__}: {exc}). Free Mobile has probably changed its "
                "subscriber area and the v2 parser needs updating.",
            ) from exc
        return Snapshot(data=data, html=home_html)

    def _fetch_pages(self, session: requests.Session) -> tuple[str, str] | None:
        """Both pages, or None when Free sends us to the login page."""
        out = []
        for page in (USAGE_PAGE, HOME_PAGE):
            r = session.get(f"{BASE_URL}/{page}", timeout=30)
            where = _landing(r.url)
            if where == "login":
                return None
            if where == "otp":
                raise FreeMobileError("otp_required", OTP_HELP.format(channel=_channel(None)))
            if r.status_code >= 400:
                raise FreeMobileError("network_error", f"mobile.free.fr answered HTTP {r.status_code}")
            out.append(r.text)
        return out[0], out[1]

    # ------------------------------------------------------------------ login

    def _login(self, session: requests.Session) -> None:
        """Password step. Raises otp_required (after saving the pending login)
        when Free asks for a code."""
        self._post_credentials(
            session,
            {"username": self.username, "password": self.password},
            callback="login",
            what="identifiant/password",
        )
        user = self._session_user(session)
        if user.get("type2FA"):
            self._save_session(
                session,
                otp_pending=True,
                otp_type=str(user.get("type2FA")),
                otp_id=user.get("otpId"),
                otp_requested_at=time.time(),
            )
            raise FreeMobileError(
                "otp_required", OTP_HELP.format(channel=_channel(user.get("type2FA")))
            )

    def start_login(self) -> str | None:
        """Interactive login, step 1. Returns the 2FA channel ("sms", "email")
        when a code is needed, or None when the login is complete.

        Reuses a login that is already waiting for a code, if it is recent,
        rather than making Free send another one.
        """
        self._require_credentials()
        state = self._load_state()
        if state.get("otp_pending") and time.time() - float(state.get("otp_requested_at") or 0) < OTP_PENDING_REUSE_SECONDS:
            return str(state.get("otp_type") or "sms")
        state.pop("otp_pending", None)
        session = self._session(state)
        try:
            self._login(session)
        except FreeMobileError as exc:
            if exc.kind == "otp_required":
                return str(self._load_state().get("otp_type") or "sms")
            raise
        except requests.RequestException as exc:
            raise FreeMobileError("network_error", f"Could not reach mobile.free.fr: {exc}") from exc
        self._save_session(session)
        with self._lock:
            self._snapshot = None
        return None

    def submit_otp(self, code: str, trust_device: bool = True) -> None:
        """Interactive login, step 2: send the code Free sent, once."""
        state = self._load_state()
        if not state.get("otp_pending"):
            raise FreeMobileError("auth_failed", "No login is waiting for a code; run --login again.")
        code = code.strip().replace(" ", "")
        if not code.isdigit():
            raise FreeMobileError("auth_failed", "The security code should only contain digits.")
        session = self._session(state)
        try:
            data = {"codeOtp": code, "isTrusted": "true" if trust_device else "false", "redirect": "false"}
            if state.get("otp_type") == "email" and state.get("otp_id") is not None:
                data["otpId"] = str(state["otp_id"])
            self._post_credentials(session, data, callback="otp", what="security code")
            user = self._session_user(session)
        except requests.RequestException as exc:
            raise FreeMobileError("network_error", f"Could not reach mobile.free.fr: {exc}") from exc
        if user.get("type2FA") or user.get("otpId"):
            raise FreeMobileError("auth_failed", "Free Mobile did not accept the security code.")
        # Success: the pending flag goes, the session and trusted-device cookies stay.
        self._save_session(session)
        with self._lock:
            self._snapshot = None

    def _post_credentials(self, session: requests.Session, fields: dict, callback: str, what: str) -> None:
        csrf = session.get(f"{BASE_URL}/api/auth/csrf", timeout=30)
        if csrf.status_code >= 400:
            raise FreeMobileError("network_error", f"mobile.free.fr answered HTTP {csrf.status_code}")
        try:
            token = csrf.json()["csrfToken"]
        except (ValueError, KeyError, TypeError) as exc:
            raise FreeMobileError(
                "layout_changed", "The v2 login no longer returns a CSRF token from /api/auth/csrf."
            ) from exc
        r = session.post(
            f"{BASE_URL}/api/auth/callback/credentials",
            data={**fields, "csrfToken": token, "callbackUrl": f"{BASE_URL}/{callback}", "json": "true"},
            headers={"X-Auth-Return-Redirect": "1"},
            timeout=30,
            allow_redirects=False,
        )
        if r.status_code >= 500:
            raise FreeMobileError("network_error", f"mobile.free.fr answered HTTP {r.status_code}")
        error = _auth_error(r)
        if error:
            hint = (
                f" Check {ENV_USERNAME} (your 8-digit identifiant) and {ENV_PASSWORD}."
                if callback == "login"
                else ""
            )
            raise FreeMobileError(
                "auth_failed", f"Free Mobile rejected the {what} (NextAuth error: {error}).{hint}"
            )

    @staticmethod
    def _session_user(session: requests.Session) -> dict:
        r = session.get(f"{BASE_URL}/api/auth/session", timeout=30)
        try:
            body = r.json()
        except ValueError:
            return {}
        return (body or {}).get("user") or {}

    def _require_credentials(self) -> None:
        if not self.username or not self.password:
            raise FreeMobileError(
                "not_configured",
                f"Set {ENV_USERNAME} (your 8-digit Free Mobile identifiant) and "
                f"{ENV_PASSWORD} in the MCP server's environment.",
            )

    # ------------------------------------------------------------------ state

    def _load_state(self) -> dict:
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        # A state file belongs to one account.
        if not isinstance(state, dict) or state.get("username") != self.username:
            return {}
        return state

    def _session(self, state: dict) -> requests.Session:
        session = self._session_factory()
        for c in state.get("cookies") or []:
            try:
                session.cookies.set(
                    c["name"], c["value"], domain=c.get("domain"), path=c.get("path") or "/",
                    expires=c.get("expires"),
                )
            except (KeyError, TypeError):
                continue
        return session

    def _save_session(self, session: requests.Session, **extra: Any) -> None:
        """Persist cookies (session + trusted device). The file is private (0600)."""
        cookies = [
            {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path, "expires": c.expires}
            for c in session.cookies
        ]
        state = {"username": self.username, "cookies": cookies, **extra}
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            tmp = self.state_path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state, f)
            os.replace(tmp, self.state_path)
        except OSError as exc:
            log.warning("Could not save the Free Mobile session to %s: %s", self.state_path, exc)

    @staticmethod
    def _dump_html(html: str) -> None:
        """Opt-in debugging aid. The file contains personal account data."""
        path = os.environ.get(ENV_DUMP_HTML)
        if path and html:
            try:
                Path(path).write_text(html, encoding="utf-8")
                log.warning("Subscriber-area HTML written to %s (contains personal data)", path)
            except OSError as exc:
                log.warning("Could not write %s: %s", path, exc)


def _landing(url: str) -> str | None:
    path = urlparse(url or "").path.rstrip("/")
    if path.endswith("/login"):
        return "login"
    if path.endswith("/otp"):
        return "otp"
    return None


def _auth_error(resp: requests.Response) -> str | None:
    """NextAuth reports failures as ...?error=CODE in the returned url."""
    url = ""
    try:
        url = (resp.json() or {}).get("url") or ""
    except ValueError:
        url = resp.headers.get("Location", "")
    if "error=" in url:
        return (parse_qs(urlparse(url).query).get("error") or ["unknown"])[0]
    if resp.status_code in (401, 403):
        return f"HTTP {resp.status_code}"
    return None


def _channel(type2fa: Any) -> str:
    return {"sms": "sent by SMS", "email": "sent by e-mail"}.get(str(type2fa or "").lower(), "sent by SMS or e-mail")


# ------------------------------------------------------------- page parsing
#
# The v2 pages are server-rendered React. Usage figures are read from the
# React Server Components payload (self.__next_f.push(...) script chunks),
# which carries them as structured JSON; the rest (identity, out-of-plan
# charges, billing period, plan name) comes from the rendered HTML.


def rsc_rows(html: str) -> list[Any]:
    """Decode the RSC payload of a Next.js App Router page into JSON rows."""
    chunks = []
    for m in re.finditer(r"self\.__next_f\.push\((\[.*?\])\)\s*</script>", html, re.S):
        try:
            arr = json.loads(m.group(1))
        except ValueError:
            continue
        if len(arr) > 1 and isinstance(arr[1], str):
            chunks.append(arr[1])
    rows = []
    for line in "".join(chunks).split("\n"):
        m = re.match(r"^[0-9a-f]+:(.*)$", line)
        if not m:
            continue
        try:
            rows.append(json.loads(m.group(1)))
        except ValueError:
            continue
    return rows


def _walk(node: Any):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _consumption_blocks(rows: list[Any]) -> dict[str, dict]:
    """{'internet'|'call'|'messages': consumptionDescription} by title/icon."""
    out: dict[str, dict] = {}
    for node in _walk(rows):
        desc = node.get("consumptionDescription")
        if not isinstance(desc, dict):
            continue
        title = str(desc.get("title", "")).lower()
        icon = str(desc.get("icon", "")).lower()
        if title.startswith("internet") or "wifi" in icon:
            out.setdefault("internet", desc)
        elif title.startswith("appel") or "phone" in icon:
            out.setdefault("call", desc)
        elif title.startswith("sms") or "message" in icon:
            out.setdefault("messages", desc)
    return out


def _values(desc: dict, scope: str) -> dict[str, str]:
    """[{'label': 'Restant', 'value': '350 Go'}, ...] -> {'restant': '350 Go'}."""
    out = {}
    for item in desc.get(scope) or []:
        if isinstance(item, dict) and item.get("label") is not None:
            out[_norm(str(item["label"])).lower()] = item.get("value")
    return out


def _gauge_label(desc: dict, scope: str) -> str | None:
    return ((desc.get("consumptionsData") or {}).get(scope) or {}).get("label")


def _norm(text: str) -> str:
    return " ".join(text.replace("’", "'").split())


def _sum_durations(*values: str | None) -> str | None:
    from .parsing import parse_duration_seconds

    parsed = [parse_duration_seconds(v) for v in values if v is not None]
    if not parsed or any(p is None for p in parsed):
        return None
    total = sum(parsed)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return (f"{h}h" if h else "") + (f"{m}min" if m else "") + (f"{s}s" if s or not (h or m) else "")


def _internet(desc: dict | None, scope: str) -> dict:
    if not desc:
        return {}
    gauge = _gauge_label(desc, scope) or ""
    used, _, allowance = (part.strip() for part in gauge.partition("/"))
    return {
        "conso": used or None,
        "consoMax": allowance or None,
        "remaining": _values(desc, scope).get("restant"),
    }


def _call(desc: dict | None, scope: str) -> dict:
    if not desc:
        return {}
    vals = _values(desc, scope)
    out: dict[str, Any] = {"consoMax": _gauge_label(desc, scope)}
    if scope == "national":
        to_fr, to_intl = vals.get("vers la france"), vals.get("vers l'international")
        out.update(callToMyCountry=to_fr, callToInternational=to_intl, conso=_sum_durations(to_fr, to_intl))
    else:
        # Abroad, Free splits by direction (made / received) instead.
        made, received = vals.get("émis"), vals.get("reçus")
        out.update(outgoing=made, incoming=received, conso=_sum_durations(made, received))
    return out


def _messages(desc: dict | None, scope: str, kind: str) -> dict:
    if not desc:
        return {}
    count = _values(desc, scope).get(kind.lower())
    allowance = _gauge_label(desc, scope)
    return {"conso": count, f"nb{kind}": count, "consoMax": allowance, f"maxNb{kind}": allowance}


_OVERAGE_KEYS = {"internet": "internet", "appels": "call", "sms": "SMS", "mms": "MMS"}


def _overage_cards(soup: BeautifulSoup) -> dict[str, dict]:
    """'Hors-forfait en France' / 'Hors-forfait à l'étranger' cards."""
    out: dict[str, dict] = {}
    for h3 in soup.find_all("h3"):
        title = _norm(h3.get_text(" ", strip=True)).lower()
        if not title.startswith("hors-forfait"):
            continue
        scope = "roaming" if "étranger" in title or "etranger" in title else "local"
        total_el = h3.find_next_sibling("p")
        card = h3.parent.parent if isinstance(h3.parent, Tag) and isinstance(h3.parent.parent, Tag) else h3
        lines: dict[str, str] = {}
        for row in card.find_all("div"):
            ps = row.find_all("p", recursive=False)
            if len(ps) == 2 and row is not h3.parent:
                label = _norm(ps[0].get_text(" ", strip=True)).lower()
                if label in _OVERAGE_KEYS:
                    lines[_OVERAGE_KEYS[label]] = _norm(ps[1].get_text(" ", strip=True))
        out.setdefault(scope, {"total": _norm(total_el.get_text(" ", strip=True)) if total_el else None, **lines})
    return out


def _text_by_id(soup: BeautifulSoup, element_id: str) -> str | None:
    el = soup.find(id=element_id)
    return _norm(el.get_text(" ", strip=True)) if el else None


def _billing_period_text(soup: BeautifulSoup) -> str | None:
    el = soup.find(string=re.compile(r"P[ée]riode du"))
    if el is None:
        return None
    parent = el.find_parent(["p", "div", "span"])
    text = _norm((parent or el).get_text(" ", strip=True) if parent else str(el))
    return text.replace(" .", ".")


_PLAN_RE = re.compile(r"^(Forfait\b.*|S[ée]rie Free\b.*|Free (?:Max|Flex)\b.*)$", re.IGNORECASE)


def extract_plan_name(home_html: str) -> str | None:
    """The plan card on the home page: <img alt="Forfait Free 5G+"> next to
    a 'Voir mon offre' link (the footer also lists plan names, so anchor on it)."""
    if not home_html:
        return None
    soup = BeautifulSoup(home_html, "html.parser")
    link = soup.find(string=re.compile(r"Voir mon offre"))
    card = link.find_parent("a").find_parent("div").find_parent("div") if link and link.find_parent("a") else None
    if card is not None:
        for p in card.find_all("p"):
            text = _norm(p.get_text(" ", strip=True))
            if _PLAN_RE.match(text):
                return text
        img = card.find("img", alt=_PLAN_RE)
        if img:
            return _norm(img["alt"])
    return None


def _carbon(home_html: str, home_rows: list[Any]) -> str | None:
    for node in _walk(home_rows):
        value = node.get("carbonFootprint")
        if isinstance(value, str) and value.strip():
            return _norm(value)
    m = re.search(r"empreinte carbone est de\s*(?:<[^>]+>\s*)*([^<]+CO\S*)", home_html, re.IGNORECASE)
    return _norm(m.group(1)) if m else None


def parse_pages(usage_html: str, home_html: str) -> dict[str, Any]:
    """Build the FreeMobileConso-shaped dict that ``report`` consumes.

    Raises ValueError when the usage page doesn't carry the consumption data
    at all (a layout change); missing individual fields come back as None and
    are reported by the health check's canaries instead.
    """
    blocks = _consumption_blocks(rsc_rows(usage_html))
    if "internet" not in blocks:
        raise ValueError("no 'Internet' consumption block in the page's RSC payload")
    usage_soup = BeautifulSoup(usage_html, "html.parser")
    home_soup = BeautifulSoup(home_html, "html.parser") if home_html else BeautifulSoup("", "html.parser")
    overage = _overage_cards(usage_soup)

    data: dict[str, Any] = {
        "nameAcount": _text_by_id(usage_soup, "user-name") or _text_by_id(home_soup, "user-name"),
        "identifier": _text_by_id(usage_soup, "user-login") or _text_by_id(home_soup, "user-login"),
        "number": (_text_by_id(usage_soup, "user-msisdn") or _text_by_id(home_soup, "user-msisdn") or "").replace(" ", "") or None,
        "date": _billing_period_text(home_soup),
        "plan": extract_plan_name(home_html),
    }
    for scope, site_scope in (("local", "national"), ("roaming", "roaming")):
        charges = overage.get(scope, {})
        section = {
            "internet": _internet(blocks.get("internet"), site_scope),
            "call": _call(blocks.get("call"), site_scope),
            "SMS": _messages(blocks.get("messages"), site_scope, "SMS"),
            "MMS": _messages(blocks.get("messages"), site_scope, "MMS"),
        }
        for key, metric in section.items():
            metric["excludingPackage"] = charges.get(key)
        data[scope] = section
    data["local"]["internet"]["carbonFootprint"] = _carbon(home_html, rsc_rows(home_html))
    totals = [overage[s]["total"] for s in ("local", "roaming") if overage.get(s, {}).get("total")]
    data["totalExcludingPackage"] = " + ".join(totals) if totals else None
    return data
