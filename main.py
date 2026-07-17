import os
import re
import sqlite3
import tempfile
from pathlib import Path
from shutil import copyfileobj
from subprocess import CalledProcessError, run
from typing import Any, Literal, Optional

from fastapi import FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
IMESSAGE_SCRIPT = BASE_DIR / "send-imessage.scpt"
IMESSAGE_ATTACHMENT_SCRIPT = BASE_DIR / "send-imessage-attachment.scpt"
CHAT_DB = Path.home() / "Library" / "Messages" / "chat.db"
ATTACHMENTS_ROOT = (Path.home() / "Library" / "Messages" / "Attachments").resolve()

# Required. Generate with: python3 -c "import secrets; print(secrets.token_urlsafe(32))"
API_TOKEN = os.environ.get("IMESSAGE_API_TOKEN", "").strip()
if not API_TOKEN:
    raise RuntimeError(
        "Set IMESSAGE_API_TOKEN in your environment before starting the server "
        "(see .env.example / README)."
    )

APPLE_EPOCH_UNIX = 978307200


class IMessageRequest(BaseModel):
    to: str
    text: str
    service: Literal["imessage", "sms"] = "imessage"


app = FastAPI(title="Local iMessage API", docs_url="/docs")


def _require_token(x_api_token: Optional[str]) -> None:
    if not API_TOKEN or x_api_token != API_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")


def _open_chat_db() -> sqlite3.Connection:
    if not CHAT_DB.exists():
        raise HTTPException(status_code=503, detail=f"chat.db not found at {CHAT_DB}")
    try:
        conn = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)
    except sqlite3.Error as e:
        detail = str(e)
        if "authorization denied" in detail.lower():
            detail += (
                " — grant Full Disk Access to the process running uvicorn "
                "(Terminal, Cursor, or python)."
            )
        raise HTTPException(status_code=503, detail=detail) from e
    conn.row_factory = sqlite3.Row
    return conn


def _apple_time_ns_to_unix(date_ns: Optional[int]) -> Optional[float]:
    if not date_ns:
        return None
    return APPLE_EPOCH_UNIX + (date_ns / 1_000_000_000)


def _text_from_attributed_body(blob: Optional[bytes]) -> Optional[str]:
    if not blob:
        return None
    try:
        raw = blob.decode("utf-8", errors="ignore")
    except Exception:
        return None
    if "NSString" not in raw:
        return None
    chunk = raw.split("NSString", 1)[1]
    if "NSDictionary" in chunk:
        chunk = chunk.split("NSDictionary", 1)[0]
    cleaned = "".join(ch for ch in chunk if ch.isprintable() or ch in "\n\r\t").strip()
    cleaned = re.sub(r"^[\x00-\x1f]+", "", cleaned).strip()
    return cleaned or None


def _message_text(text: Optional[str], attributed_body: Optional[bytes]) -> Optional[str]:
    if text and text.strip():
        return text
    return _text_from_attributed_body(attributed_body)


def _expand_attachment_path(filename: Optional[str]) -> Optional[Path]:
    if not filename:
        return None
    path = Path(filename).expanduser()
    if not path.is_absolute():
        path = Path.home() / path
    try:
        return path.resolve()
    except OSError:
        return None


