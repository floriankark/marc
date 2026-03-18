"""Write email threads as Obsidian markdown notes with YAML frontmatter and wikilinks."""

import re
import yaml
from datetime import datetime
from pathlib import Path
from typing import Optional

from .thread_builder import canonical_subject


class ObsidianWriter:
    def __init__(self, cfg: dict):
        self.vault = Path(cfg["vault_path"])
        self.email_folder = cfg.get("email_folder", "Emails")
        self.people_folder = cfg.get("people_folder", "People")

    def write_thread(self, thread_id: str, emails: list[dict], analysis: dict) -> Path:
        """
        Write or update an Obsidian note for an email thread.
        Returns the path of the written note.
        """
        emails = sorted(emails, key=lambda e: e["date"])
        subject = canonical_subject(emails)
        first_date = emails[0]["date"]

        # Determine folder: Emails/YYYY-MM/
        month_dir = self.vault / self.email_folder / first_date.strftime("%Y-%m")
        month_dir.mkdir(parents=True, exist_ok=True)

        filename = _safe_filename(subject, first_date) + ".md"
        note_path = month_dir / filename

        # Handle filename collision
        if note_path.exists():
            # Check if it's a different thread (by reading its thread_id frontmatter)
            existing_id = _read_frontmatter_field(note_path, "thread_id")
            if existing_id and existing_id != thread_id:
                note_path = _resolve_collision(month_dir, filename)

        content = self._render_note(thread_id, subject, emails, analysis)
        note_path.write_text(content, encoding="utf-8")
        return note_path

    def _render_note(self, thread_id: str, subject: str,
                     emails: list[dict], analysis: dict) -> str:
        emails_sorted = sorted(emails, key=lambda e: e["date"])
        first = emails_sorted[0]
        last = emails_sorted[-1]

        all_participants = _collect_participants(emails_sorted)
        participant_links = [
            _person_link(_person_name(p), self.people_folder) for p in all_participants
        ]

        tags = [f"email/{t}" for t in analysis.get("tags", [])]
        if not tags:
            tags = ["email"]

        frontmatter = {
            "type": "email-thread",
            "subject": subject,
            "date_first": first["date"].strftime("%Y-%m-%d"),
            "date_last": last["date"].strftime("%Y-%m-%d"),
            "participants": participant_links,
            "tags": tags,
            "category": analysis.get("category", "other"),
            "priority": analysis.get("priority", "medium"),
            "language": analysis.get("language", "en"),
            "email_count": len(emails_sorted),
            "thread_id": thread_id,
            "message_ids": [e["message_id"] for e in emails_sorted],
            "archived": datetime.now().strftime("%Y-%m-%d"),
        }

        lines = ["---"]
        lines.append(yaml.dump(frontmatter, allow_unicode=True, default_flow_style=False).rstrip())
        lines.append("---")
        lines.append("")
        lines.append(f"# {subject}")
        lines.append("")

        # Summary block
        summary = analysis.get("summary", "")
        if summary:
            lines.append(f"> {summary}")
            lines.append("")

        # Action items
        action_items = analysis.get("action_items", [])
        if action_items:
            lines.append("**Action Items**")
            for item in action_items:
                lines.append(f"- [ ] {item}")
            lines.append("")

        lines.append("---")
        lines.append("")

        # Individual emails
        for i, em in enumerate(emails_sorted, 1):
            sender_name = _person_name(em["from"])
            date_fmt = em["date"].strftime("%Y-%m-%d %H:%M")
            lines.append(f"## Email {i} — {date_fmt} — {sender_name}")
            lines.append("")
            lines.append(f"**From**: {em['from']}  ")
            lines.append(f"**To**: {em['to']}  ")
            if em.get("cc"):
                lines.append(f"**Cc**: {em['cc']}  ")
            lines.append(f"**Subject**: {em['subject']}")
            lines.append("")
            lines.append(em["body"])
            lines.append("")
            lines.append("---")
            lines.append("")

        lines.append(
            f"*Archived by email-archiver · Thread: `{thread_id}`*"
        )

        return "\n".join(lines)

    def update_thread_note(self, note_path: Path, new_emails: list[dict],
                           analysis: dict) -> Path:
        """
        Append new emails to an existing thread note and update its frontmatter.
        Skips emails whose message_id is already in the note (idempotent).
        Falls back to write_thread if the note is missing.
        """
        if not note_path.exists():
            # Note was moved or deleted — reconstruct with what we have
            thread_id = _read_frontmatter_field(note_path, "thread_id") or new_emails[0]["message_id"]
            return self.write_thread(thread_id, new_emails, analysis)

        text = note_path.read_text(encoding="utf-8")
        fm = _parse_frontmatter_dict(text)
        if fm is None:
            thread_id = new_emails[0]["message_id"]
            return self.write_thread(thread_id, new_emails, analysis)

        # Filter out already-present emails
        existing_mids = set(fm.get("message_ids", []))
        truly_new = [e for e in new_emails if e["message_id"] not in existing_mids]
        if not truly_new:
            return note_path  # Nothing to add

        # Update frontmatter fields
        new_dates = [e["date"] for e in truly_new]
        existing_last = fm.get("date_last", fm.get("date_first", ""))
        new_last = max(e.strftime("%Y-%m-%d") for e in new_dates)
        fm["date_last"] = max(str(existing_last), new_last)
        fm["email_count"] = fm.get("email_count", 0) + len(truly_new)
        fm["message_ids"] = list(existing_mids) + [e["message_id"] for e in truly_new]
        fm["archived"] = datetime.now().strftime("%Y-%m-%d")

        # Merge participants
        new_participants = [
            _person_link(_person_name(p), self.people_folder)
            for p in _collect_participants(truly_new)
        ]
        existing_parts = fm.get("participants", [])
        merged_parts = list(dict.fromkeys(existing_parts + new_participants))
        fm["participants"] = merged_parts

        # Merge tags
        new_tags = [f"email/{t}" for t in analysis.get("tags", [])]
        fm["tags"] = list(dict.fromkeys(fm.get("tags", []) + new_tags))

        # Build new email sections
        start_index = fm.get("email_count", 0) - len(truly_new) + 1
        new_sections = []
        for i, em in enumerate(sorted(truly_new, key=lambda e: e["date"]), start_index):
            sender_name = _person_name(em["from"])
            date_fmt = em["date"].strftime("%Y-%m-%d %H:%M")
            lines = [
                f"## Email {i} — {date_fmt} — {sender_name}",
                "",
                f"**From**: {em['from']}  ",
                f"**To**: {em['to']}  ",
            ]
            if em.get("cc"):
                lines.append(f"**Cc**: {em['cc']}  ")
            lines += [f"**Subject**: {em['subject']}", "", em["body"], "", "---", ""]
            new_sections.append("\n".join(lines))

        # Replace frontmatter and insert new sections before footer
        new_fm_block = "---\n" + yaml.dump(fm, allow_unicode=True, default_flow_style=False).rstrip() + "\n---"
        text = _replace_frontmatter_block(text, new_fm_block)

        footer_marker = "*Archived by email-archiver"
        footer_pos = text.rfind(footer_marker)
        insert_text = "\n" + "\n".join(new_sections) + "\n"
        if footer_pos != -1:
            text = text[:footer_pos].rstrip() + "\n" + insert_text + text[footer_pos:]
        else:
            text = text.rstrip() + "\n" + insert_text

        note_path.write_text(text, encoding="utf-8")
        return note_path

    def note_exists(self, note_path: Path) -> bool:
        return note_path.exists() and note_path.stat().st_size > 0


