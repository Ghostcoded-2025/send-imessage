# send-imessage

Local macOS HTTP API for **iMessage / SMS**: read full conversations (you + others), serve attachment files, and send text / photos / videos via Messages.app.

Designed for same-machine clients (n8n, scripts, AI agents). The server binds to `127.0.0.1` unless `HOST` is set. Reaching it from anywhere else requires a tunnel or reverse proxy.

**Repo:** https://github.com/Ghostcoded-2025/send-imessage

---

## Agent contract (copy this)

| Item | Value |
|------|--------|
| Base URL | `http://127.0.0.1:8000` |
| Auth | Header `X-API-Token: <token>` on **every** route except `GET /health` |
| Token env | **`IMESSAGE_API_TOKEN` (required)** — read from the process environment at startup in `main.py` (`os.environ`). The app does not load `.env` itself |
| Bind | **`HOST`**, default `127.0.0.1` (`python main.py`). Another machine needs a tunnel or reverse proxy |
| Chat allow-list | **`ALLOWED_CHAT_GUIDS`** — optional comma-separated `chat.guid` values. Empty means every chat |
| Send | **`ENABLE_SEND`**, default off. Send routes return **403** and do not run osascript until it is `true` |
| Interactive docs | `GET /docs` |
| Host OS | macOS with Messages signed in |
| Send path | AppleScript → Messages.app, only when `ENABLE_SEND=true` |
| Read path | Read-only SQLite → `~/Library/Messages/chat.db` |

**Safe defaults for agents**

1. Always send `X-API-Token`.
2. Call `GET /health` first; 503 on reads usually means Full Disk Access is missing.
3. Paginate: `after_rowid` → response `next_after_rowid` until `count == 0`.
4. Use `direction=all` for full threads (inbound + outbound).
5. Leave `HOST` at `127.0.0.1`. Do not publish this API on a LAN or public interface.
6. Set `ALLOWED_CHAT_GUIDS` to the chats this client may read.
7. Leave `ENABLE_SEND` unset unless this client must send.
8. Never commit real tokens — use `.env` (gitignored) and export it into the environment.

---

## Quick start

```bash
git clone https://github.com/Ghostcoded-2025/send-imessage.git
cd send-imessage

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env — set IMESSAGE_API_TOKEN (see "Rotate your token" below)
export $(grep -v '^#' .env | xargs)

python main.py
# equivalent if you call uvicorn yourself (it does not read HOST):
# uvicorn main:app --host "${HOST:-127.0.0.1}" --port 8000
```

OpenAPI: http://127.0.0.1:8000/docs

### Background

```bash
source .venv/bin/activate
export $(grep -v '^#' .env | xargs)
nohup python main.py > server.log 2>&1 &
echo $! > uvicorn.pid

# stop
kill "$(cat uvicorn.pid)" && rm -f uvicorn.pid
```

**Docker n8n on the same Mac:** keep `HOST=127.0.0.1` and call `http://host.docker.internal:8000`. If a client cannot reach loopback, put a tunnel or reverse proxy in front of the API instead of binding a public interface.

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
| `GET` | `/chats` | yes | List conversations (`limit` 1–1000). Filtered when `ALLOWED_CHAT_GUIDS` is set |
| `GET` | `/messages` | yes | History; both directions by default. Filtered to allowed chats; other `chat_identifier` values return **403** |
| `GET` | `/attachments/{id}/file` | yes | Binary file from Messages library. **404** unless it belongs to a message in an allowed chat |
| `POST` | `/send-imessage` | yes | JSON `{ to, text, service? }`. **403** unless `ENABLE_SEND=true` |
| `POST` | `/send-imessage-with-attachment` | yes | multipart: `to`, `file`, `caption?`, `service?`. **403** unless `ENABLE_SEND=true` |

Auth failure → **401**. Sending while `ENABLE_SEND` is off → **403**, and osascript is not run.

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

# Requires ENABLE_SEND=true. Otherwise the send routes return 403.
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
| `main.py` | FastAPI: read, send, attachment serve. Reads env at startup |
| `send-imessage.scpt` | Text send (reached only when `ENABLE_SEND=true`) |
| `send-imessage-attachment.scpt` | File / photo / video send (same gate) |
| `requirements.txt` | `fastapi`, `uvicorn`, `python-multipart` |
| `requirements-dev.txt` | Test dependencies |
| `tests/test_security.py` | Auth, allow-list, and send-gate tests |
| `.env.example` | Env template (placeholders only) |

---

## Errors / gotchas

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Server won't start / RuntimeError about token | `IMESSAGE_API_TOKEN` unset | Copy `.env.example` → `.env` and export |
| 401 | Wrong/missing header | Match env token exactly |
| 403 on send | `ENABLE_SEND` is not true | Set `ENABLE_SEND=true` only for a client that must send, then restart |
| 403 on `/messages` | `chat_identifier` is outside `ALLOWED_CHAT_GUIDS` | Add that chat's `guid`, or omit the filter |
| 503 + authorization denied | No Full Disk Access | Grant FDA to the app that runs uvicorn; restart that app |
| 503 chat.db not found | Messages unused for this user | Open Messages once |
| 404 attachment | Not downloaded / purged | Open thread in Messages; check `exists` |
| 500 on send | Automation denied / bad `to` | Approve Automation; test in Messages UI |
| Empty `text` | Tapback / sticker / undecoded body | Check `has_attachments` |

Attachment files are only served if the path is under `~/Library/Messages/Attachments`.

---

## Security

- `python main.py` binds `HOST` (default `127.0.0.1`). `GET /health` reports that address as `bind_hint`.
- Exposing the API beyond localhost requires a tunnel or reverse proxy. Do not bind a LAN or public address and do not port-forward the raw service.
- A non-loopback `HOST` logs a startup warning. Loopback is `127.0.0.1`, `localhost`, or `::1`.
- Token is a shared secret — treat it like a password. Comparison is constant-time (`secrets.compare_digest`). A missing `X-API-Token` is **401**.
- `ALLOWED_CHAT_GUIDS` is an optional comma-separated list of `chat.guid` values from the local chat table. When set, `/chats` and `/messages` only return those chats, a request for another chat returns **403**, and `/attachments/{id}/file` only serves files attached to messages in those chats. When unset, behavior is unchanged and the server logs a startup warning.
- Look up guids locally: `sqlite3 ~/Library/Messages/chat.db 'select guid, chat_identifier from chat'`
- `ENABLE_SEND` defaults to off. Both send routes return **403** and never call osascript until it is `true` (`1`, `yes`, and `on` also enable sending).
- This API can read your Messages history and, when sending is enabled, send as you.
- Older commits of this repo may have contained secrets or logs. Rotate any token that was ever shared.

## Rotate your token

Generate a new token:

```bash
python -c 'import secrets;print(secrets.token_urlsafe(32))'
```

Set it as `IMESSAGE_API_TOKEN` in `.env` (same keys as `.env.example`). `main.py` does not open `.env`. It reads `IMESSAGE_API_TOKEN` from the process environment at startup via `os.environ.get`. Load the file, then restart:

```bash
export $(grep -v '^#' .env | xargs)
python main.py
```

The previous token stops working as soon as the new process is up.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```
