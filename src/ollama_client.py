"""Ollama LLM client for email thread analysis."""

import json
import re
import requests
from typing import Optional


SYSTEM_PROMPT = """You are processing academic research emails for archival into a knowledge base.
Be concise and factual. Return ONLY valid JSON with no additional text or markdown fences."""

ANALYSIS_PROMPT_TEMPLATE = """Analyze this email thread and return a JSON object with exactly these keys:

{{
  "summary": "2-3 sentence summary of the thread topic and outcome",
  "tags": ["list", "of", "3-8", "relevant", "lowercase", "tags"],
  "category": "one of: meeting|paper|data|admin|collaboration|funding|conference|personal|newsletter|notification|other",
  "action_items": ["list of action items if any, empty list if none"],
  "priority": "one of: high|medium|low|archive",
  "language": "primary language code, e.g. en|de|fr"
}}

Thread participants and subjects will be extracted separately. Focus on content.

EMAIL THREAD (oldest first):
---
{thread_text}
---"""


class OllamaClient:
    def __init__(self, cfg: dict):
        self.host = cfg.get("host", "http://localhost:11434")
        self.model = cfg.get("model", "phi3:3.8b")
        self.timeout = cfg.get("timeout_seconds", 180)
        self.max_chars = cfg.get("max_prompt_chars", 6000)

    def analyze_thread(self, emails: list[dict]) -> dict:
        """Summarize and categorize an email thread. Returns analysis dict."""
        thread_text = self._build_thread_text(emails)
        prompt = ANALYSIS_PROMPT_TEMPLATE.format(thread_text=thread_text)

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "think": False,   # Disable thinking mode for structured output
            "options": {
                "temperature": 0.1,
                "num_predict": 1024,
            },
        }
        # connect timeout is short; read timeout is generous (model may be loading)
        request_timeout = (10, self.timeout)

        for attempt in (1, 2):
            try:
                response = requests.post(
                    f"{self.host}/api/chat",
                    json=payload,
                    timeout=request_timeout,
                )
                response.raise_for_status()
                raw = response.json().get("message", {}).get("content", "")
                return self._parse_response(raw)
            except requests.exceptions.ConnectionError:
                print(f"[Ollama] Cannot connect to {self.host} — using fallback metadata")
                return _fallback()
            except requests.exceptions.Timeout:
                if attempt == 1:
                    print(f"[Ollama] Timed out after {self.timeout}s, retrying...")
                    continue
                print(f"[Ollama] Timed out again — using fallback metadata")
                return _fallback()
            except Exception as e:
                print(f"[Ollama] Error: {e} — using fallback metadata")
                return _fallback()

    def is_available(self) -> bool:
        try:
            r = requests.get(f"{self.host}/api/tags", timeout=5)
            return r.status_code == 200
        except Exception:
            return False

    def _build_thread_text(self, emails: list[dict]) -> str:
        """Build truncated thread text for the prompt."""
        parts = []
        for em in emails:
            header = (
                f"From: {em['from']}\n"
                f"To: {em['to']}\n"
                f"Date: {em['date_str']}\n"
                f"Subject: {em['subject']}\n\n"
                f"{em['body']}"
            )
            parts.append(header)

        full_text = "\n\n---\n\n".join(parts)

        if len(full_text) <= self.max_chars:
            return full_text

        # Truncation: keep first 2 emails + last email + marker
        if len(emails) > 3:
            first_two = "\n\n---\n\n".join(parts[:2])
            last_one = parts[-1]
            omitted = len(emails) - 3
            marker = f"\n\n[... {omitted} emails omitted for length ...]\n\n"
            truncated = first_two + marker + last_one
            if len(truncated) > self.max_chars:
                truncated = truncated[: self.max_chars] + "\n[truncated]"
            return truncated

        return full_text[: self.max_chars] + "\n[truncated]"

    def _parse_response(self, raw: str) -> dict:
        """Extract JSON from LLM response, handling common formatting issues."""
        text = raw.strip()

        # Strip <think>...</think> blocks if they appear in response despite think:false
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

        # Strip markdown code fences if present
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
        text = re.sub(r"\s*```$", "", text, flags=re.MULTILINE)
        text = text.strip()

        # Find first { ... } block
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1:
            text = text[start : end + 1]

        try:
            data = json.loads(text)
            return {
                "summary": str(data.get("summary", "")).strip(),
                "tags": _clean_list(data.get("tags", [])),
                "category": str(data.get("category", "other")).strip().lower(),
                "action_items": _clean_list(data.get("action_items", [])),
                "priority": str(data.get("priority", "medium")).strip().lower(),
                "language": str(data.get("language", "en")).strip().lower(),
            }
        except json.JSONDecodeError:
            print(f"[Ollama] JSON parse failed on: {raw[:200]}")
            return _fallback()


def _clean_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if v]


def _fallback() -> dict:
    return {
        "summary": "",
        "tags": ["needs-review"],
        "category": "other",
        "action_items": [],
        "priority": "medium",
        "language": "en",
    }
