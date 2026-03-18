#!/usr/bin/env python3
"""Marc — conversational CLI for the email archiver.

Type 'marc' to start an interactive session.
"""

import collections
import json
import os
import re
import signal
import sys
import threading
from datetime import date, timedelta
from pathlib import Path

import keyring
import requests

# Make sure project root is importable when invoked as a script
sys.path.insert(0, str(Path(__file__).parent))
import main as archiver
from src.imap_client import ImapClient
from src.person_merger import PersonMerger
from src.state_manager import StateManager

CONFIG_PATH = Path(__file__).parent / "config.yaml"
KEYCHAIN_SERVICE = "email-archiver"


# ------------------------------------------------------------------ #
# Thinking animation
# ------------------------------------------------------------------ #

class _ThinkingIndicator:
    """Animates 'thinking.' → 'thinking..' → 'thinking...' on a loop.

    Usage:
        with _ThinkingIndicator():
            result = slow_call()
    """
    _FRAMES = ("thinking.  ", "thinking.. ", "thinking...")
    _INTERVAL = 0.45

    def __enter__(self):
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_):
        self._stop.set()
        self._thread.join()
        # Erase the animation line
        sys.stdout.write("\r" + " " * len(self._FRAMES[-1]) + "\r")
        sys.stdout.flush()

    def _run(self):
        i = 0
        while True:
            sys.stdout.write("\r" + self._FRAMES[i % len(self._FRAMES)])
            sys.stdout.flush()
            i += 1
            if self._stop.wait(self._INTERVAL):
                break


# ------------------------------------------------------------------ #
# System prompt builder
# ------------------------------------------------------------------ #

def _system_prompt() -> str:
    today = date.today()
    dow = today.weekday()
    this_mon = today - timedelta(days=dow)
    last_mon = this_mon - timedelta(days=7)
    last_sun = this_mon - timedelta(days=1)
    first_this = today.replace(day=1)
    last_month_end = first_this - timedelta(days=1)
    last_month_start = last_month_end.replace(day=1)

    return f"""You are Marc, an intelligent email archiving assistant. You help users manage their Kerio Connect mailbox by archiving emails to an Obsidian vault, maintaining a people directory, and keeping the mailbox clean.

Today is {today.isoformat()}.

── YOUR CAPABILITIES ──────────────────────────────────────────────────────────

ARCHIVING
  Fetches emails from IMAP, runs LLM analysis (summary, tags, category, priority,
  action items), writes thread notes to Obsidian, saves raw .eml backups to
  ~/EmailArchive (with all attachments), then deletes from the server.
  Filters supported:
    · By sender name   — searches IMAP FROM field, shows matched addresses for confirmation
    · By exact email   — archives all emails from that address
    · By date range    — SINCE / UNTIL, accepts natural language or exact dates
    · Combined         — sender + date range together
    · Batch size       — limit how many emails to process in one run

PERSON MERGING
  Scans the Obsidian People/ folder. Each contact note stores a name, email, and
  links to their email threads. The same person can appear under multiple notes
  (different work/personal emails, name abbreviations, etc.).
  Merge flow: LLM compares names + emails + thread titles → groups duplicates →
  user confirms each group → secondary note's threads & email are folded into the
  primary → all [[wikilinks]] in email notes are updated → secondary file deleted.
  Can scan all persons or filter by a specific name.

DELETING EMAILS
  Removes emails from the server to free space. Always saves a raw .eml backup
  first. Before deleting, Marc checks whether the emails are already archived in
  Obsidian. For any that are not: the user can choose to archive them first, or
  delete directly. Supports the same filters as archiving (sender, date, combined).

MAILBOX QUOTA
  Queries the IMAP server for current storage usage as a percentage.

SYNC BACKUPS
  Keeps ~/EmailArchive in sync with the Obsidian vault. If an email note is
  deleted from Obsidian, the corresponding .eml file(s) are removed from the
  local backup folder. Empty month directories are also cleaned up.
  Trigger with: "sync", "sync backups", "clean up deleted notes", etc.


SETUP
  First-time wizard: stores IMAP password in macOS Keychain, verifies the Obsidian
  vault path, tests the Ollama connection. Run once with: marc setup

CONVERSATION
  You can answer questions about how Marc works, what happens to attachments,
  explain the archiving pipeline, help the user decide what to do, and more.
  Speak naturally — Marc understands plain English.

── ACTION FORMAT ───────────────────────────────────────────────────────────────

When the user wants to archive emails, return:
{{
  "action": "archive",
  "sender_name": "<name to search>" or null,
  "sender_email": "<exact email address>" or null,
  "since": "YYYY-MM-DD" or null,
  "until": "YYYY-MM-DD" or null,
  "batch_size": <integer> or null,
  "message": "<friendly confirmation of what you understood>"
}}

When the user wants to merge duplicate person/contact notes, return:
{{
  "action": "merge_persons",
  "name_filter": "<specific name to look for>" or null,
  "message": "<friendly confirmation>"
}}
Use name_filter when the user mentions a specific person (e.g. "merge Anurag Bajpai").
Leave name_filter null for "find all duplicates" / "scan for duplicates".

When the user wants to delete emails from the server, return:
{{
  "action": "delete_emails",
  "sender_name": "<name to search>" or null,
  "sender_email": "<exact email address>" or null,
  "since": "YYYY-MM-DD" or null,
  "until": "YYYY-MM-DD" or null,
  "message": "<friendly confirmation of what you understood>"
}}
Deletion always saves a local .eml backup first. Marc checks whether emails are
already archived and asks what to do with any that are not.

For quota check:       {{"action": "quota",        "message": "..."}}
For sync backups:      {{"action": "sync",          "message": "..."}}
For help / manual:     {{"action": "help",          "message": "..."}}
For general chat / Q&A:{{"action": "chat",          "message": "<your full response>"}}

Use "chat" for questions about how Marc works, what features exist, pipeline
explanations, or anything that doesn't trigger a concrete action.
Be helpful, accurate, and concise in chat responses.

── DATE REFERENCE ──────────────────────────────────────────────────────────────

Use these exact resolved values:
- "last week"    → since: {last_mon}, until: {last_sun}
- "this week"    → since: {this_mon}, until: {today}
- "last month"   → since: {last_month_start}, until: {last_month_end}
- "January 2026" → since: 2026-01-01, until: 2026-01-31
- "Q1 2026"      → since: 2026-01-01, until: 2026-03-31
- "01.01.2026 to 14.01.2026" → since: 2026-01-01, until: 2026-01-14
- Single date like "01.01.2026" → since: that date, until: null

If the user gives both sender and date, include both fields.

Return ONLY valid JSON — no markdown, no extra text."""


