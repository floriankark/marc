#!/usr/bin/env python3
"""
Email Archiver — main orchestrator.

Usage:
  python main.py --setup            # First-time setup (store password in Keychain)
  python main.py --quota-check      # Just print current mailbox quota
  python main.py                    # Full run
"""

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml

# Graceful imports with clear error messages
try:
    import keyring
except ImportError:
    print("Missing dependency: pip install keyring")
    sys.exit(1)

try:
    import requests
except ImportError:
    print("Missing dependency: pip install requests")
    sys.exit(1)

from src.imap_client import ImapClient
from src.thread_builder import build_threads, resolve_existing_threads
from src.ollama_client import OllamaClient
from src.obsidian_writer import ObsidianWriter, PersonRegistry
from src.state_manager import StateManager
from src.local_backup import LocalBackup

CONFIG_PATH = Path(__file__).parent / "config.yaml"
KEYCHAIN_SERVICE = "email-archiver"
AUDIT_LOG = Path(__file__).parent / "processed.jsonl"
DEFAULT_BACKUP_PATH = Path("~/EmailArchive")


def _make_backup(cfg: dict) -> LocalBackup:
    backup_cfg = cfg.get("backup", {})
    path = Path(backup_cfg.get("local_path", DEFAULT_BACKUP_PATH))
    return LocalBackup(path)


# ------------------------------------------------------------------ #
# Config
# ------------------------------------------------------------------ #

def load_config(path: Path) -> dict:
    if not path.exists():
        print(f"Config not found: {path}")
        print(f"Copy config.yaml.example → config.yaml and fill in your details.")
        sys.exit(1)
    with open(path) as f:
        return yaml.safe_load(f)


# ------------------------------------------------------------------ #
# Setup
# ------------------------------------------------------------------ #

def run_setup(cfg: dict):
    username = cfg["imap"]["username"]
    print(f"Setting up credentials for: {username}")
    password = input("IMAP password: ")
    keyring.set_password(KEYCHAIN_SERVICE, username, password)
    print("Password stored in macOS Keychain.")

    vault = Path(cfg["obsidian"]["vault_path"])
    if not vault.exists():
        print(f"Warning: Obsidian vault not found at {vault}")
    else:
        print(f"Obsidian vault OK: {vault}")

    # Test Ollama
    ollama = OllamaClient(cfg["ollama"])
    if ollama.is_available():
        print(f"Ollama OK: model={cfg['ollama']['model']}")
    else:
        print(f"Warning: Ollama not reachable at {cfg['ollama']['host']}")
        print("Start Ollama with: ollama serve")

    print("\nSetup complete. Run: python main.py --batch 5")


# ------------------------------------------------------------------ #
# Audit logging
# ------------------------------------------------------------------ #

