# Минимальный образ + ffmpeg + aria2 + сертификаты
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# системные пакеты
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg aria2 ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# создаём непривилегированного пользователя с uid/gid 1000
RUN useradd -m -u 1000 -s /bin/bash appuser

# каталоги в контейнере
RUN mkdir -p /app /data /cookies \
    && chown -R appuser:appuser /app /data /cookies

WORKDIR /app

# зависимости
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r /app/requirements.txt

# код
COPY app/ /app/

# смена пользователя
USER appuser

# простой healthcheck — поднимается ли интерпретатор и видит файл
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
    CMD python -c "import os; assert os.path.exists('/app/bot.py')"

CMD ["python", "-u", "/app/bot.py"]