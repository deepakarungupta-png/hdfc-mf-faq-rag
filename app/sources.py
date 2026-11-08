"""Domain rule for official sources, shared by the Collector and the API.

A URL is allowed only if it uses https and its host equals an allow-listed
domain or ends with "." + an allow-listed domain. A plain substring or
endswith(domain) check is deliberately avoided: it would accept lookalikes
such as "evilhdfcfund.com".
"""

from urllib.parse import urlparse

from app.config import load_config


def is_allowed_host(url: str, allowlist: list[str] | None = None) -> bool:
    if allowlist is None:
        allowlist = load_config()["source_allowlist"]
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    # .hostname drops any port and user-info and is already lowercased.
    host = (parsed.hostname or "").rstrip(".")
    if not host:
        return False
    for domain in allowlist:
        domain = domain.lower().strip(".")
        if host == domain or host.endswith("." + domain):
            return True
    return False