def audit(event: str, **kwargs):
    entry = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **kwargs}
    with open(AUDIT_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")


# ------------------------------------------------------------------ #
# Quota check
# ------------------------------------------------------------------ #

def quota_check(cfg: dict):
    username = cfg["imap"]["username"]
    password = keyring.get_password(KEYCHAIN_SERVICE, username)
    if not password:
        print("No password found. Run: python main.py --setup")
        sys.exit(1)

    client = ImapClient(cfg["imap"])
    client.connect(password)
    pct = client.get_quota_percent()
    client.disconnect()

    if pct is not None:
        print(f"Mailbox usage: {pct:.1f}%")
    else:
        print("Quota information not available from this server.")


# ------------------------------------------------------------------ #
# Main run
# ------------------------------------------------------------------ #

def run(cfg: dict, batch_override: int = None):
    safety = cfg.get("safety", {})
    max_delete = safety.get("max_delete_per_run", 100)
    target_quota = safety.get("target_quota_percent", 88)
    move_to_trash = safety.get("move_to_trash", True)
    batch_size = batch_override or cfg["imap"].get("batch_size", 50)

    username = cfg["imap"]["username"]
    password = keyring.get_password(KEYCHAIN_SERVICE, username)
    if not password:
        print("No password found. Run: python main.py --setup")
        sys.exit(1)

    state = StateManager(cfg["state"]["db_path"])
    ollama = OllamaClient(cfg["ollama"])
    writer = ObsidianWriter(cfg["obsidian"])
    people = PersonRegistry(cfg["obsidian"])

    run_id = str(uuid.uuid4())
    errors = []
    stats = {"fetched": 0, "archived": 0, "deleted": 0}

    # ── Connect & quota gate ──────────────────────────────────────────
    client = ImapClient(cfg["imap"])
    client.connect(password)

    quota_before = client.get_quota_percent()
    if quota_before is not None:
        print(f"Mailbox usage: {quota_before:.1f}%")
        if quota_before <= target_quota:
            print(f"Already below {target_quota}% — nothing to do.")
            client.disconnect()
            state.close()
            return

    state.start_run(run_id, quota_before or 0.0)

    # ── Fetch UIDs ────────────────────────────────────────────────────
    print("Fetching UIDs from server...")
    all_uids = client.fetch_all_uids()
    print(f"Total messages on server: {len(all_uids)}")

    # Filter already-processed
    processed_ids = state.get_all_processed_ids()
    uid_to_mid = client.fetch_message_ids_for_uids(all_uids)
    mid_to_uid = {v: k for k, v in uid_to_mid.items()}

    unprocessed_uids = [
        uid for uid in all_uids
        if uid_to_mid.get(uid, "") not in processed_ids
    ]
    print(f"Unprocessed: {len(unprocessed_uids)} | Batch size: {batch_size}")

    batch_uids = unprocessed_uids[:batch_size]
    if not batch_uids:
        print("No new emails to process.")
        client.disconnect()
        state.close()
        return

    # ── Fetch full emails & back up locally ───────────────────────────
    print(f"Fetching {len(batch_uids)} emails...")
    emails, raw_by_uid = client.fetch_emails_with_raw(batch_uids)
    stats["fetched"] = len(emails)
    print(f"Fetched: {len(emails)} emails")

    backup = _make_backup(cfg)
    saved = backup.save_batch(raw_by_uid, emails)
    print(f"Backed up: {len(saved)} email(s) → {backup.root}")
    mid_to_backup = {uid_to_mid[uid]: str(path) for uid, path in saved.items() if uid in uid_to_mid}

    # ── Thread clustering ─────────────────────────────────────────────
    threads = build_threads(emails)
    threads = resolve_existing_threads(threads, state)
    print(f"Threads detected: {len(threads)}")

    # ── Process each thread ───────────────────────────────────────────
    uids_to_delete = []

    for thread_id, thread_emails in threads.items():
        subject_preview = thread_emails[0]["subject"][:50]
        print(f"\n[Thread] {subject_preview!r} ({len(thread_emails)} email(s))")

        # ── LLM analysis ──────────────────────────────────────────────
        print("  Analyzing with Ollama...")
        try:
            analysis = ollama.analyze_thread(thread_emails)
            print(f"  Category: {analysis['category']} | Priority: {analysis['priority']}")
            if analysis["summary"]:
                print(f"  Summary: {analysis['summary'][:80]}...")
        except Exception as e:
            print(f"  Ollama error: {e}")
            analysis = {"summary": "", "tags": ["needs-review"], "category": "other",
                        "action_items": [], "priority": "medium", "language": "en"}
            errors.append(str(e))

        # ── Write or update Obsidian note ─────────────────────────────
        try:
            existing_note = state.get_note_path_for_thread(thread_id)
            if existing_note:
                note_path = writer.update_thread_note(Path(existing_note), thread_emails, analysis)
                print(f"  Updated: {note_path.relative_to(Path(cfg['obsidian']['vault_path']))}")
            else:
                note_path = writer.write_thread(thread_id, thread_emails, analysis)
                print(f"  Note: {note_path.relative_to(Path(cfg['obsidian']['vault_path']))}")
            audit("archived", thread_id=thread_id, note=str(note_path),
                  email_count=len(thread_emails))
        except Exception as e:
            print(f"  Failed to write note: {e}")
            errors.append(str(e))
            continue

        # ── Update person notes ───────────────────────────────────────
        try:
            people.update(thread_emails, note_path)
        except Exception as e:
            print(f"  People update error: {e}")
            errors.append(str(e))

        # ── Record in state ───────────────────────────────────────────
        for em in thread_emails:
            state.record_archived(
                message_id=em["message_id"],
                subject=em["subject"],
                from_addr=em["from"],
                date_received=em["date_str"],
                thread_id=thread_id,
                obsidian_note_path=str(note_path),
                backup_path=mid_to_backup.get(em["message_id"]),
            )
        stats["archived"] += len(thread_emails)

        # ── Queue for deletion ────────────────────────────────────────
        if safety.get("require_file_verify", True) and not writer.note_exists(note_path):
            print(f"  WARNING: Note not found on disk — skipping deletion")
            continue

        for em in thread_emails:
            uid = mid_to_uid.get(em["message_id"])
            if uid:
                uids_to_delete.append((uid, em["message_id"]))

    # ── Delete from server ────────────────────────────────────────────
    if uids_to_delete:
        capped = uids_to_delete[:max_delete]
        if len(uids_to_delete) > max_delete:
            print(f"\nDeletion capped at {max_delete} (of {len(uids_to_delete)} queued)")

        del_uids = [u for u, _ in capped]
        del_mids = [m for _, m in capped]

        print(f"\nDeleting {len(del_uids)} emails from server...")
        if move_to_trash:
            client.move_to_trash(del_uids)
        client.mark_deleted(del_uids)
        client.expunge()

        for mid in del_mids:
            state.record_deleted(mid)
            audit("deleted", message_id=mid)

        stats["deleted"] = len(del_uids)

        # Re-check quota
        quota_after = client.get_quota_percent()
        if quota_after is not None:
            print(f"Mailbox usage after: {quota_after:.1f}%")
    else:
        quota_after = quota_before

    # ── Finish ────────────────────────────────────────────────────────
    client.disconnect()
    state.finish_run(run_id, stats["fetched"], stats["archived"],
                     stats["deleted"], quota_after or 0.0, errors)
    state.close()

    print(f"\n{'=' * 60}")
    print(f"Run complete | Fetched: {stats['fetched']} | "
          f"Archived: {stats['archived']} | Deleted: {stats['deleted']}")
    if errors:
        print(f"Errors: {len(errors)} (see {AUDIT_LOG})")


# ------------------------------------------------------------------ #
# Filtered run (used by CLI)
# ------------------------------------------------------------------ #

def run_filtered(cfg: dict, filter_uids: list[str]) -> bool:
    """Archive a specific set of UIDs (e.g. from a filtered IMAP search).

    Skips the quota gate — caller has already decided what to archive.
    Returns True on success, False if credentials are missing.
    """
    if not filter_uids:
        print("No emails to archive.")
        return True

    safety = cfg.get("safety", {})
    max_delete = safety.get("max_delete_per_run", 100)
    move_to_trash = safety.get("move_to_trash", True)

    username = cfg["imap"]["username"]
    password = keyring.get_password(KEYCHAIN_SERVICE, username)
    if not password:
        print("No password found. Run: marc setup")
        return False

    state = StateManager(cfg["state"]["db_path"])
    ollama = OllamaClient(cfg["ollama"])
    writer = ObsidianWriter(cfg["obsidian"])
    people = PersonRegistry(cfg["obsidian"])

    run_id = str(uuid.uuid4())
    errors: list[str] = []
    stats = {"fetched": 0, "archived": 0, "deleted": 0}

    client = ImapClient(cfg["imap"])
    client.connect(password)

    # Filter already-processed
    processed_ids = state.get_all_processed_ids()
    uid_to_mid = client.fetch_message_ids_for_uids(filter_uids)
    mid_to_uid = {v: k for k, v in uid_to_mid.items()}

    unprocessed = [
        uid for uid in filter_uids
        if uid_to_mid.get(uid, "") not in processed_ids
    ]

    already_done = len(filter_uids) - len(unprocessed)
    if already_done:
        print(f"  ({already_done} already archived, skipping)")

    if not unprocessed:
        print("All matched emails are already archived.")
        client.disconnect()
        state.close()
        return True

    print(f"Fetching {len(unprocessed)} email(s)...")
    emails, raw_by_uid = client.fetch_emails_with_raw(unprocessed)
    stats["fetched"] = len(emails)

    backup = _make_backup(cfg)
    saved = backup.save_batch(raw_by_uid, emails)
    print(f"Backed up: {len(saved)} email(s) → {backup.root}")
    mid_to_backup = {uid_to_mid[uid]: str(path) for uid, path in saved.items() if uid in uid_to_mid}

    threads = build_threads(emails)
    threads = resolve_existing_threads(threads, state)
    print(f"Threads: {len(threads)}")

    state.start_run(run_id, 0.0)
    uids_to_delete: list[tuple[str, str]] = []

    for thread_id, thread_emails in threads.items():
        subject_preview = thread_emails[0]["subject"][:60]
        print(f"  [{len(thread_emails)}] {subject_preview!r}")

        try:
            analysis = ollama.analyze_thread(thread_emails)
        except Exception as e:
            analysis = {"summary": "", "tags": ["needs-review"], "category": "other",
                        "action_items": [], "priority": "medium", "language": "en"}
            errors.append(str(e))

        try:
            existing_note = state.get_note_path_for_thread(thread_id)
            if existing_note:
                note_path = writer.update_thread_note(Path(existing_note), thread_emails, analysis)
            else:
                note_path = writer.write_thread(thread_id, thread_emails, analysis)
            audit("archived", thread_id=thread_id, note=str(note_path),
                  email_count=len(thread_emails))
        except Exception as e:
            errors.append(str(e))
            continue

        try:
            people.update(thread_emails, note_path)
        except Exception as e:
            errors.append(str(e))

        for em in thread_emails:
            state.record_archived(
                message_id=em["message_id"],
                subject=em["subject"],
                from_addr=em["from"],
                date_received=em["date_str"],
                thread_id=thread_id,
                obsidian_note_path=str(note_path),
                backup_path=mid_to_backup.get(em["message_id"]),
            )
        stats["archived"] += len(thread_emails)

        if safety.get("require_file_verify", True) and not writer.note_exists(note_path):
            print(f"  WARNING: Note not found on disk — skipping deletion")
            continue

        for em in thread_emails:
            uid = mid_to_uid.get(em["message_id"])
            if uid:
                uids_to_delete.append((uid, em["message_id"]))

    remaining = list(uids_to_delete)
    while remaining:
        batch = remaining[:max_delete]
        remaining = remaining[max_delete:]
        del_uids = [u for u, _ in batch]
        del_mids = [m for _, m in batch]
        print(f"Deleting {len(del_uids)} email(s) from server...")
        if move_to_trash:
            client.move_to_trash(del_uids)
        client.mark_deleted(del_uids)
        client.expunge()
        for mid in del_mids:
            state.record_deleted(mid)
            audit("deleted", message_id=mid)
        stats["deleted"] += len(del_uids)

    client.disconnect()
    state.finish_run(run_id, stats["fetched"], stats["archived"],
                     stats["deleted"], 0.0, errors)
    state.close()

    print(f"\nArchived: {stats['archived']} | Deleted: {stats['deleted']}")
    if errors:
        print(f"Errors: {len(errors)}")
    return True


# ------------------------------------------------------------------ #
# Server-only deletion (used by CLI delete flow)
# ------------------------------------------------------------------ #

def delete_from_server(cfg: dict, uids: list[str]) -> bool:
    """Delete specific UIDs from the IMAP server.

    Always saves a local .eml backup first.
    Updates state.db for UIDs that were previously archived.
    Loops in batches of max_delete_per_run until all UIDs are deleted.
    Returns True on success, False if credentials are missing.
    """
    if not uids:
        return True

    safety = cfg.get("safety", {})
    max_delete = safety.get("max_delete_per_run", 100)
    move_to_trash = safety.get("move_to_trash", True)

    username = cfg["imap"]["username"]
    password = keyring.get_password(KEYCHAIN_SERVICE, username)
    if not password:
        print("No password found. Run: marc setup")
        return False

    client = ImapClient(cfg["imap"])
    client.connect(password)

    # ── Local backup (all at once) ────────────────────────────────────
    emails, raw_by_uid = client.fetch_emails_with_raw(uids)
    backup = _make_backup(cfg)
    saved = backup.save_batch(raw_by_uid, emails)
    print(f"Backed up: {len(saved)} email(s) → {backup.root}")

    state = StateManager(cfg["state"]["db_path"])
    processed_ids = state.get_all_processed_ids()

    total_deleted = 0
    remaining = list(uids)

    while remaining:
        batch = remaining[:max_delete]
        remaining = remaining[max_delete:]

        uid_to_mid = client.fetch_message_ids_for_uids(batch)

        if move_to_trash:
            client.move_to_trash(batch)
        client.mark_deleted(batch)
        client.expunge()
        total_deleted += len(batch)
        print(f"Deleted {total_deleted}/{len(uids)} email(s) from server...")

        for uid in batch:
            mid = uid_to_mid.get(uid, "")
            if mid and mid in processed_ids:
                state.record_deleted(mid)
                audit("deleted", message_id=mid)

    state.close()
    client.disconnect()
    print(f"Done. Deleted {total_deleted} email(s) from server.")
    return True


# ------------------------------------------------------------------ #
# Sync: remove .eml files whose Obsidian notes have been deleted
# ------------------------------------------------------------------ #

def sync_backups(cfg: dict) -> tuple[int, int]:
    """Delete local .eml files for email threads whose Obsidian notes no longer exist.

    Returns (notes_cleaned, emls_deleted).
    """
    state = StateManager(cfg["state"]["db_path"])
    paths_by_note = state.get_backup_paths_by_note()
    state.close()

    notes_cleaned = 0
    emls_deleted = 0

    for note_path_str, backup_paths in paths_by_note.items():
        if Path(note_path_str).exists():
            continue  # Note still present — nothing to do

        notes_cleaned += 1
        for bp in backup_paths:
            p = Path(bp)
            if p.exists():
                try:
                    p.unlink()
                    emls_deleted += 1
                    # Remove empty month directory
                    try:
                        p.parent.rmdir()
                    except OSError:
                        pass  # Not empty, that's fine
                except OSError as e:
                    print(f"[Sync] Could not delete {p.name}: {e}")

    return notes_cleaned, emls_deleted


# ------------------------------------------------------------------ #
# Entry point
# ------------------------------------------------------------------ #

def main():
    parser = argparse.ArgumentParser(description="Email archiver to Obsidian")
    parser.add_argument("--setup", action="store_true", help="First-time setup")
    parser.add_argument("--quota-check", action="store_true", help="Print quota and exit")
    parser.add_argument("--batch", type=int, default=None, help="Override batch size")
    args = parser.parse_args()

    cfg = load_config(CONFIG_PATH)

    if args.setup:
        run_setup(cfg)
    elif args.quota_check:
        quota_check(cfg)
    else:
        run(cfg, batch_override=args.batch)


if __name__ == "__main__":
    main()
