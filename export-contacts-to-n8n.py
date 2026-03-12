#!/usr/bin/env python3
import json
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional
import urllib.request


# Messages DB (for iMessage/SMS info)
CHAT_DB = Path.home() / "Library" / "Messages" / "chat.db"

# Where to send contacts in n8n
N8N_CONTACTS_WEBHOOK_URL = "https://n8n.ghostcoded.com/webhook/0cb3568e-94ad-4dd3-86f8-90a934211e62"


@dataclass(frozen=True)
class ContactRow:
    name: str
    phone: str
    channel: str  # "imessage", "sms", or "unknown"


def _normalize_phone(raw: str) -> str:
    # Very light normalization: strip spaces/dashes/parentheses, keep leading + if present.
    raw = raw.strip()
    if not raw:
        return raw
    plus = raw.startswith("+")
    digits = "".join(ch for ch in raw if ch.isdigit())
    return ("+" if plus else "") + digits


def _load_handle_services() -> Dict[str, str]:
    """
    Returns a map of handle id (phone/email) -> channel ("imessage" | "sms").
    """
    if not CHAT_DB.exists():
        return {}

    try:
        conn = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return {}

    services: Dict[str, str] = {}
    with conn:
        cur = conn.execute("PRAGMA table_info(handle)")
        cols = [row[1] for row in cur.fetchall()]
        has_service_col = "service" in cols

        if has_service_col:
            rows = conn.execute("SELECT id, service FROM handle").fetchall()
            for row in rows:
                handle_id = row[0] or ""
                service = (row[1] or "").lower()
                if not handle_id:
                    continue
                if "imessage" in service:
                    services[_normalize_phone(handle_id)] = "imessage"
                elif "sms" in service:
                    # Only set SMS if we don't already know it's iMessage
                    services.setdefault(_normalize_phone(handle_id), "sms")

    return services


def _export_contacts_from_mac() -> list[ContactRow]:
    """
    Uses AppleScript to read all contacts + phone numbers from Contacts.app.
    """
    applescript = r'''
on run {}
  set textLines to ""
  tell application "Contacts"
    repeat with p in people
      set personName to name of p as text
      repeat with ph in phones of p
        set phoneValue to value of ph as text
        set textLines to textLines & personName & "||" & phoneValue & linefeed
      end repeat
    end repeat
  end tell
  return textLines
end run
'''
    proc = subprocess.run(
        ["osascript", "-e", applescript],
        capture_output=True,
        text=True,
        check=True,
    )
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]

    services = _load_handle_services()

    contacts: list[ContactRow] = []
    for line in lines:
        parts = line.split("||", 1)
        if len(parts) != 2:
            continue
        name, phone = parts[0].strip(), parts[1].strip()
        if not phone:
            continue
        norm = _normalize_phone(phone)
        channel = services.get(norm, "unknown")
        contacts.append(ContactRow(name=name, phone=phone, channel=channel))

    return contacts


def _post_contacts_to_n8n(contacts: list[ContactRow]) -> None:
    payload = {
        "source": "imessage-contacts",
        "contacts": [
            {"name": c.name, "phone": c.phone, "channel": c.channel}
            for c in contacts
        ],
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        N8N_CONTACTS_WEBHOOK_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


def main() -> int:
    contacts = _export_contacts_from_mac()
    if not contacts:
        return 0
    _post_contacts_to_n8n(contacts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

