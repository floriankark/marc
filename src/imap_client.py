"""IMAP client for Kerio Connect (or any IMAP server).

Uses UID-based commands throughout to avoid sequence-number drift.
Supports quota checking (RFC 2087) and two-phase deletion via Trash.
"""

import email
import imaplib
import re
import ssl
import time
from datetime import datetime, timezone
from email.header import decode_header as _decode_header
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Optional


class ImapClient:
    def __init__(self, cfg: dict):
        self.host = cfg["host"]
        self.port = cfg.get("port", 993)
        self.use_ssl = cfg.get("use_ssl", True)
        self.ssl_verify = cfg.get("ssl_verify", True)
        self.username = cfg["username"]
        self.mailbox = cfg.get("mailbox", "INBOX")
        self._conn: Optional[imaplib.IMAP4_SSL] = None

    # ------------------------------------------------------------------ #
    # Connection
    # ------------------------------------------------------------------ #

    def connect(self, password: str):
        if self.use_ssl:
            ctx = ssl.create_default_context()
            if not self.ssl_verify:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            self._conn = imaplib.IMAP4_SSL(self.host, self.port, ssl_context=ctx)
        else:
            self._conn = imaplib.IMAP4(self.host, self.port)
            self._conn.starttls()

        self._conn.login(self.username, password)
        self._conn.select(self.mailbox, readonly=False)
        print(f"[IMAP] Connected to {self.host} as {self.username}")

    def disconnect(self):
        if self._conn:
            try:
                self._conn.close()
                self._conn.logout()
            except Exception:
                pass
            self._conn = None

    # ------------------------------------------------------------------ #
    # Quota
    # ------------------------------------------------------------------ #

    def get_quota_percent(self) -> Optional[float]:
        """Return mailbox usage as a percentage (0-100), or None if unsupported."""
        try:
            typ, data = self._conn.getquotaroot(self.mailbox)
            if typ != "OK":
                return None
            # data is a flat list mixing bytes and lists, e.g.:
            # [b'INBOX', b'root', b'(root (STORAGE 45000 50000))']
            # or [b'INBOX root', [b'root', b'(STORAGE 45000 50000)']]
            flat = []
            for item in data:
                if isinstance(item, (list, tuple)):
                    flat.extend(item)
                else:
                    flat.append(item)
            combined = " ".join(
                i.decode(errors="replace") if isinstance(i, bytes) else str(i)
                for i in flat
            )
            m = re.search(r"STORAGE\s+(\d+)\s+(\d+)", combined, re.IGNORECASE)
            if m:
                used, limit = int(m.group(1)), int(m.group(2))
                if limit > 0:
                    return round(used / limit * 100, 2)
        except Exception as e:
            print(f"[IMAP] Quota check failed: {e}")
        return None

    # ------------------------------------------------------------------ #
    # Fetching
    # ------------------------------------------------------------------ #

    def fetch_all_uids(self) -> list[str]:
        """Return all UIDs in the mailbox, oldest first."""
        typ, data = self._conn.uid("SEARCH", None, "ALL")
        if typ != "OK" or not data[0]:
            return []
        uids = data[0].decode().split()
        return uids  # IMAP UIDs are already in ascending (oldest-first) order

    def fetch_message_ids_for_uids(self, uids: list[str]) -> dict[str, str]:
        """Batch-fetch Message-ID headers. Returns {uid: message_id}."""
        if not uids:
            return {}
        uid_str = ",".join(uids)
        typ, data = self._conn.uid(
            "FETCH", uid_str, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])"
        )
        if typ != "OK":
            return {}

        result = {}
        current_uid = None
        for part in data:
            if isinstance(part, tuple):
                # part[0] looks like b'123 (UID 456 BODY[...] {n}'
                uid_match = re.search(rb"UID (\d+)", part[0])
                if uid_match:
                    current_uid = uid_match.group(1).decode()
                header_text = part[1].decode(errors="replace") if part[1] else ""
                mid = _extract_header_value(header_text, "Message-ID")
                if current_uid and mid:
                    result[current_uid] = _normalize_mid(mid)
        return result

    def fetch_emails(self, uids: list[str]) -> list[dict]:
        """Fetch full email data for a list of UIDs. Returns list of email dicts."""
        emails, _ = self.fetch_emails_with_raw(uids)
        return emails

    def fetch_emails_with_raw(
        self, uids: list[str]
    ) -> tuple[list[dict], dict[str, bytes]]:
        """Fetch emails and return both parsed dicts and raw RFC822 bytes.

        Returns:
            (emails, raw_by_uid) where raw_by_uid maps uid → raw RFC822 bytes.
            The raw bytes are a complete .eml snapshot including all attachments.
        """
        if not uids:
            return [], {}
        uid_str = ",".join(uids)
        typ, data = self._conn.uid("FETCH", uid_str, "(UID RFC822)")
        if typ != "OK":
            return [], {}

        emails = []
        raw_by_uid: dict[str, bytes] = {}
        for part in data:
            if not isinstance(part, tuple):
                continue
            uid_match = re.search(rb"UID (\d+)", part[0])
            if not uid_match or not part[1]:
                continue
            uid = uid_match.group(1).decode()
            raw_by_uid[uid] = part[1]
            try:
                msg = email.message_from_bytes(part[1])
                parsed = _parse_message(msg, uid)
                emails.append(parsed)
            except Exception as e:
                print(f"[IMAP] Failed to parse UID {uid}: {e}")
        return emails, raw_by_uid

    # ------------------------------------------------------------------ #
    # Search / filter
    # ------------------------------------------------------------------ #

    def search_uids(
        self,
        from_query: str = None,
        since=None,   # datetime.date – inclusive start
        before=None,  # datetime.date – exclusive end (IMAP BEFORE semantics)
    ) -> list[str]:
        """Return UIDs matching optional FROM, SINCE, BEFORE criteria."""
        parts = []
        if from_query:
            parts.append(f'FROM "{from_query}"')
        if since:
            parts.append(f'SINCE {since.strftime("%d-%b-%Y")}')
        if before:
            parts.append(f'BEFORE {before.strftime("%d-%b-%Y")}')
        criteria = " ".join(parts) if parts else "ALL"
        typ, data = self._conn.uid("SEARCH", None, criteria)
        if typ != "OK" or not data[0]:
            return []
        return data[0].decode().split()

    def get_unique_senders(self, uids: list[str]) -> list[tuple[str, str]]:
        """Return unique (display_name, email) pairs for the given UIDs."""
        if not uids:
            return []
        uid_str = ",".join(uids)
        typ, data = self._conn.uid(
            "FETCH", uid_str, "(BODY.PEEK[HEADER.FIELDS (FROM)])"
        )
        if typ != "OK":
            return []
        seen: dict[str, str] = {}  # email → display_name
        for part in data:
            if not isinstance(part, tuple) or not part[1]:
                continue
            header_text = part[1].decode(errors="replace")
            from_val = _extract_header_value(header_text, "From")
            if not from_val:
                continue
            decoded = _decode_mime_header(from_val)
            m = re.search(r"<([^>]+)>", decoded)
            if m:
                addr = m.group(1).strip().lower()
                name = decoded[: decoded.index("<")].strip().strip('"').strip()
            else:
                addr = decoded.strip().lower()
                name = addr.split("@")[0]
            if addr and addr not in seen:
                seen[addr] = name or addr
        return [(name, addr) for addr, name in seen.items()]

    # ------------------------------------------------------------------ #
    # Deletion (two-phase)
    # ------------------------------------------------------------------ #

    def move_to_trash(self, uids: list[str], trash_folder: str = "Trash") -> list[str]:
        """Copy UIDs to Trash folder. Returns list of successfully moved UIDs."""
        if not uids:
            return []
        moved = []
        for uid in uids:
            typ, _ = self._conn.uid("COPY", uid, trash_folder)
            if typ == "OK":
                moved.append(uid)
            else:
                # Try common Kerio trash folder names
                for folder in ["Deleted Items", "Deleted Messages", "INBOX.Trash"]:
                    typ, _ = self._conn.uid("COPY", uid, folder)
                    if typ == "OK":
                        moved.append(uid)
                        break
                else:
                    print(f"[IMAP] Warning: could not copy UID {uid} to any trash folder")
                    moved.append(uid)  # Proceed anyway (direct delete)
        return moved

    def mark_deleted(self, uids: list[str]):
        """Mark UIDs with \\Deleted flag."""
        if not uids:
            return
        uid_str = ",".join(uids)
        self._conn.uid("STORE", uid_str, "+FLAGS", r"(\Deleted)")

    def expunge(self):
        """Permanently remove messages marked \\Deleted."""
        self._conn.expunge()
        print("[IMAP] Expunged deleted messages")


