import os
from dataclasses import dataclass


@dataclass
class Config:
    tg_token: str
    confluence_url: str
    confluence_pat: str
    root_page_id: str
    allowed_user_ids: set[int]
    cache_ttl: int


def load() -> Config:
    allowed = os.environ["ALLOWED_USER_IDS"].split(",")
    return Config(
        tg_token=os.environ["TG_TOKEN"],
        confluence_url=os.environ["CONFLUENCE_URL"].rstrip("/"),
        confluence_pat=os.environ["CONFLUENCE_PAT"],
        root_page_id=os.environ["ROOT_PAGE_ID"],
        allowed_user_ids={int(x) for x in allowed if x.strip()},
        cache_ttl=int(os.environ.get("CACHE_TTL", "600")),
    )
