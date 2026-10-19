#!/bin/sh
# Usage: entrypoint.sh web | worker | ctl | beat
set -e
case "$1" in
  web)
    python manage.py migrate --noinput
    python manage.py bootstrap
    exec gunicorn wgetpanel.wsgi -b 0.0.0.0:8000 -w "${GUNICORN_WORKERS:-3}" --access-logfile - --timeout 60
    ;;
  worker)  # runs the wget processes, one celery slot per concurrent download
    mkdir -p "$WGP_DOWNLOAD_ROOT" "$WGP_LOG_DIR"
    exec celery -A wgetpanel worker -Q downloads -n "dl@%h" -c "${WGP_WORKER_CONCURRENCY:-8}" -l INFO
    ;;
  ctl)     # scheduler ticks, reaper, maintenance
    exec celery -A wgetpanel worker -Q celery -n "ctl@%h" -c 2 -l INFO
    ;;
  beat)
    exec celery -A wgetpanel beat -l INFO -s /tmp/celerybeat-schedule
    ;;
  *) exec "$@" ;;
esac
