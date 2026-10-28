# wgp: command line client for [github.com/cumulus13/wgetpanel](https://github.com/cumulus13/wgetpanel)

wgetpanel cli client. talks to the server's **API v1** with a per-device token. Needs `requests` and
`asciichartpy` (graphs).

```bash
pip install wgp
# or 
pip install git+https://github.com/cumulus13/wgetpanel/cli
# or
pip install -r cli/requirements.txt
wgp login http://localhost:8000 -u admin     # asks for the password, token saved in ~/.config/wgetpanel/config.json (0600)
```

Optional single executable (built and run on Linux; a Windows `.exe` has to be built on Windows, not tested here):

```bash
pip install pyinstaller && pyinstaller --onefile --name wgp cli/wgp.py     # -> dist/wgp
```

Global options (`--host --token --timeout --insecure-tls --json`) work before or after the command.
Environment: `WGP_HOST`, `WGP_TOKEN`, `WGP_CONFIG`, `WGP_PASSWORD`, `WGP_HTTP_PASSWORD`, `NO_COLOR`, `WGP_ASCII=1`.
Exit codes: `0` ok, `1` refused / nothing affected / a waited download failed, `2` usage, login or connection problem.

| Command | What it does |
|---|---|
| `login HOST -u USER [--device NAME]` / `logout` / `whoami` | token handling; `logout` revokes the token on the server |
| `tokens` / `tokens revoke ID` | devices of your account |
| `health` | database + redis status (no login needed) |
| `summary [--no-graph]` | counts, speed, scheduler state, total speed graph |
| `list [-s STATUS] [-n N] [-q]` | download table |
| `show ID [--log [N]] [--no-graph]` | details, retry state, error, speed graph, wget log tail |
| `add URL... [-f FILE] [-d DIR] [-o NAME] [--limit KB/S] [--retry N\|always\|off] [--at 'HH:MM'\|--in 2h] [-w]` | queue downloads; more options: `--tries --timeout-net --user-agent --referer -H --insecure --auth-user` |
| `wait ID...` | follow until finished (exit 0 only if all completed) |
| `pause resume retry cancel remove ID...` | ids, ranges `3-7`, lists `1,4,9` |
| `cancel/remove ID --delete-files` / `--keep-files` | also delete the file on the server; no flag = server setting `files.delete_on_remove` |
| `top bottom up down ID...` | reorder queued downloads |
| `purge [ID...] [--delete-files]` | remove finished downloads from the list |
| `get ID [-o PATH] [--force] [--no-resume]` | copy a finished file from the server (progress bar); an interrupted transfer **resumes** when you run the same command again |
| `graph [ID] [--follow]` | ASCII speed graph (total or one download) |
| `watch [ID...]` | live dashboard over SSE: summary, speed graph, table |
| `events [--json] [--type T] [--count N]` | raw event stream for scripts |
| `settings` / `settings get KEY` / `settings set KEY VALUE ...` | staff: server settings (`-v` shows help texts) |
| `notify` / `notify set --ntfy on --events completed,error --retry-interval 60` / `notify rotate-topic` | your own notification targets: a private ntfy topic (the command prints what to subscribe to), optional GNTP receiver |
| `notify-test [--global]` | send a test message to your own targets (`--global`: the server's channel, staff) and show the result per transport |
| `rules` / `rules add NAME --from 09:00 --to 17:00 --limit 256 --days mon-fri` / `edit` / `enable` / `disable` / `delete ID` | staff: speed rules |

Examples:

```bash
wgp add https://example.com/big.iso --retry always --limit 500 --at 02:00      # keep trying forever, 500 KB/s, start tonight
wgp add -f urls.txt --retry 5 -w                                               # 5 consecutive failures allowed, wait for all
wgp watch                                                                      # live view, Ctrl-C to quit
wgp remove 3-9 --delete-files                                                  # drop from the list AND delete the files
wgp settings set retry.default_max 3 files.delete_on_remove true
wgp list --json | jq '.downloads[] | select(.status=="error") | .id'
```

Notes: `--at HH:MM` / `--in` use the local clock of the machine running `wgp`. `get` resumes via HTTP Range + `If-Range` (the partial data is kept as `NAME.part` with its ETag in `NAME.part.etag`; if the file changed on the server it starts over). Colors and unicode bars are switched off for pipes,
`NO_COLOR` and `WGP_ASCII=1`.

**for cli args with colors use `rchf` (pip install rchf)**

---

## 👤 Author
        
[Hadi Cahyadi](mailto:cumulus13@gmail.com)
    

[![Buy Me a Coffee](https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png)](https://www.buymeacoffee.com/cumulus13)

[![Donate via Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/cumulus13)
 
[Support me on Patreon](https://www.patreon.com/cumulus13)
