# DevSecOps: минимальный базовый образ, пиновая версия, непривилегированный пользователь
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# ffmpeg: faststart-ремукс скачанных видео (без него Telegram не стримит mp4 от IG)
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY linkfix.py bot.py ./

# Непривилегированный пользователь (DevSecOps: не root)
RUN useradd --create-home --shell /usr/sbin/nologin botuser
USER botuser

CMD ["python", "bot.py"]
