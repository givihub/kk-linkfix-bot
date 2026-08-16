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
import json
import logging
import os
import re
import secrets
import tempfile
from collections import OrderedDict
from html import escape, unescape
from urllib.parse import urlsplit, urlunsplit

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ChatType, MessageEntityType, ParseMode
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
)

from linkfix import FixedLink, convert

_log_handlers: list[logging.Handler] = [logging.StreamHandler()]
if os.path.isdir("/logs"):  # примонтированный каталог — логи переживают пересборку
    from logging.handlers import RotatingFileHandler

    _log_handlers.append(
        RotatingFileHandler("/logs/bot.log", maxBytes=5_000_000, backupCount=3)
    )
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=_log_handlers,
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
# Лимит размера видео. Облачный Bot API: 45 (потолок Telegram — 50 МБ).
# С локальным Bot API сервером (BOT_API_URL) можно ставить до ~1900.
_MAX_VIDEO = int(os.getenv("MAX_VIDEO_MB", "45")) * 1024 * 1024
# Локальный Bot API сервер (пусто = облачный api.telegram.org)
BOT_API_URL = os.getenv("BOT_API_URL") or None
# Предпочтения качества yt-dlp
_YTDLP_SORT = os.getenv("YTDLP_SORT", "res:720,vcodec:h264")
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


# Редирект на сам соцсеть-сайт = фиксер расписался в бессилии, это не видео
_PLATFORM_HOSTS = ("instagram.com", "tiktok.com", "x.com", "twitter.com")


def _is_platform_host(url: str) -> bool:
    host = urlsplit(url).netloc.split("@")[-1].split(":")[0].lower()
    return any(host == h or host.endswith("." + h) for h in _PLATFORM_HOSTS)


_OG_VIDEO_PATTERNS = (
    re.compile(
        r'<meta[^>]*?property=["\']og:video(?::url)?["\'][^>]*?content=["\']([^"\']+)',
        re.I,
    ),
    re.compile(
        r'<meta[^>]*?content=["\']([^"\']+)["\'][^>]*?property=["\']og:video(?::url)?["\']',
        re.I,
    ),
)


async def _probe_candidate(url: str) -> str | None:
    """Спросить у одного фиксера прямой URL медиа (redirect или og:video)."""
    netloc = urlsplit(url).netloc
    try:
        async with _http.get(
            url,
            proxy=PROXY_URL,
            allow_redirects=False,
            headers=_UA,
            timeout=aiohttp.ClientTimeout(total=6),
        ) as resp:
            loc = resp.headers.get("Location", "")
            if resp.status in (301, 302, 303, 307, 308) and loc.startswith("http"):
                if _is_platform_host(loc):
                    log.info("probe %s: redirect обратно на соцсеть — мимо", netloc)
                    return None
                log.info("probe %s: redirect → медиа", netloc)
                return loc
            if resp.status == 200 and "html" in resp.headers.get("Content-Type", ""):
                html_text = (await resp.content.read(262_144)).decode("utf-8", "ignore")
                for pat in _OG_VIDEO_PATTERNS:
                    m = pat.search(html_text)
                    if m and m.group(1).startswith("http"):
                        log.info("probe %s: og:video → медиа", netloc)
                        return unescape(m.group(1))
            log.info(
                "probe %s: status=%s type=%s — медиа не отдал",
                netloc, resp.status, resp.headers.get("Content-Type", "?"),
            )
    except Exception as e:  # noqa: BLE001
        log.info("probe %s: ошибка %s", netloc, e)
    return None


def _media_kind(data: bytes) -> str | None:
    """video | photo | None по сигнатуре файла."""
    head = bytes(data[:64])
    if b"ftyp" in head:
        return "video"
    if head[:3] == b"\xff\xd8\xff" or head[:8] == b"\x89PNG\r\n\x1a\n" or head[:4] == b"RIFF":
        return "photo"
    return None


