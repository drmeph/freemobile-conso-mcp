"""Build redacted test fixtures from pages saved by probe_v2_login.py.

    python scripts/make_v2_fixtures.py [probe_out] [tests/fixtures]

The raw pages contain personal data all over their RSC payload (call log with
numbers and destinations, invoices, phone model, e-mail addresses), so this
does not copy them wholesale. It keeps Free's real markup and RSC rows for
exactly the parts the parser reads (identity header, out-of-plan cards,
consumption blocks, billing period, plan card, carbon footprint), replaces
identity and usage figures with fixed fake values, and refuses to write a
fixture if anything that looks personal survives.
"""

from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup, Comment

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from freemobile_conso_mcp.client import rsc_rows  # noqa: E402

FAKE_NAME = ("Jean", "Dupont")
FAKE_LOGIN = "12345678"
FAKE_MSISDN = "06 12 34 56 78"

# Fake usage, by consumption block title and scope.
FAKE_USAGE = {
    "Internet": {
        "national": ([("Restant", "301,73 Go")], "48,27 Go / 350 Go", 13.79),
        "roaming": ([("Restant", "22,5 Go")], "12,5 Go / 35 Go", 35.71),
    },
    "Appels": {
        "national": ([("Vers la France", "1h 02min 10s"), ("Vers l'international", "10min 30s")], None, None),
        "roaming": ([("Émis", "20min 5s"), ("Reçus", "5min")], None, None),
    },
    "SMS/MMS": {
        "national": ([("SMS", "37"), ("MMS", "2")], None, None),
        "roaming": ([("SMS", "4"), ("MMS", "0")], None, None),
    },
}
FAKE_OVERAGE = {
    "Hors-forfait en France": ("0,00€", {"Internet": "0,00€", "Appels": "0,00€", "SMS": "0,00€", "MMS": "0,00€"}),
    "Hors-forfait à l'étranger": ("1,70€", {"Internet": "0,00€", "Appels": "1,50€", "SMS": "0,20€", "MMS": "0,00€"}),
}
FAKE_CARBON = "1205g CO2e"
FAKE_PERIOD = ("05/11", "05/12", "5 décembre 2026")


def _norm(s: str) -> str:
    return " ".join(s.replace("’", "'").split())


def _identity(soup: BeautifulSoup) -> list[str]:
    real = []
    for el_id in ("user-name", "user-login", "user-msisdn"):
        el = soup.find(id=el_id)
        if el is None:
            sys.exit(f"#{el_id} not found: is this a logged-in page?")
        text = _norm(el.get_text(" ", strip=True))
        real += [text, text.replace(" ", "")] + (text.split() if el_id == "user-name" else [])
    return [r for r in real if len(r) >= 2]


def _header(soup: BeautifulSoup) -> str:
    block = copy.copy(soup.find(id="user-name").find_parent("div").find_parent("div"))
    block.find(id="user-name").string = " ".join(FAKE_NAME)
    block.find(id="user-login").string = FAKE_LOGIN
    block.find(id="user-msisdn").string = FAKE_MSISDN
    return str(block)


def _rsc_text(rows: list) -> str:
    return "".join(f"{i:x}:{json.dumps(row, ensure_ascii=False, separators=(',', ':'))}\n" for i, row in enumerate(rows, 1))


def _page(title: str, source: str, body: str, rows: list) -> str:
    push = json.dumps([1, _rsc_text(rows)], ensure_ascii=False)
    return (
        '<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8">'
        f"<title>{title}</title></head><body>\n"
        f"<!-- Redacted, trimmed copy of https://mobile.free.fr/account/v2/{source} "
        "built by scripts/make_v2_fixtures.py. Markup and RSC rows are Free's; "
        "identity and usage values are fake. -->\n"
        f"{body}\n"
        "<script>(self.__next_f=self.__next_f||[]).push([0])</script>"
        f"<script>self.__next_f.push({push})</script>\n</body></html>\n"
    )


