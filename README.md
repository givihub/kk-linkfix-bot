# kk-linkfix-bot

Telegram-бот для групп: превращает ссылки Instagram (Reels/посты), TikTok и X/Twitter в сообщения с инлайн-видео, которое можно смотреть прямо в чате.

Подпись под видео: **Instagram · Ссылка**, **TikTok · Ссылка**, **𝕏 · Ссылка**. (YouTube не нужен — Telegram проигрывает его сам.)

## Как работает

1. Кто-то кидает в группу ссылку `https://www.instagram.com/reel/...` или TikTok.
2. Бот удаляет исходное сообщение.
3. Вместо него отправляет видео, а под ним подпись вида `Instagram · Ссылка`.
   - Гиперссылка «Ссылка» ведёт на **оригинальный** www-адрес.
   - Видео-превью генерируется по скрытому адресу фиксера (`kkinstagram.com` / `kktiktok.com`) через `link_preview_options.url` — сам «кривой» адрес нигде не виден.

Бот **ничего не скачивает** — видео отдаёт Telegram по превью. Поэтому боту хватает минимальных ресурсов, нет лимита 50 МБ и проблем с блокировками платформ.

## Требования

- Docker + Docker Compose
- Токен бота от [@BotFather](https://t.me/BotFather)
- В группе бот должен быть **администратором** (право «Удаление сообщений»); privacy mode выключен (`/setprivacy` → Disable) либо достаточно прав админа

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
