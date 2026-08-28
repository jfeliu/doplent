from django.utils import timezone

CA_WEEKDAYS = ["Dilluns", "Dimarts", "Dimecres", "Dijous", "Divendres", "Dissabte", "Diumenge"]


def _local(value):
    """Render times in the school's timezone. DB datetimes come back as UTC
    (USE_TZ), and these helpers bypass Django's template localtime, so convert
    here or a 9:00 absence shows as 7:00 in summer."""
    if timezone.is_aware(value):
        return timezone.localtime(value)
    return value


def format_ca_date(value):
    value = _local(value)
    return f"{CA_WEEKDAYS[value.weekday()]}, {value:%d/%m/%Y}"


def format_ca_datetime(value):
    value = _local(value)
    return f"{format_ca_date(value)} {value:%H:%M}"


def format_ca_timeframe(start, end):
    """A start-end range. When both ends fall on the same day the date is
    written once: "Dilluns, 31/08/2026 11:00 – 12:00". Otherwise both
    ends are spelled out in full."""
    start = _local(start)
    end = _local(end)
    if start.date() == end.date():
        return f"{format_ca_datetime(start)} – {end:%H:%M}"
    return f"{format_ca_datetime(start)} – {format_ca_datetime(end)}"
