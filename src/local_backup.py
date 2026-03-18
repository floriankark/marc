"""Save raw RFC822 email bytes as .eml files before server deletion.

The .eml format is a complete snapshot: headers, body, and all attachments
are embedded verbatim. Any email client (Mail.app, Thunderbird, …) can open them.

Directory layout:
    ~/EmailArchive/
        2026-01/
            2026-01-15 Project Meeting (12345).eml
            2026-01-16 Budget Review (12346).eml
        2026-03/
            ...
"""

import re
from datetime import datetime
from pathlib import Path
from typing import Optional


class LocalBackup:
    def __init__(self, root: Path):
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, uid: str, raw_bytes: bytes, date: datetime, subject: str) -> Optional[Path]:
        """Write one .eml file. Returns the saved path, or None if already exists."""
        month_dir = self.root / date.strftime("%Y-%m")
        month_dir.mkdir(exist_ok=True)

        filename = _make_filename(date, subject, uid) + ".eml"
        dest = month_dir / filename

        if dest.exists():
            return dest  # Already backed up (e.g. re-run after partial failure)

        try:
            dest.write_bytes(raw_bytes)
            return dest
        except OSError as e:
            print(f"[Backup] WARNING: could not save {dest.name}: {e}")
            return None

    def save_batch(
        self,
        raw_by_uid: dict[str, bytes],
        emails: list[dict],
    ) -> dict[str, Path]:
        """Save all emails in one batch. Returns {uid: saved_path} for successes."""
        uid_to_email = {e["uid"]: e for e in emails}
        saved: dict[str, Path] = {}

        for uid, raw in raw_by_uid.items():
            em = uid_to_email.get(uid)
            if not em:
                continue
            path = self.save(uid, raw, em["date"], em["subject"])
            if path:
                saved[uid] = path

        return saved


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #

def _make_filename(date: datetime, subject: str, uid: str) -> str:
    """Build a filesystem-safe, human-readable filename stem."""
    date_prefix = date.strftime("%Y-%m-%d")
    clean = re.sub(r'[\\/*?:"<>|#\r\n]', "", subject)
    clean = re.sub(r"\s+", " ", clean).strip()[:60].rstrip()
    uid_suffix = f"({uid})"
    return f"{date_prefix} {clean} {uid_suffix}" if clean else f"{date_prefix} {uid_suffix}"
