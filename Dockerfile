FROM python:3.12-slim AS app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends wget ca-certificates \
    nano \
    net-tools \
    iputils-ping \
    fish \
    procps \
    git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 wgp \
    && mkdir -p /data/logs && chown -R wgp /data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# static files (CSS is pre-built and committed; see tailwind/ to rebuild)
RUN DJANGO_SECRET_KEY=build-only python manage.py collectstatic --noinput && chown -R wgp /app
USER wgp
ENV WGP_DOWNLOAD_ROOT=/downloads WGP_LOG_DIR=/data/logs
EXPOSE 8000
COPY wgetpanel/settings.py wgetpanel/settings.py
COPY core/urls.py core/urls.py
HEALTHCHECK --interval=30s --timeout=15s --retries=3 CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=14).status==200 else 1)" || exit 1
ENTRYPOINT ["/app/deploy/entrypoint.sh"]
CMD ["web"]
