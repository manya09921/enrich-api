"""
Schemas for the /enrich endpoint (Assignment A17).

EnrichInput is what the caller sends — a scraped book record (matches
the shape output/books.json produces in the W5 scraper, minus fields
the model doesn't need, like product_url).

EnrichOutput is the closed, validated shape the model's answer must fit.
Every category-like field is an Enum, so "invent a category" becomes a
validation error instead of a silent surprise.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class RatingText(str, Enum):
    one = "One"
    two = "Two"
    three = "Three"
    four = "Four"
    five = "Five"
    unknown = "Unknown"


class EnrichInput(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    price_gbp: float = Field(gt=0)
    availability_text: str = Field(min_length=1, max_length=200)
    rating_text: RatingText
    description: Optional[str] = Field(default=None, max_length=4000)


class Category(str, Enum):
    fiction = "fiction"
    non_fiction = "non_fiction"
    childrens = "childrens"
    poetry = "poetry"
    classics = "classics"
    other = "other"


class QualityFlag(str, Enum):
    missing_description = "missing_description"
    low_confidence = "low_confidence"
    price_anomaly = "price_anomaly"


class EnrichOutput(BaseModel):
    category: Category
    summary: str = Field(min_length=1, max_length=200)
    quality_flags: list[QualityFlag] = Field(default_factory=list)

    @field_validator("summary")
    @classmethod
    def summary_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("summary is blank")
        return v.strip()
