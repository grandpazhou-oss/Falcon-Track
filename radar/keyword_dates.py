"""Publication dates and future event dates have different meanings."""
from datetime import date


def recent_publication(value, today):
    try:
        published = date.fromisoformat(str(value))
    except ValueError:
        return False
    age = (today - published).days
    return age >= 0 and (published.year == today.year or age <= 183)
