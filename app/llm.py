"""Optional LLM fallback over any OpenAI-compatible endpoint (Ollama's /v1,
OpenAI, LM Studio, vLLM, OpenRouter…).

It is only consulted for files the dialogue matcher could not settle, and it
only chooses among a short candidate list. Its answers are always flagged as
AI suggestions in the plan and are never applied without being ticked."""
from __future__ import annotations

import json
import re

import httpx

from .config import get_settings

SYSTEM = (
    "You identify which TV episode a video file contains. You are given dialogue "
    "from the file and a short list of candidate episodes with their official "
    "summaries. Pick the candidate whose plot matches the dialogue. If none clearly "
    "matches, answer null. Reply with JSON only: "
    '{"episode": "S01E02" or null, "confidence": 0.0-1.0, "reason": "one sentence"}'
)


def available() -> bool:
    s = get_settings()
    return bool(s["llm_enabled"] and s["llm_base_url"] and s["llm_model"])


def _chat(messages: list[dict], max_tokens: int = 400) -> str:
    s = get_settings()
    base = s["llm_base_url"].rstrip("/")
    headers = {"Content-Type": "application/json"}
    if s["llm_api_key"]:
        headers["Authorization"] = f"Bearer {s['llm_api_key']}"
    body = {"model": s["llm_model"], "messages": messages, "temperature": 0,
            "max_tokens": max_tokens}
    r = httpx.post(f"{base}/chat/completions", json=body, headers=headers, timeout=300)
    if r.status_code != 200:
        raise RuntimeError(f"LLM request failed ({r.status_code}): {r.text[:200]}")
    return r.json()["choices"][0]["message"]["content"] or ""


def test_connection() -> str:
    out = _chat([{"role": "user", "content": "Reply with the single word OK."}], 2000)
    return re.sub(r"(?s)<think>.*?</think>", "", out).strip()[:100]


def _excerpt(text: str, limit: int = 7000) -> str:
    if len(text) <= limit:
        return text
    third = limit // 3
    mid = len(text) // 2
    return text[:third] + " … " + text[mid - third // 2: mid + third // 2] + " … " + text[-third:]


def judge(dialogue: str, candidates: list[dict], series_name: str) -> dict | None:
    """candidates: [{"code", "title", "overview"}]. Returns
    {"code", "confidence", "reason"} or None."""
    if not candidates or not dialogue.strip():
        return None
    lines = [f"{c['code']} — \"{c['title']}\": {c.get('overview') or '(no summary)'}"
             for c in candidates]
    user = (f"Series: {series_name}\n\nCandidate episodes:\n" + "\n".join(lines) +
            f"\n\nDialogue from the file:\n{_excerpt(dialogue)}")
    raw = _chat([{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
                2000)
    raw = re.sub(r"(?s)<think>.*?</think>", "", raw)
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    code = data.get("episode")
    valid = {c["code"] for c in candidates}
    if not code or code.upper() not in valid:
        return None
    try:
        conf = float(data.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    return {"code": code.upper(), "confidence": round(conf, 2),
            "reason": str(data.get("reason") or "")[:300]}
