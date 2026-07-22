"""Логика распознавания и преобразования ссылок Instagram/TikTok/X/Reddit.

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
TWITTER_FIX_DOMAIN = os.getenv("TWITTER_FIX_DOMAIN", "fixupx.com")
REDDIT_FIX_DOMAIN = os.getenv("REDDIT_FIX_DOMAIN", "rxddit.com")

# Иконка источника для подписи под видео
EMOJI = {
    "instagram": "📸",
    "tiktok": "🎵",
    "x": "🐦",
    "reddit": "👽",
}

# Пути Instagram, у которых бывает видео-превью
_INSTA_PREFIXES = ("/reel/", "/reels/", "/p/", "/tv/")


@dataclass(frozen=True)
class FixedLink:
    original: str  # каноничная «красивая» ссылка (www-вид) — идёт в текст
    embed: str     # фикс-ссылка — идёт в link_preview_options.url (скрыта)
    platform: str  # instagram | tiktok | x | reddit

    @property
    def emoji(self) -> str:
        return EMOJI.get(self.platform, "🎬")


def _norm_host(netloc: str) -> str:
    host = netloc.split("@")[-1].split(":")[0].lower()
    return host[4:] if host.startswith("www.") else host


def convert(url: str) -> FixedLink | None:
    """Вернуть FixedLink (оригинал + embed + платформа) или None."""
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
            platform="instagram",
        )

    # --- TikTok: короткие ссылки vm./vt. -----------------------------------
    if host in ("vm.tiktok.com", "vt.tiktok.com"):
        code = path.strip("/").split("/")[0]
        if not code:
            return None
        return FixedLink(
            original=f"https://www.tiktok.com/t/{code}/",
            embed=f"https://{TIKTOK_FIX_DOMAIN}/t/{code}/",
            platform="tiktok",
        )

    # --- TikTok: обычные и kk-ссылки ---------------------------------------
    if host in ("tiktok.com", TIKTOK_FIX_DOMAIN, "vxtiktok.com"):
        if path in ("", "/"):
            return None
        query = f"?{parts.query}" if parts.query and "/video/" in path else ""
        return FixedLink(
            original=f"https://www.tiktok.com{path}{query}",
            embed=f"https://{TIKTOK_FIX_DOMAIN}{path}",
            platform="tiktok",
        )

    # --- X / Twitter --------------------------------------------------------
    if host in (
        "x.com", "twitter.com", "mobile.twitter.com", "mobile.x.com",
        TWITTER_FIX_DOMAIN, "fxtwitter.com", "vxtwitter.com", "fixupx.com",
    ):
        if "/status/" not in path:
            return None
        return FixedLink(
            original=f"https://x.com{path}",
            embed=f"https://{TWITTER_FIX_DOMAIN}{path}",
            platform="x",
        )

    # --- Reddit: короткие redd.it ------------------------------------------
    if host == "redd.it":
        code = path.strip("/").split("/")[0]
        if not code:
            return None
        return FixedLink(
            original=f"https://www.reddit.com/comments/{code}/",
            embed=f"https://{REDDIT_FIX_DOMAIN}/comments/{code}/",
            platform="reddit",
        )

    # --- Reddit: обычные ссылки на пост -------------------------------------
    if host in ("reddit.com", "old.reddit.com", REDDIT_FIX_DOMAIN, "vxreddit.com"):
        if "/comments/" not in path and not path.startswith("/comments/"):
            return None
        return FixedLink(
            original=f"https://www.reddit.com{path}",
            embed=f"https://{REDDIT_FIX_DOMAIN}{path}",
            platform="reddit",
        )

    return None