# ------------------------------------------------------------------ #
# CLI class
# ------------------------------------------------------------------ #

class MarcCLI:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.ollama = cfg["ollama"]
        self.history: list[dict] = []    # conversation history for LLM
        self.pending: dict | None = None  # pending confirmation state

    # ── LLM ──────────────────────────────────────────────────────────

    def _chat(self, user_msg: str) -> dict:
        """Send message to Ollama, streaming <think> content live in grey. Returns parsed intent dict."""
        self.history.append({"role": "user", "content": user_msg})
        _timeout = (10, self.ollama.get("timeout_seconds", 180))
        try:
            resp = requests.post(
                f"{self.ollama['host']}/api/chat",
                json={
                    "model": self.ollama["model"],
                    "messages": [
                        {"role": "system", "content": _system_prompt()},
                        *self.history,
                    ],
                    "stream": True,
                    "keep_alive": -1,
                    "think": True,
                    "options": {"temperature": 0.1},
                },
                timeout=_timeout,
                stream=True,
            )
            raw = _stream_with_thinking(resp)
        except Exception as e:
            return {"action": "chat",
                    "message": f"(Ollama unavailable: {e})\nIs Ollama running? Try: ollama serve"}

        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
        m = re.search(r"\{[\s\S]+\}", raw)
        if m:
            raw = m.group(0)
        try:
            intent = json.loads(raw)
        except json.JSONDecodeError:
            intent = {"action": "chat", "message": raw}

        msg = intent.get("message", "")
        if msg:
            self.history.append({"role": "assistant", "content": msg})
        return intent

    def _ollama_task(self, prompt: str) -> dict:
        """One-shot Ollama call for structured tasks (no conversation history)."""
        _timeout = (10, self.ollama.get("timeout_seconds", 180))
        payload = {
            "model": self.ollama["model"],
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "keep_alive": -1,
            "options": {"temperature": 0.1},
        }
        for attempt in (1, 2):
            try:
                resp = requests.post(
                    f"{self.ollama['host']}/api/chat",
                    json=payload,
                    timeout=_timeout,
                )
                raw = resp.json()["message"]["content"]
                break
            except requests.exceptions.Timeout:
                if attempt == 1:
                    print(f"  (Ollama timed out, retrying...)")
                    continue
                print(f"  (Ollama timed out again — skipping LLM step)")
                return {}
            except Exception as e:
                print(f"  (Ollama error: {e})")
                return {}

        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        m = re.search(r"\{[\s\S]+\}", raw)
        if not m:
            return {}
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return {}

    def _warmup(self):
        """Pre-load the model into Ollama's memory (best-effort, runs in background)."""
        try:
            requests.post(
                f"{self.ollama['host']}/api/generate",
                json={
                    "model": self.ollama["model"],
                    "prompt": "",
                    "keep_alive": -1,
                    "options": {"num_predict": 0},
                },
                timeout=120,
            )
        except Exception:
            pass

    def _unload_ollama(self):
        """Release the model from Ollama's memory on exit (best-effort)."""
        try:
            requests.post(
                f"{self.ollama['host']}/api/generate",
                json={
                    "model": self.ollama["model"],
                    "prompt": "",
                    "keep_alive": 0,
                },
                timeout=5,
            )
        except Exception:
            pass

    # ── IMAP helpers ─────────────────────────────────────────────────

    def _password(self) -> str | None:
        pw = keyring.get_password(KEYCHAIN_SERVICE, self.cfg["imap"]["username"])
        if not pw:
            print("No IMAP password found. Run: marc setup")
        return pw

    def _imap(self, password: str) -> ImapClient:
        client = ImapClient(self.cfg["imap"])
        client.connect(password)
        return client

    def _find_senders(self, name: str, since=None, before=None) -> list[tuple[str, str]]:
        """Search IMAP for senders whose FROM field matches *name*."""
        pw = self._password()
        if not pw:
            return []
        print(f"  Searching for '{name}'...")
        client = self._imap(pw)

        uids = client.search_uids(from_query=name, since=since, before=before)
        if not uids:
            # Fallback: intersect results for individual words
            words = name.split()
            if len(words) > 1:
                sets = [
                    set(client.search_uids(from_query=w, since=since, before=before))
                    for w in words
                ]
                combined = sets[0]
                for s in sets[1:]:
                    combined &= s
                uids = list(combined)

        senders = client.get_unique_senders(uids) if uids else []
        client.disconnect()

        # Local filter: at least one name word must appear in display name or email
        words_lower = [w.lower() for w in name.split()]
        return [
            (n, e) for n, e in senders
            if any(w in n.lower() or w in e.lower() for w in words_lower)
        ]

    def _get_uids(self, sender_email: str = None, since=None, before=None) -> list[str]:
        """Fetch UIDs matching the given criteria."""
        pw = self._password()
        if not pw:
            return []
        client = self._imap(pw)
        uids = client.search_uids(from_query=sender_email, since=since, before=before)
        client.disconnect()
        return uids

    # ── Date helpers ─────────────────────────────────────────────────

    @staticmethod
    def _to_date(s: str | None) -> date | None:
        if not s:
            return None
        try:
            return date.fromisoformat(s)
        except ValueError:
            return None

    @staticmethod
    def _fmt_range(since: date | None, until: date | None) -> str:
        if since and until:
            return f" from {since} to {until}"
        if since:
            return f" since {since}"
        if until:
            return f" until {until}"
        return ""

    # ── Delete helpers ────────────────────────────────────────────────

    def _check_archive_status(self, uids: list[str]) -> tuple[list[str], list[str]]:
        """Return (archived_uids, unarchived_uids) by checking state.db.

        Fetches Message-IDs via IMAP and cross-references with the local DB.
        """
        pw = self._password()
        if not pw:
            return [], uids
        client = self._imap(pw)
        uid_to_mid = client.fetch_message_ids_for_uids(uids)
        client.disconnect()

        state = StateManager(self.cfg["state"]["db_path"])
        processed = state.get_all_processed_ids()
        state.close()

        archived   = [u for u in uids if uid_to_mid.get(u, "") in processed]
        unarchived = [u for u in uids if uid_to_mid.get(u, "") not in processed]
        return archived, unarchived

    def _delete_emails(self, intent: dict) -> str:
        sender_name  = intent.get("sender_name")
        sender_email = intent.get("sender_email")
        since  = self._to_date(intent.get("since"))
        until  = self._to_date(intent.get("until"))
        before = (until + timedelta(days=1)) if until else None

        # ── Resolve sender name ───────────────────────────────────────
        if sender_name and not sender_email:
            senders = self._find_senders(sender_name, since=since, before=before)
            if not senders:
                return (
                    f"No emails found from '{sender_name}'. "
                    "Could you try a different name or provide the exact email address?"
                )
            if len(senders) == 1:
                _, sender_email = senders[0]
            else:
                lines = [f"Found {len(senders)} addresses for '{sender_name}':"]
                for i, (n, e) in enumerate(senders, 1):
                    lines.append(f"  {i}. {n} <{e}>")
                lines.append("\nWhich should I delete? Enter number(s), 'all', or 'cancel'.")
                self.pending = {
                    "type": "delete_select",
                    "senders": senders,
                    "since": since,
                    "until": until,
                    "before": before,
                }
                return "\n".join(lines)

        # ── Find UIDs ─────────────────────────────────────────────────
        uids = self._get_uids(sender_email=sender_email, since=since, before=before)
        if not uids:
            criterion = sender_email or (self._fmt_range(since, until).strip()) or "those criteria"
            return (
                f"No emails found for {criterion}. "
                "Try a different name, address, or date range."
            )

        return self._build_delete_pending(uids, since, until, sender_email)

    def _build_delete_pending(
        self,
        uids: list[str],
        since,
        until,
        sender_email: str | None,
    ) -> str:
        """Check archive status, set pending state, return summary message."""
        print(f"  Checking archive status for {len(uids)} email(s)...")
        archived, unarchived = self._check_archive_status(uids)

        date_range = self._fmt_range(since, until)
        desc = f"{len(uids)} email(s)"
        if sender_email:
            desc += f" from {sender_email}"
        desc += date_range

        self.pending = {
            "type": "delete_confirm",
            "stage": "choose" if unarchived else "confirm",
            "all_uids": uids,
            "archived_uids": archived,
            "unarchived_uids": unarchived,
            "archive_first": None,
        }

        if not unarchived:
            return (
                f"Found {desc}.\n"
                f"All {len(archived)} are already archived in Obsidian.\n\n"
                f"Delete them from the server? (y/n)"
            )
        elif not archived:
            return (
                f"Found {desc}.\n"
                f"None are archived yet.\n\n"
                f"  'archive' — archive all to Obsidian first, then delete\n"
                f"  'delete'  — delete directly (local .eml backup still saved)\n"
                f"  'cancel'  — do nothing"
            )
        else:
            return (
                f"Found {desc}.\n"
                f"  Already archived : {len(archived)}\n"
                f"  Not yet archived : {len(unarchived)}\n\n"
                f"What should I do with the {len(unarchived)} unarchived email(s)?\n"
                f"  'archive' — archive them to Obsidian first, then delete all\n"
                f"  'delete'  — delete all directly (local .eml backup still saved)\n"
                f"  'cancel'  — do nothing"
            )

    # ── Merge helpers ─────────────────────────────────────────────────

    def _find_duplicate_groups(self, persons: list[dict]) -> list[dict]:
        """Identify duplicate-person groups using rule-based + LLM passes."""
        if not persons:
            return []

        groups: list[dict] = []
        ungrouped_indices: set[int] = set(range(len(persons)))

        # ── Rule-based pass: collision-numbered names ──────────────────
        # obsidian_writer adds "(2)", "(3)" suffixes when the same person
        # gets multiple notes. These are guaranteed duplicates.
        norm_map: dict[str, list[int]] = {}
        for i, p in enumerate(persons):
            norm = re.sub(r"\s*\(\d+\)\s*$", "", p["name"]).strip().lower()
            norm_map.setdefault(norm, []).append(i)

        for indices in norm_map.values():
            if len(indices) < 2:
                continue
            # Keep the one without a suffix (lowest index) as primary
            primary_idx = min(indices, key=lambda i: (
                bool(re.search(r"\s*\(\d+\)\s*$", persons[i]["name"])), i
            ))
            secondaries = [persons[i] for i in indices if i != primary_idx]
            groups.append({
                "primary": persons[primary_idx],
                "secondaries": secondaries,
                "confidence": "high",
                "reason": "Same name (collision-numbered duplicates from Obsidian)",
            })
            ungrouped_indices -= set(indices)

        # ── LLM pass: harder cases among remaining persons ─────────────
        remaining = [persons[i] for i in sorted(ungrouped_indices)]
        if len(remaining) < 2:
            return groups

        lines = []
        for i, p in enumerate(remaining):
            line = f'[{i}] "{p["name"]}" <{p["email"]}> — {p["thread_count"]} thread(s)'
            freq = p.get("frequent_contacts", [])[:3]
            if freq:
                line += f' | frequent contacts: {", ".join(freq)}'
            lines.append(line)
        prompt = (
            "You are analyzing a contact list from an email archive. "
            "Find groups of entries that are likely THE SAME person. "
            "Consider: different email addresses for the same person, name variations, "
            "initials (e.g. 'F Kark' and 'Florian Kark' are the same person), "
            "abbreviations, middle names omitted, nicknames, and typos. "
            "Use frequent contacts as an additional signal: two people who share the same "
            "frequent contacts are more likely to be the same person.\n\n"
            "Return ONLY valid JSON:\n"
            '{"groups": [{"indices": [0, 3], "primary": 0, '
            '"confidence": "high|medium|low", "reason": "brief reason"}]}\n\n'
            '- "indices": indices of entries that are the same person\n'
            '- "primary": index of the canonical entry to keep\n'
            '- "confidence": how sure you are\n'
            "Return an empty groups list if no duplicates found.\n\n"
            "Contact list:\n" + "\n".join(lines)
        )

        data = self._ollama_task(prompt)
        for g in data.get("groups", []):
            indices = g.get("indices", [])
            primary_idx = g.get("primary", indices[0] if indices else 0)
            if len(indices) < 2 or primary_idx not in indices:
                continue
            try:
                primary_person = remaining[primary_idx]
                secondaries = [remaining[i] for i in indices if i != primary_idx]
            except IndexError:
                continue
            groups.append({
                "primary": primary_person,
                "secondaries": secondaries,
                "confidence": g.get("confidence", "medium"),
                "reason": g.get("reason", ""),
            })
        return groups

    # ── Intent dispatcher ─────────────────────────────────────────────

    def handle(self, user_input: str) -> str:
        if self.pending:
            return self._confirm(user_input)

        intent = self._chat(user_input)
        action = intent.get("action", "chat")

        if action == "archive":
            return self._archive(intent)
        if action == "delete_emails":
            return self._delete_emails(intent)
        if action == "merge_persons":
            return self._merge_persons(intent)
        if action == "quota":
            return self._quota()
        if action == "sync":
            return self._sync()
        if action == "help":
            return intent.get("message") or self._help()
        return intent.get("message") or "How can I help you?"

    # ── Archive flow ──────────────────────────────────────────────────

    def _archive(self, intent: dict) -> str:
        sender_name = intent.get("sender_name")
        sender_email = intent.get("sender_email")
        since = self._to_date(intent.get("since"))
        until = self._to_date(intent.get("until"))
        # IMAP BEFORE is exclusive → add 1 day to *until*
        before = (until + timedelta(days=1)) if until else None

        if sender_name and not sender_email:
            senders = self._find_senders(sender_name, since=since, before=before)
            if not senders:
                return f"No emails found from '{sender_name}' in your mailbox."

            if len(senders) == 1:
                name, addr = senders[0]
                uids = self._get_uids(sender_email=addr, since=since, before=before)
                date_range = self._fmt_range(since, until)
                self.pending = {
                    "type": "confirm",
                    "uids": uids,
                    "desc": f"{len(uids)} email(s) from {name} <{addr}>{date_range}",
                }
                return (
                    f"Found 1 address for '{sender_name}':\n"
                    f"  {name} <{addr}>\n\n"
                    f"Found {len(uids)} email(s){date_range}. Archive them? (y/n)"
                )
            else:
                lines = [f"Found {len(senders)} addresses for '{sender_name}':"]
                for i, (n, e) in enumerate(senders, 1):
                    lines.append(f"  {i}. {n} <{e}>")
                lines.append(
                    "\nWhich should I archive? "
                    "Enter number(s) (e.g. '1', '1 2', 'all') or 'cancel'."
                )
                self.pending = {
                    "type": "select",
                    "senders": senders,
                    "since": since,
                    "until": until,
                    "before": before,
                }
                return "\n".join(lines)

        # Exact email or date-only
        uids = self._get_uids(sender_email=sender_email, since=since, before=before)
        if not uids:
            criterion = sender_email or "the specified criteria"
            return f"No emails found for {criterion}."

        date_range = self._fmt_range(since, until)
        desc = f"{len(uids)} email(s)"
        if sender_email:
            desc += f" from {sender_email}"
        desc += date_range

        self.pending = {"type": "confirm", "uids": uids, "desc": desc}
        return f"Found {desc}. Archive them? (y/n)"

    # ── Merge flow ────────────────────────────────────────────────────

    def _merge_persons(self, intent: dict) -> str:
        name_filter = intent.get("name_filter")
        merger = PersonMerger(self.cfg["obsidian"])

        if name_filter:
            print(f"  Scanning for persons matching '{name_filter}'...")
        else:
            print("  Scanning People folder...")

        persons = merger.scan_persons(name_filter=name_filter)

        if not persons:
            return (f"No person notes found matching '{name_filter}'."
                    if name_filter else "No person notes found in your vault.")

        candidates = merger.candidate_persons(persons) if not name_filter else persons

        if not candidates:
            return (
                f"Scanned {len(persons)} person(s) — no duplicate candidates found "
                "(no shared name words between any two entries)."
            )

        print(f"  Checking {len(candidates)} candidate(s) for duplicates with LLM...")
        groups = self._find_duplicate_groups(candidates)

        if not groups:
            return (
                f"Scanned {len(candidates)} candidate person(s) — "
                "the LLM found no likely duplicates."
            )

        self.pending = {"type": "merge_interactive", "queue": groups, "merger": merger}
        return f"Found {len(groups)} duplicate group(s)."

    def run_merge_interactive(self):
        """Per-group interactive merge. Runs after ThinkingIndicator exits.

        For each detected group:
          1. Show all members as a checkbox list — uncheck false positives.
          2. Pick the primary from the remaining members.
          3. Merge.
        """
        queue = self.pending["queue"]
        merger = self.pending["merger"]
        self.pending = None
        merged = 0

        def _searcher(query: str, current_persons: list[dict]) -> list[dict]:
            """Find persons matching query that are not already in current_persons."""
            existing = {p["email"].lower() for p in current_persons}
            q = query.lower().strip()
            return [
                p for p in merger.scan_persons()
                if p["email"].lower() not in existing
                and (q in p["name"].lower() or q in p["email"].lower())
            ]

        for i, group in enumerate(queue, 1):
            all_persons = [group["primary"]] + group["secondaries"]
            tag = {"high": "HIGH", "medium": "MED", "low": "LOW"}.get(
                group["confidence"], group["confidence"].upper()
            )
            header = (
                f"\nGroup {i}/{len(queue)} [{tag}]: {group['reason']}"
                if len(queue) > 1
                else f"\n[{tag}]: {group['reason']}"
            )
            print(header)
            print("Uncheck false positives or press 'a' to add a missing person, then Enter:\n")

            members = _pick_members(all_persons, searcher=_searcher)
            if len(members) < 2:
                print("  Skipped (fewer than 2 selected).")
                continue

            print("\nSelect the primary entry:\n")
            primary_idx = _pick_primary(members)
            if primary_idx is None:
                print("  Skipped.")
                continue

            primary = members[primary_idx]
            secondaries = [p for j, p in enumerate(members) if j != primary_idx]
            print(f'\n  Merging into "{primary["name"]}" <{primary["email"]}>')
            for secondary in secondaries:
                try:
                    updated = merger.merge(primary, secondary)
                    print(f'  ✓ "{secondary["name"]}" merged ({updated} note(s) updated)')
                except Exception as e:
                    print(f'  ✗ "{secondary["name"]}" failed: {e}')
            merged += 1

        blank = merger.cleanup_blank_notes()
        suffix = f"  Removed {blank} blank file(s)." if blank else ""
        print(f"\nMarc: Done. Merged {merged} of {len(queue)} group(s).{suffix}\n")

    # ── Confirmation state machine ────────────────────────────────────

    def _confirm(self, user_input: str) -> str:
        ptype = self.pending["type"]
        inp = user_input.strip().lower()

        # ── Archive: y/n ──────────────────────────────────────────────
        if ptype == "confirm":
            if inp in ("y", "yes"):
                uids, desc = self.pending["uids"], self.pending["desc"]
                self.pending = None
                print(f"\nArchiving {desc}...")
                archiver.run_filtered(self.cfg, uids)
                return "Done!"
            if inp in ("n", "no", "cancel"):
                self.pending = None
                return "Cancelled."
            return f"Please answer 'y' or 'n'. Archive {self.pending['desc']}?"

        # ── Sender selection ──────────────────────────────────────────
        if ptype == "select":
            senders = self.pending["senders"]
            since, until, before = self.pending["since"], self.pending["until"], self.pending["before"]

            if inp in ("cancel", "none", "n", "no"):
                self.pending = None
                return "Cancelled."

            indices = _parse_indices(inp, len(senders))
            if indices is None:
                return (
                    f"Didn't understand '{user_input}'. "
                    "Enter number(s) like '1', '1 2', 'all', or 'cancel'."
                )

            all_uids: list[str] = []
            names: list[str] = []
            for idx in indices:
                name, addr = senders[idx]
                all_uids.extend(self._get_uids(sender_email=addr, since=since, before=before))
                names.append(f"{name} <{addr}>")

            all_uids = list(dict.fromkeys(all_uids))
            date_range = self._fmt_range(since, until)
            desc = f"{len(all_uids)} email(s) from {', '.join(names)}{date_range}"
            self.pending = {"type": "confirm", "uids": all_uids, "desc": desc}
            return f"Found {desc}. Archive them? (y/n)"

        # ── Delete: resolve multiple senders ─────────────────────────
        if ptype == "delete_select":
            senders = self.pending["senders"]
            since, until, before = self.pending["since"], self.pending["until"], self.pending["before"]

            if inp in ("cancel", "none", "n", "no"):
                self.pending = None
                return "Cancelled."

            indices = _parse_indices(inp, len(senders))
            if indices is None:
                return "Enter number(s) like '1', '1 2', 'all', or 'cancel'."

            all_uids: list[str] = []
            addrs: list[str] = []
            for idx in indices:
                _, addr = senders[idx]
                all_uids.extend(self._get_uids(sender_email=addr, since=since, before=before))
                addrs.append(addr)
            all_uids = list(dict.fromkeys(all_uids))
            self.pending = None
            return self._build_delete_pending(all_uids, since, until, ", ".join(addrs))

        # ── Delete: archive-or-not choice + final confirm ─────────────
        if ptype == "delete_confirm":
            stage        = self.pending["stage"]
            all_uids     = self.pending["all_uids"]
            unarchived   = self.pending["unarchived_uids"]
            archive_first = self.pending["archive_first"]

            if inp in ("cancel",) or (inp in ("n", "no") and stage == "choose"):
                self.pending = None
                return "Cancelled."

            if stage == "choose":
                if inp == "archive":
                    self.pending["archive_first"] = True
                    self.pending["stage"] = "confirm"
                    return (
                        f"Archive {len(unarchived)} email(s) to Obsidian first, "
                        f"then delete all {len(all_uids)} from the server? (y/n/cancel)"
                    )
                if inp in ("delete", "d"):
                    self.pending["archive_first"] = False
                    self.pending["stage"] = "confirm"
                    return (
                        f"Delete all {len(all_uids)} email(s) directly "
                        f"({len(unarchived)} without archiving)? (y/n/cancel)"
                    )
                return "Please type 'archive', 'delete', or 'cancel'."

            # stage == "confirm"
            if inp in ("n", "no"):
                self.pending = None
                return "Cancelled."

            if inp in ("y", "yes"):
                archive_first = self.pending["archive_first"]
                archived_uids = self.pending["archived_uids"]
                unarchived    = self.pending["unarchived_uids"]
                self.pending  = None

                if archive_first and unarchived:
                    print(f"\nArchiving {len(unarchived)} email(s) to Obsidian...")
                    archiver.run_filtered(self.cfg, unarchived)

                if not archive_first and unarchived:
                    # Skip archiving — delete everything directly
                    print(f"\nDeleting {len(all_uids)} email(s) from server...")
                    archiver.delete_from_server(self.cfg, all_uids)
                else:
                    # Unarchived were archived+deleted by run_filtered; delete already-archived separately
                    if archived_uids:
                        print(f"\nDeleting {len(archived_uids)} already-archived email(s) from server...")
                        archiver.delete_from_server(self.cfg, archived_uids)
                return "Done!"

            return "Please answer 'y', 'n', or 'cancel'."

        self.pending = None
        return "Something went wrong. Please try again."

    # ── Quota, sync & help ────────────────────────────────────────────

    def _quota(self) -> str:
        pw = self._password()
        if not pw:
            return "No IMAP password. Run: marc setup"
        client = self._imap(pw)
        pct = client.get_quota_percent()
        client.disconnect()
        if pct is not None:
            return f"Mailbox usage: {pct:.1f}%"
        return "Quota information not available from this server."

    def _sync(self) -> str:
        notes_cleaned, emls_deleted = archiver.sync_backups(self.cfg)
        if notes_cleaned == 0:
            return "Everything is in sync — no orphaned .eml files found."
        return (
            f"Sync complete. Removed {emls_deleted} .eml file(s) "
            f"for {notes_cleaned} deleted Obsidian note(s)."
        )


    @staticmethod
    def _help() -> str:
        return """\
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  MARC — Email Archiver  ·  Command Reference
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ARCHIVING
  Fetches emails from your mailbox, analyses each thread with an
  LLM (summary, tags, category, priority, action items), writes a
  Markdown note to your Obsidian vault, saves a local .eml backup,
  then deletes the email from the server.

  By sender name  (Marc looks up their address and asks you to confirm):
    "Archive emails from Anurag Bajpai"
    "Archive all mails from John"
    "Archive everything from the IT department"

  By exact email address:
    "Archive emails from anurag@institute.de"
    "Archive everything from newsletter@service.com"

  By date range  (plain language or exact dates both work):
    "Archive emails from last week"
    "Archive emails from last month"
    "Archive emails from January 2026"
    "Archive emails from Q1 2026"
    "Archive emails from 01.01.2026 to 14.01.2026"
    "Archive emails since 2026-03-01"

  Sender + date combined:
    "Archive emails from Anurag Bajpai last month"
    "Archive emails from john@example.com from 01.01.2026 to 14.01.2026"
    "Archive emails from the newsletter since January"

  Confirmation flow:
    · When searching by name, Marc shows all matched addresses.
      You pick which one(s) to archive: '1', '1 2', 'all', or 'cancel'.
    · Marc always shows the email count before doing anything.
      Confirm with 'y' or cancel with 'n'.

LOCAL BACKUP  (automatic — no action needed)
  Before archiving and deleting, every email is saved as a raw
  .eml file to:
    ~/EmailArchive/YYYY-MM/YYYY-MM-DD Subject (uid).eml

  The .eml format is self-contained: body + all attachments are
  embedded. Open with Mail.app, Thunderbird, or any email client.
  The backup path can be changed via backup.local_path in config.yaml.

PERSON MERGING
  The same person can appear in multiple People/ notes if they
  sent emails from different addresses, or if their name was
  formatted differently. Marc uses the LLM to detect duplicates
  by comparing names, emails, and shared thread titles.

  Scan your entire People folder for duplicates:
    "Find duplicate persons"
    "Are there any duplicate contacts?"
    "Scan for duplicate people"

  Target a specific person:
    "Merge Anurag Bajpai"
    "Check if John Smith has duplicate entries"
    "Are there multiple notes for Sarah?"

  Merge flow:
    1. Marc shows duplicate groups with confidence level and reason.
    2. You select which groups to process: '1', '1 2', 'all', 'none'.
    3. For each group, Marc shows who would be kept (primary) and
       who would be merged in (secondary).
         · 'y'    — confirm merge
         · 'n'    — skip this group
         · 'swap' — swap which entry is kept as the primary note
    4. On merge:
         · Secondary's email address is added as an alias on primary
         · Secondary's thread links are appended to primary (no duplicates)
         · All [[wikilinks]] in email thread notes are updated to the
           primary name
         · The secondary note file is deleted

DELETING EMAILS
  Removes emails from the server to free space.
  A local .eml backup is always saved first (see LOCAL BACKUP above).
  Marc checks whether matched emails are already archived before deleting.

  By sender name, email, or date — same syntax as archiving:
    "Delete emails from Anurag Bajpai"
    "Delete emails from newsletter@service.com"
    "Delete emails from last month"
    "Delete emails from 01.01.2026 to 14.01.2026"
    "Delete emails from john@example.com last week"

  Deletion flow:
    1. Marc searches the mailbox and shows how many emails matched.
    2. Marc splits them into already-archived vs not-yet-archived.
    3. If all are already archived → asks 'y/n' to delete.
    4. If some are not archived → you choose:
         'archive' — archive the unarchived ones to Obsidian first,
                     then delete everything from the server
         'delete'  — delete everything directly (local backup still saved,
                     but no Obsidian note will be created)
         'cancel'  — abort
    5. Final 'y/n' confirmation before anything is deleted.

MAILBOX QUOTA
  "Check my quota"
  "How full is my mailbox?"
  "What is the current mailbox usage?"

SYNC BACKUPS
  "sync"
  "sync backups"
  "clean up deleted notes"

  Keeps ~/EmailArchive in sync with your Obsidian vault.
  When you delete an email note from Obsidian, marc has no way to know
  automatically — but running sync will scan the database, find every
  .eml file whose Obsidian note no longer exists, and delete it.
  Empty month folders in ~/EmailArchive are also removed.

SETUP  (run once before first use, outside of marc)
  marc setup  — stores your IMAP password in macOS Keychain,
                verifies the Obsidian vault path, tests Ollama

CONVERSATION
  You can talk to Marc in plain English at any time:
    "What happens to attachments when I archive?"
    "How does the LLM analysis work?"
    "What's the difference between archive and delete?"
    "Walk me through what happens when I say archive"
    "What date formats can I use?"

  Marc knows the full pipeline and can explain any part of it.

SESSION COMMANDS
  help  — show this reference
  quit  — exit Marc  (also: exit, q, bye)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"""

    # ── Main loop ────────────────────────────────────────────────────

    def _run_migrations(self):
        """One-time vault migrations. Runs once per installation via sentinel file."""
        sentinel = Path(__file__).parent / ".vault_migrated_v1"
        if sentinel.exists():
            return
        merger = PersonMerger(self.cfg["obsidian"])
        _, links_replaced = merger.fix_participant_links()
        _, lines_removed = merger.remove_participant_lines()
        msgs = []
        if links_replaced:
            msgs.append(f"fixed {links_replaced} participant link(s)")
        if lines_removed:
            msgs.append(f"removed {lines_removed} redundant participant line(s)")
        if msgs:
            print(f"[Migration] {', '.join(msgs).capitalize()}.")
        sentinel.touch()

    def run(self):
        print("Marc — Email Archiver")
        print("Type 'help' for the full manual, or just tell me what you want to do.")
        print()

        self._run_migrations()

        # Pre-load the model in the background so the first response is fast
        threading.Thread(target=self._warmup, daemon=True).start()

        # Unload the model when the process is killed (Ctrl+C, terminal close, SIGHUP)
        def _on_signal(sig, frame):
            self._unload_ollama()
            sys.exit(0)

        for sig in (signal.SIGTERM, signal.SIGHUP):
            try:
                signal.signal(sig, _on_signal)
            except (OSError, ValueError):
                pass  # SIGHUP not available on all platforms

        try:
            while True:
                try:
                    user_input = input("You: ").strip()
                except (EOFError, KeyboardInterrupt):
                    print("\nGoodbye!")
                    break
                if not user_input:
                    continue
                if user_input.lower() in ("quit", "exit", "q", "bye"):
                    print("Goodbye!")
                    break
                response = self.handle(user_input)
                print(f"\nMarc: {response}\n")
                if self.pending and self.pending.get("type") == "merge_interactive":
                    self.run_merge_interactive()
        finally:
            self._unload_ollama()


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #

