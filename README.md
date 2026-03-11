# iMessage FastAPI bridge

Small FastAPI service that lets n8n (or anything else) send iMessages via the local Messages.app on a Mac.

**Requirements:** macOS, Messages signed in with iMessage.

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

The first time you send, macOS will prompt to allow Terminal (or the process running the script) to control Messages—click Allow.

## n8n configuration

### n8n running directly on macOS (no Docker)

Use an HTTP Request node:

- Method: `POST`
- URL: `http://127.0.0.1:8000/send-imessage`
- Header `X-API-Token`: the same token you configured in `main.py`
- JSON body:

```json
{
  "to": "+15551234567",
  "text": "Hello from n8n via FastAPI"
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
