"""kk-linkfix-bot — превращает ссылки Instagram/TikTok в группе в инлайн-видео.

Механика: бот видит сообщение со ссылкой, удаляет его и отправляет вместо него
аккуратное сообщение: видео, а под ним подпись «🎬 Ссылка», где:
  * видимая гиперссылка «Ссылка» ведёт на оригинальный www-адрес;
  * видео-превью генерируется по скрытому kk-адресу
    (link_preview_options.url — URL превью не обязан присутствовать в тексте).

Скачивания видео нет — превью отдаёт Telegram, боту хватает минимума ресурсов.
"""
from __future__ import annotations

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ChatType, MessageEntityType, ParseMode
from aiogram.types import LinkPreviewOptions, Message

from linkfix import FixedLink, convert

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("kk-linkfix-bot")

router = Router()


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

    sent_all = True
    for fixed in links:
        text = f'{fixed.emoji} <a href="{fixed.original}">Ссылка</a>'
        try:
            await message.answer(
                text,
                link_preview_options=LinkPreviewOptions(
                    url=fixed.embed,
                    prefer_large_media=True,
                    # превью (видео) над текстом — подпись «Ссылка» оказывается снизу
                    show_above_text=True,
                ),
            )
        except Exception:  # noqa: BLE001
            sent_all = False
            log.exception("Не удалось отправить сообщение с превью")

    # Удаляем исходное сообщение только если все замены отправились
    if sent_all:
        try:
            await message.delete()
        except Exception:  # noqa: BLE001
            log.warning(
                "Нет прав на удаление сообщения в чате %s — оставляю оригинал",
                message.chat.id,
            )


async def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("BOT_TOKEN не задан (см. .env.example)")

    proxy_url = os.getenv("PROXY_URL") or None
    session = AiohttpSession(proxy=proxy_url) if proxy_url else None
    if proxy_url:
        log.info("Работаю через прокси: %s", proxy_url)

    bot = Bot(
        token=token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)

    me = await bot.get_me()
    log.info("Запущен как @%s (id=%s)", me.username, me.id)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, allowed_updates=["message"])


if __name__ == "__main__":
    asyncio.run(main())
