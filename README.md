# send-imessage

Local macOS HTTP API for **iMessage / SMS**: read full conversations (you + others), serve attachment files, and send text / photos / videos via Messages.app.

`relay.py` is a separate read-only poller that POSTs allowed chats to an HTTPS webhook. It cannot send.

Designed for same-machine clients (n8n, scripts, AI agents). Bind to `127.0.0.1` so message data stays on the Mac.

**Repo:** https://github.com/Ghostcoded-2025/send-imessage

---

## Agent contract (copy this)

| Item | Value |
|------|--------|
| Base URL | `http://127.0.0.1:8000` |
| Auth | Header `X-API-Token: <token>` on **every** route except `GET /health` |
| Token env | **`IMESSAGE_API_TOKEN` (required)** — server will not start without it |
| Interactive docs | `GET /docs` |
| Host OS | macOS with Messages signed in |
| Send path | AppleScript → Messages.app |
| Read path | Read-only SQLite → `~/Library/Messages/chat.db` |

**Safe defaults for agents**

1. Always send `X-API-Token`.
2. Call `GET /health` first; 503 on reads usually means Full Disk Access is missing.
3. Paginate: `after_rowid` → response `next_after_rowid` until `count == 0`.
4. Use `direction=all` for full threads (inbound + outbound).
5. Do **not** bind `0.0.0.0` unless a same-host Docker client needs it.
6. Never commit real tokens — use `.env` (gitignored) or shell env.

---

## Quick start

```bash
git clone https://github.com/Ghostcoded-2025/send-imessage.git
cd send-imessage

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env — set IMESSAGE_API_TOKEN to a long random string
export $(grep -v '^#' .env | xargs)

# generate a token if needed:
# python3 -c "import secrets; print(secrets.token_urlsafe(32))"

uvicorn main:app --host 127.0.0.1 --port 8000
```

OpenAPI: http://127.0.0.1:8000/docs

### Background

```bash
source .venv/bin/activate
export $(grep -v '^#' .env | xargs)
nohup uvicorn main:app --host 127.0.0.1 --port 8000 > server.log 2>&1 &
echo $! > uvicorn.pid

# stop
kill "$(cat uvicorn.pid)" && rm -f uvicorn.pid
```

**Docker n8n on the same Mac:** use `http://host.docker.internal:8000`. Only then consider `--host 0.0.0.0`.

---

## macOS permissions (required)

| Capability | Why | Where |
|------------|-----|--------|
| **Full Disk Access** | Read `chat.db` + attachment files | System Settings → Privacy & Security → Full Disk Access → enable the app that launches uvicorn (Terminal, iTerm, Cursor, etc.) |
| **Automation → Messages** | Send text/media | Prompt on first send, or Privacy → Automation |
| Messages signed in | Delivery | Messages.app |

Without Full Disk Access, SQLite returns **authorization denied** → API **503**.

---

## Endpoints

| Method | Path | Auth | Notes |
|--------|------|------|--------|
| `GET` | `/health` | no | `{ ok, chat_db_exists, bind_hint }` |
| `GET` | `/chats` | yes | List conversations (`limit` 1–1000) |
| `GET` | `/messages` | yes | History; both directions by default |
| `GET` | `/attachments/{id}/file` | yes | Binary photo/video/file from Messages library |
| `POST` | `/send-imessage` | yes | JSON `{ to, text, service? }` |
| `POST` | `/send-imessage-with-attachment` | yes | multipart: `to`, `file`, `caption?`, `service?` |

Auth failure → **401**.

### `GET /messages` query params

| Param | Default | Notes |
|-------|---------|--------|
| `after_rowid` | `0` | Exclusive cursor (backfill from `0`) |
| `before_rowid` | omit | Exclusive upper bound |
| `chat_identifier` | omit | One thread (`+1555…`, email, or group id) |
| `handle` | omit | Filter by handle id |
| `direction` | `all` | `all` \| `inbound` \| `outbound` |
| `limit` | `100` | Max 500 |
| `include_attachments` | `true` | Metadata + `local_url` |

### Message shape

