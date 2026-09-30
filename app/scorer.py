"""Vehicle scoring helpers used by the CarFinder shortlist.

The CarFinder score is intentionally personal and explainable.  It reflects the
current buying priorities for this project rather than a generic market rating:
workflow interest first, realistic local viewing, low annual mileage, sensible
price, and only a small bonus for age.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any


MAX_POINTS = {
    "interest": 25,
    "reachability": 30,
    "mileage_per_year": 25,
    "price": 15,
    "age": 5,
}



CURRENT_YEAR = date.today().year


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None




def _text(value: Any, default: str = "") -> str:
    """Return a safe stripped string for user-entered/database values.

    Pandas can pass missing SQLite values into scoring as float NaN rather than
    None.  Scoring helpers should therefore never assume text fields support
    .strip().
    """
    if value is None:
        return default
    try:
        if value != value:  # NaN is not equal to itself.
            return default
    except Exception:
        return default
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "<na>"}:
        return default
    return text

def _date_or_none(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def interest_score(interest_status: str | None) -> int:
    return {
        "interested": 25,
        "rejected": 0,
    }.get(_text(interest_status).lower(), 12)


def reachability_score(reachability_status: str | None) -> int:
    return {
        "LOCAL": 30,
        "TRANSFERABLE": 28,
        "REMOTE": 5,
    }.get(_text(reachability_status, "REMOTE").upper(), 5)


def mileage_per_year_score(miles_per_year: Any) -> int:
    miles = _int_or_none(miles_per_year)
    if miles is None:
        return 8
    if miles <= 4000:
        return 25
    if miles <= 5000:
        return 23
    if miles <= 6000:
        return 22
    if miles <= 7000:
        return 20
    if miles <= 8000:
        return 18
    if miles <= 9000:
        return 15
    if miles <= 10000:
        return 12
    if miles <= 12000:
        return 8
    if miles <= 15000:
        return 4
    return 0


def price_score(price: Any, min_price: Any = None, max_price: Any = None) -> int:
    """Score price relative to the current result set when possible."""
    price_int = _int_or_none(price)
    if price_int is None:
        return 6

    low = _int_or_none(min_price)
    high = _int_or_none(max_price)
    if low is not None and high is not None and high > low:
        relative = (high - price_int) / (high - low)
        return max(0, min(15, int(round(relative * 15))))

    # Fallback bands keep tests and single-row views deterministic.
    if price_int <= 14500:
        return 15
    if price_int <= 15500:
        return 12
    if price_int <= 16500:
        return 9
    if price_int <= 17500:
        return 6
    if price_int <= 18500:
        return 3
    return 0


def age_score(year: Any = None, registration_date: Any = None) -> int:
    """Small tie-breaker only: newer is good, but not at the expense of value."""
    reg_date = _date_or_none(registration_date)
    if reg_date is not None:
        age_months = max(0, (date.today().year - reg_date.year) * 12 + date.today().month - reg_date.month)
        if age_months <= 12:
            return 5
        if age_months <= 24:
            return 4
        if age_months <= 36:
            return 3
        if age_months <= 48:
            return 2
        return 1

    year_int = _int_or_none(year)
    if year_int is None:
        return 2
    age_years = max(0, CURRENT_YEAR - year_int)
    if age_years <= 1:
        return 5
    if age_years == 2:
        return 4
    if age_years == 3:
        return 3
    if age_years == 4:
        return 2
    return 1


def interest_detail(interest_status: Any) -> str:
    status = _text(interest_status).lower()
    if status == "interested":
        return "Marked interested"
    if status == "rejected":
        return "Marked not suitable"
    return "Not reviewed yet"


def reachability_detail(reachability_status: Any) -> str:
    status = _text(reachability_status, "REMOTE").upper()
    if status == "LOCAL":
        return "Already local"
    if status == "TRANSFERABLE":
        return "Transferable to a local branch"
    return "Remote dealer / no local route configured"


def mileage_per_year_detail(miles_per_year: Any) -> str:
    miles = _int_or_none(miles_per_year)
    if miles is None:
        return "Mileage per year unknown"
    return f"{miles:,} miles/year"


def price_detail(price: Any, min_price: Any = None, max_price: Any = None) -> str:
    price_int = _int_or_none(price)
    if price_int is None:
        return "Price unknown"

    low = _int_or_none(min_price)
    high = _int_or_none(max_price)
    if low is not None and high is not None and high > low:
        if price_int <= low:
            return f"£{price_int:,}, cheapest in current results"
        if price_int >= high:
            return f"£{price_int:,}, highest in current results"
        return f"£{price_int:,}, scored relative to £{low:,}-£{high:,} current range"
    return f"£{price_int:,}"


def age_detail(year: Any = None, registration_date: Any = None) -> str:
    reg_date = _date_or_none(registration_date)
    if reg_date is not None:
        return f"Estimated registered {reg_date:%b %Y}"
    year_int = _int_or_none(year)
    if year_int is not None:
        return f"Model/listing year {year_int}"
    return "Age unknown"


def carfinder_headline(row: dict[str, Any]) -> str:
    """Short reason to show beside a high-scoring car."""
    breakdown = carfinder_score_breakdown(row)
    earned = {item["factor"]: item["points"] for item in breakdown}

    if earned.get("Mileage / year", 0) >= 23 and earned.get("Price", 0) >= 12:
        return "Outstanding value"
    if earned.get("Mileage / year", 0) >= 23:
        return "Very low mileage"
    if earned.get("Price", 0) >= 13:
        return "Strong price"
    if earned.get("Reachability", 0) >= 28:
        return "Easy to view locally"
    if earned.get("Age", 0) >= 4:
        return "Newer example"
    return carfinder_band(calculate_carfinder_score(row))


def carfinder_deductions(row: dict[str, Any], limit: int = 3) -> list[str]:
    """Return the main reasons a car did not score higher."""
    losses = []
    for item in carfinder_score_breakdown(row):
        lost = int(item["max_points"] - item["points"])
        if lost > 0:
            losses.append((lost, f"{item['factor']}: lost {lost} point{'s' if lost != 1 else ''} — {item['detail']}"))
    losses.sort(reverse=True, key=lambda x: x[0])
    return [text for _, text in losses[:limit]]


def carfinder_breakdown_text(row: dict[str, Any]) -> str:
    parts = []
    for item in carfinder_score_breakdown(row):
        parts.append(f"{item['factor']} +{item['points']}/{item['max_points']} ({item['detail']})")
    return " · ".join(parts)


def carfinder_score_breakdown(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the component scores that make up the CarFinder score.

    Keeping this as structured data means the UI, export, tests, and any future
    recommendation screen can all explain the same score without duplicating
    scoring rules.
    """
    return [
        {
            "factor": "Interest",
            "points": interest_score(row.get("interest_status")),
            "max_points": MAX_POINTS["interest"],
            "detail": interest_detail(row.get("interest_status")),
        },
        {
            "factor": "Reachability",
            "points": reachability_score(row.get("reachability_status")),
            "max_points": MAX_POINTS["reachability"],
            "detail": reachability_detail(row.get("reachability_status")),
        },
        {
            "factor": "Mileage / year",
            "points": mileage_per_year_score(row.get("miles_per_year")),
            "max_points": MAX_POINTS["mileage_per_year"],
            "detail": mileage_per_year_detail(row.get("miles_per_year")),
        },
        {
            "factor": "Price",
            "points": price_score(row.get("price_current"), row.get("price_min"), row.get("price_max")),
            "max_points": MAX_POINTS["price"],
            "detail": price_detail(row.get("price_current"), row.get("price_min"), row.get("price_max")),
        },
        {
            "factor": "Age",
            "points": age_score(row.get("year"), row.get("estimated_registration_date")),
            "max_points": MAX_POINTS["age"],
            "detail": age_detail(row.get("year"), row.get("estimated_registration_date")),
        },
    ]


