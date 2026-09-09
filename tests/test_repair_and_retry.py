"""
Tests for Stage 3 (parse, validate, repair once, quarantine) and Stage 4
(retry policy, kill switch) — all against a fake client, so no real
model or network is needed to prove the logic works.
"""

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ.setdefault("LLM_MODEL", "gemma3:1b")

import httpx  # noqa: E402
import pytest  # noqa: E402
from openai import APIStatusError, APITimeoutError  # noqa: E402


def _status_error(status_code: int) -> APIStatusError:
    request = httpx.Request("POST", "http://localhost:11434/v1/chat/completions")
    response = httpx.Response(status_code, request=request, json={"error": "test"})
    return APIStatusError(message=f"status {status_code}", response=response, body=None)

import llm.client as client_module  # noqa: E402
from llm.client import (  # noqa: E402
    ModelDisabledError,
    ModelTimeoutError,
    RepairFailedError,
    enrich,
)
from llm.schema import EnrichInput  # noqa: E402

VALID_RECORD = EnrichInput(
    title="A Light in the Attic",
    price_gbp=51.77,
    availability_text="In stock (22 available)",
    rating_text="Three",
    description="A classic collection of poetry and drawings.",
)

GOOD_ANSWER = json.dumps(
    {"category": "poetry", "summary": "A poetry collection.", "quality_flags": []}
)
BAD_ANSWER = json.dumps({"category": "sci-fi-not-in-list", "summary": "x", "quality_flags": []})


class _FakeResponse:
    def __init__(self, content: str):
        self.choices = [SimpleNamespace(message=SimpleNamespace(content=content))]
        self.usage = SimpleNamespace(prompt_tokens=100, completion_tokens=20)


def _fake_client_returning(*answers):
    """A fake OpenAI client whose .chat.completions.create() returns each
    answer in `answers` in sequence, one per call."""
    remaining = list(answers)

    def create(**kwargs):
        item = remaining.pop(0)
        if isinstance(item, Exception):
            raise item
        return _FakeResponse(item)

    fake = SimpleNamespace()
    fake.chat = SimpleNamespace(completions=SimpleNamespace(create=create))
    return fake


def test_happy_path_no_repair_needed():
    fake = _fake_client_returning(GOOD_ANSWER)
    with patch.object(client_module, "_get_client", return_value=fake):
        result = enrich(VALID_RECORD)
    assert result.output.category == "poetry"
    assert result.needed_repair is False


def test_bad_first_answer_triggers_one_repair_then_succeeds():
    fake = _fake_client_returning(BAD_ANSWER, GOOD_ANSWER)
    with patch.object(client_module, "_get_client", return_value=fake):
        result = enrich(VALID_RECORD)
    assert result.output.category == "poetry"
    assert result.needed_repair is True


def test_bad_answer_twice_raises_repair_failed_and_quarantines(tmp_path):
    quarantine_file = tmp_path / "quarantine.jsonl"
    with patch.object(client_module, "QUARANTINE_PATH", quarantine_file):
        fake = _fake_client_returning(BAD_ANSWER, BAD_ANSWER)
        with patch.object(client_module, "_get_client", return_value=fake):
            with pytest.raises(RepairFailedError):
                enrich(VALID_RECORD)
    assert quarantine_file.exists()
    logged = json.loads(quarantine_file.read_text().strip().splitlines()[-1])
    assert logged["prompt_version"] == "enrich-v1"


def test_markdown_fenced_json_is_parsed():
    fenced = f"Sure! Here's the JSON:\n```json\n{GOOD_ANSWER}\n```"
    fake = _fake_client_returning(fenced)
    with patch.object(client_module, "_get_client", return_value=fake):
        result = enrich(VALID_RECORD)
    assert result.output.category == "poetry"


def test_kill_switch_raises_model_disabled_without_any_call():
    with patch.dict(os.environ, {"LLM_ENABLED": "false"}):
        with pytest.raises(ModelDisabledError):
            enrich(VALID_RECORD)


def test_429_is_retried_then_succeeds():
    err = _status_error(429)
    fake = _fake_client_returning(err, GOOD_ANSWER)
    with patch.object(client_module, "_get_client", return_value=fake), \
         patch.object(client_module.time, "sleep", return_value=None):  # skip real backoff delay
        result = enrich(VALID_RECORD)
    assert result.output.category == "poetry"


def test_401_is_never_retried():
    err = _status_error(401)
    # only one item queued: if the code retried, this would raise IndexError
    # instead of propagating the original 401, which the test would also catch —
    # so we additionally assert only one call was attempted via call count.
    call_count = {"n": 0}

    def create(**kwargs):
        call_count["n"] += 1
        raise err

    fake = SimpleNamespace()
    fake.chat = SimpleNamespace(completions=SimpleNamespace(create=create))

    with patch.object(client_module, "_get_client", return_value=fake):
        with pytest.raises(APIStatusError):
            enrich(VALID_RECORD)
    assert call_count["n"] == 1  # no retry on 401
