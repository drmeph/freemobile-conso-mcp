"""Thin, cached, failure-aware wrapper around FreeMobileConso.

FreeMobileConso (MIT, CorentinMre) logs in to https://mobile.free.fr/account/
with a plain ``requests`` session and scrapes the dashboard HTML. It was
written in 2021 and parses everything in a single pass, so any change to the
dashboard markup surfaces as an opaque exception. This module adds:

* a short-lived cache, so a burst of tool calls costs one login, not nine;
* capture of the raw HTML response, used to tell a bad password apart from a
  dashboard redesign, and to extract fields the library does not expose
  (plan name);
* a typed error with a machine-readable ``kind`` that the health check reports.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("freemobile_conso_mcp")

ENV_USERNAME = "FREEMOBILE_USERNAME"
ENV_PASSWORD = "FREEMOBILE_PASSWORD"
ENV_CACHE_TTL = "FREEMOBILE_CACHE_TTL"
ENV_DUMP_HTML = "FREEMOBILE_DUMP_HTML"
DEFAULT_CACHE_TTL = 300  # seconds


class FreeMobileError(Exception):
    """Raised when usage data can't be fetched.

    ``kind`` is one of:
      - ``not_configured``: credentials missing from the environment
      - ``network_error``: mobile.free.fr unreachable / HTTP error
      - ``auth_failed``: the login form came back (wrong credentials, or an
        extra verification step such as an SMS/e-mail code or captcha)
      - ``layout_changed``: logged in, but the dashboard markup no longer
        matches what FreeMobileConso expects
    """

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


@dataclass
class Snapshot:
    """One successful scrape of the dashboard."""

    data: dict[str, Any]
    html: str
    fetched_at: float = field(default_factory=time.time)

    @property
    def age_seconds(self) -> float:
        return time.time() - self.fetched_at


def _default_client_factory(username: str, password: str):
    from FreeMobileConso import Client  # imported lazily: keeps tests/offline use cheap

    return Client(username, password)


class FreeMobileAccount:
    def __init__(
        self,
        username: str | None = None,
        password: str | None = None,
        cache_ttl: float | None = None,
        client_factory: Callable[[str, str], Any] = _default_client_factory,
    ):
        self.username = username if username is not None else os.environ.get(ENV_USERNAME, "")
        self.password = password if password is not None else os.environ.get(ENV_PASSWORD, "")
        if cache_ttl is None:
            try:
                cache_ttl = float(os.environ.get(ENV_CACHE_TTL, DEFAULT_CACHE_TTL))
            except ValueError:
                cache_ttl = DEFAULT_CACHE_TTL
        self.cache_ttl = cache_ttl
        self._client_factory = client_factory
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
        if not self.username or not self.password:
            raise FreeMobileError(
                "not_configured",
                f"Set {ENV_USERNAME} (your 8-digit Free Mobile identifiant) and "
                f"{ENV_PASSWORD} in the MCP server's environment.",
            )

        client = self._client_factory(self.username, self.password)

        # Record the raw dashboard response the library receives, without
        # changing what it does with it.
        captured: dict[str, requests.Response] = {}
        original_post = client.session.post

        def recording_post(*args, **kwargs):
            kwargs.setdefault("timeout", 30)
            resp = original_post(*args, **kwargs)
            captured["response"] = resp
            return resp

        client.session.post = recording_post

        try:
            data = client.getConsoDict()
        except requests.RequestException as exc:
            raise FreeMobileError("network_error", f"Could not reach mobile.free.fr: {exc}") from exc
        except Exception as exc:  # the library raises bare Exception / AttributeError / IndexError
            resp = captured.get("response")
            html = resp.text if resp is not None else ""
            self._dump_html(html)
            if resp is not None and resp.status_code >= 400:
                raise FreeMobileError(
                    "network_error", f"mobile.free.fr answered HTTP {resp.status_code}"
                ) from exc
            if looks_like_login_page(html):
                raise FreeMobileError(
                    "auth_failed",
                    "Free Mobile returned the login page: the identifiant/password were "
                    "rejected, or Free asked for an extra verification step (SMS/e-mail "
                    "code, captcha). Log in once at https://mobile.free.fr/account/ from a "
                    "browser, then retry.",
                ) from exc
            raise FreeMobileError(
                "layout_changed",
                "Logged in, but the dashboard could not be parsed "
                f"({type(exc).__name__}: {exc}). Free Mobile has probably changed its "
                "dashboard markup and the FreeMobileConso selectors need updating.",
            ) from exc

        resp = captured.get("response")
        return Snapshot(data=data, html=resp.text if resp is not None else "")

    @staticmethod
    def _dump_html(html: str) -> None:
        """Opt-in debugging aid. The file contains personal account data."""
        path = os.environ.get(ENV_DUMP_HTML)
        if path and html:
            try:
                Path(path).write_text(html, encoding="utf-8")
                log.warning("Dashboard HTML written to %s (contains personal data)", path)
            except OSError as exc:
                log.warning("Could not write %s: %s", path, exc)


# ---------------------------------------------------------------- HTML helpers


def looks_like_login_page(html: str) -> bool:
    if not html:
        return False
    soup = BeautifulSoup(html, "html.parser")
    return bool(
        soup.find("input", {"name": "login-ident"}) or soup.find("input", {"name": "login-pwd"})
    )


_PLAN_PATTERNS = [
    # "Forfait Free 5G 250Go", "Forfait 2€", "Série Free", ...
    re.compile(r"(Forfait(?:\s+[^\s|]+){1,4})", re.IGNORECASE),
    re.compile(r"(S[ée]rie\s+Free(?:\s+[^\s|]+){0,2})", re.IGNORECASE),
]


def extract_plan_name(html: str) -> str | None:
    """Best-effort: the library does not expose the plan (offer) name.

    Looks for an element whose class mentions 'offre'/'forfait', then falls
    back to the first 'Forfait …' phrase in the page text.
    """
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    for el in soup.find_all(class_=re.compile(r"offre|forfait|offer|plan", re.IGNORECASE)):
        text = " ".join(el.get_text(" ", strip=True).split())
        if 3 <= len(text) <= 80:
            return text
    text = " ".join(soup.get_text(" ", strip=True).split())
    for pattern in _PLAN_PATTERNS:
        m = pattern.search(text)
        if m:
            name = m.group(1).strip(" .:-")
            # Avoid capturing generic sentences ("forfait inclus dans ...").
            if any(ch.isdigit() for ch in name) or "free" in name.lower():
                return name
    return None
