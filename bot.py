import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from functools import wraps
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

import config
from confluence import ConfluenceClient
from parser import Mirror, ProjectData, parse_project

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("bot")

cfg = config.load()
client = ConfluenceClient(cfg.confluence_url, cfg.confluence_pat)

CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache.json")
REFRESH_INTERVAL = 24 * 3600

_cache: dict[str, Any] = {"refreshed_at": None, "index": None, "pages": {}}
_refresh_lock = asyncio.Lock()


def load_cache_from_disk() -> None:
    if not os.path.exists(CACHE_FILE):
        log.info("no cache file on disk")
        return
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        _cache.update(data)
        log.info(
            "cache loaded, refreshed_at=%s pages=%d",
            _cache.get("refreshed_at"),
            len(_cache.get("pages") or {}),
        )
    except Exception:
        log.exception("failed to load cache from disk")


def save_cache_to_disk() -> None:
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_cache, f, ensure_ascii=False)
    os.replace(tmp, CACHE_FILE)


def _refresh_sync() -> tuple[bool, str]:
    try:
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
        _cache["pages"] = pages
        _cache["index"] = idx
        _cache["refreshed_at"] = datetime.now(timezone.utc).isoformat()
        save_cache_to_disk()
        return True, f"pages={len(pages)}"
    except Exception as e:
        log.exception("refresh failed")
        return False, str(e)


async def do_refresh() -> tuple[bool, str]:
    async with _refresh_lock:
        return await asyncio.to_thread(_refresh_sync)


def get_index() -> dict | None:
    return _cache.get("index")


def get_project(page_id: str) -> ProjectData | None:
    p = (_cache.get("pages") or {}).get(page_id)
    if not p:
        return None
    mirrors = {
        name: Mirror(name=m["name"], default_allow=m["default_allow"], deny=list(m["deny"]))
        for name, m in p.get("mirrors", {}).items()
    }
    return ProjectData(redirector=dict(p.get("redirector", {})), mirrors=mirrors)


def auth(fn):
    @wraps(fn)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id if update.effective_user else None
        if uid not in cfg.allowed_user_ids:
            log.warning("denied uid=%s", uid)
            if update.message:
                await update.message.reply_text("Access denied.")
            elif update.callback_query:
                await update.callback_query.answer("Access denied.", show_alert=True)
            return
        return await fn(update, context)
    return wrapper


def _format_project(entry: dict, data: ProjectData, geo: str | None) -> str:
    title = entry["title"]
    if not data.redirector:
        return f"*{title}* — Redirector таблица не найдена"

    if geo:
        geo_u = geo.upper()
        target = data.redirector.get(geo.lower()) or data.redirector.get("default")
        if not target:
            return f"Нет записи для {geo_u} в {title}"
        warn = ""
        mirror = data.mirrors.get(target)
        if mirror and geo_u in mirror.deny:
            warn = f"\n⚠️ {geo_u} в DENY у {target}"
        elif mirror and not mirror.default_allow:
            warn = f"\n⚠️ {target} не ALLOW by default"
        return f"*{title}* / {geo_u}\n→ `{target}`{warn}"

    lines = [f"*{title}*", ""]
    for g, t in data.redirector.items():
        lines.append(f"`{g.upper():<8}` → `{t}`")
    return "\n".join(lines)


async def _send_project(update: Update, entry: dict, geo: str | None):
    chat = update.effective_chat
    data = get_project(entry["id"])
    if not data:
        await chat.send_message(f"Нет данных по странице {entry['title']} в кэше")
        return
    await chat.send_message(_format_project(entry, data, geo), parse_mode="Markdown")


@auth
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Команды:\n"
        "/projects — список проектов\n"
        "/p <project> [geo] — ссылка по гео\n"
        "/status — последнее обновление кэша\n"
        "/refresh — обновить кэш сейчас (нужен VPN)"
    )


@auth
async def cmd_projects(update: Update, context: ContextTypes.DEFAULT_TYPE):
    idx = get_index()
    if not idx:
        await update.message.reply_text(
            "Кэш пуст. Включи VPN и вызови /refresh."
        )
        return
    lines: list[str] = []
    for cat in sorted(idx["categories"]):
        pages = sorted(idx["categories"][cat], key=lambda x: x["title"])
        lines.append(f"📁 *{cat}* ({len(pages)})")
        for p in pages:
            lines.append(f"  • {p['title']}")
        lines.append("")
    text = "\n".join(lines).strip() or "Пусто"
    chunk = 3500
    for i in range(0, len(text), chunk):
        await update.message.reply_text(text[i:i + chunk], parse_mode="Markdown")


@auth
async def cmd_project(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /p <project> [geo]\nПример: /p blitz-bet DE")
        return

    idx = get_index()
    if not idx:
        await update.message.reply_text("Кэш пуст. Включи VPN и вызови /refresh.")
        return

    name = args[0].lower().split(".")[0]
    geo = args[1] if len(args) > 1 else None

    matches = idx["by_basename"].get(name, [])
    if not matches:
        similar = sorted({b for b in idx["by_basename"] if name in b or b in name})[:10]
        hint = "\nПохожие: " + ", ".join(similar) if similar else ""
        await update.message.reply_text(f"Не найдено: {name}{hint}")
        return

    if len(matches) == 1:
        await _send_project(update, matches[0], geo)
        return

    geo_tag = geo if geo else "-"
    keyboard = [
        [InlineKeyboardButton(m["title"], callback_data=f"p:{m['id']}:{geo_tag}")]
        for m in matches
    ]
    await update.message.reply_text(
        f"Несколько проектов '{name}':",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


@auth
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ts = _cache.get("refreshed_at") or "никогда"
    n = len(_cache.get("pages") or {})
    cats = len((_cache.get("index") or {}).get("categories") or {})
    await update.message.reply_text(
        f"Обновлён: {ts}\nКатегорий: {cats}\nСтраниц: {n}\nФайл: {CACHE_FILE}"
    )


@auth
async def cmd_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _refresh_lock.locked():
        await update.message.reply_text("Уже обновляю, подожди…")
        return
    await update.message.reply_text("Обновляю…")
    ok, msg = await do_refresh()
    await update.message.reply_text(("✅ " if ok else "❌ ") + msg)


@auth
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not q.data or not q.data.startswith("p:"):
        return
    _, page_id, geo_tag = q.data.split(":", 2)
    geo = None if geo_tag == "-" else geo_tag
    idx = get_index()
    entry = (idx or {}).get("by_id", {}).get(page_id)
    if not entry:
        await q.message.reply_text("Запись устарела, попробуйте снова.")
        return
    await _send_project(update, entry, geo)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("handler error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_chat:
        try:
            await update.effective_chat.send_message(f"Ошибка: {context.error}")
        except Exception:
            pass


async def scheduled_refresh(context: ContextTypes.DEFAULT_TYPE):
    log.info("scheduled refresh starting")
    ok, msg = await do_refresh()
    log.info("scheduled refresh done ok=%s %s", ok, msg)


def main():
    load_cache_from_disk()
    app = Application.builder().token(cfg.tg_token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("projects", cmd_projects))
    app.add_handler(CommandHandler("p", cmd_project))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("refresh", cmd_refresh))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_error_handler(on_error)

    first_delay = 10 if _cache.get("index") else 3
    app.job_queue.run_repeating(
        scheduled_refresh, interval=REFRESH_INTERVAL, first=first_delay
    )

    log.info("bot started, whitelist=%s", cfg.allowed_user_ids)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
