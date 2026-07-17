# send-imessage

Local macOS HTTP API for **iMessage / SMS**: read full conversations (you + others), serve attachment files, and send text / photos / videos via Messages.app.

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
| `send-imessage.scpt` | Text send |
| `send-imessage-attachment.scpt` | File / photo / video send |
| `requirements.txt` | `fastapi`, `uvicorn`, `python-multipart` |
| `.env.example` | Token template |

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

## Security

- Prefer `--host 127.0.0.1` (same machine only).
- Token is a shared secret — treat like a password.
- This API can read your Messages history and send as you. Do **not** expose it to the LAN/WAN.
- Older commits of this repo may have contained secrets/logs; rotate any previously published tokens.
