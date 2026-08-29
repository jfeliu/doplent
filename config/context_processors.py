"""A throwaway UI-theme switcher for trying different looks on the real pages.

Staff pick a theme from the navbar; the choice rides in a cookie. `default` is
the app's own hand-tuned styling; the rest are drop-in Bootswatch rebuilds of
Bootstrap 5.3, loaded from the same CDN.
"""

UI_THEME_COOKIE = "ui_theme"

# key -> label. Keys other than "default" must be Bootswatch 5.3 theme names.
UI_THEMES = {
    "default": "Doplent",
    "zephyr": "Zephyr",
    "litera": "Litera",
    "minty": "Minty",
    "lux": "Lux",
    "sandstone": "Sandstone",
    "pulse": "Pulse",
    "cosmo": "Cosmo",
}

_BOOTSTRAP = "https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css"
_BOOTSWATCH = "https://cdn.jsdelivr.net/npm/bootswatch@5.3.3/dist/{key}/bootstrap.min.css"


def ui_theme(request):
    # ?ui_theme=<key> wins over the cookie, so a theme can be linked or previewed
    # without committing to it.
    key = request.GET.get(UI_THEME_COOKIE) or request.COOKIES.get(UI_THEME_COOKIE, "default")
    if key not in UI_THEMES:
        key = "default"
    return {
        "ui_theme": key,
        "ui_themes": UI_THEMES,
        "ui_bootstrap_href": _BOOTSTRAP if key == "default" else _BOOTSWATCH.format(key=key),
    }
