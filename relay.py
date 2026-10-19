"""Read-only poller: chat.db -> HTTPS webhook.

Does not send iMessages. No subprocess, no AppleScript.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

from message_text import message_text

logger = logging.getLogger("imessage_relay")

APPLE_EPOCH_UNIX = 978307200
# Magnitudes at or above this are nanoseconds since the Apple epoch.
# 1e12 ns is ~16.7 minutes, so real nanosecond timestamps clear the bar
# and real second timestamps (currently ~1e9) stay below it.
NANOSECOND_THRESHOLD = 1_000_000_000_000

DEFAULT_POLL_INTERVAL = 30.0
DEFAULT_MAX_PER_RUN = 100
DEFAULT_AUTH_SCHEME = "Bearer"
WEBHOOK_TIMEOUT_SECONDS = 10


class ConfigError(ValueError):
    """Invalid or missing relay configuration."""


class WebhookError(Exception):
    """Network failure talking to the webhook. Status is unknown."""


@dataclass(frozen=True)
class Config:
    chat_db: Path
    allowed_chat_guids: tuple[str, ...]
    webhook_url: str
    authorization_header: str | None
    state_file: Path
    poll_interval: float
    max_per_run: int
    backfill: bool

    def __repr__(self) -> str:
        return (
            f"Config(chats={len(self.allowed_chat_guids)}, "
            f"poll_interval={self.poll_interval}, max_per_run={self.max_per_run}, "
            f"backfill={self.backfill})"
        )


def default_chat_db() -> Path:
    return Path.home() / "Library" / "Messages" / "chat.db"


def default_state_file() -> Path:
    return Path.home() / ".imessage-relay-state.json"


def configure_logging() -> None:
    if any(getattr(handler, "_imessage_relay", False) for handler in logger.handlers):
        return
    handler = logging.StreamHandler(sys.stderr)
    handler._imessage_relay = True  # type: ignore[attr-defined]
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def _load_env_simple(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if not key or not key.replace("_", "").isalnum():
            continue
        os.environ.setdefault(key, value)


def load_env_file(path: Path) -> None:
    """Load path into the environment. Existing variables win. dotenv is optional."""
    path = Path(path)
    if not path.is_file():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        _load_env_simple(path)
        return
    load_dotenv(dotenv_path=path, override=False)


def _reject_ctl(value: str, name: str) -> str:
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ConfigError(f"{name} contains invalid characters")
    return value


def _parse_guids(raw: str | None) -> tuple[str, ...]:
    if raw is None or not raw.strip():
        raise ConfigError(
            "ALLOWED_CHAT_GUIDS is required (comma-separated chat GUIDs); refusing to start"
        )
    guids = tuple(part.strip() for part in raw.split(",") if part.strip())
    if not guids:
        raise ConfigError(
            "ALLOWED_CHAT_GUIDS is required (comma-separated chat GUIDs); refusing to start"
        )
    return guids


def _validate_webhook_url(url: str | None) -> str:
    if url is None or not url.strip():
        raise ConfigError("WEBHOOK_URL must be an https URL with a host")
    cleaned = _reject_ctl(url.strip(), "WEBHOOK_URL")
    parsed = urllib.parse.urlparse(cleaned)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ConfigError("WEBHOOK_URL must be an https URL with a host")
    return cleaned


def _parse_positive_float(raw: str, name: str, default: float) -> float:
    if raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a positive number") from exc
    if not math.isfinite(value) or value <= 0:
        raise ConfigError(f"{name} must be a positive number")
    return value


def _parse_positive_int(raw: str, name: str, default: int) -> int:
    if raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be a positive integer")
    return value


def _parse_bool(raw: str, default: bool = False) -> bool:
    if raw.strip() == "":
        return default
    val = raw.strip().lower()
    if val in {"1", "true", "yes", "on"}:
        return True
    if val in {"0", "false", "no", "off"}:
        return False
    raise ConfigError("BACKFILL must be 0/1, true/false, yes/no, or on/off")


def _authorization(env: Mapping[str, str]) -> str | None:
    raw = _reject_ctl(env.get("AUTH_HEADER_RAW", "").strip(), "AUTH_HEADER_RAW")
    if raw:
        return raw
    key = _reject_ctl(env.get("WEBHOOK_KEY", "").strip(), "WEBHOOK_KEY")
    if not key:
        return None
    scheme = env.get("WEBHOOK_AUTH_SCHEME", "").strip() or DEFAULT_AUTH_SCHEME
    scheme = _reject_ctl(scheme, "WEBHOOK_AUTH_SCHEME")
    return f"{scheme} {key}"


def _env_path(env: Mapping[str, str], key: str, default: Path) -> Path:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    return Path(raw).expanduser()


def load_config(env: Mapping[str, str] | None = None) -> Config:
    source = os.environ if env is None else env
    return Config(
        chat_db=_env_path(source, "CHAT_DB", default_chat_db()),
        allowed_chat_guids=_parse_guids(source.get("ALLOWED_CHAT_GUIDS")),
        webhook_url=_validate_webhook_url(source.get("WEBHOOK_URL")),
        authorization_header=_authorization(source),
        state_file=_env_path(source, "STATE_FILE", default_state_file()),
        poll_interval=_parse_positive_float(
            source.get("POLL_INTERVAL", ""), "POLL_INTERVAL", DEFAULT_POLL_INTERVAL
        ),
        max_per_run=_parse_positive_int(
            source.get("MAX_PER_RUN", ""), "MAX_PER_RUN", DEFAULT_MAX_PER_RUN
        ),
        backfill=_parse_bool(source.get("BACKFILL", "")),
    )


def chat_db_uri(path: Path) -> str:
    resolved = Path(path).expanduser().resolve()
    quoted = urllib.parse.quote(resolved.as_posix(), safe="/")
    return f"file:{quoted}?mode=ro"


def open_chat_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(chat_db_uri(path), uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def apple_date_to_iso(value: object) -> str | None:
    """Convert an Apple-epoch timestamp (seconds or nanoseconds) to UTC ISO-8601."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return None
        value = int(value)
    if not isinstance(value, int):
        return None
    nanos = abs(value) >= NANOSECOND_THRESHOLD
    if nanos:
        sign = -1 if value < 0 else 1
        whole, rem = divmod(abs(value), 1_000_000_000)
        unix_seconds = APPLE_EPOCH_UNIX + sign * whole
        frac = rem
        if sign < 0 and frac:
            unix_seconds -= 1
            frac = 1_000_000_000 - frac
    else:
        unix_seconds = APPLE_EPOCH_UNIX + value
        frac = 0
    try:
        dt = datetime.fromtimestamp(unix_seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    if frac == 0:
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{frac:09d}".rstrip("0") + "Z"


def _field(row: Mapping[str, object] | sqlite3.Row, key: str) -> object:
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def _is_tapback(value: object) -> bool:
    if value is None:
        return False
    try:
        return int(value) != 0  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return True


def _is_from_me(value: object) -> bool:
    if value is True:
        return True
    if value is False or value is None:
        return False
    try:
        return int(value) == 1  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


def _as_text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return None


def _as_blob(value: object) -> bytes | None:
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, memoryview):
        return value.tobytes()
    return None


