"""Data shapes passed between the brain, the risk engine and the executor."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field


class Decision(BaseModel):
    """One proposal from Claude. Validated strictly; anything invalid is discarded."""

    model_config = {"extra": "forbid"}

    symbol: str = Field(min_length=3, max_length=20)
    action: Literal["buy", "sell", "hold"]
    usd_amount: float = Field(ge=0, le=1_000_000)
    confidence: float = Field(ge=0, le=1)
    stop_loss_pct: float = Field(ge=0, le=100)
    take_profit_pct: float = Field(ge=0, le=1000)
    rationale: str = Field(max_length=2000)


@dataclass(frozen=True)
class OrderIntent:
    """An order the risk engine has approved, already sized and quantized."""

    symbol: str
    side: Literal["buy", "sell"]
    asset_quantity: Decimal
    est_usd: float
    source: Literal["claude", "stop_loss", "take_profit"]
    reason: str
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None
