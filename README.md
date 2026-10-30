# wgetpanel

Django control panel for **wget** (django + celery + redis+ postgresql): live dashboard with speed graph, download queue with a
**max simultaneous downloads** scheduler, time based **speed rules**, per-user isolation, dark mode,
all configuration stored in **PostgreSQL**. Same concept as [aria2panel](https://github.com/cumulus13/aria2panel),
but the backend is plain `wget`, which has no RPC.

![Django](https://img.shields.io/badge/django-%23092E20.svg?style=flat&logo=django&logoColor=white) ![Redis](https://img.shields.io/badge/redis-%23DD0031.svg?style=flat&logo=redis&logoColor=white) ![Celery](https://img.shields.io/badge/celery-%2337814A.svg?style=flat&logo=celery&logoColor=white) ![PostgreSQL](https://img.shields.io/badge/postgresql-%23316192.svg?style=flat&logo=postgresql&logoColor=white) ![CI Status](https://github.com/cumulus13/wgetpanel/actions/workflows/ci.yml/badge.svg)

<p align="center">
  <img src="https://raw.githubusercontent.com/licface15/images/refs/heads/main/wgetpanel/wgetpanel_support.png" width="80%" alt="Powered by">
</p>


## How it works without RPC

```
browser ──HTTP/JSON──► web (Django + gunicorn) ──► PostgreSQL (queue, settings, history)
                         │  ▲                              ▲
              ctl msgs   ▼  │ live speed                   │
                        Redis ◄──────────────┐             │
                                             │             │
 beat ─► ctl worker (scheduler tick, reaper) ┘─────────────┘
          │ celery task per download
          ▼
 downloads worker ── spawns ──► wget -c -O <file> <url>   (one process group per download)
          └─ samples the size of <file> every second ──► speed, ETA, graph
```

```mermaid
graph TD
    %% Nodes
    Browser["browser"]
    Web["web<br/>(Django + gunicorn)"]
    Postgres["PostgreSQL<br/>(queue, settings, history)"]
    Redis["Redis"]
    Beat["beat"]
    CtlWorker["ctl worker<br/>(scheduler tick, reaper)"]
    DownloadsWorker["downloads worker"]
    Wget["wget -c -O &lt;file&gt; &lt;url&gt;<br/>(one process group per download)"]

    %% Connections
    Browser -- "HTTP/JSON" --> Web
    Web --> Postgres
    Web <-->|"live speed"| Redis
    CtlWorker -->|"ctl msgs"| Redis
    Beat --> CtlWorker
    CtlWorker -->|"celery task per download"| DownloadsWorker
    DownloadsWorker -- "spawns" --> Wget
    DownloadsWorker -. "samples the size of &lt;file&gt; every second" .-> Redis
    DownloadsWorker -. "samples the size of &lt;file&gt; every second" .-> Postgres
```

[![Architecture](https://raw.githubusercontent.com/licface15/images/refs/heads/main/wgetpanel/wgetpanel_architeture.jpg)](https://raw.githubusercontent.com/licface15/images/refs/heads/main/wgetpanel/wgetpanel_architeture.jpg)

<p align="center">
  <a href="https://raw.githubusercontent.com/licface15/images/refs/heads/main/wgetpanel/wgetpanel_screenshot1.png" target="_blank">
    <img src="https://raw.githubusercontent.com/licface15/images/refs/heads/main/wgetpanel/wgetpanel_screenshot1.png" width="500" alt="Screenshot 1">
  </a>
  <a href="https://raw.githubusercontent.com/licface15/images/refs/heads/main/wgetpanel/wgetpanel_screenshot2.png" target="_blank">
    <img src="https://raw.githubusercontent.com/licface15/images/refs/heads/main/wgetpanel/wgetpanel_screenshot2.png" width="500" alt="Screenshot 2">
  </a>
  <a href="https://raw.githubusercontent.com/licface15/images/refs/heads/main/wgetpanel/wgetpanel_screenshot3.png" target="_blank">
    <img src="https://raw.githubusercontent.com/licface15/images/refs/heads/main/wgetpanel/wgetpanel_screenshot3.png" width="500" alt="Screenshot 3">
  </a>
  <a href="https://raw.githubusercontent.com/licface15/images/refs/heads/main/wgetpanel/wgetpanel_screenshot4.png" target="_blank">
    <img src="https://raw.githubusercontent.com/licface15/images/refs/heads/main/wgetpanel/wgetpanel_screenshot4.png" width="500" alt="Screenshot 4">
  </a>
  <a href="https://raw.githubusercontent.com/licface15/images/refs/heads/main/wgetpanel/wgetpanel_screenshot5.png" target="_blank">
    <img src="https://raw.githubusercontent.com/licface15/images/refs/heads/main/wgetpanel/wgetpanel_screenshot5.png" width="500" alt="Screenshot 5">
  </a>
  <a href="https://raw.githubusercontent.com/licface15/images/refs/heads/main/wgetpanel/wgetpanel_screenshot6.png" target="_blank">
    <img src="https://raw.githubusercontent.com/licface15/images/refs/heads/main/wgetpanel/wgetpanel_screenshot6.png" width="500" alt="Screenshot 6">
  </a>
</p>


* **Progress / speed graph**: wget exposes nothing to query, so the worker measures the **size of the file
  being written** once per second (speed = bytes delta / time delta, smoothed) and takes the total from the
  `Length:` line in wget's own log. Values go to Redis (hash + capped series), the dashboard polls one
  `/api/snapshot/` endpoint. The database only gets a checkpoint every 5 s.
* **Pause / resume**: wget is terminated, `wget -c` continues from the bytes on disk (needs a server that
  supports `Range`; on servers that ignore it, resume restarts from zero and the file is still correct, tested).
* **Speed rules**: wget cannot change `--limit-rate` while running, so when the effective limit changes the
  worker restarts wget with `-c` (one short reconnect). Limits apply **per running download**; wget cannot
  enforce one shared cap.
* **Scheduler**: every tick locks a singleton row (`SELECT … FOR UPDATE`) so several beat/worker processes can
  never exceed *max simultaneous downloads*. Queue is FIFO, reorderable, items can have a start time.
* **Crash recovery**: a running download without heartbeat for 30 s (worker killed, broker lost the task) is
  put back in the queue and resumed with `-c`. Orphaned wget processes are killed (only if `/proc` confirms
  the PID is *our* wget for that download).
* **Why Celery + Redis**: one long-running task per download gives you process isolation, horizontal
  concurrency (`WGP_WORKER_CONCURRENCY`) and restarts for free. RabbitMQ is not needed: Redis is required anyway
  for the live metrics. (`acks_late` is off on purpose: with Redis' visibility timeout a long download would be
  redelivered and run twice; the reaper handles lost workers instead.)

## Quick start (Docker)

```bash
cp .env.example .env     # edit DJANGO_SECRET_KEY, DJANGO_SUPERUSER_PASSWORD, ...
docker compose up -d --build
# http://localhost:8000
```

Without Docker (needs `wget`, PostgreSQL, Redis):

```bash
python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
cp .env.example .env && set -a && . ./.env && set +a      # adjust hosts to localhost
python manage.py migrate && python manage.py bootstrap && python manage.py collectstatic --noinput
gunicorn wgetpanel.wsgi -b 0.0.0.0:8000                                     # web
celery -A wgetpanel worker -Q downloads -n dl@%h -c 8 -l INFO               # runs wget
celery -A wgetpanel worker -Q celery -n ctl@%h -c 2 -l INFO                 # scheduler tick / reaper
celery -A wgetpanel beat -l INFO                                            # periodic tasks
```

`deploy/smoke_test.py` drives a running stack end to end (login → add → pause → resume → completed).

## Frontend: Tailwind CSS

Tailwind v4, built with its CLI (no CDN in production, no runtime JS framework). The compiled
`core/static/core/css/app.css` is committed, so deploying needs no Node. To change the UI:

```bash
cd tailwind && npm install && npm run build:all     # watch: npm run watch
```

Chart.js is vendored (no third party requests). All user supplied text is inserted with `textContent`.

## Configuration

Environment: database, broker, secret, hosts/TLS flags (see `.env.example`). Everything else is in the
**Settings** page (max simultaneous downloads, per-user queue cap, retention, default retries/timeout/UA/limit,
allowed download directories, SSRF guard) and the **Speed rules** page.

## Auto retry

Per download (`retry` in the API, `--retry` in the CLI, a select in the web form) or as a server default
(`retry.default_max`): a number of consecutive failures, `always` / `-1` = never give up, `0` / `off` = no retry.
The delay starts at `retry.delay_seconds` and doubles after every failed attempt up to `retry.max_delay_seconds`;
the download goes back to *queued* with a start time and `wget -c` continues from the bytes on disk. A run that
downloaded new bytes resets the counter. **Only temporary errors are retried**: network failure (wget exit 4) and
HTTP 408 / 425 / 429 / 5xx. A 404, wrong password, TLS error or full disk is never retried, not even with `always`.
Default is off (`retry.default_max = 0`).

## Deleting the real files

Cancel / remove / purge accept `delete_files` (web: a prompt, API: `delete_files: true|false`, CLI:
`--delete-files` / `--keep-files`). Omitting it applies the setting `files.delete_on_remove` (default off), and
`files.delete_on_retention` does the same for the automatic history cleanup. Safety: the parent directory of the file is
resolved and must be inside an allowed download directory, then only that entry is removed (a symlink is removed, never its
target). Running downloads are stopped first and their file is deleted by the worker (`files_pending` in the response).

## Notifications (ntfy and GNTP)

Two kinds of channel, both optional (nothing is sent unless one is configured):

* **Global channel** (environment): `NTFY_URL` + `NTFY_TOPIC` (token or user/password optional) and/or `GNTP_HOST`.
  It receives the events of **all users** (`NOTIFY_GLOBAL_SCOPE=all`, default) or of nobody (`none`, for installs where
  users must not see each other's file names).
* **Per-user channels**: every user enables their own targets on the *Notifications* page, with `wgp notify` or
  `/api/v1/notify`, and only ever receives **their own** downloads.
  * **ntfy:** the server generates a private topic `<NTFY_USER_TOPIC_PREFIX>-<96 random bits>` and publishes to it with the
    server's credentials; the user just subscribes to the topic shown (it can be regenerated if it leaks). The ntfy
    credentials must be allowed to publish to that prefix. On a public server such as ntfy.sh anyone who knows a topic name
    can read it, so the random name is the only protection; a self-hosted ntfy with access control is the safer choice.
  * **GNTP:** off unless the admin lists networks in `NOTIFY_USER_GNTP_ALLOW` (CIDRs). The *server* connects to the host the
    user enters, so without that allow-list users could use it to probe the internal network. The host must resolve inside the
    allowed networks, checked when saving **and again at every send**. The password is stored encrypted.

Events: `completed` (alias `success`), `error`, `paused`, `retry`, `canceled` (off by default); the server default is
`NOTIFY_EVENTS`, each user may choose their own. Messages are sent from a Celery task with retries, only to the transport
that failed. The task payload holds the message and a target key; credentials are looked up at delivery, never put in the
broker. A message contains the file name, size, host and the reason, never the full URL (signed links carry secrets).

**Retry notices have their own interval**, independent of the retry delay: `NOTIFY_RETRY_INTERVAL=60` means at most one
retry notice per download and per target every 60 seconds, whether the retry delay is 3 s or 30 s. The first retry is announced
at once, further retries in the window are counted, and the next notice says "N more retries since the last notice". Each user
can set their own interval. The final `completed` / `error` message is never delayed and reports how often the download was
retried. `0` notifies on every retry, `-1` never. Check the setup with `wgp notify-test` (your own targets) or
`wgp notify-test --global` (staff), or `POST /api/v1/notify/test`.

## Command line client

`cli/wgp/wgp.py` covers the whole API (list, detail, add with schedule/retry/limits, actions, get, live `watch`, ASCII
speed graphs, settings, rules, tokens). See [cli/README.md](cli/README.md).

## Mobile API

`/api/v1/*` with per-device bearer tokens, a Server-Sent Events stream for live status/progress (no Firebase, no
ntfy app needed) and an OpenAPI document at `/api/v1/openapi.json`. Details, event list and Android/iOS notes:
[docs/MOBILE.md](docs/MOBILE.md). The web UI keeps using its own session based `/api/*`.

## Security notes

* Login required; settings and rules need `is_staff`; users only see their own downloads (staff see all).
* CSRF on all POSTs, HttpOnly session/CSRF cookies, `X-Frame-Options: DENY`, nosniff.
* wget runs without a shell; options come from a whitelist and are written to a private `0600` wgetrc, so
  credentials never appear in `ps`. HTTP passwords are encrypted at rest (Fernet).
* Downloads are confined to allowed directories (symlinks resolved). File names are sanitised and made unique.
* **SSRF guard** (on by default): URLs resolving to loopback/private/link-local addresses are rejected.
  **Limitation:** wget follows redirects itself and they are not re-checked, and DNS can change between check
  and download. For hostile multi-tenant use, also block internal ranges at the network level (egress firewall).
* A finished file is only served to its owner / staff.
* Mobile tokens: only the SHA-256 is stored, per device, expiring, revocable; login is rate limited (10 failures / 15 min per IP + username).
  **Limitation:** the limiter keys on `REMOTE_ADDR`, and gunicorn does not rewrite it from `X-Forwarded-For`. Behind a reverse proxy every client therefore shares one IP, the limit effectively becomes per username, and anyone can lock a known username out for 15 minutes. Rate limit per client IP at the proxy (e.g. nginx `limit_req` on `/api/v1/auth/token`) until this is made proxy-aware.

## Known limitations

* Resume needs `Range` support (otherwise restart from zero).
* Downloads run on the host(s) of the `worker` containers; with several worker hosts the download directory must
  be a shared filesystem.
* Crash recovery takes up to ~40 s (30 s heartbeat grace + 10 s reaper interval).
* Speed graph resolution is 1 s per download, 2 s for the aggregate series.
* File name from `Content-Disposition` is not used (name is taken from the URL or the form).

## Tests

```bash
pip install -r requirements-dev.txt                                   # adds requests + asciichartpy for the CLI tests
export DJANGO_DEBUG=1 DATABASE_URL=postgres://user:pass@localhost/db   # role needs CREATEDB
export TEST_REDIS_URL=redis://localhost:6379/15                         # DB is flushed!
python manage.py test
```

The suite (215 tests) runs a real `wget` against a local Range-capable server: completion + checksum, 404/refused/auth
errors, pause→resume, cancel, speed-rule restart, `--limit-rate`, speed series, scheduler limits and concurrent
ticks, reaper, API permissions/CSRF/SSRF/path traversal.


## License

[MIT](LICENSE)

## 👤 Author
        
[Hadi Cahyadi](mailto:cumulus13@gmail.com)
    

[![Buy Me a Coffee](https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png)](https://www.buymeacoffee.com/cumulus13)

[![Donate via Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/cumulus13)
 
[Support me on Patreon](https://www.patreon.com/cumulus13)