async def _fetch_media(fixed: FixedLink) -> tuple[str, bytes] | None:
    """Перебрать всю цепочку фиксеров. Приоритет — видео; если видео нет
    нигде, но кто-то отдал картинку (фото-пост) — вернём её."""
    if _http is None:
        return None
    photo: bytes | None = None
    for url in fixed.candidates:
        media_url = await _probe_candidate(url)
        if not media_url:
            continue
        data = await _download_video(media_url)
        if not data:
            # у некоторых фиксеров (vxinstagram) файл генерируется с задержкой —
            # одна повторная попытка после короткой паузы
            await asyncio.sleep(2.5)
            data = await _download_video(media_url)
        if not data:
            continue
        kind = _media_kind(data)
        if kind == "video":
            return "video", data
        if kind == "photo" and photo is None:
            log.info("Фиксер %s отдал картинку — запомню, ищу видео дальше", urlsplit(url).netloc)
            photo = data
    if photo is not None:
        return "photo", photo
    log.warning("Медиа не найдено ни у одного фиксера: %s", fixed.original)
    return None


# Признаки «контент закрыт владельцем / нужен логин» в ошибках yt-dlp
_RESTRICTED_MARKERS = (
    "cookies", "login", "logged-in", "logged in", "empty media response",
    "age-restricted", "restricted video", "private", "registered users",
    "rate-limit reached or login required",
    "isn't available to everyone", "certain audiences",
    "not available to everyone",
)


async def _ytdlp_fetch(url: str) -> tuple[tuple[str, bytes] | None, bool]:
    """Последний рубеж: yt-dlp напрямую с платформы (без авторизации).

    Возвращает (media, restricted): media = ("video", bytes) при успехе;
    restricted=True, если контент закрыт владельцем / требует логина.
    """
    try:
        with tempfile.TemporaryDirectory(dir="/tmp") as td:
            out = os.path.join(td, "v.mp4")
            cmd = [
                "yt-dlp", "-q", "--no-warnings", "--no-playlist",
                "--max-filesize", f"{max(_MAX_VIDEO // 1048576, 200)}M",
                # качество из _YTDLP_SORT (по умолчанию до 1080p, кодек h264);
                # видео+звук склеиваются ffmpeg'ом при раздельных дорожках (DASH)
                "-S", _YTDLP_SORT,
                "--merge-output-format", "mp4",
                "-o", out,
                url,
            ]
            if PROXY_URL:
                cmd += ["--proxy", PROXY_URL]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, err = await asyncio.wait_for(proc.communicate(), timeout=120)
            except asyncio.TimeoutError:
                proc.kill()
                log.warning("yt-dlp: таймаут")
                return None, False
            if os.path.exists(out) and os.path.getsize(out) > 10_000:
                with open(out, "rb") as f:
                    data = f.read()
                log.info("yt-dlp: видео добыто напрямую (%d КБ)", len(data) // 1024)
                return ("video", data), False
            err_text = (err or b"").decode("utf-8", "ignore").lower()
            restricted = any(m in err_text for m in _RESTRICTED_MARKERS)
            log.info("yt-dlp: не вышло (restricted=%s): %s", restricted, err_text[-250:])
            return None, restricted
    except Exception as e:  # noqa: BLE001
        log.warning("yt-dlp: ошибка запуска: %s", e)
        return None, False


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
            # Минимальный санити-чек: не пустышка и не страница ошибки
            if len(buf) < 5_000:
                log.warning("Скачанное подозрительно мало (%d байт) — отбрасываю", len(buf))
                return None
            return bytes(buf)
    except Exception as e:  # noqa: BLE001
        log.warning("Не удалось скачать видео: %s", e)
        return None


async def _run(cmd: list[str]) -> tuple[int, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    return proc.returncode or 0, out or b""


async def _prepare_video(data: bytes) -> tuple[bytes, dict, bytes | None]:
    """Faststart-ремукс (чтобы Telegram стримил) + размеры/длительность + обложка.

    Fail-soft: при любой ошибке возвращаем исходные байты без метаданных.
    """
    meta: dict = {}
    thumb: bytes | None = None
    try:
        with tempfile.TemporaryDirectory(dir="/tmp") as td:
            src = os.path.join(td, "in.mp4")
            dst = os.path.join(td, "out.mp4")
            th = os.path.join(td, "thumb.jpg")
            with open(src, "wb") as f:
                f.write(data)
            # Кодек видеодорожки: клиенты Telegram играют только h264 в mp4.
            # VP9/AV1 (частый случай у yt-dlp/DASH) — статичный кадр со звуком.
            rc, out = await _run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=codec_name", "-of", "csv=p=0", src]
            )
            codec = out.decode("utf-8", "ignore").strip().lower() if rc == 0 else ""
            # Перекодируем, если кодек несовместим ИЛИ файл не влезает в лимит
            if (codec and codec != "h264") or len(data) > _MAX_VIDEO:
                # Потолок битрейта из длительности: файл должен влезть в 45 МБ
                rc, out = await _run(
                    ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                     "-of", "csv=p=0", src]
                )
                try:
                    dur = float(out.decode().strip()) if rc == 0 else 0.0
                except ValueError:
                    dur = 0.0
                kbps = 6000
                if dur > 1:
                    cap = int(_MAX_VIDEO * 8 * 0.9 / dur / 1000) - 128
                    kbps = max(300, min(6000, cap))
                log.info("Кодек %s — перекодирую в h264 (%d kbps, %.0f c)", codec, kbps, dur)
                rc, _ = await _run(
                    ["ffmpeg", "-y", "-i", src,
                     "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                     "-maxrate", f"{kbps}k", "-bufsize", f"{kbps * 2}k",
                     "-c:a", "aac", "-b:a", "128k",
                     "-movflags", "+faststart", dst]
                )
            else:
                rc, _ = await _run(
                    ["ffmpeg", "-y", "-i", src, "-c", "copy", "-movflags", "+faststart", dst]
                )
            target = dst if rc == 0 and os.path.getsize(dst) > 0 else src
            if target == dst:
                with open(dst, "rb") as f:
                    data = f.read()
            rc, out = await _run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height:format=duration",
                 "-of", "json", target]
            )
            if rc == 0:
                j = json.loads(out.decode("utf-8", "ignore") or "{}")
                st = (j.get("streams") or [{}])[0]
                dur = (j.get("format") or {}).get("duration") or 0
                meta = {
                    "width": st.get("width"),
                    "height": st.get("height"),
                    "duration": int(float(dur)) or None,
                }
            rc, _ = await _run(
                ["ffmpeg", "-y", "-i", target, "-ss", "0.1", "-frames:v", "1",
                 "-vf", "scale=320:-2", th]
            )
            if rc == 0 and os.path.exists(th):
                with open(th, "rb") as f:
                    thumb = f.read()
    except Exception as e:  # noqa: BLE001
        log.warning("ffmpeg-подготовка не удалась: %s", e)
    return data, meta, thumb


