"""Person deduplication and merging for Obsidian People notes."""

import re
import yaml
from pathlib import Path
from typing import Optional


class PersonMerger:
    def __init__(self, cfg: dict):
        self.vault = Path(cfg["vault_path"])
        self.people_dir = self.vault / cfg.get("people_folder", "People")
        self.email_dir = self.vault / cfg.get("email_folder", "Emails")

    # ------------------------------------------------------------------ #
    # Scanning
    # ------------------------------------------------------------------ #

    def scan_persons(self, name_filter: str = None) -> list[dict]:
        """Return all person dicts from People/, optionally filtered by name."""
        if not self.people_dir.exists():
            return []
        persons = []
        for md_file in sorted(self.people_dir.glob("*.md")):
            p = _parse_person_note(md_file)
            if p:
                persons.append(p)

        if name_filter:
            words = [w.lower() for w in name_filter.split() if len(w) >= 2]
            persons = [
                p for p in persons
                if any(w in p["name"].lower() for w in words)
            ]
        return persons

    def candidate_persons(self, persons: list[dict]) -> list[dict]:
        """Return the subset of persons involved in at least one name-overlap pair.

        Used as a pre-filter before sending to the LLM, to keep the prompt short.
        """
        involved: set[int] = set()
        for i in range(len(persons)):
            for j in range(i + 1, len(persons)):
                a_words = {w.lower() for w in persons[i]["name"].split() if len(w) >= 3}
                b_words = {w.lower() for w in persons[j]["name"].split() if len(w) >= 3}
                if a_words & b_words:
                    involved.add(i)
                    involved.add(j)
        return [persons[i] for i in sorted(involved)]

    # ------------------------------------------------------------------ #
    # Merging
    # ------------------------------------------------------------------ #

    def merge(self, primary: dict, secondary: dict) -> int:
        """Merge secondary person note into primary.

        - Adds secondary's email as an alias on primary
        - Appends secondary's thread links to primary (deduplicated)
        - Replaces [[secondary name]] with [[primary name]] in all email notes
        - Deletes secondary's file

        Returns the number of email thread notes updated.
        """
        self._update_primary(primary, secondary)
        updated = self._replace_wikilinks(secondary["name"], primary["name"])
        sec_path: Path = secondary["path"]
        if sec_path.exists():
            sec_path.unlink()
        return updated

    def remove_participant_lines(self) -> tuple[int, int]:
        """Strip '**Participants**: ...' lines from all email notes.

        Returns (files_changed, lines_removed).
        """
        if not self.email_dir.exists():
            return 0, 0

        pattern = re.compile(r"^\*\*Participants\*\*:.*\n?", re.MULTILINE)
        files_changed = 0
        lines_removed = 0

        for md_file in self.email_dir.rglob("*.md"):
            try:
                text = md_file.read_text(encoding="utf-8")
            except Exception:
                continue
            new_text, n = pattern.subn("", text)
            if n:
                # Also collapse any double blank lines left behind
                new_text = re.sub(r"\n{3,}", "\n\n", new_text)
                md_file.write_text(new_text, encoding="utf-8")
                files_changed += 1
                lines_removed += n

        return files_changed, lines_removed

    def fix_participant_links(self) -> tuple[int, int]:
        """Rewrite bare [[Name]] participant wikilinks to [[People/Name|Name]].

        Scans every note under Emails/ and replaces any bare [[Name]] link whose
        Name matches a known person note in People/ with the fully-qualified form.
        Returns (files_changed, links_replaced).
        """
        if not self.email_dir.exists():
            return 0, 0

        people_folder = self.people_dir.name

        # Build the set of known person names from People/*.md frontmatter
        known_names: set[str] = set()
        if self.people_dir.exists():
            for md_file in self.people_dir.glob("*.md"):
                p = _parse_person_note(md_file)
                if p:
                    known_names.add(p["name"])

        if not known_names:
            return 0, 0

        # Build a regex that matches bare [[Name]] for any known name,
        # but NOT already-qualified links like [[People/Name|Name]].
        # Escape each name for use in a regex.
        escaped = sorted(
            (re.escape(n) for n in known_names),
            key=len,
            reverse=True,  # longest first to avoid partial matches
        )
        pattern = re.compile(
            r"\[\[(?!" + re.escape(people_folder) + r"/)(" + "|".join(escaped) + r")\]\]"
        )

        files_changed = 0
        links_replaced = 0

        for md_file in self.email_dir.rglob("*.md"):
            try:
                text = md_file.read_text(encoding="utf-8")
            except Exception:
                continue
            if not pattern.search(text):
                continue

            def _repl(m: re.Match) -> str:
                name = m.group(1)
                return f"[[{people_folder}/{name}|{name}]]"

            new_text, n = pattern.subn(_repl, text)
            if n:
                md_file.write_text(new_text, encoding="utf-8")
                files_changed += 1
                links_replaced += n

        return files_changed, links_replaced

    def cleanup_blank_notes(self) -> int:
        """Delete empty or whitespace-only .md files left in People/ and Emails/.

        Returns the number of files deleted.
        """
        deleted = 0
        for folder in (self.people_dir, self.email_dir):
            if not folder.exists():
                continue
            for md_file in folder.rglob("*.md"):
                try:
                    if not md_file.read_text(encoding="utf-8").strip():
                        md_file.unlink()
                        deleted += 1
                except Exception:
                    pass
        return deleted

    def _update_primary(self, primary: dict, secondary: dict):
        path: Path = primary["path"]
        text = path.read_text(encoding="utf-8")

        # ── Update frontmatter ─────────────────────────────────────────
        if text.startswith("---"):
            fm_end = text.find("---", 3)
            if fm_end != -1:
                try:
                    fm = yaml.safe_load(text[3:fm_end]) or {}
                except Exception:
                    fm = {}

                alias_email = secondary.get("email", "")
                if alias_email:
                    aliases = fm.get("aliases", [])
                    if isinstance(aliases, str):
                        aliases = [aliases]
                    if alias_email not in aliases:
                        aliases.append(alias_email)
                    fm["aliases"] = aliases

                # Also record merged name as a name alias
                alias_names = fm.get("name_aliases", [])
                if isinstance(alias_names, str):
                    alias_names = [alias_names]
                sec_name = secondary.get("name", "")
                if sec_name and sec_name != fm.get("name") and sec_name not in alias_names:
                    alias_names.append(sec_name)
                if alias_names:
                    fm["name_aliases"] = alias_names

                new_fm = "---\n" + yaml.dump(fm, allow_unicode=True, default_flow_style=False).rstrip() + "\n---"
                text = new_fm + text[fm_end + 3:]

        # ── Append new thread links ────────────────────────────────────
        existing = set(re.findall(r"\[\[[^\]]+\]\]", text))
        to_add = [
            f"[[{t}]]" for t in secondary.get("threads", [])
            if f"[[{t}]]" not in existing
        ]
        if to_add:
            anchor = "<!-- email-threads -->"
            new_lines = "\n".join(f"- {link}" for link in to_add)
            if anchor in text:
                text = text.replace(anchor, f"{anchor}\n{new_lines}")
            else:
                text = text.rstrip() + "\n" + new_lines + "\n"

        path.write_text(text, encoding="utf-8")

    def _replace_wikilinks(self, old_name: str, new_name: str) -> int:
        """Replace person wikilinks for old_name with new_name in all email thread notes.

        Handles both the legacy bare format [[old_name]] and the current
        fully-qualified format [[People/old_name|old_name]], replacing each
        with [[People/new_name|new_name]].
        """
        if not self.email_dir.exists():
            return 0
        people_folder = self.people_dir.name
        new_link = f"[[{people_folder}/{new_name}|{new_name}]]"
        # Both formats that may appear in existing notes
        old_links = [
            f"[[{people_folder}/{old_name}|{old_name}]]",
            f"[[{old_name}]]",
        ]
        count = 0
        for md_file in self.email_dir.rglob("*.md"):
            try:
                text = md_file.read_text(encoding="utf-8")
            except Exception:
                continue
            if not any(ol in text for ol in old_links):
                continue
            for ol in old_links:
                text = text.replace(ol, new_link)
            md_file.write_text(text, encoding="utf-8")
            count += 1
        return count


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #

def _parse_person_note(path: Path) -> Optional[dict]:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None
    if not text.startswith("---"):
        return None
    fm_end = text.find("---", 3)
    if fm_end == -1:
        return None
    try:
        fm = yaml.safe_load(text[3:fm_end]) or {}
    except Exception:
        return None
    if fm.get("type") != "person":
        return None

    # All wikilinks after frontmatter are assumed to be thread links
    threads = re.findall(r"\[\[([^\]]+)\]\]", text[fm_end + 3:])

    return {
        "path": path,
        "name": str(fm.get("name", path.stem)),
        "email": str(fm.get("email", "")),
        "aliases": fm.get("aliases", []),
        "threads": threads,
        "thread_count": len(threads),
        "frequent_contacts": fm.get("frequent_contacts", []),
    }
