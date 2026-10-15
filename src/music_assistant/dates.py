"""Calcolo delle finestre temporali (mese precedente) e filtro sulle date di uscita."""
from calendar import monthrange
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


def month_window(year: int, month: int) -> tuple[date, date]:
    """Primo e ultimo giorno di un mese (gestisce febbraio e anni bisestili)."""
    last_day = monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last_day)


def previous_month_window(tz_name: str, today: date | None = None) -> tuple[date, date]:
    """Mese precedente rispetto a oggi, calcolato nel fuso orario indicato."""
    if today is None:
        today = datetime.now(ZoneInfo(tz_name)).date()
    last_day_prev = today.replace(day=1) - timedelta(days=1)
    return month_window(last_day_prev.year, last_day_prev.month)


def release_in_window(release_date: str, precision: str, start: date, end: date) -> bool:
    """True se la data di uscita cade nella finestra.

    Spotify può dare la data come 'YYYY-MM-DD', 'YYYY-MM' o 'YYYY'.
    Con precisione 'anno' non possiamo sapere il mese: la release viene scartata.
    """
    try:
        if precision == "day":
            return start <= date.fromisoformat(release_date) <= end
        if precision == "month":
            year, month = (int(part) for part in release_date.split("-")[:2])
            m_start, m_end = month_window(year, month)
            return m_start <= end and m_end >= start
    except (ValueError, TypeError):
        return False
    return False