def calculate_carfinder_score(row: dict[str, Any]) -> int:
    """Return an explainable 1-100 score for a VW Polo candidate."""
    total = sum(item["points"] for item in carfinder_score_breakdown(row))
    return max(1, min(100, int(total)))


def carfinder_band(score: Any) -> str:
    score_int = _int_or_none(score)
    if score_int is None:
        return "Unscored"
    if score_int >= 90:
        return "Exceptional"
    if score_int >= 82:
        return "Excellent"
    if score_int >= 74:
        return "Very strong"
    if score_int >= 65:
        return "Strong"
    if score_int >= 50:
        return "Worth checking"
    return "Low priority"


def confidence_score(row: dict[str, Any]) -> int:
    """How much confidence to place in the score, separate from desirability."""
    score = 0

    if _int_or_none(row.get("miles_per_year")) is not None:
        score += 25
    if _int_or_none(row.get("price_current")) is not None:
        score += 20

    sensor = _text(row.get("sensor_status"), "unknown").lower()
    if sensor == "front_rear":
        score += 25
    elif sensor in {"single", "none"}:
        score += 15
    else:
        score += 5

    photo = _text(row.get("photo_status"), "unknown").lower()
    if photo == "photos":
        score += 15
    elif photo == "awaiting":
        score += 5

    if (_int_or_none(row.get("checked")) or 0):
        score += 15

    return max(0, min(100, score))


def confidence_band(score: Any) -> str:
    score_int = _int_or_none(score)
    if score_int is None:
        return "Unknown"
    if score_int >= 80:
        return "High"
    if score_int >= 55:
        return "Medium"
    return "Low"


def carfinder_reason(row: dict[str, Any]) -> str:
    score = calculate_carfinder_score(row)
    parts = [f"{score}/100 {carfinder_band(score)}", carfinder_headline(row)]
    parts.extend(
        f"{item['factor']} +{item['points']}"
        for item in carfinder_score_breakdown(row)
    )
    deductions = carfinder_deductions(row, limit=2)
    if deductions:
        parts.append("Main deductions: " + "; ".join(deductions))
    return " · ".join(parts)


def confidence_reason(row: dict[str, Any]) -> str:
    score = confidence_score(row)
    parts = [f"{confidence_band(score)} confidence ({score}/100)"]

    sensor = _text(row.get("sensor_status"), "unknown").lower()
    if sensor == "front_rear":
        parts.append("sensors confirmed")
    elif sensor == "unknown":
        parts.append("sensors still unknown")
    else:
        parts.append("sensor status recorded")

    photo = _text(row.get("photo_status"), "unknown").lower()
    if photo == "photos":
        parts.append("photos available")
    elif photo == "awaiting":
        parts.append("awaiting photos")
    else:
        parts.append("photo status unknown")

    if (_int_or_none(row.get("checked")) or 0):
        parts.append("advert checked")
    else:
        parts.append("advert not checked")

    return " · ".join(parts)


# Backwards-compatible aliases for older call sites/tests.
calculate_best_buy_score = calculate_carfinder_score
best_buy_band = carfinder_band
best_buy_reason = carfinder_reason