def _sender_mention(message: Message) -> str:
    u = message.from_user
    if u is None:
        return escape(message.sender_chat.title if message.sender_chat else "аноним")
    return f'<a href="tg://user?id={u.id}">{escape(u.full_name)}</a>'


def _build_text(fixed: FixedLink, meta: dict[str, str], sender: str | None) -> str:
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
    if sender:  # в личке с ботом подпись «от кого» не нужна
        lines.append(f"👤 от {sender}")
    # Пустая строка допустима: у видео/фото подпись опциональна
    return "\n".join(lines)


# Кэш «кнопка → ссылка» для извлечения звука (callback_data ограничена 64 байтами)
_AUDIO_CACHE: OrderedDict[str, str] = OrderedDict()


def _remember_audio(url: str) -> str:
    key = secrets.token_urlsafe(6)
    _AUDIO_CACHE[key] = url
    while len(_AUDIO_CACHE) > 500:
        _AUDIO_CACHE.popitem(last=False)
    return key


def _keyboard(fixed: FixedLink, with_audio: bool = False) -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton(text=f"{fixed.label} ↗", url=fixed.original)]
    if with_audio:
        row.append(
            InlineKeyboardButton(
                text="🎵 Звук",
                callback_data=f"aud:{_remember_audio(fixed.original)}",
            )
        )
    return InlineKeyboardMarkup(inline_keyboard=[row])


