"""Mac-side refresher.

Fetches Confluence through VPN+proxy, builds cache, POSTs to Railway.

Usage:
    python refresh_local.py              # always refresh
    python refresh_local.py --if-requested  # only refresh if bot flagged one

Exit codes:
    0 = success (or --if-requested and nothing to do)
    1 = fetch or upload failed
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

import httpx

import config
from confluence import ConfluenceClient
from parser import parse_project

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("refresh")


def build_cache(cfg) -> dict[str, Any]:
    client = ConfluenceClient(cfg.confluence_url, cfg.confluence_pat)
    categories = client.get_children(cfg.root_page_id)
    idx: dict[str, Any] = {"categories": {}, "by_basename": {}, "by_id": {}}
    pages: dict[str, dict] = {}
    for cat in categories:
        cat_name = cat["title"]
        cat_pages = client.get_children(cat["id"])
        idx["categories"][cat_name] = []
        for p in cat_pages:
            entry = {"id": p["id"], "title": p["title"], "category": cat_name}
            idx["categories"][cat_name].append(entry)
            idx["by_id"][p["id"]] = entry
            basename = p["title"].split(".")[0].lower()
            idx["by_basename"].setdefault(basename, []).append(entry)
            try:
                html = client.get_page_content(p["id"])
                data = parse_project(html)
                pages[p["id"]] = {
                    "redirector": data.redirector,
                    "mirrors": {
                        name: {
                            "name": m.name,
                            "default_allow": m.default_allow,
                            "deny": m.deny,
                        }
                        for name, m in data.mirrors.items()
                    },
                }
            except Exception:
                log.exception("parse page failed id=%s title=%s", p["id"], p["title"])
    return {
        "refreshed_at": datetime.now(timezone.utc).isoformat(),
        "index": idx,
        "pages": pages,
    }


def check_flag(cfg) -> bool:
    url = f"{cfg.railway_url}/refresh-flag"
    try:
        r = httpx.get(url, headers={"X-Refresh-Secret": cfg.refresh_secret}, timeout=10.0)
        r.raise_for_status()
        return bool(r.json().get("requested_at"))
    except Exception:
        log.exception("failed to check refresh flag")
        return False


def upload_cache(cfg, payload: dict[str, Any]) -> None:
    url = f"{cfg.railway_url}/cache"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    r = httpx.post(
        url,
        content=body,
        headers={
            "X-Refresh-Secret": cfg.refresh_secret,
            "Content-Type": "application/json",
        },
        timeout=60.0,
    )
    r.raise_for_status()
    log.info("cache uploaded: %s", r.json())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--if-requested", action="store_true",
                    help="only refresh when bot has flagged a request")
    args = ap.parse_args()

    cfg = config.load()
    if cfg.mode != "refresh":
        log.error("MODE must be 'refresh', got %r", cfg.mode)
        sys.exit(2)

    if args.if_requested:
        if not check_flag(cfg):
            log.info("no refresh requested, skipping")
            return
        log.info("refresh requested by bot, proceeding")

    try:
        log.info("fetching confluence…")
        payload = build_cache(cfg)
        log.info("fetched pages=%d", len(payload["pages"]))
    except Exception:
        log.exception("fetch failed")
        sys.exit(1)

    try:
        upload_cache(cfg, payload)
    except Exception:
        log.exception("upload failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
