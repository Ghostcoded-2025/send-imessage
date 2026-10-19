import ast
import io
import json
import os
import plistlib
import sqlite3
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from email import message_from_string
from pathlib import Path
from unittest.mock import patch

import relay
from message_text import message_text
from relay import (
    APPLE_EPOCH_UNIX,
    NANOSECOND_THRESHOLD,
    ConfigError,
    WebhookError,
    apple_date_to_iso,
    chat_db_uri,
    default_chat_db,
    default_state_file,
    deliver,
    event_from_row,
    find_chats,
    load_config,
    load_env_file,
    load_state,
    main,
    open_chat_db,
    post_json,
    run_once,
    save_state,
    _load_env_simple,
)

SCHEMA = """
CREATE TABLE message (
  ROWID INTEGER PRIMARY KEY,
  guid TEXT,
  text TEXT,
  attributedBody BLOB,
  handle_id INTEGER,
  is_from_me INTEGER,
  date INTEGER,
  associated_message_type INTEGER
);
CREATE TABLE handle (
  ROWID INTEGER PRIMARY KEY,
  id TEXT
);
CREATE TABLE chat (
  ROWID INTEGER PRIMARY KEY,
  guid TEXT,
  chat_identifier TEXT,
  display_name TEXT,
  style INTEGER
);
CREATE TABLE chat_message_join (
  chat_id INTEGER,
  message_id INTEGER
);
CREATE TABLE chat_handle_join (
  chat_id INTEGER,
  handle_id INTEGER
);
"""

ROOT = Path(__file__).resolve().parents[1]
KEY = "super-secret-webhook-key-xyz"


def _base_env(**overrides):
    env = {
        "ALLOWED_CHAT_GUIDS": "chat-guid",
        "WEBHOOK_URL": "https://example.com/hook",
        "WEBHOOK_KEY": "test-key",
    }
    env.update(overrides)
    return env


def _row(**overrides):
    row = {
        "rowid": 1,
        "guid": "message-guid",
        "date": 1,
        "is_from_me": 0,
        "text": "hello",
        "attributed_body": None,
        "associated_message_type": 0,
        "handle": "handle-id",
        "chat_guid": "chat-guid",
    }
    row.update(overrides)
    return row


