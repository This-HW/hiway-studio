"""Provider adapter interface (design report §3.4). This file is the hardest to change.

Every adapter implements this. Adapters know ONLY the provider's call protocol:
routing, retries and fallback live outside (router.py, tasks/).
Adapter exceptions must be converted to app.core.errors StudioError.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional


@dataclass(frozen=True)
class JobStatus:
    state: str  # queued | running | succeeded | failed
    progress_pct: float = 0.0
    stage: str = ""
    output_urls: list[str] = field(default_factory=list)
    error_code: Optional[str] = None
    error_message: Optional[str] = None


@dataclass(frozen=True)
class EstimatedCost:
    amount_usd: Decimal
    unit_breakdown: str = ""


class ProviderAdapter(ABC):
    provider_id: str  # e.g. "fal", "replicate", "openai-direct", "comfyui", "stub"

    @abstractmethod
    def submit(self, model_id: str, payload: dict) -> str:
        """Submit a generation request. Returns provider_job_id
        (a synchronous model may return an immediately-completed id)."""

    @abstractmethod
    def poll(self, provider_job_id: str) -> JobStatus:
        """Poll provider state. Must not raise on transient network errors;
        report them via JobStatus(state=...) or raise StudioError(PROVIDER_ERROR)."""

    @abstractmethod
    def cancel(self, provider_job_id: str) -> None:
        """Best-effort cancellation."""

    @abstractmethod
    def estimate(self, model_id: str, payload: dict) -> EstimatedCost:
        """Cost estimate from the registry price table. No network calls."""

    @abstractmethod
    def fetch_result(self, provider_job_id: str) -> bytes:
        """Download the finished artifact bytes. Called once after poll()
        reports succeeded."""
