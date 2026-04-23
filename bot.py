import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from functools import wraps
from typing import Any

from aiohttp import web
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

import config
from parser import Mirror, ProjectData

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("bot")

cfg = config.load()
if cfg.mode != "serve":
    raise RuntimeError("bot.py must run with MODE=serve; use refresh_local.py on the mac")

os.makedirs(cfg.cache_dir, exist_ok=True)
CACHE_FILE = os.path.join(cfg.cache_dir, "cache.json")

_cache: dict[str, Any] = {"refreshed_at": None, "index": None, "pages": {}}
_refresh_flag: dict[str, Any] = {"requested_at": None}


def load_cache_from_disk() -> None:
    if not os.path.exists(CACHE_FILE):
        log.info("no cache file on disk at %s", CACHE_FILE)
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
        "/refresh — запросить обновление кэша (нужен онлайн мак с VPN)"
    )


@auth
async def cmd_projects(update: Update, context: ContextTypes.DEFAULT_TYPE):
    idx = get_index()
    if not idx:
        await update.message.reply_text("Кэш пуст. Запусти refresh на маке.")
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
        await update.message.reply_text("Кэш пуст. Запусти refresh на маке.")
        return

    query = args[0].lower().split(".")[0].strip()
    geo = args[1] if len(args) > 1 else None

    matches: list[dict] = []
    for pages in idx["categories"].values():
        for p in pages:
            if query in p["title"].lower():
                matches.append(p)

    if not matches:
        await update.message.reply_text(f"Не найдено: {query}")
        return

    if len(matches) == 1:
        await _send_project(update, matches[0], geo)
        return

    if len(matches) > 20:
        titles = ", ".join(p["title"] for p in matches[:20])
        await update.message.reply_text(
            f"Слишком много совпадений ({len(matches)}). Уточни запрос.\n"
            f"Первые 20: {titles}"
        )
        return

    if geo:
        geo_u = geo.upper()
        lines = [f"*Найдено {len(matches)} по '{query}' / {geo_u}:*", ""]
        for m in matches:
            data = get_project(m["id"])
            if not data or not data.redirector:
                lines.append(f"• {m['title']} — нет данных")
                continue
            target = data.redirector.get(geo.lower()) or data.redirector.get("default")
            if not target:
                lines.append(f"• {m['title']} — нет {geo_u}")
                continue
            mirror = data.mirrors.get(target)
            warn = ""
            if mirror and geo_u in mirror.deny:
                warn = " ⚠️DENY"
            elif mirror and not mirror.default_allow:
                warn = " ⚠️not ALLOW"
            lines.append(f"• {m['title']} → `{target}`{warn}")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return

    keyboard = [
        [InlineKeyboardButton(m["title"], callback_data=f"p:{m['id']}:-")]
        for m in matches
    ]
    await update.message.reply_text(
        f"Найдено {len(matches)} проектов по '{query}':",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


@auth
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ts = _cache.get("refreshed_at") or "никогда"
    n = len(_cache.get("pages") or {})
    cats = len((_cache.get("index") or {}).get("categories") or {})
    flag_ts = _refresh_flag.get("requested_at")
    flag_line = f"\nЗапрос refresh: {flag_ts}" if flag_ts else ""
    await update.message.reply_text(
        f"Обновлён: {ts}\nКатегорий: {cats}\nСтраниц: {n}{flag_line}"
    )


@auth
async def cmd_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    before = _cache.get("refreshed_at")
    _refresh_flag["requested_at"] = datetime.now(timezone.utc).isoformat()
    await update.message.reply_text("Запрос отправлен на мак, жду ответа…")

    deadline = time.time() + 120
    while time.time() < deadline:
        await asyncio.sleep(2)
        if _refresh_flag.get("requested_at") is None and _cache.get("refreshed_at") != before:
            n = len(_cache.get("pages") or {})
            await update.message.reply_text(f"✅ Обновлено. Страниц: {n}")
            return

    _refresh_flag["requested_at"] = None
    await update.message.reply_text("❌ Мак спит или недоступен — разбуди и повтори /refresh")


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


def _check_secret(request: web.Request) -> bool:
    return request.headers.get("X-Refresh-Secret") == cfg.refresh_secret


async def http_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "refreshed_at": _cache.get("refreshed_at")})


async def http_get_flag(request: web.Request) -> web.Response:
    if not _check_secret(request):
        return web.Response(status=401, text="unauthorized")
    return web.json_response({"requested_at": _refresh_flag.get("requested_at")})


async def http_put_cache(request: web.Request) -> web.Response:
    if not _check_secret(request):
        return web.Response(status=401, text="unauthorized")
    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400, text="invalid json")
    if not isinstance(data, dict) or "refreshed_at" not in data:
        return web.Response(status=400, text="missing refreshed_at")
    _cache["refreshed_at"] = data["refreshed_at"]
    _cache["index"] = data.get("index")
    _cache["pages"] = data.get("pages") or {}
    save_cache_to_disk()
    _refresh_flag["requested_at"] = None
    n = len(_cache["pages"])
    log.info("cache received via HTTP, refreshed_at=%s pages=%d", _cache["refreshed_at"], n)
    return web.json_response({"ok": True, "pages": n})


async def run_http_server():
    app = web.Application()
    app.router.add_get("/", http_health)
    app.router.add_get("/refresh-flag", http_get_flag)
    app.router.add_post("/cache", http_put_cache)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=cfg.http_port)
    await site.start()
    log.info("http server listening on :%d", cfg.http_port)


async def post_init(app: Application):
    await run_http_server()


def main():
    load_cache_from_disk()
    app = (
        Application.builder()
        .token(cfg.tg_token)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("projects", cmd_projects))
    app.add_handler(CommandHandler("p", cmd_project))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("refresh", cmd_refresh))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_error_handler(on_error)

    log.info("bot started (serve mode), whitelist=%s cache_dir=%s", cfg.allowed_user_ids, cfg.cache_dir)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
