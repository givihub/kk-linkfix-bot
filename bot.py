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
    BufferedInputFile,
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
_BROWSER_UA = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
}
_MAX_VIDEO = 45 * 1024 * 1024  # 45 МБ (лимит загрузки ботом — 50 МБ)
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


async def _resolve_video(fixed: FixedLink) -> str | None:
    """Прямой URL видеофайла: фиксеры отвечают redirect'ом на mp4. None = нет."""
    if _http is None:
        return None
    try:
        async with _http.get(
            fixed.embed,
            proxy=PROXY_URL,
            allow_redirects=False,
            headers=_UA,
            timeout=aiohttp.ClientTimeout(total=6),
        ) as resp:
            loc = resp.headers.get("Location", "")
            if resp.status in (301, 302, 303, 307, 308) and loc.startswith("http"):
                return loc
    except Exception:  # noqa: BLE001
        pass
    return None


async def _download_video(url: str) -> bytes | None:
    """Скачать видеофайл (в память, до 45 МБ). None при любой ошибке."""
    if _http is None:
        return None
    try:
        async with _http.get(
            url,
            proxy=PROXY_URL,
            headers=_BROWSER_UA,
            timeout=aiohttp.ClientTimeout(total=120),
        ) as resp:
            if resp.status != 200:
                return None
            clen = int(resp.headers.get("Content-Length") or 0)
            if clen > _MAX_VIDEO:
                log.warning("Видео слишком большое: %d МБ", clen // 1048576)
                return None
            buf = bytearray()
            async for chunk in resp.content.iter_chunked(65536):
                buf.extend(chunk)
                if len(buf) > _MAX_VIDEO:
                    log.warning("Видео превысило лимит %d МБ при скачивании", _MAX_VIDEO // 1048576)
                    return None
            return bytes(buf)
    except Exception as e:  # noqa: BLE001
        log.warning("Не удалось скачать видео: %s", e)
        return None


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
        if len(desc) > 750:  # лимит подписи к видео — 1024 видимых символа
            desc = desc[:749] + "…"
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
        text = _build_text(fixed, meta, sender)
        sent = False

        # Основной путь: скачать видео через прокси и загрузить в Telegram
        # как файл — не зависит ни от кэша превью, ни от блокировок CDN.
        video_url = await _resolve_video(fixed)
        if video_url:
            data = await _download_video(video_url)
            if data:
                try:
                    await message.answer_video(
                        video=BufferedInputFile(data, filename="video.mp4"),
                        caption=text,
                        reply_markup=_keyboard(fixed),
                        disable_notification=True,
                        request_timeout=300,
                    )
                    sent = True
                except Exception as e:  # noqa: BLE001
                    log.warning(
                        "Загрузка видео в Telegram не прошла (%s): %s — откат на превью",
                        fixed.platform,
                        e,
                    )

        # Fallback: сообщение с веб-превью (без лимита 20 МБ)
        if not sent:
            try:
                await message.answer(
                    text,
                    link_preview_options=LinkPreviewOptions(
                        url=fixed.embed,
                        prefer_large_media=True,
                        show_above_text=True,
                    ),
                    reply_markup=_keyboard(fixed),
                    disable_notification=True,
                )
                sent = True
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
