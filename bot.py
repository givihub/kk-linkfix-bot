"""kk-linkfix-bot — превращает ссылки Instagram/TikTok/X в группе в инлайн-видео.

Механика: бот видит сообщение со ссылкой, удаляет его и отправляет вместо него
сообщение с видео. Под видео: автор/текст поста (из OG-метатегов фиксера, если
доступны; Instagram метаданные не отдаёт), строка «от кого» (кликабельное имя
отправителя) и инлайн-кнопка со ссылкой на оригинал.
Видео-превью генерируется по скрытому фикс-адресу (link_preview_options.url).
Для удаления чужих сообщений боту нужны права админа («Удаление сообщений»);
без прав бот мягко деградирует: оригинал остаётся, замена всё равно приходит.

Скачивания видео нет — превью отдаёт Telegram, боту хватает минимума ресурсов.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from html import escape, unescape
from urllib.parse import urlsplit, urlunsplit

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ChatType, MessageEntityType, ParseMode
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
)

from linkfix import FixedLink, convert

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("kk-linkfix-bot")

router = Router()

PROXY_URL = os.getenv("PROXY_URL") or None

# Откуда брать автора и текст поста (og:title / og:description).
# Пусто = для этой платформы текст не подтягиваем.
CAPTION_DOMAINS = {
    "tiktok": os.getenv("TIKTOK_CAPTION_DOMAIN", "tnktok.com"),
    "x": os.getenv("TWITTER_CAPTION_DOMAIN", "fixupx.com"),
    "instagram": os.getenv("INSTAGRAM_CAPTION_DOMAIN", ""),
}

_UA = {"User-Agent": "TelegramBot (like TwitterBot)"}
_OG_PATTERNS = (
    re.compile(
        r'<meta[^>]*?property=["\']og:(title|description)["\'][^>]*?content=["\']([^"\']*)',
        re.I | re.S,
    ),
    re.compile(
        r'<meta[^>]*?content=["\']([^"\']*)["\'][^>]*?property=["\']og:(title|description)',
        re.I | re.S,
    ),
)

_http: aiohttp.ClientSession | None = None


def _caption_url(fixed: FixedLink) -> str | None:
    domain = CAPTION_DOMAINS.get(fixed.platform)
    if not domain:
        return None
    parts = urlsplit(fixed.embed)
    return urlunsplit((parts.scheme, domain, parts.path, parts.query, ""))


async def _fetch_meta(fixed: FixedLink) -> dict[str, str]:
    """og:title/og:description со страницы фиксера. Fail-soft: {} при любой ошибке."""
    url = _caption_url(fixed)
    if not url or _http is None:
        return {}
    try:
        async with _http.get(
            url,
            proxy=PROXY_URL,
            allow_redirects=True,
            headers=_UA,
            timeout=aiohttp.ClientTimeout(total=4),
        ) as resp:
            ctype = resp.headers.get("Content-Type", "")
            if resp.status != 200 or "html" not in ctype:
                return {}
            raw = await resp.content.read(262_144)
    except Exception:  # noqa: BLE001
        return {}
    html_text = raw.decode("utf-8", "ignore")
    meta: dict[str, str] = {}
    for key, val in _OG_PATTERNS[0].findall(html_text):
        meta.setdefault(key.lower(), unescape(val).strip())
    for val, key in _OG_PATTERNS[1].findall(html_text):
        meta.setdefault(key.lower(), unescape(val).strip())
    return meta


def _sender_mention(message: Message) -> str:
    u = message.from_user
    if u is None:
        return escape(message.sender_chat.title if message.sender_chat else "аноним")
    return f'<a href="tg://user?id={u.id}">{escape(u.full_name)}</a>'


def _build_text(fixed: FixedLink, meta: dict[str, str], sender: str) -> str:
    lines: list[str] = []
    title = meta.get("title", "").strip()
    if title:
        if len(title) > 80:
            title = title[:79] + "…"
        lines.append(f"<b>{escape(title)}</b>")
    desc = meta.get("description", "").strip()
    if desc:
        if len(desc) > 900:
            desc = desc[:899] + "…"
        lines.append(f"<blockquote expandable>{escape(desc)}</blockquote>")
    lines.append(f"👤 от {sender}")
    return "\n".join(lines)


def _keyboard(fixed: FixedLink) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text=f"{fixed.label} ↗",
                url=fixed.original,
            )
        ]]
    )


def _extract_links(message: Message) -> list[FixedLink]:
    """Достать из сообщения все конвертируемые ссылки (по entities)."""
    text = message.text or message.caption or ""
    entities = message.entities or message.caption_entities or []
    found: list[FixedLink] = []
    seen: set[str] = set()
    for ent in entities:
        if ent.type == MessageEntityType.URL:
            url = ent.extract_from(text)
        elif ent.type == MessageEntityType.TEXT_LINK and ent.url:
            url = ent.url
        else:
            continue
        fixed = convert(url)
        if fixed and fixed.embed not in seen:
            seen.add(fixed.embed)
            found.append(fixed)
    return found


@router.message(
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP, ChatType.PRIVATE})
)
async def on_message(message: Message, bot: Bot) -> None:
    if message.from_user and message.from_user.is_bot:
        return
    links = _extract_links(message)
    if not links:
        return

    log.info(
        "chat=%s user=%s links=%d",
        message.chat.id,
        message.from_user.id if message.from_user else "?",
        len(links),
    )

    # Режим «заменить»: бот шлёт видео с подписью и кнопкой-ссылкой,
    # затем удаляет исходное сообщение (если хватает прав).
    sender = _sender_mention(message)
    sent_all = True
    for fixed in links:
        meta = await _fetch_meta(fixed)
        try:
            await message.answer(
                _build_text(fixed, meta, sender),
                link_preview_options=LinkPreviewOptions(
                    url=fixed.embed,
                    prefer_large_media=True,
                    # превью (видео) над текстом — подпись оказывается снизу
                    show_above_text=True,
                ),
                reply_markup=_keyboard(fixed),
                disable_notification=True,
            )
        except Exception:  # noqa: BLE001
            sent_all = False
            log.exception("Не удалось отправить сообщение с превью")

    # Удаляем оригинал только если все замены дошли
    if sent_all:
        try:
            await message.delete()
        except Exception:  # noqa: BLE001
            log.warning(
                "Нет прав на удаление в чате %s — оригинал остаётся",
                message.chat.id,
            )


async def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("BOT_TOKEN не задан (см. .env.example)")

    session = AiohttpSession(proxy=PROXY_URL) if PROXY_URL else None
    if PROXY_URL:
        log.info("Работаю через прокси: %s", PROXY_URL)

    bot = Bot(
        token=token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)

    global _http
    _http = aiohttp.ClientSession()
    try:
        me = await bot.get_me()
        log.info("Запущен как @%s (id=%s)", me.username, me.id)
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=["message"])
    finally:
        await _http.close()


if __name__ == "__main__":
    asyncio.run(main())
