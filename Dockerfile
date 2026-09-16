FROM python:3.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_NO_SYNC=1 APP_HOST=0.0.0.0 APP_PORT=8090
WORKDIR /app
RUN apt-get update \
  && apt-get install -y --no-install-recommends nginx libnginx-mod-stream certbot openssl \
  && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY backend /app/backend
COPY alembic /app/alembic
COPY alembic.ini main.py entrypoint.sh /app/
COPY docker/nginx.conf /etc/nginx/nginx.conf
COPY docker/nginx-admin-available.conf /etc/nginx/conf.d/registry-http.conf
RUN mkdir -p /app/backend/data /etc/nginx/sites-available /etc/nginx/sites-enabled \
  /etc/nginx/streams-available /etc/nginx/streams-enabled /app/backend/runtime/backups \
  /var/www/certbot /etc/letsencrypt /var/lib/letsencrypt /var/log/letsencrypt \
  && chmod +x /app/entrypoint.sh
EXPOSE 8090 80 443
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s CMD ["/app/.venv/bin/python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('APP_PORT','8090')+'/health', timeout=3)"]
ENTRYPOINT ["/app/entrypoint.sh"]
