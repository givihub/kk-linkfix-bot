# kk-linkfix-bot

Telegram-бот для групп: превращает ссылки Instagram (Reels/посты), TikTok и X/Twitter в сообщения с инлайн-видео, которое можно смотреть прямо в чате.

Подпись под видео: **Instagram** / **TikTok** / **𝕏**. (YouTube не нужен — Telegram проигрывает его сам.)

## Как работает

1. Кто-то кидает в группу ссылку `https://www.instagram.com/reel/...` или TikTok.
2. Бот **удаляет** исходное сообщение и отправляет вместо него видео. Под видео:
   - автор и текст поста (из OG-метатегов caption-домена: TikTok — `tnktok.com`, X — `fixupx.com`; Instagram метаданные не отдаёт);
   - строка «👤 от Имя» — кликабельное имя отправителя ссылки;
   - инлайн-кнопка «Открыть в Instagram/TikTok/𝕏 ↗» со ссылкой на оригинал.
3. Видео-превью генерируется по скрытому адресу фиксера через `link_preview_options.url` — сам «кривой» адрес нигде не виден. Замена отправляется без звука.

Бот **ничего не скачивает** — видео отдаёт Telegram по превью. Поэтому боту хватает минимальных ресурсов, нет лимита 50 МБ и проблем с блокировками платформ.

## Требования

- Docker + Docker Compose
- Токен бота от [@BotFather](https://t.me/BotFather)
- Privacy mode выключен (`/setprivacy` → Disable), чтобы бот видел ссылки в группе
- Бот — админ группы с правом «Удаление сообщений» (без него оригиналы просто не будут удаляться)

## Запуск

```bash
cp .env.example .env   # вписать BOT_TOKEN (и PROXY_URL при необходимости)
docker compose up -d --build
```

Логи: `docker compose logs -f`

## Конфигурация (.env)

| Переменная | Описание |
|---|---|
| `BOT_TOKEN` | токен от BotFather (секрет, только в `.env`) |
| `PROXY_URL` | прокси до api.telegram.org (для серверов в РФ), пусто = напрямую |
| `INSTAGRAM_FIX_DOMAIN` | домен фиксера Instagram (по умолчанию `kkinstagram.com`) |
| `TIKTOK_FIX_DOMAIN` | домен фиксера TikTok (по умолчанию `kktiktok.com`) |
| `TWITTER_FIX_DOMAIN` | домен фиксера X/Twitter (по умолчанию `fixupx.com`) |

Если домен-фиксер перестал работать — просто замените его в `.env` на аналог (`ddinstagram.com`, `vxtiktok.com`, ...) и перезапустите: `docker compose up -d`.

## Тесты

```bash
python3 test_linkfix.py
```

## Деплой (текущий прод)

Сервер FreeTier (Cloud.ru), каталог `/opt/kk-linkfix-bot`, Telegram API — через локальный Xray-прокси `http://127.0.0.1:7890`. Меры безопасности — см. [DEVSECOPS.md](DEVSECOPS.md).