class AppleEpochTests(unittest.TestCase):
    def test_zero_seconds_is_apple_epoch(self):
        self.assertEqual(apple_date_to_iso(0), "2001-01-01T00:00:00Z")

    def test_seconds_and_nanoseconds_are_the_same_instant(self):
        apple_seconds = 721692800
        expected = datetime.fromtimestamp(
            APPLE_EPOCH_UNIX + apple_seconds, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(expected, "2023-11-14T22:13:20Z")
        self.assertEqual(apple_date_to_iso(apple_seconds), expected)
        self.assertEqual(apple_date_to_iso(apple_seconds * 1_000_000_000), expected)

    def test_nanosecond_fraction_is_preserved(self):
        # Above the seconds/nanoseconds threshold (1e12 ns ~= 16.7 minutes).
        self.assertEqual(apple_date_to_iso(1_500_500_000_000), "2001-01-01T00:25:00.5Z")
        self.assertEqual(apple_date_to_iso(1_000_000_000_001), "2001-01-01T00:16:40.000000001Z")

    def test_threshold_treats_small_values_as_seconds(self):
        seconds = 999_999_999
        as_seconds = datetime.fromtimestamp(
            APPLE_EPOCH_UNIX + seconds, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertLess(seconds, NANOSECOND_THRESHOLD)
        self.assertEqual(apple_date_to_iso(seconds), as_seconds)
        self.assertEqual(apple_date_to_iso(NANOSECOND_THRESHOLD), "2001-01-01T00:16:40Z")
        # Same instant written as nanoseconds must match, not overflow as seconds.
        self.assertEqual(apple_date_to_iso(seconds * 1_000_000_000), as_seconds)

    def test_missing_and_non_finite_dates(self):
        self.assertIsNone(apple_date_to_iso(None))
        self.assertIsNone(apple_date_to_iso(True))
        self.assertIsNone(apple_date_to_iso(float("nan")))
        self.assertEqual(apple_date_to_iso(1.0), "2001-01-01T00:00:01Z")


class FilterTests(unittest.TestCase):
    def test_reuses_shared_decoder(self):
        self.assertIs(relay.message_text, message_text)

    def test_tapback_and_empty_text_are_skipped(self):
        self.assertIsNone(event_from_row(_row(associated_message_type=2000, text="Loved “hi”")))
        self.assertIsNone(event_from_row(_row(text="")))
        self.assertIsNone(event_from_row(_row(text="   ")))
        self.assertIsNone(event_from_row(_row(text=None, attributed_body=None)))
        self.assertIsNone(event_from_row(_row(date=None)))

    def test_null_text_uses_attributed_body(self):
        blob = b"xxNSString\x00Hello thereNSDictionaryyy"
        event = event_from_row(_row(text=None, attributed_body=blob, date=0))
        self.assertEqual(
            event,
            {
                "sender": "handle-id",
                "text": "Hello there",
                "timestamp": "2001-01-01T00:00:00Z",
                "chat_id": "chat-guid",
                "guid": "message-guid",
            },
        )

    def test_outgoing_sender_is_me_even_when_handle_is_set(self):
        event = event_from_row(_row(is_from_me=1, handle="handle-id"))
        self.assertEqual(event["sender"], "me")

    def test_null_association_is_not_a_tapback(self):
        event = event_from_row(_row(associated_message_type=None))
        self.assertEqual(event["text"], "hello")


class StateTests(unittest.TestCase):
    def test_atomic_replace_and_failed_replace_keeps_previous(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "state.json"
            save_state(path, 5)
            self.assertEqual(load_state(path), 5)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"last_rowid": 5})
            with patch("relay.os.replace", side_effect=OSError("boom")):
                with self.assertRaises(OSError):
                    save_state(path, 9)
            self.assertEqual(load_state(path), 5)
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_corrupt_state_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_state(path)
            path.write_text('{"last_rowid": true}', encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_state(path)
            self.assertIsNone(load_state(Path(tmp) / "missing.json"))


class ConfigTests(unittest.TestCase):
    def test_defaults_and_guid_list(self):
        cfg = load_config(_base_env(WEBHOOK_KEY="", ALLOWED_CHAT_GUIDS=" guid-a, ,guid-b "))
        self.assertEqual(cfg.allowed_chat_guids, ("guid-a", "guid-b"))
        self.assertEqual(cfg.chat_db, default_chat_db())
        self.assertEqual(cfg.state_file, default_state_file())
        self.assertEqual(cfg.poll_interval, 30)
        self.assertEqual(cfg.max_per_run, 100)
        self.assertFalse(cfg.backfill)
        self.assertIsNone(cfg.authorization_header)

    def test_refuses_empty_guids_and_non_https(self):
        for raw in ("", "  ", " , ", None):
            env = _base_env()
            if raw is None:
                del env["ALLOWED_CHAT_GUIDS"]
            else:
                env["ALLOWED_CHAT_GUIDS"] = raw
            with self.assertRaises(ConfigError):
                load_config(env)
        with self.assertRaises(ConfigError) as ctx:
            load_config(_base_env(WEBHOOK_URL=f"http://example.com/hook?k={KEY}"))
        self.assertNotIn(KEY, str(ctx.exception))
        with self.assertRaises(ConfigError):
            load_config(_base_env(WEBHOOK_URL="https://"))
        with self.assertRaises(ConfigError):
            load_config(_base_env(WEBHOOK_URL="ftp://example.com/hook"))

    def test_https_scheme_is_case_insensitive(self):
        cfg = load_config(_base_env(WEBHOOK_URL="HTTPS://example.com/hook"))
        self.assertEqual(cfg.webhook_url, "HTTPS://example.com/hook")

    def test_authorization_header(self):
        bearer = load_config(_base_env(WEBHOOK_KEY="k"))
        self.assertEqual(bearer.authorization_header, "Bearer k")
        custom = load_config(_base_env(WEBHOOK_KEY="k", WEBHOOK_AUTH_SCHEME="Token"))
        self.assertEqual(custom.authorization_header, "Token k")
        raw = load_config(
            _base_env(WEBHOOK_KEY="k", AUTH_HEADER_RAW="Token raw-value")
        )
        self.assertEqual(raw.authorization_header, "Token raw-value")
        self.assertNotIn("raw-value", repr(raw))
        self.assertNotIn("Token", repr(raw))

    def test_rejects_header_injection_without_echoing_the_key(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config(_base_env(WEBHOOK_KEY=f"{KEY}\r\nX-Evil: 1"))
        self.assertNotIn(KEY, str(ctx.exception))

    def test_numeric_and_bool_validation(self):
        cfg = load_config(_base_env(POLL_INTERVAL="1.5", MAX_PER_RUN="2", BACKFILL="yes"))
        self.assertEqual(cfg.poll_interval, 1.5)
        self.assertEqual(cfg.max_per_run, 2)
        self.assertTrue(cfg.backfill)
        home_cfg = load_config(_base_env(CHAT_DB="~/chat.db", STATE_FILE="~/relay-state.json"))
        self.assertEqual(home_cfg.chat_db, Path.home() / "chat.db")
        self.assertEqual(home_cfg.state_file, Path.home() / "relay-state.json")
        with self.assertRaises(ConfigError):
            load_config(_base_env(POLL_INTERVAL="0"))
        with self.assertRaises(ConfigError):
            load_config(_base_env(MAX_PER_RUN="nope"))
        with self.assertRaises(ConfigError):
            load_config(_base_env(BACKFILL="maybe"))

    def test_env_file_does_not_override(self):
        os.environ.pop("RELAY_TEST_KEY", None)
        os.environ["RELAY_TEST_EXISTING"] = "keep"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / ".env"
                path.write_text(
                    "\n".join(
                        [
                            "# comment",
                            'export RELAY_TEST_KEY="abc def"',
                            "RELAY_TEST_EXISTING=changed",
                            "not a pair",
                        ]
                    ),
                    encoding="utf-8",
                )
                _load_env_simple(path)
                self.assertEqual(os.environ["RELAY_TEST_KEY"], "abc def")
                self.assertEqual(os.environ["RELAY_TEST_EXISTING"], "keep")
                os.environ["RELAY_TEST_KEY"] = "from-env"
                load_env_file(path)
                self.assertEqual(os.environ["RELAY_TEST_KEY"], "from-env")
        finally:
            os.environ.pop("RELAY_TEST_KEY", None)
            os.environ.pop("RELAY_TEST_EXISTING", None)

    def test_main_refuses_bad_config(self):
        self.assertEqual(
            main(["--once"], env={"ALLOWED_CHAT_GUIDS": " , ", "WEBHOOK_URL": "http://example.com"}),
            2,
        )


class ChatDbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "dir with space" / "chat.db"
        self.db_path.parent.mkdir()
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.state_path = Path(self.tmp.name) / "state.json"

    def tearDown(self):
        try:
            self.conn.close()
        except sqlite3.ProgrammingError:
            pass

    def _chat(self, rowid, guid, identifier="", display_name=None, style=45):
        self.conn.execute(
            "INSERT INTO chat (ROWID, guid, chat_identifier, display_name, style) VALUES (?, ?, ?, ?, ?)",
            (rowid, guid, identifier, display_name, style),
        )

    def _handle(self, rowid, ident):
        self.conn.execute("INSERT INTO handle (ROWID, id) VALUES (?, ?)", (rowid, ident))

    def _message(
        self,
        rowid,
        guid,
        chat_id,
        *,
        text="hello",
        body=None,
        handle_id=None,
        is_from_me=0,
        date=1,
        assoc=0,
    ):
        self.conn.execute(
            """
            INSERT INTO message (
              ROWID, guid, text, attributedBody, handle_id, is_from_me, date, associated_message_type
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (rowid, guid, text, body, handle_id, is_from_me, date, assoc),
        )
        self.conn.execute(
            "INSERT INTO chat_message_join (chat_id, message_id) VALUES (?, ?)",
            (chat_id, rowid),
        )

    def _cfg(self, **overrides):
        env = _base_env(
            CHAT_DB=str(self.db_path),
            STATE_FILE=str(self.state_path),
            ALLOWED_CHAT_GUIDS="chat-guid",
            **overrides,
        )
        return load_config(env)

    def test_uri_is_read_only(self):
        uri = chat_db_uri(self.db_path)
        self.assertTrue(uri.startswith("file:"))
        self.assertIn("?mode=ro", uri)
        self.assertIn("dir%20with%20space", uri)
        self.conn.commit()
        ro = open_chat_db(self.db_path)
        self.addCleanup(ro.close)
        with self.assertRaises(sqlite3.OperationalError):
            ro.execute("CREATE TABLE nope (id INTEGER)")
        with patch("relay.sqlite3.connect", return_value=self.conn) as connect:
            open_chat_db(self.db_path)
        self.assertTrue(connect.call_args.kwargs["uri"])
        self.assertIn("?mode=ro", connect.call_args.args[0])

    def test_cursor_advances_past_skips_only_after_2xx(self):
        self._handle(1, "handle-id")
        self._chat(1, "chat-guid", identifier="handle-id", display_name="Example", style=45)
        self._chat(2, "other-guid")
        self._message(5, "tap-guid", 1, text="Loved", assoc=2000, handle_id=1)
        self._message(6, "ok-guid", 1, text="hello", handle_id=1, is_from_me=0, date=721692800)
        self._message(7, "blank-guid", 1, text="  ", handle_id=1)
        self._message(8, "me-guid", 1, text="from me", is_from_me=1, handle_id=1, date=1)
        self._message(9, "other-msg", 2, text="ignore", date=1)
        blob = b"xxNSString\x00From bodyNSDictionary"
        self._message(10, "body-guid", 1, text=None, body=blob, handle_id=1, date=1_500_500_000_000)
        self.conn.commit()
        save_state(self.state_path, 4)
        cfg = self._cfg(MAX_PER_RUN="10")

        def post(events):
            self.assertEqual(
                [event["guid"] for event in events],
                ["ok-guid", "me-guid", "body-guid"],
            )
            self.assertEqual(events[0]["sender"], "handle-id")
            self.assertEqual(events[0]["chat_id"], "chat-guid")
            self.assertEqual(events[0]["timestamp"], "2023-11-14T22:13:20Z")
            self.assertEqual(events[1]["sender"], "me")
            self.assertEqual(events[2]["text"], "From body")
            self.assertEqual(events[2]["timestamp"], "2001-01-01T00:25:00.5Z")
            self.assertEqual(set(events[0]), {"sender", "text", "timestamp", "chat_id", "guid"})
            return 500

        with self.assertLogs("imessage_relay", level="INFO") as captured:
            self.assertFalse(run_once(cfg, conn=self.conn, post=post))
        self.assertIn("will retry next poll", "\n".join(captured.output))
        self.assertEqual(load_state(self.state_path), 4)

        calls = []

        def ok(events):
            calls.append(events)
            return 204

        self.assertTrue(run_once(cfg, conn=self.conn, post=ok))
        self.assertEqual(len(calls), 1)
        self.assertEqual(load_state(self.state_path), 10)

        calls.clear()
        self.assertTrue(run_once(cfg, conn=self.conn, post=ok))
        self.assertEqual(calls, [])
        self.assertEqual(load_state(self.state_path), 10)

    def test_skipped_only_batch_advances_without_post(self):
        self._chat(1, "chat-guid")
        self._message(2, "tap", 1, text="Liked", assoc=2001)
        self._message(3, "empty", 1, text=None)
        self.conn.commit()
        save_state(self.state_path, 1)
        calls = []
        self.assertTrue(run_once(self._cfg(), conn=self.conn, post=lambda events: calls.append(events) or 200))
        self.assertEqual(calls, [])
        self.assertEqual(load_state(self.state_path), 3)

    def test_4xx_and_network_do_not_advance(self):
        self._chat(1, "chat-guid")
        self._message(3, "m", 1, text="hello")
        self.conn.commit()
        save_state(self.state_path, 0)
        cfg = self._cfg()
        with self.assertLogs("imessage_relay", level="INFO") as captured:
            self.assertFalse(run_once(cfg, conn=self.conn, post=lambda events: 403))
        text = "\n".join(captured.output)
        self.assertIn("status=403", text)
        self.assertIn("cursor not advanced", text)
        self.assertNotIn("test-key", text)
        self.assertEqual(load_state(self.state_path), 0)

        def boom(events):
            raise WebhookError("down")

        with self.assertLogs("imessage_relay", level="INFO") as captured:
            self.assertFalse(run_once(cfg, conn=self.conn, post=boom))
        self.assertIn("webhook unreachable", "\n".join(captured.output))
        self.assertEqual(load_state(self.state_path), 0)

    def test_first_run_max_rowid_unless_backfill(self):
        self._chat(1, "chat-guid")
        self._chat(2, "other-guid")
        self._message(4, "old", 1, text="old")
        self._message(9, "newer-other", 2, text="nope")
        self.conn.commit()
        calls = []
        cfg = self._cfg(BACKFILL="0")
        self.assertTrue(run_once(cfg, conn=self.conn, post=lambda events: calls.append(events) or 200))
        self.assertEqual(calls, [])
        self.assertEqual(load_state(self.state_path), 9)

        self.state_path.unlink()
        calls.clear()
        backfill = self._cfg(BACKFILL="1")
        self.assertTrue(run_once(backfill, conn=self.conn, post=lambda events: calls.append(events) or 200))
        self.assertEqual([event["guid"] for event in calls[0]], ["old"])
        self.assertEqual(load_state(self.state_path), 4)

    def test_max_per_run_pages(self):
        self._chat(1, "chat-guid")
        self._message(1, "a", 1, text="a")
        self._message(2, "b", 1, text="b")
        self.conn.commit()
        seen = []
        cfg = self._cfg(MAX_PER_RUN="1", BACKFILL="1")
        self.assertTrue(run_once(cfg, conn=self.conn, post=lambda events: seen.append(events[0]["guid"]) or 200))
        self.assertEqual(seen, ["a"])
        self.assertEqual(load_state(self.state_path), 1)
        self.assertTrue(run_once(cfg, conn=self.conn, post=lambda events: seen.append(events[0]["guid"]) or 200))
        self.assertEqual(seen, ["a", "b"])
        self.assertEqual(load_state(self.state_path), 2)

    def test_find_chat_matches_name_identifier_and_handle(self):
        self._handle(1, "handle-id")
        self._handle(2, "other-handle")
        self._chat(1, "guid-example", identifier="chat-ident", display_name="Example Chat", style=43)
        self._chat(2, "guid-dm", identifier="handle-id", display_name=None, style=45)
        self._chat(3, "guid-other", identifier="nope", display_name="Nope", style=45)
        self.conn.execute("INSERT INTO chat_handle_join (chat_id, handle_id) VALUES (1, 1)")
        self.conn.execute("INSERT INTO chat_handle_join (chat_id, handle_id) VALUES (1, 2)")
        self.conn.execute("INSERT INTO chat_handle_join (chat_id, handle_id) VALUES (2, 1)")
        self._message(5, "m", 2, text="hi", date=0)
        self.conn.commit()

        by_name = find_chats(self.conn, "example")
        self.assertEqual([row["guid"] for row in by_name], ["guid-example"])
        self.assertEqual(by_name[0]["style"], 43)
        self.assertIsNone(by_name[0]["last_message_date"])

        by_ident = find_chats(self.conn, "chat-ident")
        self.assertEqual([row["guid"] for row in by_ident], ["guid-example"])

        by_handle = find_chats(self.conn, "handle-id")
        self.assertEqual([row["guid"] for row in by_handle], ["guid-example", "guid-dm"])
        self.assertEqual(apple_date_to_iso(by_handle[1]["last_message_date"]), "2001-01-01T00:00:00Z")

        self.assertEqual(find_chats(self.conn, "_"), [])
        self._chat(4, "guid-us", identifier="a_b", display_name="100%", style=45)
        self.conn.commit()
        self.assertEqual([row["guid"] for row in find_chats(self.conn, "a_b")], ["guid-us"])
        self.assertEqual([row["guid"] for row in find_chats(self.conn, "%")], ["guid-us"])

    def test_find_chat_cli_does_not_need_webhook_config(self):
        self._chat(7, "guid-example", display_name="Example Chat", style=45)
        self.conn.commit()
        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            code = main(
                ["--find-chat", "Example"],
                env={"CHAT_DB": str(self.db_path)},
            )
        self.assertEqual(code, 0)
        lines = stdout.getvalue().splitlines()
        self.assertEqual(lines[0], "ROWID\tguid\tdisplay_name\tstyle\tlast_message_date")
        self.assertEqual(lines[1], "7\tguid-example\tExample Chat\t45\t")

    def test_once_hides_key_and_sets_exit_code(self):
        self._chat(1, "chat-guid")
        self._message(5, "message-guid", 1, text="hello", is_from_me=1, date=1)
        self.conn.commit()
        self.conn.close()
        env = _base_env(
            CHAT_DB=str(self.db_path),
            STATE_FILE=str(self.state_path),
            WEBHOOK_KEY=KEY,
            BACKFILL="1",
        )
        with patch("relay.post_json", return_value=403) as posted:
            with self.assertLogs("imessage_relay", level="INFO") as captured:
                code = main(["--once"], env=env)
        self.assertEqual(code, 1)
        self.assertEqual(posted.call_args.args[0], "https://example.com/hook")
        self.assertEqual(posted.call_args.args[2], f"Bearer {KEY}")
        payload = posted.call_args.args[1]
        self.assertEqual(payload[0]["sender"], "me")
        self.assertEqual(payload[0]["guid"], "message-guid")
        logged = "\n".join(captured.output)
        self.assertNotIn(KEY, logged)
        self.assertIn("cursor not advanced", logged)
        self.assertEqual(load_state(self.state_path), 0)

        with patch("relay.post_json", return_value=204):
            self.assertEqual(main(["--once"], env=env), 0)
        self.assertEqual(load_state(self.state_path), 5)


class HttpTests(unittest.TestCase):
    def test_post_json_sends_array_and_timeout(self):
        payload = [
            {
                "sender": "me",
                "text": "hi",
                "timestamp": "2001-01-01T00:00:00Z",
                "chat_id": "chat-guid",
                "guid": "message-guid",
            }
        ]

        class Resp:
            status = 202

            def read(self):
                return b""

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        with patch("relay.urllib.request.urlopen", return_value=Resp()) as urlopen:
            status = post_json("https://example.com/hook", payload, "Bearer secret")
        self.assertEqual(status, 202)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer secret")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(json.loads(request.data.decode()), payload)
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 10)

        with patch("relay.urllib.request.urlopen", return_value=Resp()) as urlopen:
            post_json("https://example.com/hook", payload, None)
        self.assertFalse(urlopen.call_args.args[0].has_header("Authorization"))

    def test_http_error_and_network_error(self):
        err = urllib.error.HTTPError(
            "https://example.com/hook",
            503,
            "no",
            message_from_string("Content-Type: text/plain\n\n"),
            io.BytesIO(b"nope"),
        )
        with patch("relay.urllib.request.urlopen", side_effect=err):
            self.assertEqual(post_json("https://example.com/hook", [], "Bearer k"), 503)
        with patch("relay.urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
            with self.assertRaises(WebhookError):
                post_json("https://example.com/hook", [], "Bearer k")


class SourceTests(unittest.TestCase):
    def test_relay_does_not_import_send_machinery(self):
        source = (ROOT / "relay.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertNotIn("subprocess", imported)
        self.assertNotIn("osascript", source)

    def test_plist_template_uses_keepalive(self):
        plist = plistlib.loads((ROOT / "com.example.imessage-relay.plist").read_bytes())
        self.assertEqual(plist["Label"], "com.example.imessage-relay")
        self.assertIs(plist["KeepAlive"], True)
        self.assertNotIn("StartInterval", plist)
        self.assertEqual(
            plist["ProgramArguments"],
            ["/REPLACE/WITH/REPO/.venv/bin/python", "/REPLACE/WITH/REPO/relay.py"],
        )
        self.assertEqual(plist["WorkingDirectory"], "/REPLACE/WITH/REPO")
        log = "/Users/REPLACE_ME/Library/Logs/imessage-relay.log"
        self.assertEqual(plist["StandardOutPath"], log)
        self.assertEqual(plist["StandardErrorPath"], log)
        text = (ROOT / "com.example.imessage-relay.plist").read_text(encoding="utf-8")
        self.assertNotIn("--once", text)


class DeliverOrderTests(unittest.TestCase):
    def test_out_of_order_rows_post_in_rowid_order(self):
        seen = []

        def post(events):
            seen.extend(event["guid"] for event in events)
            return 200

        outcome, cursor = deliver(
            [_row(rowid=8, guid="b"), _row(rowid=6, guid="a")],
            post,
        )
        self.assertEqual(outcome, "ok")
        self.assertEqual(cursor, 8)
        self.assertEqual(seen, ["a", "b"])


if __name__ == "__main__":
    unittest.main()