async def _ytdlp_audio(url: str) -> bytes | None:
    """Достать аудиодорожку (mp3) через yt-dlp. None при ошибке."""
    try:
        with tempfile.TemporaryDirectory(dir="/tmp") as td:
            out = os.path.join(td, "a.%(ext)s")
            cmd = [
                "yt-dlp", "-q", "--no-warnings", "--no-playlist",
                "--max-filesize", "200M",
                "-x", "--audio-format", "mp3", "--audio-quality", "192K",
                "-o", out, url,
            ]
            if PROXY_URL:
                cmd += ["--proxy", PROXY_URL]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(proc.communicate(), timeout=300)
            except asyncio.TimeoutError:
                proc.kill()
                return None
            p = os.path.join(td, "a.mp3")
            if os.path.exists(p) and os.path.getsize(p) > 10_000:
                with open(p, "rb") as f:
                    return f.read()
    except Exception as e:  # noqa: BLE001
        log.warning("yt-dlp audio: %s", e)
    return None


async def _expand_playlist(url: str) -> list[str]:
    """Первые 10 роликов плейлиста YouTube (только для лички)."""
    cmd = ["yt-dlp", "-q", "--no-warnings", "--flat-playlist",
           "--playlist-end", "10", "--print", "url", url]
    if PROXY_URL:
        cmd += ["--proxy", PROXY_URL]
    rc, out = await _run(cmd)
    urls = [l.strip() for l in out.decode("utf-8", "ignore").splitlines()
            if l.strip().startswith("http")]
    log.info("Плейлист: беру %d роликов (лимит 10)", len(urls))
    return urls


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
    is_private = message.chat.type == ChatType.PRIVATE

    # YouTube — только в личке: в группах Telegram сам играет его нативно
    links = [f for f in links if f.platform != "youtube" or is_private]

    # Плейлисты YouTube — только в личке, первые 10 роликов
    if is_private:
        text_all = message.text or message.caption or ""
        m = re.search(r"https?://\S*youtube\.com/playlist\?\S+", text_all)
        if m:
            for u in await _expand_playlist(m.group(0)):
                fx = convert(u)
                if fx and fx.embed not in {f.embed for f in links}:
                    links.append(fx)

    if not links:
        return

    log.info(
        "chat=%s user=%s links=%s",
        message.chat.id,
        message.from_user.id if message.from_user else "?",
        [f.original for f in links],
    )

    # Режим «заменить»: бот шлёт видео с подписью и кнопкой-ссылкой,
    # затем удаляет исходное сообщение (если хватает прав).
    # В группах подписываем автора ссылки; в личке с ботом — не нужно
    sender = (
        _sender_mention(message) if message.chat.type != ChatType.PRIVATE else None
    )
    sent_all = True
    all_video = True  # оригинал удаляем только если видео реально доставлено
    for fixed in links:
        # Индикатор «отправляет видео…» в шапке чата
        try:
            await bot.send_chat_action(message.chat.id, "upload_video")
        except Exception:  # noqa: BLE001
            pass

        # Текст поста и поиск медиа — параллельно (экономит до ~4 с)
        meta_task = asyncio.create_task(_fetch_meta(fixed))
        media = await _fetch_media(fixed)
        if media is None:
            # у фиксеров бывают транзиентные 5xx — второй проход цепочки
            await asyncio.sleep(4)
            media = await _fetch_media(fixed)
        restricted = False
        if media is None or media[0] == "photo":
            # Последний рубеж: yt-dlp напрямую с платформы, мимо фиксеров.
            # Запускаем и когда фиксеры нашли только картинку: возможно,
            # это видео-пост, у которого фиксеры видят лишь обложку.
            yt_media, restricted = await _ytdlp_fetch(fixed.original)
            if yt_media is not None:
                media = yt_media  # видео побеждает фото
        meta = await meta_task
        text = _build_text(fixed, meta, sender)
        sent = False

        # Основной путь: скачанное медиа загружаем в Telegram файлом —
        # не зависит ни от кэша превью, ни от блокировок CDN.
        if media:
            kind, data = media
            try:
                if kind == "video":
                    data, vmeta, thumb = await _prepare_video(data)
                    await message.answer_video(
                        video=BufferedInputFile(data, filename="video.mp4"),
                        caption=text or None,
                        reply_markup=_keyboard(fixed, with_audio=True),
                        disable_notification=True,
                        supports_streaming=True,
                        width=vmeta.get("width"),
                        height=vmeta.get("height"),
                        duration=vmeta.get("duration"),
                        thumbnail=BufferedInputFile(thumb, "thumb.jpg") if thumb else None,
                        request_timeout=300,
                    )
                else:  # photo — фото-пост без видео
                    await message.answer_photo(
                        photo=BufferedInputFile(data, filename="photo.jpg"),
                        caption=text or None,
                        reply_markup=_keyboard(fixed),
                        disable_notification=True,
                        request_timeout=120,
                    )
                sent = True
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "Загрузка медиа в Telegram не прошла (%s, %s): %s — откат на превью",
                    fixed.platform,
                    kind,
                    e,
                )

        # Контент закрыт владельцем: честно сообщаем, оригинал не трогаем
        if not sent and restricted:
            all_video = False
            try:
                await message.reply(
                    "🔒 Владелец закрыл это видео — платформа показывает его "
                    "только авторизованным пользователям, бот бессилен. "
                    "Открыть можно по кнопке.",
                    reply_markup=_keyboard(fixed),
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                    disable_notification=True,
                )
                sent = True
            except Exception:  # noqa: BLE001
                log.exception("Не удалось отправить сообщение об ограничении")

        # Fallback: сообщение с веб-превью (без лимита 45 МБ)
        if not sent:
            all_video = False
            try:
                await message.answer(
                    # текстовое сообщение пустым быть не может — минимум название
                    text or f"<b>{fixed.label}</b>",
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

    # Удаляем оригинал только если каждое видео реально доставлено файлом.
    # Если пришлось откатиться на превью — оригинал не трогаем (честнее).
    if sent_all and all_video:
        try:
            await message.delete()
        except Exception:  # noqa: BLE001
            log.warning(
                "Нет прав на удаление в чате %s — оригинал остаётся",
                message.chat.id,
            )


@router.callback_query(F.data.startswith("aud:"))
async def on_audio_button(cb: CallbackQuery, bot: Bot) -> None:
    """Кнопка «🎵 Звук»: достаём аудиодорожку и шлём ответом на видео."""
    url = _AUDIO_CACHE.get((cb.data or "")[4:])
    if not url or cb.message is None:
        await cb.answer("Кнопка устарела — киньте ссылку ещё раз", show_alert=True)
        return
    await cb.answer("Достаю звук…")
    try:
        await bot.send_chat_action(cb.message.chat.id, "upload_document")
    except Exception:  # noqa: BLE001
        pass
    log.info("chat=%s: извлекаю аудио из %s", cb.message.chat.id, url)
    data = await _ytdlp_audio(url)
    if data:
        try:
            await cb.message.reply_audio(
                audio=BufferedInputFile(data, filename="audio.mp3"),
                disable_notification=True,
                request_timeout=300,
            )
            return
        except Exception:  # noqa: BLE001
            log.exception("Не удалось отправить аудио")
    try:
        await cb.message.reply(
            "🎵 Не смог достать звук из этого видео, увы",
            disable_notification=True,
        )
    except Exception:  # noqa: BLE001
        pass


async def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("BOT_TOKEN не задан (см. .env.example)")

    # Большой таймаут: загрузка крупных видео не влезает в дефолтные 60 с
    session_kwargs: dict = {"timeout": 900 if BOT_API_URL else 300}
    if PROXY_URL:
        session_kwargs["proxy"] = PROXY_URL
        log.info("Работаю через прокси: %s", PROXY_URL)
    if BOT_API_URL:
        # Локальный Bot API сервер: лимит загрузки 2 ГБ вместо 50 МБ
        session_kwargs["api"] = TelegramAPIServer.from_base(BOT_API_URL, is_local=True)
        log.info("Работаю через локальный Bot API: %s", BOT_API_URL)
    session = AiohttpSession(**session_kwargs)

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
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        await _http.close()


if __name__ == "__main__":
    asyncio.run(main())
