"""Date/time helpers: ISO parsing and day/week scope math."""
import datetime as dt


def _parse_iso(s):
    if isinstance(s, dt.datetime):
        return s if s.tzinfo else s.replace(tzinfo=dt.timezone.utc)
    d = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def parse_date(s, tz):
    if not s or s in ("now", "today", "."):
        return dt.datetime.now(tz).date()
    return dt.date.fromisoformat(s)


def week_days(d):
    monday = d - dt.timedelta(days=d.weekday())
    return [monday + dt.timedelta(days=i) for i in range(7)]
