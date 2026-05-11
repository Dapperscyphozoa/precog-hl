"""Wall-router backtest harness. Council Day 1.

Components:
  recorder.py    — append-only writer: wall snapshots + signal attempts → JSONL on /var/data
  router.py      — wall_router(signal, wall_context, spoof_context, regime) → Decision
  scorer.py      — for each (signal, decision) outcome at +30/60/120m via HL candles
  harness.py     — orchestrates: load recorded streams, apply router fn, score, summarise
"""
