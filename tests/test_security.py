import os
import secrets
import sqlite3

os.environ.setdefault("IMESSAGE_API_TOKEN", "test-token")

import pytest
from fastapi.testclient import TestClient

import main

TOKEN = os.environ["IMESSAGE_API_TOKEN"]
AUTH = {"X-API-Token": TOKEN}


@pytest.fixture
def client():
    with TestClient(main.app) as test_client:
        yield test_client


def _seed_db(tmp_path, monkeypatch):
    root = tmp_path / "Attachments"
    root.mkdir()
    allowed_file = root / "allowed.jpg"
    blocked_file = root / "blocked.jpg"
    allowed_file.write_bytes(b"allowed-bytes")
    blocked_file.write_bytes(b"blocked-bytes")

    db_path = tmp_path / "chat.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE chat (
            ROWID INTEGER PRIMARY KEY,
            guid TEXT,
            chat_identifier TEXT,
            display_name TEXT,
            service_name TEXT
        );
        CREATE TABLE handle (
            ROWID INTEGER PRIMARY KEY,
            id TEXT
        );
        CREATE TABLE message (
            ROWID INTEGER PRIMARY KEY,
            guid TEXT,
            date INTEGER,
            is_from_me INTEGER,
            text TEXT,
            attributedBody BLOB,
            cache_has_attachments INTEGER,
            service TEXT,
            handle_id INTEGER
        );
        CREATE TABLE chat_message_join (
            chat_id INTEGER,
            message_id INTEGER
        );
        CREATE TABLE attachment (
            ROWID INTEGER PRIMARY KEY,
            guid TEXT,
            filename TEXT,
            transfer_name TEXT,
            mime_type TEXT,
            total_bytes INTEGER,
            is_outgoing INTEGER
        );
        CREATE TABLE message_attachment_join (
            message_id INTEGER,
            attachment_id INTEGER
        );
        """
    )
    conn.execute("INSERT INTO handle (ROWID, id) VALUES (1, 'handle-alpha')")
    chats = [
        (1, "guid-alpha", "alpha", "Alpha"),
        (2, "guid-beta", "beta", "Beta"),
        (3, "guid-gamma", "gamma", "Gamma"),
    ]
    for rowid, guid, identifier, name in chats:
        conn.execute(
            "INSERT INTO chat (ROWID, guid, chat_identifier, display_name, service_name) VALUES (?, ?, ?, ?, 'iMessage')",
            (rowid, guid, identifier, name),
        )
        conn.execute(
            """
            INSERT INTO message (
                ROWID, guid, date, is_from_me, text, attributedBody,
                cache_has_attachments, service, handle_id
            ) VALUES (?, ?, 1000000000, 0, ?, NULL, 0, 'iMessage', 1)
            """,
            (rowid, f"msg-{identifier}", f"{identifier}-body"),
        )
        conn.execute(
            "INSERT INTO chat_message_join (chat_id, message_id) VALUES (?, ?)",
            (rowid, rowid),
        )

    conn.execute(
        """
        INSERT INTO attachment (
            ROWID, guid, filename, transfer_name, mime_type, total_bytes, is_outgoing
        ) VALUES (1, 'att-alpha', ?, 'allowed.jpg', 'image/jpeg', 13, 0)
        """,
        (str(allowed_file),),
    )
    conn.execute(
        """
        INSERT INTO attachment (
            ROWID, guid, filename, transfer_name, mime_type, total_bytes, is_outgoing
        ) VALUES (2, 'att-beta', ?, 'blocked.jpg', 'image/jpeg', 13, 0)
        """,
        (str(blocked_file),),
    )
    conn.execute("INSERT INTO message_attachment_join (message_id, attachment_id) VALUES (1, 1)")
    conn.execute("INSERT INTO message_attachment_join (message_id, attachment_id) VALUES (2, 2)")
    conn.commit()
    conn.close()

    monkeypatch.setattr(main, "CHAT_DB", db_path)
    monkeypatch.setattr(main, "ATTACHMENTS_ROOT", root.resolve())
    return {"allowed_file": allowed_file, "blocked_file": blocked_file}


def test_missing_token_header_is_401(client):
    response = client.get("/chats")
    assert response.status_code == 401


def test_non_ascii_token_is_rejected():
    with pytest.raises(main.HTTPException) as exc:
        main._require_token("tökën")
    assert exc.value.status_code == 401


def test_compare_digest_acceptance_overrides_raw_equality(client, monkeypatch):
    monkeypatch.setattr(secrets, "compare_digest", lambda provided, expected: True)
    response = client.get("/chats", headers={"X-API-Token": "not-the-token"})
    assert response.status_code != 401


def test_compare_digest_rejection_overrides_raw_equality(client, monkeypatch):
    monkeypatch.setattr(secrets, "compare_digest", lambda provided, expected: False)
    response = client.get("/chats", headers=AUTH)
    assert response.status_code == 401


def test_health_bind_hint_defaults_to_loopback(client, monkeypatch):
    monkeypatch.delenv("HOST", raising=False)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["bind_hint"] == "127.0.0.1"


def test_health_bind_hint_follows_host_env(client, monkeypatch):
    monkeypatch.setenv("HOST", "localhost")
    response = client.get("/health")
    assert response.json()["bind_hint"] == "localhost"


def test_empty_allowlist_returns_every_chat(client, monkeypatch, tmp_path):
    _seed_db(tmp_path, monkeypatch)
    monkeypatch.delenv("ALLOWED_CHAT_GUIDS", raising=False)
    response = client.get("/chats", headers=AUTH)
    assert response.status_code == 200
    identifiers = {chat["chat_identifier"] for chat in response.json()["chats"]}
    assert identifiers == {"alpha", "beta", "gamma"}
    allowed = client.get("/attachments/1/file", headers=AUTH)
    blocked = client.get("/attachments/2/file", headers=AUTH)
    assert allowed.status_code == 200
    assert blocked.status_code == 200


def test_allowlist_filters_chats_and_strips_whitespace(client, monkeypatch, tmp_path):
    _seed_db(tmp_path, monkeypatch)
    monkeypatch.setenv("ALLOWED_CHAT_GUIDS", "guid-alpha, guid-beta")
    response = client.get("/chats", headers=AUTH)
    assert response.status_code == 200
    identifiers = {chat["chat_identifier"] for chat in response.json()["chats"]}
    assert identifiers == {"alpha", "beta"}


def test_allowlist_filters_messages(client, monkeypatch, tmp_path):
    _seed_db(tmp_path, monkeypatch)
    monkeypatch.setenv("ALLOWED_CHAT_GUIDS", "guid-alpha")
    response = client.get("/messages", headers=AUTH)
    assert response.status_code == 200
    texts = {message["text"] for message in response.json()["messages"]}
    assert texts == {"alpha-body"}


def test_allowlist_keeps_messages_for_an_allowed_chat(client, monkeypatch, tmp_path):
    _seed_db(tmp_path, monkeypatch)
    monkeypatch.setenv("ALLOWED_CHAT_GUIDS", "guid-alpha")
    response = client.get("/messages", headers=AUTH, params={"chat_identifier": "alpha"})
    assert response.status_code == 200
    texts = {message["text"] for message in response.json()["messages"]}
    assert texts == {"alpha-body"}


def test_allowlist_rejects_disallowed_chat_identifier(client, monkeypatch, tmp_path):
    _seed_db(tmp_path, monkeypatch)
    monkeypatch.setenv("ALLOWED_CHAT_GUIDS", "guid-alpha")
    response = client.get("/messages", headers=AUTH, params={"chat_identifier": "beta"})
    assert response.status_code == 403


def test_allowlist_serves_only_attachments_in_allowed_chats(client, monkeypatch, tmp_path):
    _seed_db(tmp_path, monkeypatch)
    monkeypatch.setenv("ALLOWED_CHAT_GUIDS", "guid-alpha")
    allowed = client.get("/attachments/1/file", headers=AUTH)
    blocked = client.get("/attachments/2/file", headers=AUTH)
    assert allowed.status_code == 200
    assert allowed.content == b"allowed-bytes"
    assert blocked.status_code == 404


def test_empty_allowlist_logs_startup_warning(monkeypatch, caplog):
    monkeypatch.delenv("ALLOWED_CHAT_GUIDS", raising=False)
    with caplog.at_level("WARNING", logger="imessage_api"):
        with TestClient(main.app):
            pass
    assert any("ALLOWED_CHAT_GUIDS" in record.message for record in caplog.records)


def test_send_endpoints_are_closed_unless_enabled(client, monkeypatch):
    monkeypatch.delenv("ENABLE_SEND", raising=False)
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(args)
        raise AssertionError("send path invoked")

    monkeypatch.setattr(main, "run", fake_run)

    text = client.post(
        "/send-imessage",
        headers=AUTH,
        json={"to": "person", "text": "hi", "service": "imessage"},
    )
    attachment = client.post(
        "/send-imessage-with-attachment",
        headers=AUTH,
        data={"to": "person", "service": "imessage", "caption": ""},
        files={"file": ("note.txt", b"hello", "text/plain")},
    )
    assert text.status_code == 403
    assert attachment.status_code == 403
    assert calls == []


def test_send_endpoints_invoke_osascript_when_enabled(client, monkeypatch):
    monkeypatch.setenv("ENABLE_SEND", "true")
    calls = []

    class Result:
        stdout = "sent\n"
        stderr = ""

    def fake_run(args, **kwargs):
        calls.append(list(args))
        return Result()

    monkeypatch.setattr(main, "run", fake_run)

    text = client.post(
        "/send-imessage",
        headers=AUTH,
        json={"to": "person", "text": "hi", "service": "imessage"},
    )
    attachment = client.post(
        "/send-imessage-with-attachment",
        headers=AUTH,
        data={"to": "person", "service": "imessage", "caption": "cap"},
        files={"file": ("note.txt", b"hello", "text/plain")},
    )
    assert text.status_code == 200
    assert attachment.status_code == 200
    assert len(calls) == 2
    assert calls[0][0] == "osascript"
    assert calls[1][0] == "osascript"
