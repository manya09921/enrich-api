"""
Runs every case in evals/cases.json against a running instance of the API
and reports how many matched on category (the key field).

Usage:
    uvicorn src.main:app --reload &   # start the API first, LLM_STUB unset
    python evals/run_eval.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import requests

API_URL = "http://127.0.0.1:8000/enrich"
CASES_PATH = Path(__file__).resolve().parent / "cases.json"


def main() -> None:
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    passed = 0
    failures = []

    for case in cases:
        try:
            resp = requests.post(API_URL, json=case["input"], timeout=60)
        except requests.exceptions.RequestException as exc:
            failures.append((case["id"], f"request failed: {exc}"))
            continue

        if resp.status_code != 200:
            failures.append((case["id"], f"HTTP {resp.status_code}: {resp.text}"))
            continue

        body = resp.json()
        got_category = body.get("category")
        expected = case["expected_category"]

        if got_category == expected:
            passed += 1
        else:
            failures.append(
                (case["id"], f"expected category={expected!r}, got {got_category!r}")
            )

    total = len(cases)
    print(f"\n{passed}/{total} matched on category\n")
    if failures:
        print("Failures:")
        for case_id, reason in failures:
            print(f"  - {case_id}: {reason}")

    sys.exit(0 if not failures else 1)


if __name__ == "__main__":
    main()