```json
{
  "rowid": 49281,
  "guid": "...",
  "direction": "inbound",
  "is_from_me": false,
  "handle": "+15551234567",
  "chat_id": 12,
  "chat_identifier": "+15551234567",
  "display_name": null,
  "service": "iMessage",
  "text": "hello",
  "has_attachments": true,
  "timestamp_unix": 1710000000.0,
  "attachments": [
    {
      "id": 123,
      "transfer_name": "IMG_1234.jpg",
      "mime_type": "image/jpeg",
      "exists": true,
      "local_url": "/attachments/123/file"
    }
  ]
}
```

`text` uses `message.text`, or a best-effort decode of `attributedBody` when text is null.

---

## Curl recipes

```bash
TOKEN="${IMESSAGE_API_TOKEN:?set IMESSAGE_API_TOKEN}"
H=(-H "X-API-Token: $TOKEN")

curl -s http://127.0.0.1:8000/health
curl -s "${H[@]}" http://127.0.0.1:8000/chats

# URL-encode + as %2B
curl -s "${H[@]}" \
  'http://127.0.0.1:8000/messages?after_rowid=0&chat_identifier=%2B15551234567&direction=all&limit=100'

curl -OJ "${H[@]}" http://127.0.0.1:8000/attachments/123/file

curl -s "${H[@]}" -X POST http://127.0.0.1:8000/send-imessage \
  -H 'Content-Type: application/json' \
  -d '{"to":"+15551234567","text":"Hello","service":"imessage"}'

curl -s "${H[@]}" -X POST http://127.0.0.1:8000/send-imessage-with-attachment \
  -F 'to=+15551234567' \
  -F 'caption=Optional caption' \
  -F 'service=imessage' \
  -F 'file=@/path/to/photo.jpg'
```

`service`: `"imessage"` (default) or `"sms"`.

---

## Agent workflow: backfill + poll

```text
1. GET /chats → pick chat_identifier
2. after = 0
3. GET /messages?chat_identifier=...&after_rowid={after}&direction=all&limit=100
4. Process messages[] (is_from_me true/false)
5. If count == 0: backfill done
6. after = next_after_rowid; goto 3
7. Live poll: sleep; repeat from 3 with last after
8. Media: if attachments[].exists → GET local_url with same token
```

---

## Layout

| Path | Role |
|------|------|
| `main.py` | FastAPI: read, send, attachment serve |
| `message_text.py` | Shared `attributedBody` text decode |
| `relay.py` | Read-only chat.db → HTTPS webhook poller |
| `com.example.imessage-relay.plist` | launchd LaunchAgent template |
| `send-imessage.scpt` | Text send |
| `send-imessage-attachment.scpt` | File / photo / video send |
| `requirements.txt` | `fastapi`, `uvicorn`, `python-multipart` |
| `.env.example` | API token + relay placeholders |
| `tests/test_relay.py` | Relay unit tests |

---

## Errors / gotchas

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Server won't start / RuntimeError about token | `IMESSAGE_API_TOKEN` unset | Copy `.env.example` → `.env` and export |
| 401 | Wrong/missing header | Match env token exactly |
| 503 + authorization denied | No Full Disk Access | Grant FDA to the app that runs uvicorn; restart that app |
| 503 chat.db not found | Messages unused for this user | Open Messages once |
| 404 attachment | Not downloaded / purged | Open thread in Messages; check `exists` |
| 500 on send | Automation denied / bad `to` | Approve Automation; test in Messages UI |
| Empty `text` | Tapback / sticker / undecoded body | Check `has_attachments` |

Attachment files are only served if the path is under `~/Library/Messages/Attachments`.

---

## Read-only iMessage relay

`relay.py` tails `chat.db` and POSTs new messages for an allowlist of chat GUIDs to an HTTPS webhook. It does not import the send API and does not use AppleScript or `subprocess`.

The database is opened with a read-only SQLite URI (`file:<path>?mode=ro`). The cursor lives in `STATE_FILE` (default `~/.imessage-relay-state.json`, gitignored) and is written via a temp file and `os.replace`. It advances only after a **2xx**. Tapbacks (`associated_message_type != 0`), empty bodies, and rows with no date are skipped, and the cursor still moves past them. On **5xx** or a network error the cursor stays put and the next poll retries. A **4xx** is logged the same way: stop, do not advance. The webhook key is never written to the log.

