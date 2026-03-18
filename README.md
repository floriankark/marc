# Marc — eMAil ARChiver

<video src="https://github.com/user-attachments/assets/be2c9751-6ef5-42e6-8e77-a4f52d21cbea" autoplay loop muted playsinline></video>

Marc fetches emails from any IMAP mailbox, archives them as Markdown notes in your [Obsidian](https://obsidian.md) vault, and keeps your inbox clean — all through a conversational interface powered by a local LLM via [Ollama](https://ollama.com).

```
You: archive new emails
Marc: Fetched 12 new threads. Archived to Obsidian. Deleted 12 from server (quota now 61%).

You: merge duplicate contacts
Marc: Found 3 likely duplicate groups.
[interactive merge UI — checkbox list + arrow-key primary selection]

You: show quota
Marc: Mailbox quota: 61 % used (2.4 GB / 4.0 GB)
```

While Marc thinks, you see its reasoning live in a scrolling grey viewport — then just the answer.

---

## Features

- **Email → Obsidian** — each thread becomes a Markdown note with YAML frontmatter, full message history, summary, action items, and `[[People/Name|Name]]` wikilinks
- **People notes** — contacts are auto-created, linked to their threads, and enriched with frequent-contact stats
- **Duplicate merging** — rule-based pre-pass + LLM deduplication; interactive TUI to review groups, uncheck false positives, and pick the primary note
- **Inbox cleanup** — safely deletes archived emails from the server with a local `.eml` backup and a trash-folder safety net
- **Backup sync** — `sync` removes `.eml` backups for Obsidian notes you have deleted
- **Quota-aware** — stops deleting once your inbox drops below a configurable threshold
- **Live thinking** — streams the model's reasoning in a scrolling 10-line grey viewport while it works; disappears when the answer arrives
- **Fully local** — no cloud AI, no telemetry; everything runs on your machine via Ollama

---

## Requirements

| | Minimum |
|---|---|
| Python | 3.11+ |
| [Ollama](https://ollama.com) | any recent |
| Obsidian vault | any |
| IMAP account | any provider (SSL recommended) |

**Recommended models** — must support both **thinking** and **tools**
([browse the full list](https://ollama.com/search?c=tools&c=thinking)):

```bash
ollama pull qwen3.5:2b          # minimum recommended — fast, low RAM
ollama pull qwen3.5:4b          # better quality, needs ~4 GB RAM
ollama pull nemotron-3-nano:4b  # alternative at 4 B
ollama pull deepseek-r1:7b      # strong reasoning, needs ~8 GB RAM
```

> Models smaller than 2 B are not recommended, but you are welcome to try and
> share your results. What matters is that the model supports both thinking and
> tool use — stick to the [tools + thinking](https://ollama.com/search?c=tools&c=thinking)
> category on Ollama.

---

## Installation

```bash
git clone https://github.com/your-username/marc.git
cd marc
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
```

This installs the `marc` command into your virtual environment.

---

## Setup

**1. Copy the example config:**

```bash
cp config.yaml.example config.yaml
```

**2. Edit `config.yaml` with your details:**

```yaml
imap:
  host: "mail.your-provider.com"
  username: "you@example.com"

ollama:
  model: "qwen3:4b"

obsidian:
  vault_path: "/Users/you/Obsidian/MyVault"
```

**3. Store your IMAP password in the system keychain** (never written to disk):

```bash
marc setup
```

---

## Usage

```bash
marc
```

Then just type what you want:

| You type | What happens |
|---|---|
| `archive new emails` | Fetch, analyse, and write to Obsidian |
| `archive emails from Alice` | Filter by sender before archiving |
| `archive emails from last week` | Filter by date range |
| `quota` | Show current mailbox usage |
| `merge duplicate contacts` | Find and merge duplicate People notes |
| `sync` | Remove `.eml` backups for deleted Obsidian notes |
| `help` | Full command reference |
| `exit` | Quit and unload the model |

---

## How it works

```
IMAP server  ──(SSL)──▶  raw .eml saved to ~/EmailArchive/YYYY-MM/
                                    │
                                    ▼
                        parsed & thread-grouped
                                    │
                         Ollama analyses thread
                         (summary · tags · priority
                          action items · language)
                                    │
                                    ▼
                        Markdown note written to
                        Obsidian Emails/YYYY-MM/
                                    │
                        People/ notes created/updated
                        (wikilinks · frequent contacts)
                                    │
                                    ▼
                        email deleted from server
                        (quota check · trash copy · cap)
```

State is tracked in a local SQLite database (`state.db`) so re-runs never double-process a message.

---

## Configuration reference

See [`config.yaml.example`](config.yaml.example) for all options with inline comments.

Key safety settings:

```yaml
safety:
  require_file_verify: true    # only delete from server if the Obsidian note exists on disk
  max_delete_per_run: 100      # hard cap per run (loops in batches until all done)
  target_quota_percent: 88     # stop deleting once inbox drops below this %
  move_to_trash: true          # copy to Trash folder before permanent deletion
```

---

## Privacy

- IMAP password lives in the OS keychain only — never in any file
- All AI processing is local via Ollama — no data leaves your machine
- `config.yaml`, `state.db`, `processed.jsonl`, and `~/EmailArchive` are all `.gitignore`d

---

## License

MIT — see [LICENSE](LICENSE)
