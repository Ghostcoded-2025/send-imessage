"""Best-effort iMessage text extraction shared by the API and the relay."""

import re
from typing import Optional


def text_from_attributed_body(blob: Optional[bytes]) -> Optional[str]:
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


def message_text(text: Optional[str], attributed_body: Optional[bytes]) -> Optional[str]:
    if text and text.strip():
        return text
    return text_from_attributed_body(attributed_body)
