#!/usr/bin/env python3
import json
import sqlite3
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


CHAT_DB = Path.home() / "Library" / "Messages" / "chat.db"
STATE_FILE = Path(__file__).resolve().parent / ".received_state.json"

# Set this to your n8n Webhook URL.
# Example: "http://localhost:8080/webhook/REPLACE_ME"
N8N_WEBHOOK_URL = "http://localhost:8080/webhook/REPLACE_ME"

MAX_MESSAGES_PER_RUN = 50


@dataclass(frozen=True)
class ReceivedMessage:
    rowid: int
    date_ns: int
    is_from_me: int
    text: Optional[str]
    handle: Optional[str]
    chat_identifier: Optional[str]


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {"last_rowid": 0}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"last_rowid": 0}


def _save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(STATE_FILE)


def _apple_time_ns_to_unix_seconds(date_ns: int) -> float:
    apple_epoch_unix = 978307200
    return apple_epoch_unix + (date_ns / 1_000_000_000)


def _fetch_new_received_messages(conn: sqlite3.Connection, last_rowid: int) -> list[ReceivedMessage]:
    rows = conn.execute(
        """
        SELECT
          m.ROWID,
          m.date,
          m.is_from_me,
          m.text,
          h.id AS handle,
          c.chat_identifier
        FROM message m
        LEFT JOIN handle h ON m.handle_id = h.ROWID
        LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        LEFT JOIN chat c ON c.ROWID = cmj.chat_id
        WHERE m.ROWID > ?
          AND m.is_from_me = 0
        ORDER BY m.ROWID ASC
        LIMIT ?
        """,
        (last_rowid, MAX_MESSAGES_PER_RUN),
    ).fetchall()

    msgs: list[ReceivedMessage] = []
    for row in rows:
        msgs.append(
            ReceivedMessage(
                rowid=row[0],
                date_ns=row[1] or 0,
                is_from_me=row[2] or 0,
                text=row[3],
                handle=row[4],
                chat_identifier=row[5],
            )
        )
    return msgs


def _post_to_n8n(msg: ReceivedMessage) -> None:
    payload = {
        "source": "imessage",
        "direction": "inbound",
        "rowid": msg.rowid,
        "handle": msg.handle,
        "chat_identifier": msg.chat_identifier,
        "text": msg.text,
        "timestamp_unix": _apple_time_ns_to_unix_seconds(msg.date_ns) if msg.date_ns else None,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        N8N_WEBHOOK_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()


def main() -> int:
    if not CHAT_DB.exists():
        print(f"chat.db not found at {CHAT_DB}", file=sys.stderr)
        return 2

    state = _load_state()
    last_rowid = int(state.get("last_rowid", 0) or 0)

    try:
        conn = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)
    except sqlite3.OperationalError as e:
        print(f"Failed to open chat.db: {e}", file=sys.stderr)
        return 3

    with conn:
        msgs = _fetch_new_received_messages(conn, last_rowid)

    if not msgs:
        return 0

    max_rowid = last_rowid
    for msg in msgs:
        if not (msg.text or "").strip():
            max_rowid = max(max_rowid, msg.rowid)
            continue

        try:
            _post_to_n8n(msg)
            max_rowid = max(max_rowid, msg.rowid)
        except Exception as e:
            print(f"Failed posting rowid={msg.rowid}: {e}", file=sys.stderr)
            break

        time.sleep(0.05)

    if max_rowid != last_rowid:
        _save_state({"last_rowid": max_rowid})

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

