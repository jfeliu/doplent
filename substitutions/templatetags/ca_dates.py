from django import template

from ..dates_ca import format_ca_date, format_ca_datetime, format_ca_timeframe

register = template.Library()


@register.filter
def ca_date(value):
    if value is None:
        return ""
    return format_ca_date(value)


@register.filter
def ca_datetime(value):
    if value is None:
        return ""
    return format_ca_datetime(value)


@register.filter
def ca_timeframe(start, end):
    if start is None or end is None:
        return ""
    return format_ca_timeframe(start, end)
