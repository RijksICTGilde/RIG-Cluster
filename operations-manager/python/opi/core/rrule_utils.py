"""Shared RRULE parsing and formatting utilities.

Centralizes RRULE string handling used by the backup scheduler,
form converters, and template filters.

Schedule format is RRULE (RFC 5545 subset):
    FREQ=DAILY;BYHOUR=2;BYMINUTE=0
    FREQ=WEEKLY;BYHOUR=3;BYMINUTE=0;BYDAY=SU
    FREQ=MONTHLY;BYHOUR=4;BYMINUTE=0;BYMONTHDAY=1
"""

from __future__ import annotations

# Dutch labels for frequencies and day names
FREQ_LABELS: dict[str, str] = {
    "DAILY": "Dagelijks",
    "WEEKLY": "Wekelijks",
    "MONTHLY": "Maandelijks",
}

DAY_LABELS: dict[str, str] = {
    "MO": "maandag",
    "TU": "dinsdag",
    "WE": "woensdag",
    "TH": "donderdag",
    "FR": "vrijdag",
    "SA": "zaterdag",
    "SU": "zondag",
}


def parse_rrule(rrule: str | None) -> dict[str, str]:
    """Parse an RRULE string into key-value parts.

    >>> parse_rrule("FREQ=DAILY;BYHOUR=2;BYMINUTE=0")
    {'FREQ': 'DAILY', 'BYHOUR': '2', 'BYMINUTE': '0'}
    >>> parse_rrule(None)
    {}
    """
    if not rrule or not isinstance(rrule, str):
        return {}
    parts: dict[str, str] = {}
    for segment in rrule.split(";"):
        if "=" in segment:
            key, _, val = segment.partition("=")
            parts[key.strip().upper()] = val.strip()
    return parts


def build_rrule(freq: str, hour: int = 2, minute: int = 0, byday: str = "", bymonthday: str = "") -> str:
    """Build an RRULE string from components.

    >>> build_rrule("DAILY", hour=3, minute=30)
    'FREQ=DAILY;BYHOUR=3;BYMINUTE=30'
    >>> build_rrule("WEEKLY", byday="MO")
    'FREQ=WEEKLY;BYHOUR=2;BYMINUTE=0;BYDAY=MO'
    """
    if not freq:
        return ""
    parts = [f"FREQ={freq.upper()}", f"BYHOUR={hour}", f"BYMINUTE={minute}"]
    if freq.upper() == "WEEKLY" and byday:
        parts.append(f"BYDAY={byday.upper()}")
    if freq.upper() == "MONTHLY" and bymonthday:
        parts.append(f"BYMONTHDAY={bymonthday}")
    return ";".join(parts)


def format_rrule(rrule: str | None) -> str:
    """Format an RRULE string into a human-readable Dutch label.

    >>> format_rrule("FREQ=DAILY;BYHOUR=2;BYMINUTE=0")
    'Dagelijks rond 02:00'
    >>> format_rrule("FREQ=WEEKLY;BYHOUR=3;BYMINUTE=0;BYDAY=MO")
    'Wekelijks op maandag rond 03:00'
    >>> format_rrule(None)
    'Geen'
    """
    if not rrule or not isinstance(rrule, str) or "FREQ=" not in rrule:
        return "Geen" if not rrule else str(rrule)

    parts = parse_rrule(rrule)
    freq = parts.get("FREQ", "")
    label = FREQ_LABELS.get(freq, freq)

    hour_str = parts.get("BYHOUR", "2")
    minute_str = parts.get("BYMINUTE", "0")
    safe_hour = int(hour_str) if str(hour_str).isdigit() else 2
    safe_minute = int(minute_str) if str(minute_str).isdigit() else 0
    time_str = f"{safe_hour:02d}:{safe_minute:02d}"

    if freq == "WEEKLY":
        day = parts.get("BYDAY", "")
        day_str = DAY_LABELS.get(day, day)
        return f"{label} op {day_str} rond {time_str}"
    if freq == "MONTHLY":
        monthday = parts.get("BYMONTHDAY", "1")
        return f"{label} op dag {monthday} rond {time_str}"
    return f"{label} rond {time_str}"
