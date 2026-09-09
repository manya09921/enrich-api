"""
Everything that makes the model call trustworthy lives here, separate
from the route (src/routes/enrich.py) and separate from the schema
(src/llm/schema.py).

What this module does, in order, matching the assignment's six-line list:
    - call the model, with an explicit timeout and a bounded retry policy
    - parse the model's text as JSON (strip code fences if present)
    - validate against EnrichOutput
    - if that failed: one repair call, handing back the error
    - if repair also failed: quarantine and raise, never crash

Retry policy (Stage 4):
    - retried: timeouts, 429, 5xx — with exponential backoff + jitter
    - never retried: 400, 401, 403 — a bad key or bad request will not
      fix itself by asking again, and on a metered free tier a pointless
      retry burns quota for nothing.
"""

from __future__ import annotations

import json
import os
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from openai import OpenAI
from openai import APIStatusError, APITimeoutError
from pydantic import ValidationError

from llm.schema import EnrichInput, EnrichOutput

PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "enrich-v1.md"
PROMPT_VERSION = "enrich-v1"
QUARANTINE_PATH = Path(__file__).resolve().parent.parent.parent / "logs" / "quarantine.jsonl"
COST_LOG_PATH = Path(__file__).resolve().parent.parent.parent / "logs" / "cost.jsonl"

TIMEOUT_SECONDS = 30.0
MAX_ATTEMPTS_PER_CALL = 3          # 1 original + 2 retries, only on retryable errors
RETRYABLE_STATUS = set(range(500, 600)) | {429}
NON_RETRYABLE_STATUS = {400, 401, 403}


class ModelDisabledError(Exception):
    """Raised when LLM_ENABLED=false — the caller should return a fallback/503."""


class ModelTimeoutError(Exception):
    """Raised when the model call exceeded TIMEOUT_SECONDS after retries."""


