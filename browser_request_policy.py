"""Explicit, site-scoped resource exclusions shared by all capture browsers."""

from urllib.parse import urlsplit


FORBES_RECAPTCHA_URL = "https://www.google.com/recaptcha/api2/aframe"
FORBES_RECAPTCHA_REASON = "forbes_recaptcha_exclusion"
FORBES_ANALYTICS_URL = "https://www.google-analytics.com/g/collect"
FORBES_ANALYTICS_REASON = "forbes_analytics_exclusion"
FORBES_GOOGLE_ANALYTICS_URL = "https://www.google.com/g/collect"
# Specific endpoints observed unfinished or failed in the Forbes captures.
# Keep versioned iframe paths explicit; do not exclude entire ad domains.
FORBES_AD_URLS = (
    "https://googleads.g.doubleclick.net/pagead/ads",
    "https://googleads.g.doubleclick.net/pagead/html/r20260923/r20190131/zrt_lookup.html",
    "https://ep2.adtrafficquality.google/sodar/sodar2/255/runner.html",
    "https://ep1.adtrafficquality.google/pagead/sodar",
)
FORBES_AD_REASON = "forbes_ad_exclusion"


def _query_rule_reasons():
    return {FORBES_ANALYTICS_URL: FORBES_ANALYTICS_REASON,
            FORBES_GOOGLE_ANALYTICS_URL: FORBES_ANALYTICS_REASON,
            **dict.fromkeys(FORBES_AD_URLS, FORBES_AD_REASON)}


def blocked_urls_for_page(url):
    host = (urlsplit(url).hostname or "").lower()
    if host == "forbeschina.com" or host.endswith(".forbeschina.com"):
        return [FORBES_RECAPTCHA_URL, *_query_rule_reasons()]
    return []


def blocked_request_reason(url, rules):
    """Match listed telemetry/ad endpoints ignoring queries; reCAPTCHA stays exact."""
    query_rules = _query_rule_reasons()
    for rule in rules:
        if rule in query_rules:
            actual, expected = urlsplit(url), urlsplit(rule)
            if (actual.scheme, actual.netloc, actual.path) == (expected.scheme, expected.netloc, expected.path):
                return query_rules[rule]
        elif url == rule:
            return FORBES_RECAPTCHA_REASON
    return None


def cdp_block_patterns(rules):
    return [{"urlPattern": pattern, "requestStage": "Request"}
            for rule in rules for pattern in
            ([rule, rule + "?*"] if rule in _query_rule_reasons() else [rule])]


def bidi_block_patterns(rules):
    patterns = []
    for rule in rules:
        if rule in _query_rule_reasons():
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
