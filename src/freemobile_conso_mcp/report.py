"""Shape the scraped usage dict into agent-friendly payloads.

The client returns the dict shape FreeMobileConso used to produce for the
legacy dashboard, so these payloads stay stable across site versions. Every
payload keeps the raw display string from the site next to the parsed
number, so nothing is lost if a parser guesses wrong.
"""

from __future__ import annotations

from typing import Any

from .client import Snapshot
from .parsing import (
    billing_period,
    parse_carbon_grams,
    parse_data_gb,
    parse_duration_seconds,
    parse_euros,
    parse_int,
)

METRICS = ("data", "calls", "sms", "mms")

# usage-dict key -> our key
_LIB_KEYS = {"data": "internet", "calls": "call", "sms": "SMS", "mms": "MMS"}


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _data(d: dict) -> dict:
    used_gb = parse_data_gb(d.get("conso"))
    allowance_gb = parse_data_gb(d.get("consoMax"))
    remaining_gb = parse_data_gb(d.get("remaining"))
    if remaining_gb is None and used_gb is not None and allowance_gb is not None:
        remaining_gb = round(max(allowance_gb - used_gb, 0.0), 6)
    percent = (
        round(100 * used_gb / allowance_gb, 1)
        if used_gb is not None and allowance_gb
        else None
    )
    return {
        "used": d.get("conso"),
        "used_gb": used_gb,
        "allowance": d.get("consoMax"),
        "allowance_gb": allowance_gb,
        "remaining": d.get("remaining"),
        "remaining_gb": remaining_gb,
        "percent_used": percent,
        "overage": d.get("excludingPackage"),
        "overage_eur": parse_euros(d.get("excludingPackage")),
    }


def _calls(d: dict) -> dict:
    out = {
        "used": d.get("conso"),
        "used_seconds": parse_duration_seconds(d.get("conso")),
        "allowance": d.get("consoMax"),
        "to_own_country": d.get("callToMyCountry"),
        "to_own_country_seconds": parse_duration_seconds(d.get("callToMyCountry")),
        "to_international": d.get("callToInternational"),
        "to_international_seconds": parse_duration_seconds(d.get("callToInternational")),
        "overage": d.get("excludingPackage"),
        "overage_eur": parse_euros(d.get("excludingPackage")),
    }
    # Abroad, the v2 site splits call time by direction rather than destination.
    for key in ("outgoing", "incoming"):
        if key in d:
            out[key] = d.get(key)
            out[f"{key}_seconds"] = parse_duration_seconds(d.get(key))
    return out


def _messages(d: dict, kind: str) -> dict:
    count_key, max_key = ("nbSMS", "maxNbSMS") if kind == "sms" else ("nbMMS", "maxNbMMS")
    return {
        "used": d.get("conso"),
        "count": parse_int(d.get(count_key)),
        "count_raw": d.get(count_key),
        "allowance": d.get("consoMax"),
        "max_count_raw": d.get(max_key),
        "overage": d.get("excludingPackage"),
        "overage_eur": parse_euros(d.get("excludingPackage")),
    }


_SHAPERS = {
    "data": _data,
    "calls": _calls,
    "sms": lambda d: _messages(d, "sms"),
    "mms": lambda d: _messages(d, "mms"),
}


def _meta(snap: Snapshot) -> dict:
    return {
        "billing_period": billing_period(snap.data.get("date")),
        "fetched_seconds_ago": round(snap.age_seconds),
    }


def usage(snap: Snapshot, scope: str, metric: str = "all") -> dict:
    """scope: 'local' (France) or 'roaming' (abroad)."""
    section = snap.data.get(scope) or {}
    wanted = METRICS if metric == "all" else (metric,)
    out: dict[str, Any] = {"scope": scope}
    for name in wanted:
        out[name] = _SHAPERS[name](section.get(_LIB_KEYS[name]) or {})
    out.update(_meta(snap))
    warnings = empty_fields(snap, scopes=(scope,))
    if warnings:
        out["warnings"] = [
            "Some fields came back empty, the dashboard layout may have changed: "
            + ", ".join(warnings)
        ]
    return out


def overage(snap: Snapshot) -> dict:
    breakdown = {}
    total = 0.0
    for scope in ("local", "roaming"):
        section = snap.data.get(scope) or {}
        breakdown[scope] = {}
        for name in METRICS:
            raw = (section.get(_LIB_KEYS[name]) or {}).get("excludingPackage")
            eur = parse_euros(raw)
            breakdown[scope][name] = {"raw": raw, "eur": eur}
            total += eur or 0.0
    lib_total = snap.data.get("totalExcludingPackage")
    return {
        "total_eur": round(total, 2),
        "total_raw": lib_total,
        "breakdown": breakdown,
        **_meta(snap),
    }


def carbon_footprint(snap: Snapshot) -> dict:
    raw = ((snap.data.get("local") or {}).get("internet") or {}).get("carbonFootprint")
    return {
        "raw": raw,
        "grams_co2e": parse_carbon_grams(raw),
        "scope": "mobile data on the French network (the dashboard does not report "
        "a footprint for roaming usage)",
        **_meta(snap),
    }


def plan_info(snap: Snapshot) -> dict:
    d = snap.data
    number = d.get("number") or ""
    return {
        "plan_name": d.get("plan"),
        "account_holder": d.get("nameAcount"),
        "identifier": d.get("identifier"),
        "phone_number": number,
        **_meta(snap),
    }


def summary(snap: Snapshot) -> dict:
    loc = usage(snap, "local", "data")
    roam = usage(snap, "roaming", "data")
    return {
        "france_data": loc["data"],
        "roaming_data": roam["data"],
        "overage_total_eur": overage(snap)["total_eur"],
        **_meta(snap),
    }


# ----------------------------------------------------------------- health


# Fields that are always populated on a working dashboard. If any is empty,
# the HTML was "parsed" but the selectors most likely matched the wrong thing.
_CANARIES = [
    ("nameAcount",),
    ("date",),
    ("local", "internet", "conso"),
    ("local", "internet", "consoMax"),
    ("roaming", "internet", "conso"),
    ("roaming", "internet", "consoMax"),
    ("local", "call", "conso"),
    ("local", "SMS", "conso"),
]


def empty_fields(snap: Snapshot, scopes: tuple[str, ...] | None = None) -> list[str]:
    missing = []
    for path in _CANARIES:
        if scopes and len(path) > 1 and path[0] not in scopes:
            continue
        if scopes and len(path) == 1:
            continue
        node: Any = snap.data
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
        if _blank(node):
            missing.append(".".join(path))
    return missing


def unparseable_numbers(snap: Snapshot) -> list[str]:
    """Fields that are present but no longer look like '12.3Go' / '35Go'."""
    bad = []
    for scope in ("local", "roaming"):
        internet = (snap.data.get(scope) or {}).get("internet") or {}
        for key in ("conso", "consoMax"):
            if not _blank(internet.get(key)) and parse_data_gb(internet.get(key)) is None:
                bad.append(f"{scope}.internet.{key}={internet.get(key)!r}")
    return bad