The first run with no state file sets the cursor to `MAX(message.ROWID)`, so existing history is not sent. `BACKFILL` applies only when there is no cursor yet. To replay history, stop the agent, delete the state file, set `BACKFILL=1`, and start again.

Each POST is one JSON array (one batch, timeout 10s):

```json
[
  {
    "sender": "me",
    "text": "hello",
    "timestamp": "2001-01-15T18:30:00Z",
    "chat_id": "chat-guid",
    "guid": "message-guid"
  }
]
```

`sender` is `me` when `is_from_me` is set, otherwise the handle id. `timestamp` is ISO-8601 UTC from the Apple epoch (seconds or nanoseconds). `chat_id` is the chat GUID. `Authorization` is `<WEBHOOK_AUTH_SCHEME> <WEBHOOK_KEY>` (scheme defaults to `Bearer`). `AUTH_HEADER_RAW`, when set, replaces that header value entirely.

| Variable | Default | Notes |
|----------|---------|--------|
| `CHAT_DB` | `~/Library/Messages/chat.db` | Read-only |
| `ALLOWED_CHAT_GUIDS` | none | Comma-separated. **Required.** Empty refuses to start |
| `WEBHOOK_URL` | none | **https only.** Anything else refuses to start |
| `WEBHOOK_KEY` | none | Sent as the Authorization credential |
| `WEBHOOK_AUTH_SCHEME` | `Bearer` | |
| `AUTH_HEADER_RAW` | none | Overrides the whole Authorization value |
| `STATE_FILE` | `~/.imessage-relay-state.json` | |
| `POLL_INTERVAL` | `30` | Seconds between polls |
| `MAX_PER_RUN` | `100` | Rows read per poll |
| `BACKFILL` | off | `1` / `true` / `yes` / `on` replays from rowid 0 on first run |

`python-dotenv` is optional. If it is not installed, `relay.py` still reads `.env` from the repo directory (existing environment variables win). Values are placeholders — do not commit a real `.env`.

### One-time Mac setup

```bash
git clone <repo-url>
cd send-imessage

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Full Disk Access must include this binary (System Settings →
# Privacy & Security → Full Disk Access). If the toggle will not
# stay on, it is a symlink — add the path this prints:
python -c 'import sys; print(sys.executable)'

python relay.py --find-chat "Example Chat"
# ROWID, guid, display_name, style, last_message_date

cp .env.example .env
# set ALLOWED_CHAT_GUIDS (from --find-chat), WEBHOOK_URL, WEBHOOK_KEY

python relay.py --once
```

launchd runs the script's own loop under **KeepAlive** (so `POLL_INTERVAL` in `.env` is what waits). `StartInterval` is not used. Replace every `REPLACE` placeholder in `com.example.imessage-relay.plist` with absolute paths — launchd does not expand `~`. Point stdout and stderr at `~/Library/Logs/imessage-relay.log`.

```bash
mkdir -p ~/Library/Logs
cp com.example.imessage-relay.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.imessage-relay.plist
tail -f ~/Library/Logs/imessage-relay.log
```

Unload:

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.example.imessage-relay.plist
```

After `.env` edits, bootout and bootstrap again (or `launchctl kickstart -k gui/$(id -u)/com.example.imessage-relay`).

| Symptom | Fix |
|---------|-----|
| Refuses to start | Set `ALLOWED_CHAT_GUIDS`. `WEBHOOK_URL` must be `https://` |
| `authorization denied` | Grant Full Disk Access to the venv Python binary, then restart the agent |
| Cursor not moving | Webhook was not 2xx. The state file is unchanged; the next poll retries |
| No history arriving | Expected. Delete the state file and set `BACKFILL=1` to replay |

```bash
python -m unittest discover -s tests -v
```

## Security

- Prefer `--host 127.0.0.1` (same machine only).
- Token is a shared secret — treat like a password.
- This API can read your Messages history and send as you. Do **not** expose it to the LAN/WAN.
- Older commits of this repo may have contained secrets/logs; rotate any previously published tokens.
