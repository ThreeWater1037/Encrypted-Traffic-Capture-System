"""Explicit, site-scoped resource exclusions shared by all capture browsers."""

from urllib.parse import urlsplit


FORBES_RECAPTCHA_URL = "https://www.google.com/recaptcha/api2/aframe"
FORBES_RECAPTCHA_REASON = "forbes_recaptcha_exclusion"
FORBES_ANALYTICS_URL = "https://www.google-analytics.com/g/collect"
FORBES_ANALYTICS_REASON = "forbes_analytics_exclusion"


def blocked_urls_for_page(url):
    host = (urlsplit(url).hostname or "").lower()
    if host == "forbeschina.com" or host.endswith(".forbeschina.com"):
        return [FORBES_RECAPTCHA_URL, FORBES_ANALYTICS_URL]
    return []


def blocked_request_reason(url, rules):
    """Only analytics ignores its query; the reCAPTCHA rule stays exact."""
    for rule in rules:
        if rule == FORBES_ANALYTICS_URL:
            actual, expected = urlsplit(url), urlsplit(rule)
            if (actual.scheme, actual.netloc, actual.path) == (expected.scheme, expected.netloc, expected.path):
                return FORBES_ANALYTICS_REASON
        elif url == rule:
            return FORBES_RECAPTCHA_REASON
    return None


def cdp_block_patterns(rules):
    return [{"urlPattern": pattern, "requestStage": "Request"}
            for rule in rules for pattern in
            ([rule, rule + "?*"] if rule == FORBES_ANALYTICS_URL else [rule])]


def bidi_block_patterns(rules):
    patterns = []
    for rule in rules:
        if rule == FORBES_ANALYTICS_URL:
            parts = urlsplit(rule)
            # BiDi rejects an empty input port. Spell out the scheme default;
            # omitting port would also match unrelated non-default ports.
            port = parts.port if parts.port is not None else {"https": 443, "http": 80}[parts.scheme]
            patterns.append({"type": "pattern", "protocol": parts.scheme,
                             "hostname": parts.hostname, "port": str(port),
                             "pathname": parts.path})
        else:
            patterns.append({"type": "string", "pattern": rule})
    return patterns
