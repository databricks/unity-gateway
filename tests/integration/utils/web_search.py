"""Shared request inputs and result URLs for live MCP web-search journeys."""

import re
from urllib.parse import urlparse

WEB_SEARCH_SERVICE = "system.ai.web_search"
WEB_SEARCH_TOOL = "web_search"
OFFICIAL_WEBSITE_QUERY = "Databricks official website"


def official_website_urls(text: str) -> set[str]:
    return {
        url
        for url in re.findall(r"https?://[^\s<>\"'\\)\]]+", text)
        if urlparse(url).hostname in {"databricks.com", "www.databricks.com"}
    }