# ------------------------------------------------------------------ #
# Email parsing helpers
# ------------------------------------------------------------------ #

def _parse_message(msg: email.message.Message, uid: str) -> dict:
    def h(name):
        return _decode_mime_header(msg.get(name, ""))

    date_str = msg.get("Date", "")
    try:
        date = parsedate_to_datetime(date_str)
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
    except Exception:
        date = datetime.now(timezone.utc)

    message_id = _normalize_mid(msg.get("Message-ID", f"<unknown-{uid}>"))
    in_reply_to = _normalize_mid(msg.get("In-Reply-To", ""))
    references = [
        _normalize_mid(r)
        for r in msg.get("References", "").split()
        if r.strip()
    ]

    return {
        "uid": uid,
        "message_id": message_id,
        "in_reply_to": in_reply_to or None,
        "references": [r for r in references if r],
        "subject": h("Subject") or "(no subject)",
        "from": h("From"),
        "to": h("To"),
        "cc": h("Cc"),
        "date": date,
        "date_str": date.isoformat(),
        "body": _extract_body(msg),
    }


def _extract_body(msg: email.message.Message) -> str:
    """Extract plaintext body, falling back to stripped HTML."""
    plain = None
    html = None

    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = part.get("Content-Disposition", "")
            if "attachment" in cd:
                continue
            if ct == "text/plain" and plain is None:
                plain = _decode_payload(part)
            elif ct == "text/html" and html is None:
                html = _decode_payload(part)
    else:
        ct = msg.get_content_type()
        if ct == "text/plain":
            plain = _decode_payload(msg)
        elif ct == "text/html":
            html = _decode_payload(msg)

    if plain:
        return plain.strip()
    if html:
        return _strip_html(html).strip()
    return "(no body)"


def _decode_payload(part: email.message.Message) -> str:
    payload = part.get_payload(decode=True)
    if not payload:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return payload.decode("utf-8", errors="replace")


def _decode_mime_header(value: str) -> str:
    if not value:
        return ""
    parts = []
    for decoded, charset in _decode_header(value):
        if isinstance(decoded, bytes):
            parts.append(decoded.decode(charset or "utf-8", errors="replace"))
        else:
            parts.append(decoded)
    return " ".join(parts)


def _normalize_mid(mid: str) -> str:
    if not mid:
        return ""
    return mid.strip().strip("<>").strip()


def _extract_header_value(header_text: str, name: str) -> str:
    for line in header_text.splitlines():
        if line.lower().startswith(name.lower() + ":"):
            return line.split(":", 1)[1].strip()
    return ""


class _HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts = []

    def handle_data(self, data):
        self._parts.append(data)

    def get_text(self):
        return " ".join(self._parts)


def _strip_html(html: str) -> str:
    stripper = _HTMLStripper()
    stripper.feed(html)
    return stripper.get_text()
