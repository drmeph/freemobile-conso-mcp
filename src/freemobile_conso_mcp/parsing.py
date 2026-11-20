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


_FR_MONTHS = {
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
    "juillet": 7, "août": 8, "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11,
    "décembre": 12, "decembre": 12,
}


def parse_french_date(value: str | None) -> date | None:
    """'18 décembre 2026' (first one found) -> date(2026, 12, 18)."""
    if not value:
        return None
    for d, month, y in re.findall(r"(\d{1,2})(?:er)?\s+([a-zéû]+)\s+(\d{4})", value, re.IGNORECASE):
        mth = _FR_MONTHS.get(month.lower())
        if mth:
            try:
                return date(int(y), mth, int(d))
            except ValueError:
                continue
    return None


def _nearest_year(day: int, month: int, anchor: date) -> date | None:
    """dd/mm without a year: the date closest to `anchor`."""
    best = None
    for year in (anchor.year - 1, anchor.year, anchor.year + 1):
        try:
            cand = date(year, month, day)
        except ValueError:
            continue
        if best is None or abs((cand - anchor).days) < abs((best - anchor).days):
            best = cand
    return best


def billing_period(value: str | None) -> dict:
    """Interpret the billing-period text.

    Legacy dashboard: 'Suivi conso du 05/11/2026 au 04/12/2026' (the quota
    resets the day after the period ends).
    v2 site: 'Période du 18/11 au 18/12. Votre forfait sera remis à zéro le
    18 décembre 2026' (no year on the period; the reset date is explicit).
    """
    dates = parse_dates(value)
    reset = parse_french_date(value)
    if not dates and value:
        anchor = reset or date.today()
        short = re.findall(r"(?<![/\d])(\d{1,2})/(\d{1,2})(?![/\d])", value)
        parsed = [_nearest_year(int(d), int(m), anchor) for d, m in short]
        dates = [d for d in parsed if d is not None]
        if len(dates) >= 2 and dates[0] > dates[-1]:  # period straddles new year
            dates[0] = dates[0].replace(year=dates[0].year - 1)
    start = dates[0] if len(dates) >= 2 else None
    end = dates[-1] if dates else None
    renewal = reset or (end + timedelta(days=1) if end else None)
    return {
        "raw": value,
        "start": start.isoformat() if start else None,
        "end": end.isoformat() if end else None,
        "next_renewal": renewal.isoformat() if renewal else None,
    }