def event_from_row(row: Mapping[str, object] | sqlite3.Row) -> dict[str, str] | None:
    if _is_tapback(_field(row, "associated_message_type")):
        return None
    decoded = message_text(_as_text(_field(row, "text")), _as_blob(_field(row, "attributed_body")))
    if not decoded or not decoded.strip():
        return None
    timestamp = apple_date_to_iso(_field(row, "date"))
    if timestamp is None:
        return None
    if _is_from_me(_field(row, "is_from_me")):
        sender = "me"
    else:
        handle = _field(row, "handle")
        sender = handle if isinstance(handle, str) else ""
    chat_guid = _field(row, "chat_guid")
    guid = _field(row, "guid")
    return {
        "sender": sender,
        "text": decoded,
        "timestamp": timestamp,
        "chat_id": chat_guid if isinstance(chat_guid, str) else "",
        "guid": guid if isinstance(guid, str) else "",
    }


def load_state(path: Path) -> int | None:
    path = Path(path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        rowid = data["last_rowid"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ConfigError(f"state file is unreadable: {path}") from exc
    if isinstance(rowid, bool) or not isinstance(rowid, int) or rowid < 0:
        raise ConfigError(f"state file is unreadable: {path}")
    return rowid


def save_state(path: Path, last_rowid: int) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"last_rowid": last_rowid}, handle)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def max_rowid(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(ROWID) AS max_rowid FROM message").fetchone()
    if row is None or row["max_rowid"] is None:
        return 0
    return int(row["max_rowid"])


def fetch_messages(
    conn: sqlite3.Connection,
    last_rowid: int,
    guids: tuple[str, ...],
    limit: int,
) -> list[sqlite3.Row]:
    if not guids:
        raise ConfigError(
            "ALLOWED_CHAT_GUIDS is required (comma-separated chat GUIDs); refusing to start"
        )
    params: dict[str, object] = {"last": last_rowid, "n": limit}
    placeholders = []
    for index, guid in enumerate(guids):
        key = f"g{index}"
        placeholders.append(f":{key}")
        params[key] = guid
    sql = f"""
        SELECT
          m.ROWID AS rowid,
          m.guid AS guid,
          m.date AS date,
          m.is_from_me AS is_from_me,
          m.text AS text,
          m.attributedBody AS attributed_body,
          m.associated_message_type AS associated_message_type,
          h.id AS handle,
          c.guid AS chat_guid
        FROM message m
        JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        JOIN chat c ON c.ROWID = cmj.chat_id
        LEFT JOIN handle h ON h.ROWID = m.handle_id
        WHERE m.ROWID > :last AND c.guid IN ({", ".join(placeholders)})
        ORDER BY m.ROWID
        LIMIT :n
    """
    return list(conn.execute(sql, params))


def post_json(
    url: str,
    payload: list[dict[str, str]],
    authorization: str | None,
    timeout: float = WEBHOOK_TIMEOUT_SECONDS,
) -> int:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("User-Agent", "imessage-relay")
    if authorization:
        request.add_header("Authorization", authorization)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read()
            return int(response.status)
    except urllib.error.HTTPError as exc:
        try:
            exc.read()
        finally:
            exc.close()
        return int(exc.code)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise WebhookError("webhook request failed") from exc


def deliver(
    rows: list[Mapping[str, object] | sqlite3.Row],
    post: Callable[[list[dict[str, str]]], int],
) -> tuple[str, int | None]:
    """Return ('idle'|'ok'|'retry', new cursor). Cursor is None when it must not move."""
    if not rows:
        return "idle", None
    ordered = sorted(rows, key=lambda row: int(_field(row, "rowid")))  # type: ignore[arg-type]
    max_id = int(_field(ordered[-1], "rowid"))  # type: ignore[arg-type]
    events = [event for event in (event_from_row(row) for row in ordered) if event is not None]
    if not events:
        logger.info("skipped %s row(s); cursor=%s", len(ordered), max_id)
        return "ok", max_id
    try:
        status = post(events)
    except WebhookError:
        logger.warning("webhook unreachable; will retry next poll")
        return "retry", None
    if 200 <= status <= 299:
        logger.info("posted %s message(s); cursor=%s", len(events), max_id)
        return "ok", max_id
    if 400 <= status <= 499:
        logger.error("webhook rejected the batch status=%s; cursor not advanced", status)
        return "retry", None
    logger.warning("webhook status=%s; will retry next poll", status)
    return "retry", None


def run_once(
    cfg: Config,
    conn: sqlite3.Connection | None = None,
    post: Callable[[list[dict[str, str]]], int] | None = None,
) -> bool:
    """Poll once. True when the cursor is consistent; False when the webhook must be retried."""
    own_conn = conn is None
    if conn is None:
        try:
            conn = open_chat_db(cfg.chat_db)
        except sqlite3.Error as exc:
            logger.error("chat.db read failed: %s", exc)
            return False
    if post is None:
        def post(events: list[dict[str, str]], cfg: Config = cfg) -> int:
            return post_json(cfg.webhook_url, events, cfg.authorization_header)

    try:
        try:
            last = load_state(cfg.state_file)
            if last is None:
                last = 0 if cfg.backfill else max_rowid(conn)
                save_state(cfg.state_file, last)
                logger.info("initialized cursor=%s backfill=%s", last, cfg.backfill)
            rows = fetch_messages(conn, last, cfg.allowed_chat_guids, cfg.max_per_run)
            outcome, new_last = deliver(rows, post)
            if outcome == "ok" and new_last is not None:
                save_state(cfg.state_file, new_last)
            return outcome != "retry"
        except sqlite3.Error as exc:
            logger.error("chat.db read failed: %s", exc)
            return False
        except OSError:
            logger.error("state file update failed")
            return False
    finally:
        if own_conn:
            conn.close()


def _like_pattern(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def find_chats(conn: sqlite3.Connection, name: str) -> list[sqlite3.Row]:
    sql = """
        SELECT
          c.ROWID AS rowid,
          c.guid AS guid,
          c.display_name AS display_name,
          c.style AS style,
          MAX(m.date) AS last_message_date
        FROM chat c
        LEFT JOIN chat_message_join cmj ON cmj.chat_id = c.ROWID
        LEFT JOIN message m ON m.ROWID = cmj.message_id
        WHERE c.ROWID IN (
            SELECT c2.ROWID
            FROM chat c2
            LEFT JOIN chat_handle_join chj ON chj.chat_id = c2.ROWID
            LEFT JOIN handle h ON h.ROWID = chj.handle_id
            WHERE IFNULL(c2.display_name, '') LIKE :q ESCAPE '\\'
               OR IFNULL(c2.chat_identifier, '') LIKE :q ESCAPE '\\'
               OR IFNULL(h.id, '') LIKE :q ESCAPE '\\'
        )
        GROUP BY c.ROWID, c.guid, c.display_name, c.style
        ORDER BY c.ROWID
    """
    return list(conn.execute(sql, {"q": _like_pattern(name)}))


def _cell(value: object) -> str:
    if value is None:
        return ""
    return str(value).replace("\t", " ").replace("\r", " ").replace("\n", " ")


def format_find_chat_row(row: Mapping[str, object] | sqlite3.Row) -> str:
    date = apple_date_to_iso(_field(row, "last_message_date")) or ""
    style = _field(row, "style")
    return "\t".join(
        (
            _cell(_field(row, "rowid")),
            _cell(_field(row, "guid")),
            _cell(_field(row, "display_name")),
            _cell(style),
            date,
        )
    )


def find_chat_main(name: str, env: Mapping[str, str]) -> int:
    if not name.strip():
        logger.error("--find-chat requires a name")
        return 2
    path = _env_path(env, "CHAT_DB", default_chat_db())
    try:
        conn = open_chat_db(path)
    except sqlite3.Error as exc:
        logger.error("chat.db read failed: %s", exc)
        if "authorization denied" in str(exc).lower():
            logger.error("grant Full Disk Access to this Python binary")
        return 1
    try:
        rows = find_chats(conn, name.strip())
    finally:
        conn.close()
    print("ROWID\tguid\tdisplay_name\tstyle\tlast_message_date")
    for row in rows:
        print(format_find_chat_row(row))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only iMessage webhook relay")
    parser.add_argument(
        "--once",
        action="store_true",
        help="poll one batch and exit",
    )
    parser.add_argument(
        "--find-chat",
        metavar="NAME",
        help="print chats whose display name, identifier, or participant handle matches NAME",
    )
    return parser


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    configure_logging()
    if env is None:
        load_env_file(Path(__file__).resolve().parent / ".env")
        env = os.environ
    args = _build_parser().parse_args(argv)
    if args.find_chat is not None:
        return find_chat_main(args.find_chat, env)
    try:
        cfg = load_config(env)
    except ConfigError as exc:
        logger.error("%s", exc)
        return 2
    logger.info(
        "relay started chats=%s poll=%ss max=%s backfill=%s",
        len(cfg.allowed_chat_guids),
        cfg.poll_interval,
        cfg.max_per_run,
        cfg.backfill,
    )
    if args.once:
        try:
            return 0 if run_once(cfg) else 1
        except ConfigError as exc:
            logger.error("%s", exc)
            return 2
    try:
        while True:
            try:
                run_once(cfg)
            except ConfigError as exc:
                logger.error("%s", exc)
                return 2
            time.sleep(cfg.poll_interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