# ------------------------------------------------------------------ #
# People / contact notes
# ------------------------------------------------------------------ #

class PersonRegistry:
    def __init__(self, cfg: dict):
        self.vault = Path(cfg["vault_path"])
        self.people_folder = cfg.get("people_folder", "People")
        self._email_index: dict[str, Path] = {}  # normalised email -> file path
        self._index_built = False

    def _build_index(self):
        """Scan People/ folder once and map email address → file path."""
        if self._index_built:
            return
        person_dir = self.vault / self.people_folder
        if person_dir.exists():
            for md_file in person_dir.glob("*.md"):
                addr = _read_frontmatter_field(md_file, "email")
                if addr:
                    self._email_index[addr.lower().strip()] = md_file
        self._index_built = True

    def update(self, emails: list[dict], note_path: Path):
        """Create or update People notes for all participants in this thread.

        Each unique email address maps to exactly one file. If a file for
        that address already exists (possibly under a different display name),
        the thread link is appended to it instead of creating a duplicate.
        Also tracks co-occurrence counts (frequent contacts) per person.
        """
        self._build_index()
        participants = _collect_participants(emails)
        note_link = f"[[{note_path.stem}]]"
        person_dir = self.vault / self.people_folder
        person_dir.mkdir(parents=True, exist_ok=True)

        # Build email → display name map for all participants in this thread
        email_to_name: dict[str, str] = {}
        for addr in participants:
            e = _person_email(addr).lower().strip()
            n = _person_name(addr)
            if e and n:
                email_to_name[e] = n
        all_emails = set(email_to_name.keys())

        for addr in participants:
            name = _person_name(addr)
            email_addr = _person_email(addr)
            if not name or name.lower() in ("undisclosed-recipients", ""):
                continue

            key = email_addr.lower().strip() if email_addr else ""
            co_emails = all_emails - {key}

            # ── Existing file for this email? ──────────────────────────
            existing_file = self._email_index.get(key) if key else None
            if existing_file and existing_file.exists():
                _append_thread_link(existing_file, note_link)
                self._update_contact_counts(existing_file, co_emails, email_to_name)
                continue

            # ── New person — check for same-name note first ────────────
            candidate = person_dir / f"{_safe_name(name)}.md"
            if candidate.exists():
                existing_email = _read_frontmatter_field(candidate, "email")
                if existing_email and existing_email.lower().strip() != key:
                    # Same display name, different email → same person, merge in
                    _append_thread_link(candidate, note_link)
                    _add_email_alias(candidate, email_addr)
                    if key:
                        self._email_index[key] = candidate
                    self._update_contact_counts(candidate, co_emails, email_to_name)
                    continue

            _create_person_note(candidate, name, email_addr, note_link)
            if key:
                self._email_index[key] = candidate
            if co_emails:
                self._update_contact_counts(candidate, co_emails, email_to_name)

    def _update_contact_counts(
        self,
        path: Path,
        co_emails: set[str],
        email_to_name: dict[str, str],
    ):
        """Increment co-occurrence counts and refresh frequent_contacts in a person note."""
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            return
        if not text.startswith("---"):
            return
        fm_end = text.find("---", 3)
        if fm_end == -1:
            return
        try:
            fm = yaml.safe_load(text[3:fm_end]) or {}
        except Exception:
            return

        counts: dict[str, int] = dict(fm.get("contact_counts") or {})
        for email in co_emails:
            if email:
                counts[email] = counts.get(email, 0) + 1
        fm["contact_counts"] = counts

        def _resolve(email: str) -> Optional[str]:
            if email in email_to_name:
                return email_to_name[email]
            f = self._email_index.get(email)
            if f and f.exists():
                return _read_frontmatter_field(f, "name")
            return None

        top = sorted(
            ((e, c) for e, c in counts.items() if _resolve(e)),
            key=lambda x: -x[1],
        )[:5]
        fm["frequent_contacts"] = [
            _person_link(_resolve(e), self.people_folder) for e, _ in top
        ]

        new_fm_block = (
            "---\n"
            + yaml.dump(fm, allow_unicode=True, default_flow_style=False).rstrip()
            + "\n---"
        )
        body = text[fm_end + 3:]

        if top:
            section_lines = "\n".join(
                f"- {_person_link(_resolve(e), self.people_folder)}"
                f" ({c} shared thread{'s' if c != 1 else ''})"
                for e, c in top
            )
            new_section = (
                "## Frequent Contacts\n<!-- frequent-contacts -->\n" + section_lines
            )
        else:
            new_section = None

        anchor = "<!-- frequent-contacts -->"
        if anchor in body:
            body = re.sub(
                r"## Frequent Contacts\n<!-- frequent-contacts -->.*?(?=\n## |\Z)",
                new_section or "",
                body,
                flags=re.DOTALL,
            )
        elif new_section:
            # Insert before ## Email Threads, or append
            threads_header = "## Email Threads"
            if threads_header in body:
                body = body.replace(
                    threads_header, new_section + "\n\n" + threads_header, 1
                )
            else:
                body = body.rstrip() + "\n\n" + new_section + "\n"

        path.write_text(new_fm_block + body, encoding="utf-8")


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #

