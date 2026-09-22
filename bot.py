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
import socket
import tempfile
import time
from collections import OrderedDict
from datetime import datetime
from html import escape, unescape
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp.abc import AbstractResolver
from aiohttp.resolver import DefaultResolver
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

# Режим тишины: с QUIET_FROM до QUIET_TO часов (по QUIET_TZ) сообщения без звука
_QUIET_TZ = ZoneInfo(os.getenv("QUIET_TZ", "Europe/Moscow"))
_QUIET_FROM = int(os.getenv("QUIET_FROM", "23"))
_QUIET_TO = int(os.getenv("QUIET_TO", "8"))


def _silent_now() -> bool:
    h = datetime.now(_QUIET_TZ).hour
    if _QUIET_FROM > _QUIET_TO:  # интервал через полночь, напр. 23 → 8
        return h >= _QUIET_FROM or h < _QUIET_TO
    return _QUIET_FROM <= h < _QUIET_TO
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


class DoHFallbackResolver(AbstractResolver):
    """Системный DNS, а при отказе — DNS-over-HTTPS (Cloudflare).

    Нужен дома: провайдерский резолвер не отдаёт имена некоторых фиксеров
    (kkinstagram.com), хотя сами серверы доступны. Внешние DNS роутер режет,
    а DoH проходит как обычный HTTPS.
    """

    def __init__(self) -> None:
        self._sys = DefaultResolver()
        self._cache: dict[str, tuple[float, list[str]]] = {}

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET):
        try:
            return await self._sys.resolve(host, port, family)
        except OSError:
            pass
        ips = await self._doh(host)
        if not ips:
            raise OSError(f"DoH: не удалось разрешить {host}")
        return [
            {"hostname": host, "host": ip, "port": port,
             "family": socket.AF_INET, "proto": 0, "flags": socket.AI_NUMERICHOST}
            for ip in ips
        ]

    async def _doh(self, host: str) -> list[str]:
        now = time.time()
        cached = self._cache.get(host)
        if cached and cached[0] > now:
            return cached[1]
        ips: list[str] = []
        try:
            async with aiohttp.ClientSession() as s:  # cloudflare-dns.com резолвится штатно
                async with s.get(
                    "https://cloudflare-dns.com/dns-query",
                    params={"name": host, "type": "A"},
                    headers={"accept": "application/dns-json"},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as r:
                    j = await r.json(content_type=None)
            ips = [a["data"] for a in j.get("Answer", []) if a.get("type") == 1]
            log.info("DoH: %s → %s", host, ips)
        except Exception as e:  # noqa: BLE001
            log.warning("DoH: %s не разрешён: %s", host, e)
        self._cache[host] = (now + 300, ips)
        return ips

    async def close(self) -> None:
        await self._sys.close()


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


async def _ytdlp_fetch(url: str, item: int | None = None) -> tuple[tuple[str, bytes] | None, bool]:
    """Последний рубеж: yt-dlp напрямую с платформы (без авторизации).

    item — номер слайда карусели (Instagram img_index). Без него из карусели
    берётся первое видео (фото-слайды пропускаются).
    Возвращает (media, restricted): media = ("video", bytes) при успехе;
    restricted=True, если контент закрыт владельцем / требует логина.
    """
    try:
        with tempfile.TemporaryDirectory(dir="/tmp") as td:
            # playlist_index: для каруселей — номер слайда, для одиночных — 0
            out = os.path.join(td, "v%(playlist_index|0)s.mp4")
            cmd = [
                "yt-dlp", "-q", "--no-warnings",
                "--max-filesize", f"{max(_MAX_VIDEO // 1048576, 200)}M",
                # качество из _YTDLP_SORT (по умолчанию до 720p, кодек h264);
                # видео+звук склеиваются ffmpeg'ом при раздельных дорожках (DASH)
                "-S", _YTDLP_SORT,
                "--merge-output-format", "mp4",
                # детектор зависания: 30 с без данных от CDN — обрыв и ретрай
                "--socket-timeout", "30", "--retries", "3",
            ]
            if item:
                cmd += ["--playlist-items", str(item)]
            else:
                # карусель без номера: фото-слайды дают ошибку — игнорируем,
                # останавливаемся на первом успешно скачанном видео
                cmd += ["--ignore-errors", "--max-downloads", "1"]
            cmd += ["-o", out, url]
            if PROXY_URL:
                cmd += ["--proxy", PROXY_URL]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                # Длинные ролики (YouTube в личке) качаются минутами —
                # даём до 10 минут; зависание на CDN отсечёт --socket-timeout
                _, err = await asyncio.wait_for(proc.communicate(), timeout=600)
            except asyncio.TimeoutError:
                proc.kill()
                log.warning("yt-dlp: таймаут (10 мин)")
                return None, False
            files = sorted(
                f for f in os.listdir(td)
                if f.endswith(".mp4") and os.path.getsize(os.path.join(td, f)) > 10_000
            )
            if files:
                with open(os.path.join(td, files[0]), "rb") as f:
                    data = f.read()
                log.info("yt-dlp: видео добыто напрямую (%d КБ, %s)", len(data) // 1024, files[0])
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
            # Детектор зависания: 30 с без единого байта — обрыв (а не 2 минуты
            # ожидания); общий потолок 10 минут — для больших файлов
            timeout=aiohttp.ClientTimeout(total=600, sock_connect=15, sock_read=30),
        ) as resp:
            if resp.status != 200:
                log.info("CDN ответил %s на %s", resp.status, url[:80])
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
            log.info("Скачано %d КБ с %s", len(buf) // 1024, urlsplit(url).netloc)
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
                t0 = time.monotonic()
                rc, _ = await _run(
                    ["ffmpeg", "-y", "-i", src,
                     "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                     "-maxrate", f"{kbps}k", "-bufsize", f"{kbps * 2}k",
                     "-c:a", "aac", "-b:a", "128k",
                     "-movflags", "+faststart", dst]
                )
                log.info("Перекодирование заняло %.0f с (rc=%d)", time.monotonic() - t0, rc)
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


# Очередь: ролики обрабатываются строго по одному. Параллельная обработка
# на 4 ядрах даёт толкотню за CPU (ffmpeg) и сеть, а одна и та же ссылка
# в двух чатах — двойное скачивание.
_WORK_LOCK = asyncio.Lock()

# Кэш доставленных медиа: канонический URL → (тип, file_id, срок годности).
# Та же ссылка во втором чате уходит мгновенно по file_id без скачивания.
_RECENT: OrderedDict[str, tuple[str, str, float]] = OrderedDict()
_RECENT_TTL = 3600.0


def _recent_get(fixed: FixedLink) -> tuple[str, str] | None:
    rec = _RECENT.get(fixed.original)
    if rec is None:
        return None
    kind, file_id, exp = rec
    if exp < time.monotonic():
        _RECENT.pop(fixed.original, None)
        return None
    return kind, file_id


def _recent_put(fixed: FixedLink, kind: str, file_id: str) -> None:
    _RECENT[fixed.original] = (kind, file_id, time.monotonic() + _RECENT_TTL)
    while len(_RECENT) > 200:
        _RECENT.popitem(last=False)


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
    # Очередь: ролики обрабатываем по одному — без толкотни за CPU/сеть
    async with _WORK_LOCK:
        for fixed in links:
            # Индикатор «отправляет видео…» в шапке чата
            try:
                await bot.send_chat_action(message.chat.id, "upload_video")
            except Exception:  # noqa: BLE001
                pass

            # Кэш: ту же ссылку недавно уже доставляли — шлём по file_id,
            # без повторного скачивания и перекодирования
            cached = _recent_get(fixed)
            if cached:
                ckind, file_id = cached
                try:
                    meta = await _fetch_meta(fixed)
                    text = _build_text(fixed, meta, sender)
                    if ckind == "video":
                        await message.answer_video(
                            video=file_id,
                            caption=text or None,
                            reply_markup=_keyboard(fixed, with_audio=True),
                            disable_notification=_silent_now(),
                        )
                    else:
                        await message.answer_photo(
                            photo=file_id,
                            caption=text or None,
                            reply_markup=_keyboard(fixed),
                            disable_notification=_silent_now(),
                        )
                    log.info("chat=%s: %s отправлено из кэша (file_id)", message.chat.id, fixed.original)
                    continue
                except Exception as e:  # noqa: BLE001
                    log.warning("Отправка из кэша не удалась (%s) — качаю заново", e)
                    _RECENT.pop(fixed.original, None)

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
                yt_media, restricted = await _ytdlp_fetch(fixed.original, fixed.item)
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
                        sent_msg = await message.answer_video(
                            video=BufferedInputFile(data, filename="video.mp4"),
                            caption=text or None,
                            reply_markup=_keyboard(fixed, with_audio=True),
                            disable_notification=_silent_now(),
                            supports_streaming=True,
                            width=vmeta.get("width"),
                            height=vmeta.get("height"),
                            duration=vmeta.get("duration"),
                            thumbnail=BufferedInputFile(thumb, "thumb.jpg") if thumb else None,
                            request_timeout=300,
                        )
                        if sent_msg.video:
                            _recent_put(fixed, "video", sent_msg.video.file_id)
                    else:  # photo — фото-пост без видео
                        sent_msg = await message.answer_photo(
                            photo=BufferedInputFile(data, filename="photo.jpg"),
                            caption=text or None,
                            reply_markup=_keyboard(fixed),
                            disable_notification=_silent_now(),
                            request_timeout=120,
                        )
                        if sent_msg.photo:
                            _recent_put(fixed, "photo", sent_msg.photo[-1].file_id)
                    sent = True
                except Exception as e:  # noqa: BLE001
                    log.warning(
                        "Загрузка медиа в Telegram не прошла (%s, %s): %s — откат на превью",
                        fixed.platform,
                        kind,
                        e,
                    )

            # Контент закрыт владельцем: честно сообщаем (с автором ссылки),
            # оригинал при этом удаляется как и при обычной замене
            if not sent and restricted:
                try:
                    locked = (
                        "🔒 Владелец закрыл это видео — платформа показывает его "
                        "только авторизованным пользователям, бот бессилен. "
                        "Открыть можно по кнопке."
                    )
                    if sender:
                        locked += f"\n👤 от {sender}"
                    await message.answer(
                        locked,
                        reply_markup=_keyboard(fixed),
                        link_preview_options=LinkPreviewOptions(is_disabled=True),
                        disable_notification=_silent_now(),
                    )
                    sent = True
                except Exception:  # noqa: BLE001
                    log.exception("Не удалось отправить сообщение об ограничении")

            # Fallback: видео добыть не вышло (CDN/фиксеры не ответили) —
            # говорим об этом честно, оригинал оставляем в чате
            if not sent:
                all_video = False
                try:
                    fail = (
                        "⚠️ Видео добыть не удалось — источник не отвечает. "
                        "Попробуйте прислать ссылку позже."
                    )
                    await message.answer(
                        f"{fail}\n{text}" if text else fail,
                        link_preview_options=LinkPreviewOptions(is_disabled=True),
                        reply_markup=_keyboard(fixed),
                        disable_notification=_silent_now(),
                    )
                    sent = True
                except Exception:  # noqa: BLE001
                    sent_all = False
                    log.exception("Не удалось отправить сообщение о неудаче")

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
    async with _WORK_LOCK:  # та же очередь, что и у видео
        data = await _ytdlp_audio(url)
    if data:
        try:
            await cb.message.reply_audio(
                audio=BufferedInputFile(data, filename="audio.mp3"),
                disable_notification=_silent_now(),
                request_timeout=300,
            )
            return
        except Exception:  # noqa: BLE001
            log.exception("Не удалось отправить аудио")
    try:
        await cb.message.reply(
            "🎵 Не смог достать звук из этого видео, увы",
            disable_notification=_silent_now(),
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
    _http = aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(resolver=DoHFallbackResolver())
    )
    try:
        me = await bot.get_me()
        log.info("Запущен как @%s (id=%s)", me.username, me.id)
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        await _http.close()


if __name__ == "__main__":
    asyncio.run(main())
