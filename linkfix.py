"""Логика распознавания и преобразования ссылок Instagram/TikTok.

Чистые функции без зависимостей от aiogram — удобно тестировать.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit

# Домены «фиксеров» (генерируют видео-превью для Telegram).
# Выносим в env, чтобы при смерти сервиса поменять одной строкой без правки кода.
INSTAGRAM_FIX_DOMAIN = os.getenv("INSTAGRAM_FIX_DOMAIN", "kkinstagram.com")
TIKTOK_FIX_DOMAIN = os.getenv("TIKTOK_FIX_DOMAIN", "kktiktok.com")

# Пути Instagram, у которых бывает видео-превью
_INSTA_PREFIXES = ("/reel/", "/reels/", "/p/", "/tv/")


@dataclass(frozen=True)
class FixedLink:
    original: str  # каноничная «красивая» ссылка (www-вид) — идёт в текст
    embed: str     # kk-ссылка — идёт в link_preview_options.url (скрыта)


def _norm_host(netloc: str) -> str:
    host = netloc.split("@")[-1].split(":")[0].lower()
    return host[4:] if host.startswith("www.") else host


def convert(url: str) -> FixedLink | None:
    """Вернуть пару (оригинал, embed) или None, если ссылка не подходит."""
    try:
        parts = urlsplit(url if "://" in url else "https://" + url)
    except ValueError:
        return None
    host = _norm_host(parts.netloc)
    path = parts.path or "/"

    # --- Instagram ---------------------------------------------------------
    if host in ("instagram.com", INSTAGRAM_FIX_DOMAIN, "ddinstagram.com"):
        if not path.startswith(_INSTA_PREFIXES):
            return None
        return FixedLink(
            original=f"https://www.instagram.com{path}",
            embed=f"https://{INSTAGRAM_FIX_DOMAIN}{path}",
        )

    # --- TikTok: короткие ссылки vm./vt. -----------------------------------
    if host in ("vm.tiktok.com", "vt.tiktok.com"):
        code = path.strip("/").split("/")[0]
        if not code:
            return None
        return FixedLink(
            original=f"https://www.tiktok.com/t/{code}/",
            embed=f"https://{TIKTOK_FIX_DOMAIN}/t/{code}/",
        )

    # --- TikTok: обычные и kk-ссылки ---------------------------------------
    if host in ("tiktok.com", TIKTOK_FIX_DOMAIN, "vxtiktok.com"):
        if path in ("", "/"):
            return None
        query = f"?{parts.query}" if parts.query and "/video/" in path else ""
        return FixedLink(
            original=f"https://www.tiktok.com{path}{query}",
            embed=f"https://{TIKTOK_FIX_DOMAIN}{path}",
        )

    return None
