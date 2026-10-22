#!/bin/sh
# Usage: entrypoint.sh web | worker | ctl | beat
set -e
case "$1" in
  web)
    python manage.py migrate --noinput
    python manage.py bootstrap
    # gthread: every open /api/v1/events stream (mobile app) occupies one thread for its lifetime
    exec gunicorn wgetpanel.wsgi -b 0.0.0.0:8000 -k gthread -w "${GUNICORN_WORKERS:-3}" --threads "${GUNICORN_THREADS:-32}" --access-logfile - --timeout 60 --graceful-timeout 20
    ;;
  worker)  # runs the wget processes, one celery slot per concurrent download
    mkdir -p "$WGP_DOWNLOAD_ROOT" "$WGP_LOG_DIR"
    exec celery -A wgetpanel worker -Q downloads -n "dl@%h" -c "${WGP_WORKER_CONCURRENCY:-8}" --loglevel "${WGP_WORKER_LOG_LEVEL:-INFO}"
    ;;
  ctl)     # scheduler ticks, reaper, maintenance
    exec celery -A wgetpanel worker -Q celery -n "ctl@%h" -c 2 --loglevel "${WGP_SCHEDULER_LOG_LEVEL:-INFO}"
    ;;
  beat)
    exec celery -A wgetpanel beat --loglevel "${WGP_BEAT_LOG_LEVEL:-INFO}" -s /tmp/celerybeat-schedule
    ;;
  *) exec "$@" ;;
esac
