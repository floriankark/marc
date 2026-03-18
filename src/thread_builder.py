"""Thread clustering via Union-Find on Message-ID / In-Reply-To / References headers."""


class _UnionFind:
    def __init__(self):
        self._parent: dict[str, str] = {}
        self._rank: dict[str, int] = {}

    def find(self, x: str) -> str:
        if x not in self._parent:
            self._parent[x] = x
            self._rank[x] = 0
        if self._parent[x] != x:
            self._parent[x] = self.find(self._parent[x])  # path compression
        return self._parent[x]

    def union(self, a: str, b: str):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self._rank[ra] < self._rank[rb]:
            ra, rb = rb, ra
        self._parent[rb] = ra
        if self._rank[ra] == self._rank[rb]:
            self._rank[ra] += 1


def build_threads(emails: list[dict]) -> dict[str, list[dict]]:
    """
    Group emails into threads.

    Returns a dict mapping thread_id → sorted list of email dicts (oldest first).
    thread_id is the Message-ID of the earliest email in the thread.
    """
    uf = _UnionFind()

    # Register all message IDs
    for em in emails:
        mid = em["message_id"]
        if mid:
            uf.find(mid)  # ensure node exists

    # Union based on reply relationships
    for em in emails:
        mid = em["message_id"]
        if not mid:
            continue
        if em.get("in_reply_to"):
            uf.union(mid, em["in_reply_to"])
        for ref in em.get("references", []):
            if ref:
                uf.union(mid, ref)

    # Group emails by their root
    groups: dict[str, list[dict]] = {}
    for em in emails:
        root = uf.find(em["message_id"]) if em["message_id"] else em["message_id"]
        groups.setdefault(root, []).append(em)

    # Sort each thread by date, then pick earliest Message-ID as canonical thread_id
    result: dict[str, list[dict]] = {}
    for root, thread_emails in groups.items():
        thread_emails.sort(key=lambda e: e["date"])
        canonical_id = thread_emails[0]["message_id"]
        result[canonical_id] = thread_emails

    return result


def resolve_existing_threads(
    threads: dict[str, list[dict]],
    state,
) -> dict[str, list[dict]]:
    """
    Remap new thread_ids to existing canonical thread_ids from state.db.

    If any email in a new thread references a message_id already stored in
    state.db (via in_reply_to or references), the whole new thread is merged
    under the existing canonical thread_id.  Multiple new threads that point
    to the same existing thread are also collapsed together.
    """
    remap: dict[str, str] = {}  # new_thread_id -> canonical_thread_id

    for thread_id, emails in threads.items():
        # Collect all message-ids this thread is related to
        all_refs: set[str] = set()
        for em in sorted(emails, key=lambda e: e["date"]):
            all_refs.add(em["message_id"])
            if em.get("in_reply_to"):
                all_refs.add(em["in_reply_to"])
            all_refs.update(em.get("references", []))

        # Check state.db for any known thread
        for ref_mid in all_refs:
            if not ref_mid:
                continue
            existing_tid = state.get_thread_id_for_message_id(ref_mid)
            if existing_tid:
                remap[thread_id] = existing_tid
                break  # First match wins; all emails go to this thread

    # Rebuild dict with canonical keys, merging collisions
    result: dict[str, list[dict]] = {}
    for thread_id, emails in threads.items():
        canonical = remap.get(thread_id, thread_id)
        if canonical in result:
            # Deduplicate by message_id before extending
            existing_mids = {e["message_id"] for e in result[canonical]}
            result[canonical].extend(e for e in emails if e["message_id"] not in existing_mids)
        else:
            result[canonical] = list(emails)

    # Re-sort each thread oldest-first
    for tid in result:
        result[tid].sort(key=lambda e: e["date"])

    return result


def canonical_subject(emails: list[dict]) -> str:
    """Return a clean subject by stripping Re:/Fwd: prefixes."""
    for em in sorted(emails, key=lambda e: e["date"]):
        subj = em.get("subject", "")
        cleaned = _strip_re(subj)
        if cleaned:
            return cleaned
    return emails[0].get("subject", "(no subject)") if emails else "(no subject)"


def _strip_re(subject: str) -> str:
    import re
    return re.sub(r"^(re|fwd|fw|aw|wg)\s*:\s*", "", subject, flags=re.IGNORECASE).strip()
