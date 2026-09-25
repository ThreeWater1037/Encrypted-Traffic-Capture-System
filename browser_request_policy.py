"""Explicit, site-scoped resource exclusions shared by all capture browsers."""

from urllib.parse import urlsplit


FORBES_RECAPTCHA_URL = "https://www.google.com/recaptcha/api2/aframe"
FORBES_RECAPTCHA_REASON = "forbes_recaptcha_exclusion"


def blocked_urls_for_page(url):
    host = (urlsplit(url).hostname or "").lower()
    if host == "forbeschina.com" or host.endswith(".forbeschina.com"):
        return [FORBES_RECAPTCHA_URL]
    return []
