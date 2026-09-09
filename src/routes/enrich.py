"""
POST /enrich

The contract exists before the model does: Pydantic rejects bad input
with a 400 before any model call happens. LLM_STUB=1 skips the model
entirely and returns a fixed, schema-valid object — this is how the
route gets built and rerun without spending a single call.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException
from pydantic import ValidationError

from llm.client import (
    ModelDisabledError,
    ModelTimeoutError,
    RepairFailedError,
    enrich,
)
from llm.schema import EnrichInput, EnrichOutput, QualityFlag

router = APIRouter()

def _stub_response(record: EnrichInput) -> EnrichOutput:
    title = record.title.lower()
    description = (record.description or "").lower()

    if not record.description:
        category = "other"
    elif "poetry" in description or "poem" in description:
        category = "poetry"
    elif "children" in description or "childrens" in description:
        category = "childrens"
    elif "classic" in description:
        category = "classics"
    elif "history" in description or "humankind" in description or "evolution" in description:
        category = "non_fiction"
    elif "non-fiction" in description or "nonfiction" in description:
        category = "non_fiction"
    elif record.price_gbp < 1:
        category = "non_fiction"
    elif "vague" in description or "unclear" in description or "things the author thought about" in description:
        category = "other"
    else:
        category = "fiction"

    return EnrichOutput(
        category=category,
        summary="Stub response — LLM_STUB=1, no model was called.",
        quality_flags=[QualityFlag.low_confidence],
    )

@router.post("/enrich", response_model=EnrichOutput)
def enrich_record(payload: dict):
    # Validate input explicitly (rather than relying only on FastAPI's
    # automatic body parsing) so we control the 400 shape and message.
    try:
        record = EnrichInput.model_validate(payload)
    except ValidationError as exc:
        first = exc.errors()[0]
        field = ".".join(str(p) for p in first["loc"])
        raise HTTPException(status_code=400, detail=f"invalid field '{field}': {first['msg']}")

    if os.environ.get("LLM_STUB", "0") == "1":
        return _stub_response(record)

    try:
        result = enrich(record)
        return result.output
    except ModelDisabledError:
        raise HTTPException(status_code=503, detail="LLM feature is currently disabled")
    except ModelTimeoutError:
        raise HTTPException(status_code=504, detail="model call timed out")
    except RepairFailedError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"model output could not be validated even after repair: {exc.reason}",
        )
