"""Логика распознавания и преобразования ссылок Instagram/TikTok/X.

Чистые функции без зависимостей от aiogram — удобно тестировать.
Домены фиксеров задаются ЦЕПОЧКАМИ (через запятую в env): бот пробует их
по порядку, пока какой-то не отдаст видео. Первый в списке — основной,
он же используется для embed-ссылки в fallback-превью.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit


def _domains(env: str, default: str) -> list[str]:
    return [d.strip() for d in os.getenv(env, default).split(",") if d.strip()]


# Цепочки фиксеров (порядок = приоритет). Меняются в .env без правки кода.
FIX_DOMAINS: dict[str, list[str]] = {
    "instagram": _domains("INSTAGRAM_FIX_DOMAINS", "kkinstagram.com,vxinstagram.com"),
    "tiktok": _domains("TIKTOK_FIX_DOMAINS", "kktiktok.com,tnktok.com,a.tnktok.com"),
    "x": _domains("TWITTER_FIX_DOMAINS", "d.fixupx.com,d.fxtwitter.com,d.vxtwitter.com"),
}

# Название источника для подписи под видео («𝕏» — юникод-логотип X)
LABEL = {
    "instagram": "Instagram",
    "tiktok": "TikTok",
    "x": "𝕏",
}

# Пути Instagram, у которых бывает видео-превью
_INSTA_PREFIXES = ("/reel/", "/reels/", "/p/", "/tv/")


@dataclass(frozen=True)
class FixedLink:
    original: str  # каноничная «красивая» ссылка (www-вид) — идёт в текст/кнопку
    embed: str     # фикс-ссылка основного домена (используется в fallback-превью)
    platform: str  # instagram | tiktok | x

    @property
    def label(self) -> str:
        return LABEL.get(self.platform, "Видео")

    @property
    def candidates(self) -> list[str]:
        """Все варианты фикс-ссылки по цепочке доменов платформы."""
        parts = urlsplit(self.embed)
        return [
            urlunsplit((parts.scheme, d, parts.path, parts.query, ""))
            for d in FIX_DOMAINS.get(self.platform, [])
        ]


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
    if host in ("instagram.com", "ddinstagram.com", *FIX_DOMAINS["instagram"]):
        if not path.startswith(_INSTA_PREFIXES):
            return None
        return FixedLink(
            original=f"https://www.instagram.com{path}",
            embed=f"https://{FIX_DOMAINS['instagram'][0]}{path}",
            platform="instagram",
        )

    # --- TikTok: короткие ссылки vm./vt. -----------------------------------
    if host in ("vm.tiktok.com", "vt.tiktok.com"):
        code = path.strip("/").split("/")[0]
        if not code:
            return None
        return FixedLink(
            original=f"https://www.tiktok.com/t/{code}/",
            embed=f"https://{FIX_DOMAINS['tiktok'][0]}/t/{code}/",
            platform="tiktok",
        )

    # --- TikTok: обычные и фикс-ссылки --------------------------------------
    if host in ("tiktok.com", "vxtiktok.com", *FIX_DOMAINS["tiktok"]):
        if path in ("", "/"):
            return None
        query = f"?{parts.query}" if parts.query and "/video/" in path else ""
        return FixedLink(
            original=f"https://www.tiktok.com{path}{query}",
            embed=f"https://{FIX_DOMAINS['tiktok'][0]}{path}",
            platform="tiktok",
        )

    # --- X / Twitter --------------------------------------------------------
    # Домены цепочки — медиа-поддомены d.* (только видео, без текста в превью):
    # текст твита бот добавляет сам в тело сообщения (его можно переводить).
    if host in (
        "x.com", "twitter.com", "mobile.twitter.com", "mobile.x.com",
        "fxtwitter.com", "vxtwitter.com", "fixupx.com",
        *FIX_DOMAINS["x"],
    ):
        if "/status/" not in path:
            return None
        return FixedLink(
            original=f"https://x.com{path}",
            embed=f"https://{FIX_DOMAINS['x'][0]}{path}",
            platform="x",
        )

    return None
