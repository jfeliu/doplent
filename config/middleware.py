from django.conf import settings
from django.utils import translation
from django.utils.cache import patch_vary_headers
from django.utils.deprecation import MiddlewareMixin


class DefaultToCatalanLocaleMiddleware(MiddlewareMixin):
    """Drop-in replacement for django.middleware.locale.LocaleMiddleware that
    never consults the browser's Accept-Language header. Every visitor gets
    settings.LANGUAGE_CODE (Catalan) unless they've explicitly picked another
    language in the switcher - django.views.i18n.set_language records that
    choice in the LANGUAGE_COOKIE_NAME cookie, which we still honour.
    """

    def process_request(self, request):
        lang = request.COOKIES.get(settings.LANGUAGE_COOKIE_NAME)
        if not lang or not translation.check_for_language(lang):
            lang = settings.LANGUAGE_CODE
        translation.activate(lang)
        request.LANGUAGE_CODE = translation.get_language()

    def process_response(self, request, response):
        patch_vary_headers(response, ("Cookie",))
        response.setdefault("Content-Language", translation.get_language())
        translation.deactivate()
        return response
