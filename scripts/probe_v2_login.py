"""Probe Free Mobile's v2 subscriber area (Next.js + NextAuth, late 2026).

The legacy dashboard scraped by FreeMobileConso (POST /account/ with
login-ident / login-pwd) now redirects to /account/v2/login. This script logs
in through the new NextAuth credentials flow and saves what the usage pages
return, so a parser for the v2 site can be written.

Usage:
    FREEMOBILE_USERNAME=... FREEMOBILE_PASSWORD=... python scripts/probe_v2_login.py

Prints a redacted summary to stdout. Raw responses go to ./probe_out/
(gitignored), and they CONTAIN PERSONAL DATA: don't commit or share them
without redacting.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import requests

BASE = "https://mobile.free.fr/account/v2"
OUT = Path("probe_out")
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
PAGES = ["consommation-mobile", "empreinte-carbone", ""]


def redact(obj, secrets):
    text = json.dumps(obj, ensure_ascii=False) if not isinstance(obj, str) else obj
    for s in secrets:
        if s:
            text = text.replace(s, "<redacted>")
    return text


def main() -> int:
    user = os.environ.get("FREEMOBILE_USERNAME", "")
    pwd = os.environ.get("FREEMOBILE_PASSWORD", "")
    if not user or not pwd:
        print("Set FREEMOBILE_USERNAME and FREEMOBILE_PASSWORD.")
        return 2
    secrets = [user, pwd]
    OUT.mkdir(exist_ok=True)

    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "fr-FR,fr;q=0.9"})

    # 1. CSRF token (also sets the csrf cookie)
    r = s.get(f"{BASE}/api/auth/csrf", timeout=30)
    print("csrf:", r.status_code)
    csrf = r.json()["csrfToken"]

    # 2. Credentials callback, as next-auth's signIn("credentials", {redirect:false}) does
    r = s.post(
        f"{BASE}/api/auth/callback/credentials",
        data={
            "username": user,
            "password": pwd,
            "csrfToken": csrf,
            "callbackUrl": f"{BASE}/login",
            "json": "true",
        },
        headers={"X-Auth-Return-Redirect": "1", "Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
        allow_redirects=False,
    )
    print("callback:", r.status_code, redact(r.text[:300], secrets))
    if "error=" in r.text:
        print("=> Login rejected (see error code above).")
        return 1

    # 3. Session: does the account require 2FA (OTP)?
    r = s.get(f"{BASE}/api/auth/session", timeout=30)
    sess = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    (OUT / "session.json").write_text(json.dumps(sess, indent=2, ensure_ascii=False))
    u = (sess or {}).get("user") or {}
    print("session keys:", sorted(sess.keys()), "| user keys:", sorted(u.keys()))
    print("type2FA:", repr(u.get("type2FA")))
    if u.get("type2FA"):
        print("=> Free requires a one-time code for this login (SMS/e-mail).")
        print("   Saving the OTP page so the OTP step can be implemented.")
        r = s.get(f"{BASE}/otp", timeout=30)
        (OUT / "otp.html").write_text(r.text)
        print("   otp page:", r.status_code, r.url, len(r.text), "bytes")

    # 4. Usage pages
    for page in PAGES:
        r = s.get(f"{BASE}/{page}", timeout=30)
        name = page or "home"
        (OUT / f"{name}.html").write_text(r.text)
        html = r.text
        landed_on_login = "/login" in r.url
        apis = sorted(set(re.findall(r'["\'`](/account/v2/api/[^"\'`?]+)', html)))
        next_data = "__NEXT_DATA__" in html
        rsc = html.count("self.__next_f.push")
        units = len(re.findall(r"\d+(?:[.,]\d+)?\s?(?:Go|Mo|Ko)", html))
        print(
            f"page {name}: {r.status_code} -> {r.url} | {len(html)} bytes | "
            f"redirected_to_login={landed_on_login} | __NEXT_DATA__={next_data} | "
            f"rsc_chunks={rsc} | 'Go/Mo' values={units} | api refs={apis[:10]}"
        )

    # 5. Known API from the login bundle
    r = s.get(f"{BASE}/api/shop/user-info", timeout=30)
    (OUT / "user-info.json").write_text(r.text)
    print("api/shop/user-info:", r.status_code, r.headers.get("content-type"), len(r.text), "bytes")

    print(f"\nRaw responses saved in {OUT.resolve()} (personal data: do not commit).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