_DIM   = "\033[2m"    # dim grey
_RESET = "\033[0m"

_VIEWPORT_HEIGHT = 10  # visible thinking lines between the two separators


class _ThinkingViewport:
    """Fixed 10-line scrolling viewport for streaming <think> content.

    Draws two separator lines with up to _VIEWPORT_HEIGHT content lines
    between them. Each new completed line shifts older lines upward and
    off the top; the partial line-in-progress is always shown at the bottom.
    """

    def __init__(self):
        try:
            self._width = os.get_terminal_size().columns
        except OSError:
            self._width = 80
        self._cw      = self._width - 4   # content width: 2 indent + 2 margin
        self._sep     = "─" * self._width
        self._lines: collections.deque = collections.deque(maxlen=_VIEWPORT_HEIGHT)
        self._partial = ""      # current incomplete line
        self._drawn   = False   # whether the initial block has been printed

    def feed(self, text: str):
        """Push a chunk of text into the viewport and redraw."""
        self._partial += text
        while "\n" in self._partial:
            line, self._partial = self._partial.split("\n", 1)
            self._wrap_into_lines(line)
        self._redraw()

    def _wrap_into_lines(self, line: str):
        """Hard-wrap a completed logical line at content width and append each segment."""
        if not line:
            self._lines.append("")
            return
        while len(line) > self._cw:
            self._lines.append(line[:self._cw])
            line = line[self._cw:]
        self._lines.append(line)

    def _display_lines(self) -> list[str]:
        """Return exactly _VIEWPORT_HEIGHT lines, padded with empty lines at the top."""
        lines = list(self._lines)
        if self._partial:
            # Show the most recent content_width chars of the partial line
            lines = lines + [self._partial[-self._cw:]]
        while len(lines) < _VIEWPORT_HEIGHT:
            lines.insert(0, "")
        return lines[-_VIEWPORT_HEIGHT:]

    def _redraw(self):
        lines = self._display_lines()
        out   = []

        if self._drawn:
            out.append(f"\033[{_VIEWPORT_HEIGHT + 2}A")
        else:
            out.append("\n")   # blank line above the first separator

        out.append(f"\r\033[2K{_DIM}{self._sep}{_RESET}\n")
        for line in lines:
            out.append(f"\r\033[2K{_DIM}  {line}{_RESET}\n")
        out.append(f"\r\033[2K{_DIM}{self._sep}{_RESET}\n")

        sys.stdout.write("".join(out))
        sys.stdout.flush()
        self._drawn = True

    def clear(self):
        """Erase the viewport block from the terminal after thinking finishes."""
        if not self._drawn:
            return
        total = _VIEWPORT_HEIGHT + 2   # content rows + 2 separators
        # Jump to top of the block, clear every line, leave cursor there
        out = [f"\033[{total}A"]
        for _ in range(total):
            out.append("\r\033[2K\n")
        out.append(f"\033[{total}A")
        sys.stdout.write("".join(out))
        sys.stdout.flush()


