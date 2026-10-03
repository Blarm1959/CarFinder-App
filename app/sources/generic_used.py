"""Small helpers for approved-used sources whose public stock data varies by site.

The helpers deliberately do not make network requests.  They only normalise
JSON records or server-rendered HTML into a predictable intermediate record.
Manufacturer modules remain responsible for URLs, filtering and commercial
vehicle rules.
"""
from __future__ import annotations

import re
from typing import Any, Iterable
from urllib.parse import urljoin

from bs4 import BeautifulSoup

UK_COMPACT = re.compile(r"^[A-Z]{2}\d{2}[A-Z]{3}$")
NI_COMPACT = re.compile(r"^[A-Z]{1,3}\d{1,4}$")
POSTCODE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\b", re.I)


def compact_plate(value: Any) -> str | None:
    text = re.sub(r"[^A-Z0-9]", "", str(value or "").upper())
    if UK_COMPACT.fullmatch(text) or NI_COMPACT.fullmatch(text):
        return text
    return None


def _key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key or "").lower())


def iter_dicts(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_dicts(child)


def values_for(record: dict[str, Any], *aliases: str) -> list[Any]:
    wanted = {_key(a) for a in aliases}
    found: list[Any] = []
    for obj in iter_dicts(record):
        for k, v in obj.items():
            if _key(k) in wanted and v not in (None, ""):
                found.append(v)
    return found


def first_value(record: dict[str, Any], *aliases: str) -> Any:
    vals = values_for(record, *aliases)
    return vals[0] if vals else None


def find_vehicle_records(payload: Any) -> list[dict[str, Any]]:
    """Return the outermost JSON dicts containing a plausible registration."""
    hits: list[dict[str, Any]] = []
    seen: set[int] = set()
    aliases = ("registration", "registrationNumber", "registrationPlate", "regNumber", "regNo", "vrm", "plate")
    for obj in iter_dicts(payload):
        reg = None
        for value in values_for(obj, *aliases):
            if isinstance(value, (str, int)):
                reg = compact_plate(value)
                if reg:
                    break
        if reg and id(obj) not in seen:
            # Avoid tiny nested registration objects when a parent record exists.
            if len(obj) >= 5:
                hits.append(obj)
                seen.add(id(obj))
    # Drop records wholly contained in a larger hit with the same plate where practical.
    best: dict[str, dict[str, Any]] = {}
    for obj in hits:
        reg = None
        for value in values_for(obj, *aliases):
            reg = compact_plate(value)
            if reg:
                break
        if reg and (reg not in best or len(obj) > len(best[reg])):
            best[reg] = obj
    return list(best.values())


def _text(node: Any) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip() if node is not None else ""


def html_vehicle_blocks(html: str, base_url: str) -> list[dict[str, Any]]:
    """Find rendered vehicle cards by locating registration numbers in the DOM.

    This is intentionally structure-tolerant: Codeweavers and Suzuki have
    changed CSS class names over time while retaining the visible labels.
    """
    soup = BeautifulSoup(html or "", "lxml")
    out: list[dict[str, Any]] = []
    used_nodes: set[int] = set()
    plate_re = re.compile(r"\b(?:[A-Z]{2}\s?\d{2}\s?[A-Z]{3}|[A-Z]{1,3}\s?\d{1,4})\b")
    for node in soup.find_all(string=plate_re):
        raw = str(node)
        m = plate_re.search(raw)
        if not m:
            continue
        reg = compact_plate(m.group(0))
        if not reg:
            continue
        cur = node.parent
        chosen = None
        for _ in range(9):
            if cur is None:
                break
            text = _text(cur)
            if len(text) >= 80 and ("mileage" in text.lower() or " mls" in text.lower()) and ("£" in text or "price" in text.lower()):
                chosen = cur
                # Prefer a reasonably sized card instead of a whole page wrapper.
                if len(text) < 3500:
                    break
            cur = cur.parent
        if chosen is None:
            continue
        # A data-bearing inner <div> often contains mileage/price while its
        # parent card carries the vehicle heading and detail link. Prefer that
        # parent when it is still a sensibly sized card.
        if not chosen.find(["h2", "h3", "h4"]) and chosen.parent is not None:
            parent_text = _text(chosen.parent)
            if chosen.parent.find(["h2", "h3", "h4"]) and len(parent_text) < 3500:
                chosen = chosen.parent
        if id(chosen) in used_nodes:
            continue
        used_nodes.add(id(chosen))
        text = _text(chosen)
        links = [a for a in chosen.find_all("a", href=True) if str(a.get("href") or "").strip()]
        link = next((a for a in links if "detail" in _text(a).lower() or "more" in _text(a).lower()), links[0] if links else None)
        headings = chosen.find_all(["h2", "h3", "h4"])
        title = _text(headings[0]) if headings else (_text(link) if link else "")
        out.append({
            "registration": reg,
            "text": text,
            "title": title,
            "url": urljoin(base_url, str(link.get("href"))) if link else None,
            "html": str(chosen),
        })
    return out


def int_from(text: Any) -> int | None:
    if text in (None, ""):
        return None
    try:
        return int(round(float(text)))
    except (TypeError, ValueError):
        digits = re.sub(r"[^0-9]", "", str(text))
        return int(digits) if digits else None


def labelled(text: str, label: str, stop: str = r"(?:Exterior|Interior|Mileage|Fuel|Transmission|Doors|Power|Capacity|Registration|First registration|Available|Vehicle price|Price)") -> str | None:
    m = re.search(rf"{re.escape(label)}\s*[:\-]?\s*(.+?)(?=\s+{stop}\b|$)", text or "", re.I)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else None