def _collect_participants(emails: list[dict]) -> list[str]:
    """Collect unique participants (From/To/Cc) across the thread."""
    seen = set()
    result = []
    for em in emails:
        for field in ("from", "to", "cc"):
            for addr in _split_addresses(em.get(field, "")):
                key = addr.lower().strip()
                if key and key not in seen:
                    seen.add(key)
                    result.append(addr.strip())
    return result


def _split_addresses(addr_str: str) -> list[str]:
    """Split a comma-separated address field into individual addresses."""
    if not addr_str:
        return []
    # Simple split that handles "Name <email>, Name2 <email2>"
    parts = re.split(r",\s*(?=[^>]*(?:<|$))", addr_str)
    return [p.strip() for p in parts if p.strip()]


def _person_name(addr: str) -> str:
    """Extract display name from 'Name <email>' or just return cleaned email."""
    m = re.match(r"^(.+?)\s*<.+>$", addr.strip())
    if m:
        name = m.group(1).strip().strip('"').strip("'")
        return name if name else addr
    # If just an email address, use the local part capitalized
    m2 = re.match(r"^([^@]+)@", addr.strip())
    if m2:
        return m2.group(1).replace(".", " ").replace("_", " ").title()
    return addr.strip()


def _person_email(addr: str) -> str:
    m = re.search(r"<([^>]+)>", addr)
    if m:
        return m.group(1)
    if "@" in addr:
        return addr.strip()
    return ""