def _stream_with_thinking(resp) -> str:
    """Consume a streaming Ollama response with thinking support.

    Ollama returns reasoning tokens in chunk["message"]["thinking"] and the
    final answer in chunk["message"]["content"]. Thinking tokens are fed into
    the scrolling viewport; content tokens are accumulated and returned.
    """
    viewport: _ThinkingViewport | None = None
    answer = ""

    for raw_line in resp.iter_lines():
        if not raw_line:
            continue
        try:
            chunk = json.loads(raw_line)
        except Exception:
            continue
        if chunk.get("done"):
            break

        msg             = chunk.get("message") or {}
        thinking_token  = msg.get("thinking") or ""
        content_token   = msg.get("content")  or ""

        if thinking_token:
            if viewport is None:
                viewport = _ThinkingViewport()
            viewport.feed(thinking_token)

        if content_token:
            answer += content_token

    if viewport is not None:
        viewport.clear()

    return answer


def _parse_indices(inp: str, length: int) -> list[int] | None:
    """Parse 'all', '1', '1 2 3' etc. into 0-based index list. Returns None if unrecognised."""
    if inp == "all":
        return list(range(length))
    indices = []
    for token in inp.split():
        try:
            idx = int(token) - 1
            if 0 <= idx < length:
                indices.append(idx)
        except ValueError:
            pass
    return indices if indices else None


