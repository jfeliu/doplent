from django import template

from ..dates_ca import format_ca_date, format_ca_datetime

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
