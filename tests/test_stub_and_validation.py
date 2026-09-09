"""
Tests that need no model at all: input validation (Stage 1) and stub
mode (Stage 1). Run with LLM_STUB=1 always set for these.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ["LLM_STUB"] = "1"
os.environ.setdefault("LLM_MODEL", "gemma3:1b")

from fastapi.testclient import TestClient  # noqa: E402
from main import app  # noqa: E402

client = TestClient(app)

VALID_INPUT = {
    "title": "A Light in the Attic",
    "price_gbp": 51.77,
    "availability_text": "In stock (22 available)",
    "rating_text": "Three",
    "description": "A classic collection of poetry and drawings.",
}


def test_valid_input_returns_200_schema_shaped_response():
    resp = client.post("/enrich", json=VALID_INPUT)
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"category", "summary", "quality_flags"}
    assert body["category"] in [
        "fiction", "non_fiction", "childrens", "poetry", "classics", "other"
    ]


def test_missing_field_returns_400_naming_the_field():
    bad = dict(VALID_INPUT)
    del bad["title"]
    resp = client.post("/enrich", json=bad)
    assert resp.status_code == 400
    assert "title" in resp.json()["detail"]


def test_wrong_type_returns_400():
    bad = dict(VALID_INPUT)
    bad["price_gbp"] = "not a number"
    resp = client.post("/enrich", json=bad)
    assert resp.status_code == 400
    assert "price_gbp" in resp.json()["detail"]


def test_invalid_rating_text_returns_400():
    bad = dict(VALID_INPUT)
    bad["rating_text"] = "Six"  # not in the closed list
    resp = client.post("/enrich", json=bad)
    assert resp.status_code == 400


def test_negative_price_returns_400():
    bad = dict(VALID_INPUT)
    bad["price_gbp"] = -5.0
    resp = client.post("/enrich", json=bad)
    assert resp.status_code == 400
