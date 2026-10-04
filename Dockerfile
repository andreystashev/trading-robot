FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY trading_bot/requirements.txt /app/requirements.txt
COPY trading_bot/constraints.txt /app/constraints.txt
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r /app/requirements.txt \
    && useradd --create-home --uid 10001 bot
COPY --chown=bot:bot trading_bot /app/trading_bot
COPY --chown=bot:bot tools /app/tools
RUN mkdir -p /app/trading_bot/.runtime /app/trading_bot/logs /app/trading_bot/reports \
    && chown -R bot:bot /app/trading_bot
USER bot
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/status', timeout=3).close()"
CMD ["python", "trading_bot/main.py", "panel"]
