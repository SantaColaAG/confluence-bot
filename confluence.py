import logging
import os
from typing import Any

import httpx

log = logging.getLogger("confluence")


class ConfluenceClient:
    def __init__(self, base_url: str, pat: str):
        self.base_url = base_url
        proxy = (
            os.environ.get("HTTPS_PROXY")
            or os.environ.get("https_proxy")
            or os.environ.get("HTTP_PROXY")
            or os.environ.get("http_proxy")
        )
        if proxy:
            log.info("using proxy=%s", proxy)
        else:
            log.info("no proxy configured")
        self.client = httpx.Client(
            headers={
                "Authorization": f"Bearer {pat}",
                "Accept": "application/json",
            },
            timeout=30.0,
            proxy=proxy,
            trust_env=True,
        )

    def get_children(self, page_id: str) -> list[dict[str, Any]]:
        url = f"{self.base_url}/rest/api/content/{page_id}/child/page"
        results: list[dict[str, Any]] = []
        start = 0
        limit = 200
        while True:
            r = self.client.get(url, params={"limit": limit, "start": start})
            r.raise_for_status()
            data = r.json()
            batch = data.get("results", [])
            results.extend(batch)
            if len(batch) < limit:
                break
            start += limit
        return results

    def get_page_content(self, page_id: str) -> str:
        url = f"{self.base_url}/rest/api/content/{page_id}"
        r = self.client.get(url, params={"expand": "body.storage"})
        r.raise_for_status()
        return r.json()["body"]["storage"]["value"]
