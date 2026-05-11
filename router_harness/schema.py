"""Shared types. Keep flat dicts so everything is JSONL-friendly."""
from dataclasses import dataclass, asdict, field
from typing import Optional, List, Literal
import time, json


@dataclass
class WallSnapshot:
    """Snapshot of verified walls for one coin at one moment."""
    ts: float                           # epoch seconds
    coin: str
    mid: float
    walls: list                         # each: {side, price, usd, distance_pct, persistence_windows}

    def to_jsonl(self) -> str:
        return json.dumps(asdict(self), separators=(',', ':'))


@dataclass
class SignalAttempt:
    """A signal proposed by an edge engine. May or may not have been executed.

    Captured BEFORE any guard logic — i.e. we record the raw intent, not the
    filtered output. Lets the harness ask: 'what would the router have done?'
    """
    ts: float
    coin: str
    side: Literal['BUY', 'SELL']
    engine: str                         # 'BB_REJ', 'PIVOT', 'LIQ_CSCD', etc.
    entry_px: float
    sl_px: Optional[float] = None
    tp_px: Optional[float] = None
    intended_size_usd: Optional[float] = None
    blocked_by: Optional[str] = None    # 'engine_disabled', 'btc_macro', 'bucket_filter', or None
    block_reason: Optional[str] = None

    def to_jsonl(self) -> str:
        return json.dumps(asdict(self), separators=(',', ':'))


@dataclass
class Decision:
    """Output of a router function for one signal."""
    action: Literal['ALLOW', 'BLOCK', 'MODIFY']
    reason: str                         # human-readable, eg 'ask_wall_0.1%_$25M_persist_300s'
    suggested_sl_px: Optional[float] = None
    suggested_tp_px: Optional[float] = None
    size_mult: float = 1.0              # 1.0 = no change, 0.5 = half, 0 = block (redundant w/ BLOCK)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Outcome:
    """What actually happened to the price after the signal."""
    ts_signal: float
    coin: str
    side: str
    entry_px: float
    max_favorable_30m: float            # how far in our direction price moved within 30m
    max_adverse_30m: float              # how far against
    max_favorable_60m: float
    max_adverse_60m: float
    max_favorable_120m: float
    max_adverse_120m: float
    # Computed: did suggested SL/TP get hit? (set per scenario in harness, not here)