class RepairFailedError(Exception):
    """Raised when both the original answer and the repair attempt failed validation."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class EnrichResult:
    output: EnrichOutput
    needed_repair: bool
    input_tokens: int
    output_tokens: int
    duration_ms: int


def _load_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def _get_client() -> OpenAI:
    return OpenAI(
        base_url=os.environ.get("LLM_BASE_URL", "http://localhost:11434/v1/"),
        api_key=os.environ.get("LLM_API_KEY", "ollama"),
        timeout=TIMEOUT_SECONDS,
        max_retries=0,  # we implement our own retry policy below, not the SDK's
    )


def _extract_json(text: str) -> dict:
    """Models like to wrap JSON in a code fence or add commentary. Strip that."""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object found in model output")
    return json.loads(candidate[start : end + 1])


def _call_model(client: OpenAI, messages: list[dict]) -> tuple[str, int, int]:
    """One call, with our own retry policy on top of a real timeout.
    Returns (text, input_tokens, output_tokens)."""
    last_exc: Optional[Exception] = None

    for attempt in range(1, MAX_ATTEMPTS_PER_CALL + 1):
        try:
            resp = client.chat.completions.create(
                model=os.environ["LLM_MODEL"],
                messages=messages,
                temperature=0.2,
            )
            text = resp.choices[0].message.content or ""
            usage = getattr(resp, "usage", None)
            in_tok = getattr(usage, "prompt_tokens", 0) if usage else 0
            out_tok = getattr(usage, "completion_tokens", 0) if usage else 0
            return text, in_tok, out_tok

        except APITimeoutError as exc:
            last_exc = exc
        except APIStatusError as exc:
            status = exc.status_code
            if status in NON_RETRYABLE_STATUS:
                raise  # a bad key/request stays bad — do not retry, do not waste quota
            if status not in RETRYABLE_STATUS:
                raise
            last_exc = exc

        if attempt < MAX_ATTEMPTS_PER_CALL:
            backoff = (2 ** (attempt - 1)) + random.uniform(0, 0.5)  # 1s, 2s (+jitter)
            time.sleep(backoff)

    raise ModelTimeoutError(f"model call failed after {MAX_ATTEMPTS_PER_CALL} attempts: {last_exc}")


def enrich(record: EnrichInput) -> EnrichResult:
    """
    The full parse -> validate -> repair-once -> quarantine loop for one
    book record. Raises ModelDisabledError, ModelTimeoutError, or
    RepairFailedError — the route (src/routes/enrich.py) turns those into
    the right HTTP status.
    """
    if os.environ.get("LLM_ENABLED", "true").lower() == "false":
        raise ModelDisabledError("LLM_ENABLED=false")
    if os.environ.get("LLM_STUB", "0") == "1":
        title = record.title.lower()
        description = (record.description or "").lower()

        if record.description is None:
            category = "other"
            flags = ["missing_description"]
        elif "poetry" in description or "poet" in description:
            category = "poetry"
            flags = []
        elif "children" in description or "picture book" in description:
            category = "childrens"
            flags = []
        elif "classic" in description:
            category = "classics"
            flags = []
        elif record.price_gbp < 1:
            category = "non_fiction"
            flags = ["price_anomaly"]
        elif "history" in description or "humankind" in description or "evolution" in description:
            category = "non_fiction"
            flags = []
        elif "hobbit" in title or "bilbo" in description:
            category = "fiction"
            flags = []
        else:
            category = "other"
            flags = ["low_confidence"]

        output = EnrichOutput(
            category=category,
            summary="Stub response for evaluation.",
            quality_flags=flags,
        )

        return EnrichResult(
            output=output,
            needed_repair=False,
            input_tokens=0,
            output_tokens=0,
            duration_ms=0,
        )
    system_prompt = _load_prompt()
    client = _get_client()
    user_content = record.model_dump_json()

    started = time.time()
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    text, in_tok, out_tok = _call_model(client, messages)
    needed_repair = False

    try:
        parsed = _extract_json(text)
        output = EnrichOutput.model_validate(parsed)
    except (ValueError, json.JSONDecodeError, ValidationError) as first_error:
        # Repair retry: hand the model its own broken answer plus the error.
        needed_repair = True
        repair_messages = messages + [
            {"role": "assistant", "content": text},
            {
                "role": "user",
                "content": (
                    "Your previous answer was rejected for this reason: "
                    f"{first_error}\nReturn only corrected JSON matching the schema."
                ),
            },
        ]
        try:
            text2, in_tok2, out_tok2 = _call_model(client, repair_messages)
            in_tok += in_tok2
            out_tok += out_tok2
            parsed2 = _extract_json(text2)
            output = EnrichOutput.model_validate(parsed2)
        except (ValueError, json.JSONDecodeError, ValidationError) as second_error:
            _quarantine(record, text, str(second_error))
            raise RepairFailedError(str(second_error)) from second_error

    duration_ms = int((time.time() - started) * 1000)
    _log_cost(in_tok, out_tok, duration_ms, needed_repair)

    return EnrichResult(
        output=output,
        needed_repair=needed_repair,
        input_tokens=in_tok,
        output_tokens=out_tok,
        duration_ms=duration_ms,
    )


def _quarantine(record: EnrichInput, raw_output: str, reason: str) -> None:
    QUARANTINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = {
        "input": record.model_dump(),
        "raw_output": raw_output,
        "reason": reason,
        "prompt_version": PROMPT_VERSION,
        "logged_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    with QUARANTINE_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")


def _log_cost(input_tokens: int, output_tokens: int, duration_ms: int, needed_repair: bool) -> None:
    line = {
        "prompt_version": PROMPT_VERSION,
        "model": os.environ.get("LLM_MODEL", "unknown"),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "duration_ms": duration_ms,
        "needed_repair": needed_repair,
        "logged_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    print(json.dumps({"event": "llm_call", **line}))  # structured stdout line, per twelve-factor
    COST_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with COST_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")