def build_usage(html: str) -> tuple[str, list[str]]:
    soup = BeautifulSoup(html, "html.parser")
    real = _identity(soup)

    blocks = []
    for node in _walk(rsc_rows(html)):
        desc = node.get("consumptionDescription")
        if not isinstance(desc, dict) or desc.get("title") not in FAKE_USAGE:
            continue
        desc = copy.deepcopy(desc)
        for scope, (values, gauge, pct) in FAKE_USAGE[desc["title"]].items():
            desc[scope] = [{"label": label, "value": value} for label, value in values]
            if gauge is not None:
                desc["consumptionsData"][scope] = {"label": gauge, "usedConsumption": pct}
        empty = {"voice": [], "sms": [], "mms": []}
        blocks.append(["$", "$L47", None, {"consumptionDescription": desc,
                                            "userDetailsConsumption": {"national": empty, "roaming": empty}}])
    if len(blocks) != 3:
        sys.exit(f"expected 3 consumption blocks, found {len(blocks)}")

    cards = []
    for h3 in soup.find_all("h3"):
        title = _norm(h3.get_text(" ", strip=True))
        if title not in FAKE_OVERAGE:
            continue
        total, lines = FAKE_OVERAGE[title]
        card = copy.copy(h3.parent.parent)
        card.find("h3").find_next_sibling("p").string = total
        for row in card.find_all("div"):
            ps = row.find_all("p", recursive=False)
            if len(ps) == 2 and _norm(ps[0].get_text()) in lines:
                ps[1].string = lines[_norm(ps[0].get_text())]
        cards.append(str(card))
    if len(cards) != 2:
        sys.exit(f"expected 2 out-of-plan cards, found {len(cards)}")

    body = f"<header>{_header(soup)}</header>\n<main><h1>Ma consommation mobile</h1>\n" + "\n".join(cards) + "</main>"
    return _page("Ma consommation mobile", "consommation-mobile", body, blocks), real


def build_home(html: str) -> tuple[str, list[str]]:
    soup = BeautifulSoup(html, "html.parser")
    real = _identity(soup)

    link = soup.find(string=re.compile("Voir mon offre"))
    plan = str(link.find_parent("a").find_parent("div").find_parent("div"))

    period_el = soup.find(string=re.compile(r"P[ée]riode du")).find_parent("p")
    period = copy.copy(period_el)
    texts = [t for t in period.find_all(string=True) if not isinstance(t, Comment)]
    shorts = iter(FAKE_PERIOD[:2])
    for t in texts:
        new = re.sub(r"\b\d{1,2}/\d{1,2}\b", lambda m: next(shorts), str(t))
        new = re.sub(r"\d{1,2}(?:er)? [a-zéû]+ \d{4}", FAKE_PERIOD[2], new)
        t.replace_with(new)

    carbon_label = soup.find(string=re.compile("empreinte carbone est de"))
    carbon = copy.copy(carbon_label.find_parent("div"))
    for t in carbon.find_all(string=re.compile(r"CO2")):
        t.replace_with(re.sub(r"[\d.,]+\s*k?g\s*CO2e?", FAKE_CARBON, str(t)))

    body = (
        f"<header>{_header(soup)}</header>\n<main>\n{plan}\n<div>{period}</div>\n{carbon}\n</main>"
    )
    rows = [["$", "$L47", None, {"carbonFootprint": FAKE_CARBON}]]
    return _page("Accueil - Espace Abonné Free Mobile", "", body, rows), real


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


_ALLOWED_NUMBERS = {FAKE_LOGIN, FAKE_MSISDN.replace(" ", "")}


def check_clean(name: str, text: str, real: list[str]) -> None:
    problems = [f"identity {r!r}" for r in set(real) if r in text]
    problems += [f"e-mail {m!r}" for m in re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", text)]
    problems += [f"masked number {m!r}" for m in re.findall(r"\d\d \d\d \d\d XX XX", text)]
    for m in re.findall(r"\b0[1-9](?:[ .]?\d\d){4}\b", text):
        if m.replace(" ", "").replace(".", "") not in _ALLOWED_NUMBERS:
            problems.append(f"phone {m!r}")
    problems += [f"long number {m!r}" for m in re.findall(r"\b\d{8,}\b", text) if m not in _ALLOWED_NUMBERS]
    problems += [f"term {m!r}" for m in re.findall(r"iPhone|Galaxy|Pixel|invoice/|otpId|uuid", text)]
    if problems:
        sys.exit(f"{name}: refusing to write, possible personal data left: {sorted(set(problems))}")


def main() -> None:
    src = Path(sys.argv[1] if len(sys.argv) > 1 else "probe_out")
    dst = Path(sys.argv[2] if len(sys.argv) > 2 else "tests/fixtures")
    dst.mkdir(parents=True, exist_ok=True)
    for name, builder, source in (
        ("v2_consommation.html", build_usage, "consommation-mobile.html"),
        ("v2_home.html", build_home, "home.html"),
    ):
        html, real = builder((src / source).read_text(encoding="utf-8"))
        check_clean(name, html, real)
        (dst / name).write_text(html, encoding="utf-8")
        print(f"wrote {dst / name} ({len(html)} bytes)")


if __name__ == "__main__":
    main()
