"""Turn the raw strings scraped from the Free Mobile dashboard into numbers.

FreeMobileConso returns everything as display strings ("12.34Go", "0.00€",
"1h05min32s", ...). Agents are much better at reasoning over numbers, so every
helper here returns ``None`` instead of raising when a value can't be parsed:
the raw string is always kept next to the parsed value.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

_NUM = r"(-?\d+(?:[.,]\d+)?)"

# Free displays French units (Go/Mo/Ko); accept English ones too, just in case.
_DATA_UNITS_GB = {
    "to": 1000.0,
    "tb": 1000.0,
    "go": 1.0,
    "gb": 1.0,
    "mo": 1 / 1000,
    "mb": 1 / 1000,
    "ko": 1 / 1_000_000,
    "kb": 1 / 1_000_000,
    "o": 1 / 1_000_000_000,
    "b": 1 / 1_000_000_000,
}


def _to_float(text: str) -> float:
    return float(text.replace(",", ".").replace(" ", "").replace(" ", ""))


def parse_data_gb(value: str | None) -> float | None:
    """'12.34Go' -> 12.34, '512Mo' -> 0.512, '35Go' -> 35.0."""
    if not value:
        return None
    m = re.search(_NUM + r"\s*(To|TB|Go|GB|Mo|MB|Ko|KB|o|B)\b", value, re.IGNORECASE)
    if not m:
        return None
    return round(_to_float(m.group(1)) * _DATA_UNITS_GB[m.group(2).lower()], 6)


def parse_euros(value: str | None) -> float | None:
    """'0.00€' / '1,50 €' -> float."""
    if not value:
        return None
    m = re.search(_NUM + r"\s*€", value) or re.fullmatch(r"\s*" + _NUM + r"\s*", value)
    return _to_float(m.group(1)) if m else None


def parse_duration_seconds(value: str | None) -> int | None:
    """'1h05min32s' -> 3932, '12min' -> 720, '45s' -> 45. None if no unit found."""
    if not value:
        return None
    h = re.search(r"(\d+)\s*h", value)
    mnt = re.search(r"(\d+)\s*m(?:in|n)?(?![a-z])", value)
    s = re.search(r"(\d+)\s*s(?:ec)?\b", value)
    if not (h or mnt or s):
        return None
    return (
        (int(h.group(1)) * 3600 if h else 0)
        + (int(mnt.group(1)) * 60 if mnt else 0)
        + (int(s.group(1)) if s else 0)
    )


def parse_int(value: str | None) -> int | None:
    if not value:
        return None
    m = re.search(r"\d+", value.replace(" ", ""))
    return int(m.group(0)) if m else None


def parse_carbon_grams(value: str | None) -> float | None:
    """'12gCO2e' -> 12.0, '1,2 kg CO2' -> 1200.0."""
    if not value:
        return None
    m = re.search(_NUM + r"\s*(kg|g|t)\b", value, re.IGNORECASE)
    if not m:
        m2 = re.search(_NUM + r"\s*(kg|g|t)(?=\s*co)", value, re.IGNORECASE)
        if not m2:
            return None
        m = m2
    factor = {"g": 1.0, "kg": 1000.0, "t": 1_000_000.0}[m.group(2).lower()]
    return round(_to_float(m.group(1)) * factor, 3)


def parse_dates(value: str | None) -> list[date]:
    """Extract every dd/mm/yyyy date from a string, in order."""
    if not value:
        return []
    out = []
    for d, mth, y in re.findall(r"(\d{1,2})/(\d{1,2})/(\d{4})", value):
        try:
            out.append(date(int(y), int(mth), int(d)))
        except ValueError:
            continue
    return out


def billing_period(value: str | None) -> dict:
    """Interpret the dashboard sub-title (e.g. 'Du 05/11/2026 au 04/12/2026').

    The quota resets the day after the period ends.
    """
    dates = parse_dates(value)
    start = dates[0] if len(dates) >= 2 else None
    end = dates[-1] if dates else None
    return {
        "raw": value,
        "start": start.isoformat() if start else None,
        "end": end.isoformat() if end else None,
        "next_renewal": (end + timedelta(days=1)).isoformat() if end else None,
    }
