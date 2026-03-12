# iMessage FastAPI bridge

Small FastAPI service that lets n8n (or anything else) send iMessages via the local Messages.app on this Mac.

## Setup

From this directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Update `API_TOKEN` in `main.py` to a strong secret string.

## Run the server

### If calling from the same host (curl, local scripts)

```bash
uvicorn main:app --host 127.0.0.1 --port 8000
```

### If calling from n8n running in Docker on this Mac

FastAPI must listen on all interfaces so the container can reach it:

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

## Run in background

From this directory, no terminal window needed:

```bash
source .venv/bin/activate
nohup uvicorn main:app --host 0.0.0.0 --port 8000 > server.log 2>&1 &
echo $! > uvicorn.pid
```

**Stop the server:**

```bash
kill "$(cat uvicorn.pid)"
rm uvicorn.pid
```

**View logs:**

```bash
tail -f server.log
```

## Test locally

Replace the `to` number with an iMessage-capable phone/email reachable from Messages on this Mac.

```bash
curl -X POST http://127.0.0.1:8000/send-imessage \
  -H 'Content-Type: application/json' \
  -H 'X-API-Token: YOUR_TOKEN_HERE' \
  -d '{"to":"+15551234567","text":"Test from FastAPI"}'
```

## n8n configuration

### n8n running directly on macOS (no Docker)

Use an HTTP Request node:

- Method: `POST`
- URL: `http://127.0.0.1:8000/send-imessage`
- Header `X-API-Token`: the same token you configured in `main.py`
- JSON body (optional `service`: `"imessage"` or `"sms"`, default `"imessage"`):

```json
{
  "to": "+15551234567",
  "text": "Hello from n8n via FastAPI",
  "service": "imessage"
}
```

### n8n running in Docker on this Mac

Start FastAPI with:

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

In the HTTP Request node:

- Method: `POST`
- URL: `http://host.docker.internal:8000/send-imessage`
- Header `X-API-Token`: the same token you configured in `main.py`
- JSON body as above.

## Push received messages into n8n (cron)

This repo includes `push-received-to-n8n.py`, which reads new **inbound** Messages from `~/Library/Messages/chat.db` and POSTs them to an n8n Webhook URL.
By default it is configured to call:

```text
http://localhost:8080/webhook/b1b85f8d-4732-4556-8348-715eb0337db5
```

If you change your n8n URL or webhook path, update `N8N_WEBHOOK_URL` in `push-received-to-n8n.py`.

### One-time test

```bash
python3 push-received-to-n8n.py
```

If macOS blocks access to `~/Library/Messages/chat.db`, you’ll need to grant your shell/cron runner permission (macOS privacy settings vary by version).

### Add to cron (runs every minute)

Edit crontab:

```bash
crontab -e
```

Add:

```bash
* * * * * /usr/bin/python3 /Users/tannerwoodrum/Documents/Ghostcoded/repos/Ghostcoded/scripts/imessage-fastapi/push-received-to-n8n.py >> /Users/tannerwoodrum/Documents/Ghostcoded/repos/Ghostcoded/scripts/imessage-fastapi/received-cron.log 2>&1
```

Notes:
- The script stores its cursor in `.received_state.json` (so it won’t resend messages).
- By default it skips blank/attachment-only rows.

## Export contacts into n8n

Use `export-contacts-to-n8n.py` to send your macOS Contacts (name + phone) plus a best-guess channel (`imessage` / `sms` / `unknown`) into n8n.

1. Set `N8N_CONTACTS_WEBHOOK_URL` in `export-contacts-to-n8n.py` to a Webhook URL in n8n that will receive the contact list.
2. Run it once:

```bash
python3 export-contacts-to-n8n.py
```

The payload looks like:

```json
{
  "source": "imessage-contacts",
  "contacts": [
    { "name": "Alice Example", "phone": "+15551234567", "channel": "imessage" },
    { "name": "Bob Example", "phone": "+15557654321", "channel": "sms" }
  ]
}
```