def _attachments_for_message(conn: sqlite3.Connection, message_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
          a.ROWID AS id,
          a.guid,
          a.filename,
          a.transfer_name,
          a.mime_type,
          a.total_bytes,
          a.is_outgoing
        FROM attachment a
        JOIN message_attachment_join maj ON maj.attachment_id = a.ROWID
        WHERE maj.message_id = ?
        ORDER BY a.ROWID ASC
        """,
        (message_id,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        path = _expand_attachment_path(row["filename"])
        out.append(
            {
                "id": row["id"],
                "guid": row["guid"],
                "filename": row["filename"],
                "transfer_name": row["transfer_name"],
                "mime_type": row["mime_type"],
                "total_bytes": row["total_bytes"],
                "is_outgoing": bool(row["is_outgoing"]) if row["is_outgoing"] is not None else None,
                "exists": bool(path and path.is_file()),
                "local_url": f"/attachments/{row['id']}/file" if path else None,
            }
        )
    return out


@app.get("/health")
def health():
    return {
        "ok": True,
        "chat_db_exists": CHAT_DB.exists(),
        "bind_hint": "127.0.0.1",
    }


@app.get("/chats")
def list_chats(
    x_api_token: Optional[str] = Header(None),
    limit: int = Query(100, ge=1, le=1000),
):
    """List conversations with latest activity (local chat.db)."""
    _require_token(x_api_token)
    with _open_chat_db() as conn:
        rows = conn.execute(
            """
            SELECT
              c.ROWID AS chat_id,
              c.chat_identifier,
              c.display_name,
              c.service_name,
              MAX(m.ROWID) AS last_message_rowid,
              MAX(m.date) AS last_message_date
            FROM chat c
            JOIN chat_message_join cmj ON cmj.chat_id = c.ROWID
            JOIN message m ON m.ROWID = cmj.message_id
            GROUP BY c.ROWID
            ORDER BY last_message_date DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return {
        "chats": [
            {
                "chat_id": r["chat_id"],
                "chat_identifier": r["chat_identifier"],
                "display_name": r["display_name"],
                "service_name": r["service_name"],
                "last_message_rowid": r["last_message_rowid"],
                "last_message_timestamp_unix": _apple_time_ns_to_unix(r["last_message_date"]),
            }
            for r in rows
        ]
    }


@app.get("/messages")
def list_messages(
    x_api_token: Optional[str] = Header(None),
    after_rowid: int = Query(0, ge=0, description="Exclusive lower bound (use for polling/backfill)"),
    before_rowid: Optional[int] = Query(None, ge=1),
    chat_identifier: Optional[str] = Query(None),
    handle: Optional[str] = Query(None, description="Phone/email handle id"),
    direction: Literal["all", "inbound", "outbound"] = Query("all"),
    limit: int = Query(100, ge=1, le=500),
    include_attachments: bool = Query(True),
):
    """
    Read iMessage/SMS history from local chat.db.
    Includes both from-me and from-others when direction=all.
    """
    _require_token(x_api_token)

    clauses = ["m.ROWID > ?"]
    params: list[Any] = [after_rowid]

    if before_rowid is not None:
        clauses.append("m.ROWID < ?")
        params.append(before_rowid)

    if direction == "inbound":
        clauses.append("m.is_from_me = 0")
    elif direction == "outbound":
        clauses.append("m.is_from_me = 1")

    if chat_identifier:
        clauses.append("c.chat_identifier = ?")
        params.append(chat_identifier)

    if handle:
        clauses.append("h.id = ?")
        params.append(handle)

    where = " AND ".join(clauses)
    params.append(limit)

    sql = f"""
        SELECT
          m.ROWID AS rowid,
          m.guid,
          m.date,
          m.is_from_me,
          m.text,
          m.attributedBody,
          m.cache_has_attachments,
          m.service,
          h.id AS handle,
          c.ROWID AS chat_id,
          c.chat_identifier,
          c.display_name
        FROM message m
        LEFT JOIN handle h ON m.handle_id = h.ROWID
        LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        LEFT JOIN chat c ON c.ROWID = cmj.chat_id
        WHERE {where}
        ORDER BY m.ROWID ASC
        LIMIT ?
    """

    with _open_chat_db() as conn:
        rows = conn.execute(sql, params).fetchall()
        messages: list[dict[str, Any]] = []
        for r in rows:
            item = {
                "rowid": r["rowid"],
                "guid": r["guid"],
                "direction": "outbound" if r["is_from_me"] else "inbound",
                "is_from_me": bool(r["is_from_me"]),
                "handle": r["handle"],
                "chat_id": r["chat_id"],
                "chat_identifier": r["chat_identifier"],
                "display_name": r["display_name"],
                "service": r["service"],
                "text": _message_text(r["text"], r["attributedBody"]),
                "has_attachments": bool(r["cache_has_attachments"]),
                "timestamp_unix": _apple_time_ns_to_unix(r["date"]),
                "attachments": [],
            }
            if include_attachments and r["cache_has_attachments"]:
                item["attachments"] = _attachments_for_message(conn, r["rowid"])
            messages.append(item)

    next_after = messages[-1]["rowid"] if messages else after_rowid
    return {
        "count": len(messages),
        "after_rowid": after_rowid,
        "next_after_rowid": next_after,
        "messages": messages,
    }


@app.get("/attachments/{attachment_id}/file")
def get_attachment_file(
    attachment_id: int,
    x_api_token: Optional[str] = Header(None),
):
    """Serve a local Messages attachment file (photos/videos/etc) from this Mac only."""
    _require_token(x_api_token)
    with _open_chat_db() as conn:
        row = conn.execute(
            "SELECT filename, transfer_name, mime_type FROM attachment WHERE ROWID = ?",
            (attachment_id,),
        ).fetchone()
    if not row or not row["filename"]:
        raise HTTPException(status_code=404, detail="Attachment not found")

    path = _expand_attachment_path(row["filename"])
    if not path or not path.is_file():
        raise HTTPException(status_code=404, detail="Attachment file missing on disk")

    # Only allow files under Messages/Attachments (or the resolved path must live there).
    try:
        path.relative_to(ATTACHMENTS_ROOT)
    except ValueError as e:
        raise HTTPException(status_code=403, detail="Attachment path outside Messages library") from e

    filename = row["transfer_name"] or path.name
    return FileResponse(
        path,
        media_type=row["mime_type"] or "application/octet-stream",
        filename=filename,
    )


@app.post("/send-imessage")
def send_imessage(payload: IMessageRequest, x_api_token: Optional[str] = Header(None)):
    _require_token(x_api_token)
    try:
        result = run(
            ["osascript", str(IMESSAGE_SCRIPT), payload.to, payload.text, payload.service],
            capture_output=True,
            text=True,
            check=True,
        )
        return {"ok": True, "stdout": result.stdout.strip()}
    except CalledProcessError as e:
        raise HTTPException(
            status_code=500,
            detail={"error": "Failed to send iMessage", "stderr": e.stderr},
        )


@app.post("/send-imessage-with-attachment")
async def send_imessage_with_attachment(
    to: str = Form(...),
    file: UploadFile = File(...),
    service: Literal["imessage", "sms"] = Form("imessage"),
    caption: str = Form(""),
    x_api_token: Optional[str] = Header(None),
):
    """Send a photo/video/file (+ optional caption) via local Messages.app."""
    _require_token(x_api_token)

    suffix = Path(file.filename or "upload").suffix
    if not suffix or len(suffix) > 20:
        suffix = ".bin"

    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    try:
        with open(tmp_path, "wb") as out:
            copyfileobj(file.file, out)

        result = run(
            [
                "osascript",
                str(IMESSAGE_ATTACHMENT_SCRIPT),
                to,
                tmp_path,
                service,
                caption or "",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return {"ok": True, "stdout": result.stdout.strip()}
    except CalledProcessError as e:
        raise HTTPException(
            status_code=500,
            detail={"error": "Failed to send iMessage attachment", "stderr": e.stderr},
        )
    finally:
        await file.close()
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
