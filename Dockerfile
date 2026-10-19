# syntax=docker/dockerfile:1
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 LIVE_ORDERS_DB=/state/orders.sqlite3
WORKDIR /app
COPY trading-bot/requirements.txt ./requirements.txt
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then \
      PIP_CERT=/run/secrets/proxy_ca pip install --no-cache-dir -r requirements.txt; \
    else pip install --no-cache-dir -r requirements.txt; fi
COPY trading-bot/bot.py trading-bot/helius.py trading-bot/live_trading.py ./
RUN chmod -R a+rX /app && mkdir /state && chown 1000:1000 /state
USER 1000:1000
ENTRYPOINT ["python", "/app/bot.py"]
CMD ["--live-status"]
