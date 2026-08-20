CA_WEEKDAYS = ["Dilluns", "Dimarts", "Dimecres", "Dijous", "Divendres", "Dissabte", "Diumenge"]


def format_ca_date(value):
    return f"{CA_WEEKDAYS[value.weekday()]}, {value:%d/%m/%Y}"


def format_ca_datetime(value):
    return f"{format_ca_date(value)} {value:%H:%M}"
