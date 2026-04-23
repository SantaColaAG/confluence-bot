import logging
import time
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
from parser import ProjectData, parse_project

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("bot")

cfg = config.load()
client = ConfluenceClient(cfg.confluence_url, cfg.confluence_pat)

_index: dict[str, Any] = {"data": None, "ts": 0.0}
_pages: dict[str, tuple[float, ProjectData]] = {}


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


def get_index(force: bool = False) -> dict:
    now = time.time()
    if not force and _index["data"] and now - _index["ts"] < cfg.cache_ttl:
        return _index["data"]

    categories = client.get_children(cfg.root_page_id)
    idx: dict[str, Any] = {"categories": {}, "by_basename": {}, "by_id": {}}
    for cat in categories:
        cat_name = cat["title"]
        pages = client.get_children(cat["id"])
        idx["categories"][cat_name] = []
        for p in pages:
            entry = {"id": p["id"], "title": p["title"], "category": cat_name}
            idx["categories"][cat_name].append(entry)
            idx["by_id"][p["id"]] = entry
            basename = p["title"].split(".")[0].lower()
            idx["by_basename"].setdefault(basename, []).append(entry)

    _index["data"] = idx
    _index["ts"] = now
    return idx


def get_project(page_id: str) -> ProjectData:
    now = time.time()
    cached = _pages.get(page_id)
    if cached and now - cached[0] < cfg.cache_ttl:
        return cached[1]
    html = client.get_page_content(page_id)
    data = parse_project(html)
    _pages[page_id] = (now, data)
    return data


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
    try:
        data = get_project(entry["id"])
    except Exception as e:
        log.exception("fetch failed")
        await chat.send_message(f"Ошибка загрузки: {e}")
        return
    text = _format_project(entry, data, geo)
    await chat.send_message(text, parse_mode="Markdown")


@auth
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Команды:\n"
        "/projects — список проектов\n"
        "/p <project> [geo] — ссылка по гео\n"
        "/refresh — сбросить кэш"
    )


@auth
async def cmd_projects(update: Update, context: ContextTypes.DEFAULT_TYPE):
    idx = get_index()
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
    name = args[0].lower().split(".")[0]
    geo = args[1] if len(args) > 1 else None

    idx = get_index()
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
async def cmd_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _pages.clear()
    get_index(force=True)
    await update.message.reply_text("Кэш обновлён")


@auth
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not q.data or not q.data.startswith("p:"):
        return
    _, page_id, geo_tag = q.data.split(":", 2)
    geo = None if geo_tag == "-" else geo_tag
    idx = get_index()
    entry = idx["by_id"].get(page_id)
    if not entry:
        await q.message.reply_text("Запись устарела, попробуйте снова.")
        return
    await _send_project(update, entry, geo)


def main():
    app = Application.builder().token(cfg.tg_token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("projects", cmd_projects))
    app.add_handler(CommandHandler("p", cmd_project))
    app.add_handler(CommandHandler("refresh", cmd_refresh))
    app.add_handler(CallbackQueryHandler(on_button))
    log.info("bot started, whitelist=%s", cfg.allowed_user_ids)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
