import os
from dataclasses import dataclass, field


@dataclass
class Config:
    mode: str
    tg_token: str
    allowed_user_ids: set[int]
    cache_dir: str
    refresh_secret: str
    http_port: int
    railway_url: str
    confluence_url: str = ""
    confluence_pat: str = ""
    root_page_id: str = ""


def _parse_ids(raw: str) -> set[int]:
    return {int(x) for x in raw.split(",") if x.strip()}


def load() -> Config:
    mode = os.environ.get("MODE", "serve").lower()
    if mode not in {"serve", "refresh"}:
        raise ValueError(f"MODE must be 'serve' or 'refresh', got {mode!r}")

    cfg = Config(
        mode=mode,
        tg_token=os.environ.get("TG_TOKEN", ""),
        allowed_user_ids=_parse_ids(os.environ.get("ALLOWED_USER_IDS", "")),
        cache_dir=os.environ.get("CACHE_DIR", os.path.dirname(os.path.abspath(__file__))),
        refresh_secret=os.environ.get("REFRESH_SECRET", ""),
        http_port=int(os.environ.get("PORT", "8080")),
        railway_url=os.environ.get("RAILWAY_URL", "").rstrip("/"),
        confluence_url=os.environ.get("CONFLUENCE_URL", "").rstrip("/"),
        confluence_pat=os.environ.get("CONFLUENCE_PAT", ""),
        root_page_id=os.environ.get("ROOT_PAGE_ID", ""),
    )

    if mode == "serve":
        missing = [k for k, v in {
            "TG_TOKEN": cfg.tg_token,
            "ALLOWED_USER_IDS": cfg.allowed_user_ids,
            "REFRESH_SECRET": cfg.refresh_secret,
        }.items() if not v]
        if missing:
            raise ValueError(f"serve mode requires: {', '.join(missing)}")
    else:
        missing = [k for k, v in {
            "CONFLUENCE_URL": cfg.confluence_url,
            "CONFLUENCE_PAT": cfg.confluence_pat,
            "ROOT_PAGE_ID": cfg.root_page_id,
            "REFRESH_SECRET": cfg.refresh_secret,
            "RAILWAY_URL": cfg.railway_url,
        }.items() if not v]
        if missing:
            raise ValueError(f"refresh mode requires: {', '.join(missing)}")

    return cfg
