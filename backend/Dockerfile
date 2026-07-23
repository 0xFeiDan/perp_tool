FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN groupadd --gid 10001 trader \
    && useradd --create-home --uid 10001 --gid 10001 trader

COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=trader:trader backend/ ./backend/
COPY --chown=trader:trader index.html app.js styles.css mt5-readonly.css ./

USER trader
EXPOSE 8790

ENTRYPOINT ["python", "-m", "backend.container_entrypoint"]

# The application binds only inside the compose network. The loopback Caddy
# proxy is reached through Tailscale Serve, which terminates HTTPS.
CMD ["uvicorn", "main:app", "--app-dir", "/app/backend", "--host", "0.0.0.0", "--port", "8790", "--no-access-log"]