# ------------------------------------------------------------------ #
# Interactive primary selector
# ------------------------------------------------------------------ #

def _pick_members(persons: list[dict], searcher=None) -> list[dict]:
    """Checkbox list of persons. All checked by default.

    User unchecks false positives with Space, adds missing persons with 'a',
    and confirms with Enter. Returns the subset the user kept checked.

    searcher: optional callable(query, current_persons) -> list[dict]
    """
    included = [True] * len(persons)
    cursor = 0

    def _hint():
        base = "↑/↓ move · Space toggle · Enter confirm · q skip"
        extra = " · a add person" if searcher else ""
        return f"\r\x1b[K  \x1b[2m{base}{extra}\x1b[0m\n"

    def _render(first=False):
        n = len(persons)
        if not first:
            sys.stdout.write(f"\x1b[{n + 1}A")
        for i, p in enumerate(persons):
            arrow = "\x1b[33m▶\x1b[0m" if i == cursor else " "
            check = "\x1b[32m[✓]\x1b[0m" if included[i] else "\x1b[31m[✗]\x1b[0m"
            dim   = "\x1b[2m" if not included[i] else ""
            line  = (
                f'  {arrow} {check} {dim}"{p["name"]}" <{p["email"]}>'
                f" · {p['thread_count']} thread(s)\x1b[0m"
            )
            sys.stdout.write(f"\r\x1b[K{line}\n")
        sys.stdout.write(_hint())
        sys.stdout.flush()

    def _add_person(fd, old_settings):
        """Temporarily leave raw mode, read a search query, return new persons."""
        import termios
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        sys.stdout.write("\r\x1b[K  Add name or email: ")
        sys.stdout.flush()
        try:
            query = input()
        except EOFError:
            query = ""
        return searcher(query.strip(), persons) if query.strip() else []

    try:
        import tty, termios
        if not sys.stdin.isatty():
            raise AttributeError
        _render(first=True)
        fd  = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            while True:
                ch = sys.stdin.read(1)
                if ch == "\x1b":
                    nxt = sys.stdin.read(1)
                    if nxt == "[":
                        arr = sys.stdin.read(1)
                        if arr == "A":
                            cursor = (cursor - 1) % len(persons)
                            _render()
                        elif arr == "B":
                            cursor = (cursor + 1) % len(persons)
                            _render()
                elif ch == " ":
                    included[cursor] = not included[cursor]
                    _render()
                elif ch in ("a", "A") and searcher:
                    found = _add_person(fd, old)
                    if found:
                        persons.extend(found)
                        included.extend([True] * len(found))
                        sys.stdout.write(
                            f"  \x1b[32mAdded {len(found)} person(s).\x1b[0m\n\n"
                        )
                    else:
                        sys.stdout.write("  \x1b[2mNo matching persons found.\x1b[0m\n\n")
                    sys.stdout.flush()
                    tty.setraw(fd)
                    _render(first=True)
                elif ch in ("\r", "\n"):
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    return [p for i, p in enumerate(persons) if included[i]]
                elif ch in ("q", "Q"):
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    return []
                elif ch == "\x03":
                    raise KeyboardInterrupt
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    except (ImportError, AttributeError, Exception):
        for i, p in enumerate(persons, 1):
            print(f'  [{i}] "{p["name"]}" <{p["email"]}> · {p["thread_count"]} thread(s)')
        while searcher:
            raw = input("  Add a person (name/email, or blank to continue): ").strip()
            if not raw:
                break
            found = searcher(raw, persons)
            if found:
                for p in found:
                    print(f'  + "{p["name"]}" <{p["email"]}> · {p["thread_count"]} thread(s)')
                persons.extend(found)
                included.extend([True] * len(found))
            else:
                print("  No matching persons found.")
        raw = input("  Include which? ('all', numbers like '1 3', or 'none'): ").strip().lower()
        if raw in ("none", "q", ""):
            return []
        if raw == "all":
            return persons
        indices = _parse_indices(raw, len(persons))
        return [persons[i] for i in indices] if indices else persons


