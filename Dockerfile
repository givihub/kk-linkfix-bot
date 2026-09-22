# DevSecOps: минимальный базовый образ, пиновая версия, непривилегированный пользователь
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DENO_DIR=/tmp/deno \
    XDG_CACHE_HOME=/tmp/cache

WORKDIR /app

# ffmpeg: faststart-ремукс скачанных видео (без него Telegram не стримит mp4 от IG)
# deno: JS-runtime для yt-dlp — без него YouTube троттлит скачивание до нуля
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl unzip ca-certificates \
    && curl -fsSL -o /tmp/deno.zip https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip \
    && unzip -q /tmp/deno.zip -d /usr/local/bin && chmod +x /usr/local/bin/deno && rm /tmp/deno.zip \
    && apt-get purge -y unzip && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY linkfix.py bot.py ./

# Непривилегированный пользователь (DevSecOps: не root)
RUN useradd --create-home --shell /usr/sbin/nologin botuser
USER botuser

CMD ["python", "bot.py"]
