# Solana PAPER bot (simulazione) - immagine con versione Python fissata
FROM python:3.12.8-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/app/data TZ=Europe/Rome
RUN apt-get update && apt-get install -y --no-install-recommends tzdata ca-certificates util-linux \
    && rm -rf /var/lib/apt/lists/*
# Chromium headless per lo screenshot orario della dashboard su Telegram (telegram.hourly_screenshot).
# Build senza browser: docker compose build --build-arg INSTALL_CHROMIUM=0 (il bot manda allora un riepilogo testuale).
ARG INSTALL_CHROMIUM=1
RUN if [ "$INSTALL_CHROMIUM" = "1" ]; then apt-get update && apt-get install -y --no-install-recommends chromium \
      fonts-dejavu-core fonts-noto-cjk fonts-noto-color-emoji && rm -rf /var/lib/apt/lists/*; fi
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot ./bot
COPY dashboard ./dashboard
COPY scripts ./scripts
COPY deploy ./deploy
RUN useradd -m -u 1000 bot && mkdir -p /app/data && chown -R bot:bot /app/data \
    && chmod +x /app/deploy/entrypoint.sh /app/deploy/healthcheck.py
# l'entrypoint parte come root solo per sistemare i permessi di ./data, poi gira come utente 'bot'
ENTRYPOINT ["/app/deploy/entrypoint.sh"]
CMD ["python", "bot/paper_bot.py"]