def _safe_filename(subject: str, date: datetime) -> str:
    prefix = date.strftime("%Y-%m-%d")
    clean = re.sub(r'[\\/*?:"<>|#^[\]{}]', "", subject)
    clean = re.sub(r"\s+", " ", clean).strip()
    clean = clean[:60].rstrip()
    return f"{prefix} {clean}" if clean else prefix


def _safe_name(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|#^[\]{}]', "", name).strip()


def _person_link(name: str, people_folder: str) -> str:
    """Return a fully-qualified Obsidian wikilink for a person note.

    Example: _person_link("Florian Kark", "People")
             → '[[People/Florian Kark|Florian Kark]]'
    """
    return f"[[{people_folder}/{name}|{name}]]"


def _resolve_collision(directory: Path, filename: str) -> Path:
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    counter = 2
    while True:
        candidate = directory / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def _parse_frontmatter_dict(text: str) -> Optional[dict]:
    """Parse the YAML frontmatter block into a dict, or None if malformed."""
    if not text.startswith("---"):
        return None
    end = text.find("---", 3)
    if end == -1:
        return None
    try:
        return yaml.safe_load(text[3:end]) or {}
    except Exception:
        return None


def _replace_frontmatter_block(text: str, new_fm_block: str) -> str:
    """Replace the --- ... --- block at the top of a note with new_fm_block."""
    if not text.startswith("---"):
        return new_fm_block + "\n\n" + text
    end = text.find("---", 3)
    if end == -1:
        return new_fm_block + "\n\n" + text
    return new_fm_block + text[end + 3:]


def _read_frontmatter_field(path: Path, field: str) -> Optional[str]:
    try:
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---"):
            return None
        end = text.find("---", 3)
        if end == -1:
            return None
        fm = yaml.safe_load(text[3:end])
        return str(fm.get(field, "")) if fm else None
    except Exception:
        return None


def _create_person_note(path: Path, name: str, email_addr: str, note_link: str):
    content = f"""---
type: person
name: "{name}"
email: "{email_addr}"
tags:
  - person
contact_counts: {{}}
frequent_contacts: []
---

# {name}

**Email**: {email_addr}

## Email Threads
<!-- email-threads -->
- {note_link}
"""
    path.write_text(content, encoding="utf-8")


def _add_email_alias(path: Path, email_addr: str):
    """Add email_addr to the aliases list in a person note's frontmatter."""
    if not email_addr:
        return
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return
    if not text.startswith("---"):
        return
    fm_end = text.find("---", 3)
    if fm_end == -1:
        return
    try:
        fm = yaml.safe_load(text[3:fm_end]) or {}
    except Exception:
        return
    aliases = fm.get("aliases", [])
    if isinstance(aliases, str):
        aliases = [aliases]
    if email_addr not in aliases:
        aliases.append(email_addr)
        fm["aliases"] = aliases
        new_fm = "---\n" + yaml.dump(fm, allow_unicode=True, default_flow_style=False).rstrip() + "\n---"
        path.write_text(new_fm + text[fm_end + 3:], encoding="utf-8")


def _append_thread_link(path: Path, note_link: str):
    text = path.read_text(encoding="utf-8")
    if note_link in text:
        return  # Already linked
    anchor = "<!-- email-threads -->"
    if anchor in text:
        text = text.replace(anchor, f"{anchor}\n- {note_link}")
    else:
        text = text.rstrip() + f"\n\n## Email Threads\n{anchor}\n- {note_link}\n"
    path.write_text(text, encoding="utf-8")