def _pick_primary(persons: list[dict]) -> int | None:
    """Arrow-key selector for choosing the primary person in a merge group.

    Renders a numbered list; ↑/↓ move the highlight, Enter confirms,
    n/q skips. Falls back to a plain numbered prompt when stdin is not a tty.

    Returns the selected 0-based index, or None to skip.
    """
    n = len(persons)

    def _render(selected: int, first: bool = False):
        if not first:
            # Move cursor back up to the first candidate line and redraw
            sys.stdout.write(f"\x1b[{n + 1}A")
        for i, p in enumerate(persons):
            arrow = "\x1b[32m▶\x1b[0m" if i == selected else " "
            tag   = "  \x1b[33m← primary\x1b[0m" if i == selected else ""
            line  = (
                f"  {arrow} [{i + 1}] \"{p['name']}\" <{p['email']}>"
                f" · {p['thread_count']} thread(s){tag}"
            )
            sys.stdout.write(f"\r\x1b[K{line}\n")
        sys.stdout.write("\r\x1b[K  \x1b[2m↑/↓ move · Enter confirm · n skip\x1b[0m\n")
        sys.stdout.flush()

    try:
        import tty, termios
        if not sys.stdin.isatty():
            raise AttributeError  # fall through to text fallback
        selected = 0
        _render(selected, first=True)
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            while True:
                ch = sys.stdin.read(1)
                if ch == "\x1b":
                    nxt = sys.stdin.read(1)
                    if nxt == "[":
                        arrow = sys.stdin.read(1)
                        if arrow == "A":          # ↑
                            selected = (selected - 1) % n
                            _render(selected)
                        elif arrow == "B":        # ↓
                            selected = (selected + 1) % n
                            _render(selected)
                elif ch in ("\r", "\n"):
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    return selected
                elif ch in ("n", "N", "q", "Q"):
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    return None
                elif ch == "\x03":               # Ctrl-C
                    raise KeyboardInterrupt
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    except (ImportError, AttributeError, Exception):
        # Plain text fallback for non-tty environments
        for i, p in enumerate(persons, 1):
            print(f"  [{i}] \"{p['name']}\" <{p['email']}> · {p['thread_count']} thread(s)")
        raw = input("  Primary number (or n to skip): ").strip().lower()
        if raw in ("n", "q", ""):
            return None
        try:
            idx = int(raw) - 1
            if 0 <= idx < n:
                return idx
        except ValueError:
            pass
        return None


# ------------------------------------------------------------------ #
# Entry point
# ------------------------------------------------------------------ #

def main():
    if len(sys.argv) > 1 and sys.argv[1] == "setup":
        cfg = archiver.load_config(CONFIG_PATH)
        archiver.run_setup(cfg)
        return

    cfg = archiver.load_config(CONFIG_PATH)
    MarcCLI(cfg).run()


if __name__ == "__main__":
    main()
