"""Strict structured-output schema for OpenAI Responses. Not a trading contract."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ResearchClaimModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_id: str
    text: str
    category: Literal["performance", "growth", "risk", "hypothesis", "other"]
    evidence_refs: list[str] = Field(default_factory=list)
    quoted_span: str | None = None
    chunk_id: str | None = None
    fact_id: str | None = None
    text_start: int | None = None
    text_end: int | None = None
    asserted_value: float | None = None
    asserted_unit: str | None = None
    asserted_period: str | None = None
    asserted_accession: str | None = None


class ResearchOutputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thesis: str
    observations: list[str] = Field(default_factory=list)
    growth_drivers: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    bullish_hypotheses: list[str] = Field(default_factory=list)
    bearish_hypotheses: list[str] = Field(default_factory=list)
    counterarguments: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    claims: list[ResearchClaimModel] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
