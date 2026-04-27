#!/usr/bin/env python3
"""PreCog v8.28 — 50-coin universe + 48 MT4 tickers

Dual signal engine:
  1. Internal BOS/pivot/RSI → per-ticker gated (73 configs)
  2. TV Trend Buy/Sell webhooks → per-ticker gated + EMA confirm (EA)

10% risk | 10x lev | 0.7% trail | 1% SL | native HL stop orders
"""
import os, json, time, random, traceback
from datetime import datetime
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants
from eth_account import Account
import threading
from queue import Queue
from flask import Flask, request as flask_request, jsonify, Response
import bybit_ws
import percoin_configs
import orderbook_ws
import news_filter
import wall_confluence
import risk_ladder
import signal_persistence
import profit_lock
import leverage_map
import wall_bounce
import wall_exhaustion
import wall_absorption
import funding_engine
import liquidation_ws
import bybit_lead
import funding_filter
import btc_correlation
import confidence
import spoof_detection
import session_scaler
import whale_filter
import cvd_ws
import oi_tracker
import funding_arb

# 2026-04-25: OKX-based candle fetcher replaces HL REST (CloudFront 429 cascade
# on shared cloud IPs). Same dict shape as info.candles_snapshot for drop-in.
import okx_fetch

# 2026-04-25 (later): flight_guard adds per-coin spacing on HL execution
# writes (cancel + order) to prevent CloudFront burst-trip on cancel→taker
# patterns observed in logs (cancel err WLFI + 619ms later taker err WLFI,
# both 429). order_state prevents duplicate lifecycle execution per trade_id.
import flight_guard
import order_state

# 2026-04-25 (event-sourced execution model): position_ledger is the local
# in-memory source of truth for position state, fed by hl_user_ws. Replaces
# REST-driven _ep_fetch_size() / info.user_state() polling. atomic_entry
# submits entry+SL+TP via bulk_orders (one atomic API call) eliminating the
# fill-before-SL race. All passive by default. Activate per-flag.
import position_ledger
import hl_user_ws
import atomic_entry
import atomic_reconciler

USE_ATOMIC_EXEC = os.environ.get('USE_ATOMIC_EXEC', '0') == '1'
USE_LEDGER_FOR_SIZE = os.environ.get('USE_LEDGER_FOR_SIZE', '0') == '1'
# Single-snapshot candle pipeline (collapses 78× per-tick fan-out into one
# controlled snapshot rebuild per cycle — eliminates CloudFront 429 cascades).
try:
    import candle_snapshot as _candle_snap
    _SNAPSHOT_OK = True
except Exception as _e:
    _candle_snap = None
    _SNAPSHOT_OK = False
    print(f'[candle_snapshot] import failed (non-fatal): {_e}', flush=True)

# Event-confirmed SL state machine (replaces poll-with-deadline that produced
# false-negative emergency closes when exchange visibility lagged placement).
try:
    import sl_state_tracker as _sl_state_tracker
    _SL_STATE_OK = True
except Exception as _e:
    _sl_state_tracker = None
    _SL_STATE_OK = False
    print(f'[sl_state_tracker] import failed (non-fatal): {_e}', flush=True)

# Post-mortem tuning engine (HL-only; MT4 close path does NOT call this).
# Import is defensive: if the module or its deps are missing, the rest of
# precog.py still runs. Every call site below is guarded.
try:
    import postmortem as _postmortem
    _POSTMORTEM_OK = True
except Exception as _e:
    _postmortem = None
    _POSTMORTEM_OK = False
    print(f'[postmortem] import failed (non-fatal): {_e}', flush=True)

# Microstructure annotator — post-hoc classifier (non-blocking).
# Defers execution timing refinement work until 200+ closes accumulated.
try:
    import microstructure as _microstructure
    _MS_OK = True
except Exception as _e:
    _microstructure = None
    _MS_OK = False
    print(f'[microstructure] import failed (non-fatal): {_e}', flush=True)

# Signal state logger — parallel telemetry for future mutual information analysis.
# Defers signal redundancy optimization until 1000+ states + 500+ closes.
try:
    import signal_logger as _signal_logger
    _signal_logger.start_trim_daemon()
    _SL_OK = True
except Exception as _e:
    _signal_logger = None
    _SL_OK = False
    print(f'[signal_log] import failed (non-fatal): {_e}', flush=True)

# Convexity scorer — silent telemetry.
# Defers convex-sizing activation until 100+ closes analyzed.
try:
    import convex_scorer as _convex
    _convex.start_trim_daemon()
    _CX_OK = True
except Exception as _e:
    _convex = None
    _CX_OK = False
    print(f'[convex] import failed (non-fatal): {_e}', flush=True)

# Counterfactual engine — replay alternatives per trade.
# Telemetry only; no auto-activation. Trigger at 50+ outcomes.
try:
    import counterfactual as _counterfactual
    _counterfactual.start_trim_daemon()
    _CF_OK = True
except Exception as _e:
    _counterfactual = None
    _CF_OK = False
    print(f'[counterfactual] import failed (non-fatal): {_e}', flush=True)

# Red team defenses — cheap structural safeguards.
# Chop cooldown multiplier, regime staleness check, funding exit audit.
try:
    import red_team as _red_team
    _red_team.funding_exit_audit()
    _RT_OK = True
except Exception as _e:
    _red_team = None
    _RT_OK = False
    print(f'[red_team] import failed (non-fatal): {_e}', flush=True)

# Optimal inaction — silent abstain scorer.
# Trigger at 100 outcomes; no live gating until activated.
try:
    import optimal_inaction as _inaction
    _inaction.start_trim_daemon()
    _IA_OK = True
except Exception as _e:
    _inaction = None
    _IA_OK = False
    print(f'[abstain] import failed (non-fatal): {_e}', flush=True)

# Edge decay monitor — LIVE rolling-window drift detector.
# Alerts (not trading gates) on decay_slow/fast/broken state transitions.
try:
    import edge_decay as _edge_decay
    _ED_OK = True
except Exception as _e:
    _edge_decay = None
    _ED_OK = False
    print(f'[edge_decay] import failed (non-fatal): {_e}', flush=True)

# Path dependency — LIVE streak detection with adaptive sizing.
# Modifies live size_mult based on consecutive losses/wins.
try:
    import path_dependency as _path_dep
    _PD_OK = True
except Exception as _e:
    _path_dep = None
    _PD_OK = False
    print(f'[path_dep] import failed (non-fatal): {_e}', flush=True)

# Reality gap — audit of backtest-to-live drift.
# Exposes correction factors for other modules to consult.
try:
    import reality_gap as _reality_gap
    _RG_OK = True
except Exception as _e:
    _reality_gap = None
    _RG_OK = False
    print(f'[reality_gap] import failed (non-fatal): {_e}', flush=True)

# Shadow thresholds — silent variant evaluation, no LLM routing.
try:
    import shadow_thresholds as _shadow
    _SH_OK = True
except Exception as _e:
    _shadow = None
    _SH_OK = False
    print(f'[shadow] import failed (non-fatal): {_e}', flush=True)

# Top-K ensemble voter — uses stored top-K configs from regime_configs.py.
try:
    import ensemble_voter as _ensemble
    _EV_OK = True
except Exception as _e:
    _ensemble = None
    _EV_OK = False
    print(f'[ensemble] import failed (non-fatal): {_e}', flush=True)

# Funding rate signal — orthogonal standalone entry.
try:
    import funding_signal as _funding_sig
    _FS_OK = True
except Exception as _e:
    _funding_sig = None
    _FS_OK = False
    print(f'[funding_sig] import failed (non-fatal): {_e}', flush=True)

# Execution contract — enforces TP/SL exit hierarchy.
# All close() calls route through contract_close() with authorization check.
# Non-elite coins receive fallback TP/SL config.
try:
    import exec_contract as _contract
    _EC_OK = True
except Exception as _e:
    _contract = None
    _EC_OK = False
    print(f'[contract] CRITICAL: contract import failed: {_e}', flush=True)

# TF isolation — each TF trades independently; HTF bias gates/sizes LTF.
try:
    import tf_isolation as _tf_iso
    _TFI_OK = True
except Exception as _e:
    _tf_iso = None
    _TFI_OK = False
    print(f'[tf_isolation] import failed (non-fatal): {_e}', flush=True)

# Contract invariants — deadman check, entry/exit invariants, order persistence.
try:
    import invariants as _invariants
    _INV_OK = True
except Exception as _e:
    _invariants = None
    _INV_OK = False
    print(f'[invariants] import failed (non-fatal): {_e}', flush=True)

# Shadow trades — track rejected trades as if taken, for expectancy comparison.
try:
    import shadow_trades as _shadow_rej
    _SR_OK = True
except Exception as _e:
    _shadow_rej = None
    _SR_OK = False
    print(f'[shadow_trades] import failed (non-fatal): {_e}', flush=True)

# Promotion engine — controlled leak of edge rejections into live (A/B test).
try:
    import promotion_engine as _promo
    _PROMO_OK = True
except Exception as _e:
    _promo = None
    _PROMO_OK = False
    print(f'[promotion_engine] import failed (non-fatal): {_e}', flush=True)

# Enforce Protection — single atomic authority for TP/SL lifecycle.
try:
    import enforce_protection as _ep
    _EP_OK = True
except Exception as _e:
    _ep = None
    _EP_OK = False
    print(f'[enforce_protection] import failed (non-fatal): {_e}', flush=True)

# Pending experimental tags: coin -> tag dict. Populated in signal-reject path
# when a promotion is granted. Consumed at entry completion to register with
# promotion engine + annotate position metadata.
_EXPERIMENT_PENDING = {}

# Profit management — extend_winners + partial_exit_tp1.
# Uses exchange-side TP/SL modifications, not polling closes.
try:
    import profit_mgmt as _pm
    _PM_OK = True
except Exception as _e:
    _pm = None
    _PM_OK = False
    print(f'[profit_mgmt] import failed (non-fatal): {_e}', flush=True)

# ─────────────────────────────────────────────────────────
# STEP 1 LIFECYCLE REBUILD — ledger + snapshot layers
# These load but are NOT wired into behavior yet (Step 1 is prep only).
# Behavior activation happens in Step 3-4.
# ─────────────────────────────────────────────────────────
try:
    import trade_ledger as _ledger
    _LEDGER_OK = True
except Exception as _e:
    _ledger = None
    _LEDGER_OK = False
    print(f'[ledger] CRITICAL: trade_ledger import failed: {_e}', flush=True)

# Gates module — used to compute expected_edge_at_entry on every entry.
try:
    import gates as _gates
    _GATES_OK = True
except Exception as _e:
    _gates = None
    _GATES_OK = False
    print(f'[gates] import failed (non-fatal): {_e}', flush=True)

# Funding accrual — used at close to record signed funding cost on notional.
try:
    import funding_accrual as _funding_accrual
    _FA_OK = True
except Exception as _e:
    _funding_accrual = None
    _FA_OK = False
    print(f'[funding_accrual] import failed (non-fatal): {_e}', flush=True)


def _current_regime():
    """Best-effort current regime classification for entry tagging.
    Returns None on any failure — never blocks entry."""
    try:
        import regime_detector as _rd_h
        return _rd_h.get_regime()
    except Exception:
        return None


def _funding_for_close(trade_record):
    """Best-effort funding accrual for a closing trade. Returns signed pct on
    notional (positive = paid, negative = received), or None if uncomputable.
    Never raises — funding tracking failure must not block a close.
    """
    if not _FA_OK or _funding_accrual is None or not trade_record:
        return None
    try:
        coin = trade_record.get('coin', '')
        side = trade_record.get('side', '')
        ts_iso = trade_record.get('timestamp', '')
        if not coin or not side or not ts_iso:
            return None
        from datetime import datetime as _dt
        entry_ts = _dt.fromisoformat(str(ts_iso).replace('Z', '+00:00')).timestamp()
        pct, _src = _funding_accrual.compute_funding_paid_pct(
            coin, side, entry_ts, time.time())
        return pct
    except Exception:
        return None

try:
    import exchange_snapshot as _snapshot
    _SNAPSHOT_OK = True
except Exception as _e:
    _snapshot = None
    _SNAPSHOT_OK = False
    print(f'[snapshot] exchange_snapshot import failed: {_e}', flush=True)

# Final-form architecture: position state machine + order finality tracker.
# Both treat exchange events as the SOLE source of truth — no snapshot/LKG
# data ever drives state transitions.
try:
    import execution_state as _exec_state
    _EXEC_STATE_OK = True
except Exception as _e:
    _exec_state = None
    _EXEC_STATE_OK = False
    print(f'[exec_state] execution_state import failed: {_e}', flush=True)

try:
    import order_finality as _order_finality
    _ORDER_FINALITY_OK = True
except Exception as _e:
    _order_finality = None
    _ORDER_FINALITY_OK = False
    print(f'[order_finality] order_finality import failed: {_e}', flush=True)

# ─────────────────────────────────────────────────────────
# STEP 3 — Intent queue + authoritative reconciler
# Ships with RECONCILER_AUTHORITATIVE=0 default (observe mode).
# Flip to 1 via env var to enable sole-writer mode.
# ─────────────────────────────────────────────────────────
try:
    import intent_queue as _intents
    _INTENTS_OK = True
except Exception as _e:
    _intents = None
    _INTENTS_OK = False
    print(f'[intents] intent_queue import failed: {_e}', flush=True)

try:
    import lifecycle_reconciler as _reconciler
    _RECONCILER_OK = True
except Exception as _e:
    _reconciler = None
    _RECONCILER_OK = False
    print(f'[reconciler] lifecycle_reconciler import failed: {_e}', flush=True)

# Reflexivity detector — silent telemetry.
# Crowding + move position + reaction-to-reaction scoring at signal fire.
try:
    import reflexivity as _reflex
    _reflex.start_trim_daemon()
    _RX_OK = True
except Exception as _e:
    _reflex = None
    _RX_OK = False
    print(f'[reflex] import failed (non-fatal): {_e}', flush=True)

# ═══════════════════════════════════════════════════════
# REGIME-SIDE BLOCKER — global filter against regime-mismatched trades
# ═══════════════════════════════════════════════════════
# 48-trade post-mortem audit (2026-04-22 session): 44 of 48 entries were
# SHORTS in bull-calm regime with positive funding. Zero TP hits. WR 45.8%.
# Expectancy +0.013% per trade (statistical zero). Every post-mortem's
# root_cause called this out: "bull-calm + positive funding = short headwind."
#
# This is a hard global gate. It runs BEFORE the LLM entry gate and cannot
# be overridden by KB patterns or per-coin tuning. If the regime is against
# the trade direction AND funding confirms the regime, the trade is blocked.
#
# Set env REGIME_SIDE_BLOCK=0 to disable (not recommended).
_REGIME_BLOCK_ENABLED = os.environ.get('REGIME_SIDE_BLOCK', '0') != '0'
# 2026-04-22: default flipped '1' → '0'. The regime detector uses BTC 4h EMA
# with 3-bar hysteresis (~12h to flip regime). In intraday pullbacks from a
# prior bull regime, this still reports 'bull' and blocks SELL signals —
# forcing the bot to open only longs exactly when shorts would be correct.
# Observed effect: 14 longs stacked into a pullback, uPnL -$30.
# Disabled until Haiku regime arbiter is built (planned post-profit).
# MTF gate (mtf_context.aligned) still catches both-HTF opposition cases.
# Funding cutoff — only block when funding meaningfully confirms regime.
# HL perps baseline sits at +0.125 bps on most coins (exchange default).
# Cutoff at 0.0 blocked every short regardless of true positioning.
# Cutoff at 0.5 bps blocks only when funding is strongly positive (longs
# paying shorts = crowded long = don't fade). Baseline ±0.125 ambiguous
# = let MTF filter decide. Env: REGIME_FUNDING_CUTOFF_BPS=0.5 (default).
_REGIME_FUNDING_CUTOFF_BPS = float(os.environ.get('REGIME_FUNDING_CUTOFF_BPS', '0.5'))

def regime_blocks_side(coin, side):
    """Return (blocked, reason). side='BUY' or 'SELL'.
    Blocks SELL in bull-* regime when funding is positive.
    Blocks BUY in bear-* regime when funding is negative.
    Fail-open on any data error."""
    if not _REGIME_BLOCK_ENABLED:
        return False, ''
    try:
        import regime_detector
        regime = regime_detector.get_regime()
        if not regime:
            return False, ''
        try:
            funding_bps = get_funding_rate(coin) * 10000.0
        except Exception:
            funding_bps = 0.0
        if regime.startswith('bull') and side == 'SELL':
            if funding_bps > _REGIME_FUNDING_CUTOFF_BPS:
                return True, f'bull-regime({regime}) + funding +{funding_bps:.2f}bps blocks short'
        if regime.startswith('bear') and side == 'BUY':
            if funding_bps < -_REGIME_FUNDING_CUTOFF_BPS:
                return True, f'bear-regime({regime}) + funding {funding_bps:.2f}bps blocks long'
    except Exception as e:
        return False, ''
    return False, ''

# Max TP cap REMOVED 2026-04-22 — was a bandage for dust_sweep snipping
# winners at the noise level. With dust_sweep disabled and MTF confluence
# gating entries, trades now have time and structure to reach their natural
# per-coin TPs (often 4-6% = matching the 1:3+ R:R the setups warrant).
# If you need a cap, set env MAX_TP_PCT=0.02 etc.
# ═══════════════════════════════════════════════════════
# SURVIVAL GUARDS (2026-04-25)
# ═══════════════════════════════════════════════════════
# Three minimum-viable constraints to stop the system from giving back
# winners and stacking bad trades. Per user spec, no overhaul — just
# survival rails. Deploy as-is, observe 6-12h before any tuning.
#
# Guard 1: PROFIT LOCK (per-tick, all positions)
#   raw_move >= 2%  -> force close (lock)
#   raw_move >= 1%  -> move SL to breakeven
#   Uses RAW PRICE move, not leveraged, not PnL%. Tier-agnostic.
#
# Guard 2: REGIME FILTER (entry-time, in process())
#   bull regime + SHORT signal -> reject
#   bear regime + BUY signal   -> reject
#   Pure direction block, no funding cutoff. Stops the "8 shorts in calm-bull"
#   leak observed today.
#
# Guard 3: WR FILTER WITH BOOTSTRAP (entry-time)
#   On boot: load trades.csv, compute per-coin WR over last 20 closes.
#   If trade_count >= 5 and coin_wr < 0.40  -> reject
#   If trade_count <  5                      -> allow, log as 'unproven'
#
# Toggles via env (all default ON):
#   PROFIT_LOCK=0           -> disable Guard 1
#   REGIME_DIR_BLOCK=0      -> disable Guard 2
#   COIN_WR_FILTER=0        -> disable Guard 3
#
# Constants exposed for tuning post-observation:
PROFIT_LOCK_ENABLED   = os.environ.get('PROFIT_LOCK', '1') != '0'
PROFIT_LOCK_CLOSE_PCT = float(os.environ.get('PROFIT_LOCK_CLOSE_PCT', '0.035'))  # 3.5% raw
PROFIT_LOCK_BE_PCT    = float(os.environ.get('PROFIT_LOCK_BE_PCT',    '0.005'))  # 0.5% raw
# 2026-04-26 (later): CLOSE 0.02 → 0.035. The 2% force-close was killing
# winners at half their target. Confluence engines have TP=4%, so a trade
# that reached +2% MFE got force-closed before it could reach TP. Worse,
# the close fires at trigger-time market price, so after retrace the
# realized PnL was often near zero (saw NOT close at MFE=+2.1% / pnl=$0.00).
# 0.035 sits just below the 4% TP — catches truly extreme wins (e.g. 5%+
# spikes that would hit TP anyway) without capping confluence's realized
# profit. BE-lock at 0.5% still protects against round-trip-to-loss.
# Tunable via env without redeploy.

# ─── TRAIL SL LADDER ──────────────────────────────────────────
# 2026-04-26: BE-lock fires once at +0.5% then SL stays at entry+0.2%. If
# trade hits MFE 1.8% then retraces, it locks only +0.2% — capturing 11%
# of MFE. With trail ladder, SL chases MFE up in tiers, locking more as
# the trade extends. /analyze TP backtest at session end showed wins
# averaging $0.001-0.02 (capped by profit_lock-2% bug); this trail
# captures more of every winner.
#
# Each rung: (mfe_threshold, lock_pct above entry). Rungs are permanent
# (sl_trail_level state); SL never moves backward. Reuses
# modify_sl_to_breakeven with a larger buffer.
#
# Same trade pattern (MFE 1.8% then retrace):
#   Before: SL at entry+0.2% → realized +0.2% × $11 = $0.022
#   After:  SL at entry+0.8% → realized +0.8% × $11 = $0.088 (4x)
TRAIL_LADDER = [
    (0.010, 0.005),   # MFE 1.0% → SL to entry+0.5% (lock 0.5%) — NEW lower rung 2026-04-27
    (0.015, 0.008),   # MFE 1.5% → SL to entry+0.8% (lock 0.8%)
    (0.025, 0.015),   # MFE 2.5% → SL to entry+1.5% (lock 1.5%)
    (0.035, 0.025),   # MFE 3.5% → SL to entry+2.5% (lock 2.5%)
]
# 2026-04-27: added 1.0%/0.5% rung. /analyze MFE distribution showed 23%
# of trades reach 1.0% MFE but only 18% reach 1.5%. The 5pp band that
# previously round-tripped back to BE-lock (+0.2%) now locks +0.5% min.
# Captures incremental profit on the most common winner-pattern.

REGIME_DIR_BLOCK_ENABLED = os.environ.get('REGIME_DIR_BLOCK', '1') != '0'

COIN_WR_FILTER_ENABLED = os.environ.get('COIN_WR_FILTER', '1') != '0'
COIN_WR_FILTER_THRESH  = float(os.environ.get('COIN_WR_FILTER_THRESH', '0.35'))   # was 0.40
COIN_WR_FILTER_MIN_N   = int(os.environ.get('COIN_WR_FILTER_MIN_N', '10'))        # was 5
COIN_WR_FILTER_WINDOW  = int(os.environ.get('COIN_WR_FILTER_WINDOW', '20'))
# 2026-04-25: three-tier WR with time-decay forgiveness
# Hard reject only true losers (<20% WR). Soft-allow marginal coins (20-35%) at
# reduced size. Time-decay weights recent trades higher (7-day half-life), so
# old performance doesn't dominate forever. Edges decay and reappear; static
# bans become self-fulfilling. Per spec: "block only extreme losers; let
# marginal coins prove themselves at reduced risk."
COIN_WR_HARD_THRESH    = float(os.environ.get('COIN_WR_HARD_THRESH', '0.20'))    # <20% = hard reject
COIN_WR_SOFT_SIZE_MULT = float(os.environ.get('COIN_WR_SOFT_SIZE_MULT', '0.3'))  # 20-35% = size×0.3
COIN_WR_DECAY_DAYS     = float(os.environ.get('COIN_WR_DECAY_DAYS', '7.0'))      # 7d half-life

# Deadlock valve: if no entries for N seconds, bypass WR + regime filters
DEADLOCK_BYPASS_SEC = int(os.environ.get('DEADLOCK_BYPASS_SEC', '1800'))  # 30 min
_LAST_OPEN_TS = 0  # module-level — updated on every successful entry

def _deadlock_active():
    """True if no opens in last DEADLOCK_BYPASS_SEC. Triggers WR/regime bypass."""
    if _LAST_OPEN_TS == 0:
        return False  # never opened — let normal filters apply on cold start
    import time as _t
    return (_t.time() - _LAST_OPEN_TS) > DEADLOCK_BYPASS_SEC

# Bootstrap-loaded per-coin WR table. Populated once at startup from trades.csv.
# Schema: { 'COIN': {'n': int, 'wins': int, 'wr': float} }
_COIN_WR_BOOTSTRAP = {}
_COIN_WR_BOOTSTRAP_LOADED = False

def _bootstrap_coin_wr_from_ledger():
    """Read trades.csv and compute per-coin WR over the last N closes.
    Idempotent — safe to call multiple times. Best-effort: silently no-op
    if ledger or csv not available."""
    global _COIN_WR_BOOTSTRAP, _COIN_WR_BOOTSTRAP_LOADED
    if _COIN_WR_BOOTSTRAP_LOADED:
        return
    try:
        if not (_LEDGER_OK and _ledger):
            _COIN_WR_BOOTSTRAP_LOADED = True
            return
        # ledger path is /var/data/trades.csv
        csv_path = getattr(_ledger, 'LEDGER_PATH', '/var/data/trades.csv')
        if not os.path.exists(csv_path):
            _COIN_WR_BOOTSTRAP_LOADED = True
            return
        import csv as _csv
        per_coin_closes = {}  # coin -> list of (ts, won_bool)
        with open(csv_path, 'r') as f:
            reader = _csv.DictReader(f)
            for row in reader:
                if row.get('event_type') != 'CLOSE':
                    continue
                coin = (row.get('coin') or '').upper()
                if not coin:
                    continue
                pnl_raw = row.get('pnl', '')
                try:
                    pnl = float(pnl_raw) if pnl_raw not in (None, '', 'None') else None
                except Exception:
                    pnl = None
                if pnl is None:
                    continue  # skip phantom closes / unknown pnl
                won = pnl > 0
                ts = row.get('timestamp') or row.get('event_seq') or ''
                per_coin_closes.setdefault(coin, []).append((ts, won))
        for coin, closes in per_coin_closes.items():
            # Take last WINDOW closes (already in CSV order = chronological)
            window = closes[-COIN_WR_FILTER_WINDOW:]
            n = len(window)
            wins = sum(1 for _, w in window if w)
            wr = (wins / n) if n > 0 else 0.0
            # 2026-04-25: store full window for time-decay computation
            # Schema: closes = [(ts_str, won_bool), ...] in chronological order
            _COIN_WR_BOOTSTRAP[coin] = {'n': n, 'wins': wins, 'wr': wr, 'closes': window}
        _COIN_WR_BOOTSTRAP_LOADED = True
        log(f"[survival] coin_wr bootstrap loaded: {len(_COIN_WR_BOOTSTRAP)} coins from {csv_path}")
    except Exception as e:
        try:
            log(f"[survival] coin_wr bootstrap err: {e}")
        except Exception: pass
        _COIN_WR_BOOTSTRAP_LOADED = True  # don't retry forever

def _compute_decayed_wr(closes):
    """Time-weighted WR using exp(-age_days / DECAY_DAYS) decay.
    Recent trades count more; old trades fade. Returns (weighted_wr, weighted_n).
    closes = [(ts_str, won_bool), ...] in chronological order.
    Falls back to flat WR if timestamps unparseable."""
    import math as _math
    import time as _time
    if not closes:
        return 0.0, 0
    now_s = _time.time()
    sum_w = 0.0
    sum_wins = 0.0
    parseable = 0
    for ts_str, won in closes:
        # ts_str may be ISO-8601 or epoch — try epoch first, then ISO
        age_s = None
        try:
            age_s = now_s - float(ts_str)
        except (TypeError, ValueError):
            try:
                from datetime import datetime as _dt
                # Strip Z suffix if present
                ts_clean = ts_str.replace('Z', '+00:00') if ts_str else ''
                age_s = now_s - _dt.fromisoformat(ts_clean).timestamp()
            except Exception:
                pass
        if age_s is None or age_s < 0:
            continue
        parseable += 1
        age_days = age_s / 86400.0
        decay = _math.exp(-age_days / max(COIN_WR_DECAY_DAYS, 0.1))
        sum_w += decay
        if won:
            sum_wins += decay
    if parseable == 0 or sum_w == 0:
        # No parseable timestamps — fall back to flat WR
        n = len(closes)
        wins = sum(1 for _, w in closes if w)
        return (wins / n) if n > 0 else 0.0, n
    return sum_wins / sum_w, parseable


def _coin_wr_blocks_entry(coin):
    """Three-tier WR filter with time-decay forgiveness.
    Returns (blocked, size_mult, reason).
      - blocked: True only for true losers (decayed WR < 20%)
      - size_mult: 0.3 for marginal coins (20-35%), 1.0 otherwise
      - reason: tagged for unified REJECT/ALLOW log
    Defaults to allow on missing data ('unproven' tag).
    Per spec: "block only extreme losers; let marginal coins prove themselves."
    """
    if not COIN_WR_FILTER_ENABLED:
        return False, 1.0, ''
    if not _COIN_WR_BOOTSTRAP_LOADED:
        _bootstrap_coin_wr_from_ledger()
    rec = _COIN_WR_BOOTSTRAP.get(coin.upper())
    if not rec:
        return False, 1.0, 'unproven_no_data'
    n = rec['n']
    if n < COIN_WR_FILTER_MIN_N:
        return False, 1.0, f'unproven_n={n}'
    # Compute time-decayed WR using stored close-tuples
    closes = rec.get('closes', [])
    if closes:
        wr_dec, _ = _compute_decayed_wr(closes)
    else:
        # Pre-decay bootstrap data — fall back to flat WR
        wr_dec = rec['wr']
    # Tier 1: hard reject true losers
    if wr_dec < COIN_WR_HARD_THRESH:
        return True, 0.0, f'hard_loser/decayed_wr={wr_dec:.2%} < {COIN_WR_HARD_THRESH:.0%} (n={n})'
    # Tier 2: soft allow marginal at reduced size
    if wr_dec < COIN_WR_FILTER_THRESH:
        return False, COIN_WR_SOFT_SIZE_MULT, f'soft_allow_marginal/decayed_wr={wr_dec:.2%} (n={n}) size×{COIN_WR_SOFT_SIZE_MULT}'
    # Tier 3: full size
    return False, 1.0, f'wr_ok/decayed_wr={wr_dec:.2%} (n={n})'

def _regime_dir_blocks_entry(sig):
    """Pure direction block. bull*+SHORT or bear*+BUY -> reject.
    Returns (blocked, reason). Fail-open on any error."""
    if not REGIME_DIR_BLOCK_ENABLED:
        return False, ''
    try:
        import regime_detector
        regime = regime_detector.get_regime()
        if not regime:
            return False, ''
        # Normalize signal to BUY/SELL representation
        side = sig.upper() if isinstance(sig, str) else ''
        if regime.startswith('bull') and side in ('SELL', 'SHORT'):
            return True, f'regime={regime} blocks {side}'
        if regime.startswith('bear') and side in ('BUY', 'LONG'):
            return True, f'regime={regime} blocks {side}'
    except Exception:
        return False, ''
    return False, ''

def _profit_lock_check(coin, live_pos, mark_px):
    """Per-tick check on a single open position.
    Returns one of: 'force_close' | 'move_be' | None.
    raw_move = (mark - entry) / entry, signed by side.
    Uses RAW price move, NOT leveraged PnL% — consistent across leverage tiers."""
    if not PROFIT_LOCK_ENABLED:
        return None
    try:
        entry = float(live_pos.get('entry') or 0)
        if entry <= 0 or not mark_px:
            return None
        is_long = live_pos.get('size', 0) > 0
        raw_move = ((mark_px - entry) / entry) if is_long else ((entry - mark_px) / entry)
        if raw_move >= PROFIT_LOCK_CLOSE_PCT:
            return 'force_close'
        if raw_move >= PROFIT_LOCK_BE_PCT:
            return 'move_be'
    except Exception:
        return None
    return None

# ═══════════════════════════════════════════════════════
MAX_TP_PCT = float(os.environ.get('MAX_TP_PCT', '0') or 0)  # 0 = no cap

# R:R FLOOR — enforce minimum profit-to-risk ratio at entry time.
# 2026-04-25: 2.0 → 1.2. Global 2.0 was overriding empirical grid sweep configs.
# Coins like STRK/TRX/POLYX/LTC/NEAR have grid-validated TP/SL ratios in the
# 1.25-1.43 range (Wilson_lb≥50% AFTER fees+slippage at those exact ratios).
# Forcing 2.0 was rejecting statistically valid setups. New default 1.2 admits
# the data-validated band; per-coin can still override via percoin_configs key
# 'min_rr'. 1.2 > 1.0 still blocks actually-inverted setups.
MIN_RR = float(os.environ.get('MIN_RR', '1.2'))

# 2026-04-25: DEBUG MODE — force every trade to fixed $11 notional.
# Set FORCE_NOTIONAL_USD=11 in env to override all sizing logic (risk_mult,
# leverage, confluence, session scalers, etc.) and produce ~$11 trades
# regardless of computed size. ($11 not $10 — buffer above HL's $10 minimum
# for rounding + slippage so orders don't bounce.) Use for diagnostic phase
# only. Set to 0 to disable and resume normal sizing.
FORCE_NOTIONAL_USD = float(os.environ.get('FORCE_NOTIONAL_USD', '44'))
# 2026-04-27: 22 → 44 (kept). User: focus on conviction × volume, not
# notional — but $44 stays as a baseline since system is proven.
# The actual lever pulled today: CONF_MIN_DOMAINS gate (domain-coverage
# requirement) — every fire must span 2+ orthogonal data domains, not
# just 2+ correlated systems. That's the real high-conviction filter.

# 2026-04-26: per-coin blocklist for engines that have shown persistent
# losing patterns. Initially RSR (PIVOT engine, single -$0.45 event) and
# JTO (FUNDING_MR engine, single -$0.34 event). Both engines have high
# WR overall (70% and 86%) — only these specific coins drag them.
# Comma-separated. Override via env.
COIN_BLOCKLIST = {c.strip().upper() for c in os.environ.get('COIN_BLOCKLIST', 'RSR,JTO').split(',') if c.strip()}

# Multi-timeframe confluence — import new module (fail-soft if missing)
try:
    import mtf_context as _mtf
    _MTF_OK = True
except Exception as _e:
    _mtf = None
    _MTF_OK = False
    print(f'[mtf_context] import failed (non-fatal): {_e}', flush=True)

# MTF conviction sizing — scales up risk when HTFs strongly confirm the signal.
# Max multiplier applied to risk_mult. Default 2.5x means a perfect-confluence
# trade risks 2.5x more than a marginal one. Combined with existing conf_mult
# (0.5-2x) and adaptive WR mult, hard-capped at RISK_MULT_CEIL to prevent
# multipliers compounding into reckless sizing.
MTF_SIZE_MAX = float(os.environ.get('MTF_SIZE_MAX', '2.5'))
RISK_MULT_CEIL = float(os.environ.get('RISK_MULT_CEIL', '4.0'))  # absolute ceiling

# Clear-path boost — no verified wall between entry and TP = free run, boost size.
# Default 1.5x. Env override: CLEAR_PATH_BOOST=2.0 etc.
CLEAR_PATH_BOOST = float(os.environ.get('CLEAR_PATH_BOOST', '1.5'))

# ═══════════════════════════════════════════════════════
# TRADE LOG — persistent CSV for real WR tracking
# ═══════════════════════════════════════════════════════
TRADE_LOG = '/var/data/trades.csv'

# Per-coin kill-switch: disable a coin if rolling 10-trade WR < 35%
COIN_KILL_MIN_N = 10
COIN_KILL_WR_THRESHOLD = 0.35
COIN_KILL_COOLDOWN_SEC = 12 * 3600  # 12h

def coin_disabled(coin, state):
    k = state.get('coin_kill', {}).get(coin)
    if not k: return False
    return time.time() < k.get('until', 0)

def update_coin_wr(coin, win, state):
    h = state.setdefault('coin_hist', {}).setdefault(coin, [])
    h.append(1 if win else 0)
    if len(h) > COIN_KILL_MIN_N:
        h.pop(0)
    if len(h) >= COIN_KILL_MIN_N:
        wr = sum(h)/len(h)
        if wr < COIN_KILL_WR_THRESHOLD:
            state.setdefault('coin_kill', {})[coin] = {'until': time.time() + COIN_KILL_COOLDOWN_SEC, 'wr': wr}
            log(f"COIN KILL {coin}: rolling 10-trade WR {wr*100:.0f}% < {COIN_KILL_WR_THRESHOLD*100:.0f}% → disabled 12h")

def record_close(pos, coin, pnl_pct, state):
    """Record a closed trade. pnl_pct is already percent (e.g. -2.0 = -2%)."""
    if pnl_pct is None: return

    # EXPERIMENTAL: track outcome in promotion bucket if this was experimental
    if pos and pos.get('experimental') and _PROMO_OK and _promo is not None:
        try:
            sl_pct = pos.get('sl_pct', 0.025) * 100  # convert to pct
            pnl_r = (pnl_pct / sl_pct) if sl_pct > 0 else 0
            # Estimate pnl_usd from notional
            entry_px = pos.get('entry', 0)
            sz = pos.get('size', 0)
            notional = entry_px * sz if (entry_px and sz) else 0
            pnl_usd = (pnl_pct / 100) * notional
            _promo.record_outcome(coin, pnl_usd, pnl_r=pnl_r, outcome=pos.get('exit_reason', 'close'))
        except Exception as _pe:
            print(f'[precog] promo outcome err: {_pe}', flush=True)

    # INVARIANT #5: trade audit — record expected vs actual levels
    if _INV_OK and _invariants is not None:
        try:
            _invariants.audit_close(
                coin=coin,
                entry_price=pos.get('entry'),
                tp_pct=pos.get('tp_pct'),
                sl_pct=pos.get('sl_pct'),
                exit_price=pos.get('exit_price') or pos.get('exit_px'),
                exit_reason=pos.get('exit_reason', 'unknown'),
                pnl_pct=pnl_pct,
                side=pos.get('side', '?'),
            )
        except Exception:
            pass

    # Clamp to sanity range — SL caps at -2%, but leveraged wild fills can blow through
    pnl_pct = max(-10.0, min(50.0, float(pnl_pct)))
    win = pnl_pct > 0
    now = time.time()
    update_coin_wr(coin, win, state)
    stats = state.setdefault('stats', {
        'by_engine': {}, 'by_hour': {}, 'by_side': {}, 'by_coin': {},
        'by_conf': {}, 'total_wins': 0, 'total_losses': 0, 'total_pnl': 0.0
    })
    def bump(bucket_name, key):
        b = stats[bucket_name].setdefault(str(key), {'w':0,'l':0,'pnl':0.0})
        if win: b['w'] += 1
        else: b['l'] += 1
        b['pnl'] += pnl_pct  # already percent
    engine = pos.get('engine') or 'UNKNOWN'
    side   = pos.get('side','?')
    utc_h  = pos.get('utc_h', time.gmtime(now).tm_hour)
    conf   = pos.get('conf', 0)
    conf_bucket = '0-29' if conf<30 else '30-49' if conf<50 else '50-69' if conf<70 else '70+'
    bump('by_engine', engine)
    bump('by_hour',   utc_h)
    bump('by_side',   side)
    bump('by_coin',   coin)
    bump('by_conf',   conf_bucket)
    if win: stats['total_wins'] += 1
    else: stats['total_losses'] += 1
    stats['total_pnl'] += pnl_pct

    # ─────────────────────────────────────────────────────
    # POST-MORTEM TUNING — HL close path only.
    # Fire-and-forget. Runs in daemon thread. Never blocks trading.
    # MT4 closes go through mt4_trade_closed() which does NOT invoke this.
    # ─────────────────────────────────────────────────────
    if _POSTMORTEM_OK and _postmortem is not None:
        try:
            _postmortem.run_postmortem_async(pos, coin, pnl_pct)
        except Exception as _e:
            pass  # never let post-mortem crash the close path

    # ─────────────────────────────────────────────────────
    # MICROSTRUCTURE ANNOTATION — post-hoc classifier.
    # Records every close with momentum/absorption/fake-breakout labels.
    # Triggers discussion flag at 200 classified closes.
    # Non-blocking (runs in daemon thread).
    # ─────────────────────────────────────────────────────
    if _MS_OK and _microstructure is not None:
        try:
            _microstructure.annotate_close(
                coin=coin,
                side=pos.get('side', '?'),
                entry_price=float(pos.get('entry', 0) or 0),
                sl_price=float(pos.get('sl', 0) or 0),
                tp_price=float(pos.get('tp', 0) or 0),
                entry_ts=pos.get('ts', time.time()),
                pnl_pct=pnl_pct,
                engine=pos.get('engine'),
                regime=pos.get('regime'),
                conf=pos.get('conf'),
                wilson_lb=pos.get('wilson_lb'),
            )
        except Exception:
            pass  # never block close path

    # ─────────────────────────────────────────────────────
    # SIGNAL STATE OUTCOME LOG — pairs with signal_logger.log_state
    # to enable mutual information analysis post-hoc.
    # ─────────────────────────────────────────────────────
    if _SL_OK and _signal_logger is not None:
        try:
            _signal_logger.log_outcome(
                bar_ts=pos.get('bar_ts') or pos.get('ts'),
                coin=coin,
                pnl_pct=pnl_pct,
                win=pnl_pct > 0,
            )
        except Exception:
            pass

    # ─────────────────────────────────────────────────────
    # CONVEXITY OUTCOME LOG — updates tail-win stats per (coin, engine)
    # and pairs outcome with signal-time convexity score for later analysis.
    # ─────────────────────────────────────────────────────
    if _CX_OK and _convex is not None:
        try:
            _convex.log_outcome(
                coin=coin,
                engine=pos.get('engine'),
                pnl_pct=pnl_pct,
                win=pnl_pct > 0,
                max_favorable_excursion_pct=pos.get('mfe_pct'),
                tp_hit_pct=pos.get('tp_pct'),
                bar_ts=pos.get('bar_ts') or pos.get('ts'),
            )
        except Exception:
            pass

    # ─────────────────────────────────────────────────────
    # COUNTERFACTUAL ANALYSIS — replay alternatives on closed trade bars.
    # Delayed/skipped/resized/signal-removed simulations.
    # Non-blocking; writes regret metrics to /app/counterfactual.jsonl.
    # ─────────────────────────────────────────────────────
    if _CF_OK and _counterfactual is not None:
        try:
            _counterfactual.analyze_close(
                coin=coin,
                side=pos.get('side', '?'),
                entry_price=float(pos.get('entry', 0) or 0),
                tp_pct=float(pos.get('tp_pct') or pos.get('tp', 0.05)),
                sl_pct=float(pos.get('sl_pct') or pos.get('sl', 0.025)),
                entry_ts=pos.get('ts', time.time()),
                pnl_pct=pnl_pct,
                actual_size_pct=pos.get('risk_pct', 0.005),
                engine=pos.get('engine'),
                regime=pos.get('regime'),
            )
        except Exception:
            pass

    # Optimal inaction outcome log — pairs with signal abstain score.
    if _IA_OK and _inaction is not None:
        try:
            _inaction.log_outcome(
                coin=coin,
                engine=pos.get('engine'),
                pnl_pct=pnl_pct,
                win=pnl_pct > 0,
                bar_ts=pos.get('bar_ts') or pos.get('ts'),
                exit_reason=pos.get('exit_reason'),
            )
        except Exception:
            pass

    # Edge decay monitor — live alerts on WR drift / R:R compression / hold expansion.
    # No trading gate; log-only alerts at state transitions.
    if _ED_OK and _edge_decay is not None:
        try:
            _hold_sec = 0
            if pos.get('ts'):
                _hold_sec = time.time() - float(pos.get('ts'))
            _cur_reg = None
            try:
                import regime_detector as _rd
                _cur_reg = _rd.get_regime()
            except Exception: pass
            _edge_decay.record_close(
                coin=coin,
                engine=pos.get('engine'),
                regime=_cur_reg,
                pnl_pct=pnl_pct,
                hold_seconds=_hold_sec,
                win=pnl_pct > 0,
                exit_reason=pos.get('exit_reason'),
                regime_at_entry=pos.get('regime'),
                config_source=pos.get('_regime_source') or pos.get('config_source'),
            )
        except Exception:
            pass

    # Path dependency — live streak tracker. Modifies size_mult at next signal.
    if _PD_OK and _path_dep is not None:
        try:
            _cur_reg = None
            try:
                import regime_detector as _rd2
                _cur_reg = _rd2.get_regime()
            except Exception: pass
            _path_dep.record_close(
                pnl_pct=pnl_pct,
                win=pnl_pct > 0,
                regime=_cur_reg,
                bar_ts=pos.get('bar_ts') or pos.get('ts'),
            )
        except Exception:
            pass

    # Reality gap — backtest vs live audit.
    if _RG_OK and _reality_gap is not None:
        try:
            _cur_reg = None
            try:
                import regime_detector as _rd3
                _cur_reg = _rd3.get_regime()
            except Exception: pass
            _reality_gap.record_close(
                coin=coin,
                engine=pos.get('engine'),
                regime_at_entry=pos.get('regime'),
                regime_at_exit=_cur_reg,
                signal_close_price=pos.get('signal_close_price'),
                actual_fill_price=pos.get('entry'),
                pnl_pct=pnl_pct,
                win=pnl_pct > 0,
                configured_lev=pos.get('configured_lev') or pos.get('lev'),
                actual_lev=pos.get('actual_lev'),
                enterprise_oos_wr=pos.get('oos_wr') or pos.get('wr'),
                bar_ts=pos.get('bar_ts') or pos.get('ts'),
            )
        except Exception:
            pass

    # Shadow thresholds — join outcome with earlier eval for WR by variant.
    if _SH_OK and _shadow is not None:
        try:
            _shadow.log_outcome(
                coin=coin,
                bar_ts=pos.get('bar_ts') or pos.get('ts'),
                pnl_pct=pnl_pct,
                win=pnl_pct > 0,
            )
        except Exception:
            pass

    # ─────────────────────────────────────────────────────
    # REFLEXIVITY OUTCOME LOG — pairs with log_signal_score on close.
    # ─────────────────────────────────────────────────────
    if _RX_OK and _reflex is not None:
        try:
            _reflex.log_outcome(
                coin=coin,
                pnl_pct=pnl_pct,
                win=pnl_pct > 0,
                bar_ts=pos.get('bar_ts') or pos.get('ts'),
            )
        except Exception:
            pass

def wr_to_mult(wr, n, min_n=5):
    """Adaptive size multiplier based on rolling WR. Never returns 0 (never blocks).
    <40%: 0.4x | 40-55%: 0.7x | 55-70%: 1.0x | 70%+: 1.3x
    Not enough data (<min_n): 1.0x (neutral)."""
    if n < min_n: return 1.0
    if wr < 0.40: return 0.4
    if wr < 0.55: return 0.7
    if wr < 0.70: return 1.0
    return 1.3

def adaptive_mult(coin, side, state):
    """Compose multiplier from per-coin × per-hour × per-side stats."""
    stats = state.get('stats', {})
    mult = 1.0
    # Per-coin
    b = stats.get('by_coin', {}).get(coin)
    if b:
        n = b.get('w',0)+b.get('l',0)
        wr = b.get('w',0)/n if n else 0
        mult *= wr_to_mult(wr, n, min_n=10)
    # Per-hour
    utc_h = str(time.gmtime().tm_hour)
    b = stats.get('by_hour', {}).get(utc_h)
    if b:
        n = b.get('w',0)+b.get('l',0)
        wr = b.get('w',0)/n if n else 0
        mult *= wr_to_mult(wr, n, min_n=15)
    # Per-side
    side_key = 'L' if side=='BUY' else 'S'
    b = stats.get('by_side', {}).get(side_key)
    if b:
        n = b.get('w',0)+b.get('l',0)
        wr = b.get('w',0)/n if n else 0
        mult *= wr_to_mult(wr, n, min_n=20)
    # Clamp 0.3-1.5
    return max(0.3, min(1.5, mult))

def log_trade(engine, coin, direction, entry, pnl, source, sl_pct=None):
    import csv
    try:
        os.makedirs('/var/data', exist_ok=True)
        exists = os.path.exists(TRADE_LOG)
        with open(TRADE_LOG, 'a', newline='') as f:
            w = csv.writer(f)
            if not exists:
                w.writerow(['timestamp','engine','coin','direction','entry','pnl','source','sl_pct'])
            w.writerow([datetime.utcnow().isoformat(), engine, coin, direction, entry, pnl, source, sl_pct or ''])
    except Exception as e:
        pass  # don't crash on log failure


def _compute_engine_health():
    """Derive per-engine health label from check_calls + success_rate_pct.

    Returns dict like {'wall_exhaustion': 'healthy', 'funding_mr': 'warming_up'}.
    Labels:
      warming_up — fewer than 10 check_calls (insufficient data)
      healthy    — success_rate_pct >= 95
      degraded   — success_rate_pct 80-94
      failed     — success_rate_pct < 80
      err:<TYPE> — status() itself raised an exception (this is the kind of
                   silent failure mode the integrity-guards layer was built
                   to surface)
    """
    out = {}
    for name, mod in [('wall_exhaustion', wall_exhaustion),
                      ('wall_absorption', wall_absorption),
                      ('wall_bounce',     wall_bounce),
                      ('funding_mr',      funding_engine)]:
        try:
            st = mod.status() if hasattr(mod, 'status') else {}
            sr = st.get('success_rate_pct', 100.0)
            calls = st.get('check_calls', 0)
            errs  = st.get('errors', 0)
            if calls < 10:
                label = 'warming_up'
            elif sr >= 95:
                label = 'healthy'
            elif sr >= 80:
                label = 'degraded'
            else:
                label = 'failed'
            out[name] = {
                'label':            label,
                'check_calls':      calls,
                'errors':           errs,
                'success_rate_pct': sr,
            }
        except Exception as e:
            out[name] = {'label': f'err:{type(e).__name__}', 'detail': str(e)[:120]}
    return out

WALLET     = os.environ['HYPERLIQUID_ACCOUNT']
PRIV_KEY   = os.environ['HL_PRIVATE_KEY']
STATE_PATH = '/var/data/precog_state.json'
KILL_FILE  = '/var/data/KILL'

# ═══════════════════════════════════════════════════════
# WEBHOOK — receives DynaPro signals from TradingView
# ═══════════════════════════════════════════════════════
WEBHOOK_SECRET = os.environ.get('WEBHOOK_SECRET', 'precog_dynapro_2026')
WEBHOOK_QUEUE = Queue()
# ═══════════════════════════════════════════════════════
# MT4 SIGNAL ROUTING — DynaPro webhook → Pepperstone EA
# ═══════════════════════════════════════════════════════
# MT4 PER-TICKER GATES — to be populated by grid optimizer
# Same approach as HL: per-ticker gate configs optimize WR from 53-65% → 85%+
# Load MT4 per-ticker gates from grid optimizer results
try:
    import json as _json
    with open(os.path.join(os.path.dirname(__file__), 'mt4_ticker_gates.json')) as _f:
        MT4_TICKER_GATES = _json.load(_f)
except Exception:
    MT4_TICKER_GATES = {}
# v4.9: structural zone confluence (OB/FVG/key levels via Yahoo candles)
try:
    import zones as _zones
    ZONES_ENABLED = True
except Exception as _e:
    _zones = None
    ZONES_ENABLED = False
# ============================================================
# v4.10: MT4 pullback gate (Yahoo 5m candles)
# Signal must be near 1h EMA20 with cooled RSI to pass.
# This filters away signals that fire mid-move (chase trades).
# ============================================================
_pb_cache = {}  # {ticker: (ts, candles_5m)}
_pb_ttl = 180  # 3min cache
MT4_PULLBACK_ENABLED = os.environ.get('MT4_PULLBACK_ENABLED', 'true').lower() == 'true'
PB_EMA = 20
PB_PROXIMITY = 0.015   # within 1.5% of 1h EMA20 (1h candles wider than 5m)
PB_RSI_HI = 60         # BUY: RSI < this
PB_RSI_LO = 40         # SELL: RSI > this

_YMAP_PB = {
    'XAUUSD':'GC=F','XAGUSD':'SI=F','XPTUSD':'PL=F','XPDUSD':'PA=F',
    'SPOTCRUDE':'CL=F','SPOTBRENT':'BZ=F','NATGAS':'NG=F','COPPER':'HG=F',
    'CORN':'ZC=F','WHEAT':'ZW=F','SOYBEANS':'ZS=F','SUGAR':'SB=F','COFFEE':'KC=F',
    'US30':'^DJI','US500':'^GSPC','NAS100':'^NDX','US2000':'^RUT',
    'GER40':'^GDAXI','UK100':'^FTSE','JPN225':'^N225','HK50':'^HSI',
    'VIX':'^VIX','USDX':'DX-Y.NYB',
}

def _fetch_pb_candles(clean_ticker):
    """Fetch 1h candles from Yahoo (7 days, always enough for EMA20).
    Returns list of (ts_ms, o, h, l, c) or None.
    NOTE: switched from 5m to 1h because Yahoo 5m is sparse over weekends/gaps,
    and EMA20 on 1h is what pullback_signal actually needs anyway.
    """
    ysym = _YMAP_PB.get(clean_ticker)
    if not ysym:
        if len(clean_ticker) == 6 and clean_ticker.isalpha():
            ysym = f"{clean_ticker}=X"
        else:
            return None
    now = time.time()
    cached = _pb_cache.get(clean_ticker)
    if cached and (now - cached[0] < _pb_ttl):
        return cached[1]
    try:
        import urllib.request as _ur
        end_ts = int(now)
        start_ts = end_ts - 86400 * 7  # 7 days — robust across weekends
        url = f'https://query1.finance.yahoo.com/v8/finance/chart/{ysym}?period1={start_ts}&period2={end_ts}&interval=1h'
        req = _ur.Request(url, headers={'User-Agent':'Mozilla/5.0'})
        data = _json.loads(_ur.urlopen(req, timeout=5).read())
        r = data['chart']['result'][0]
        ts_arr = r.get('timestamp', [])
        q = r.get('indicators',{}).get('quote',[{}])[0]
        candles = []
        for i, t in enumerate(ts_arr):
            c = q['close'][i] if i < len(q.get('close',[])) else None
            if c is None: continue
            o = q['open'][i] if q.get('open') and q['open'][i] is not None else c
            h = q['high'][i] if q.get('high') and q['high'][i] is not None else c
            l = q['low'][i]  if q.get('low')  and q['low'][i]  is not None else c
            candles.append((t*1000, o, h, l, c))
        if len(candles) >= 25:
            _pb_cache[clean_ticker] = (now, candles)
            return candles
        return None
    except Exception as _e:
        return None

def _mt4_pullback_check(clean_ticker, direction):
    """Returns (passed: bool, reason: str, meta: dict). Non-blocking on data fetch fail.
    Uses 1h candles directly (v4.11). Computes EMA20 + RSI14 on 1h close."""
    if not MT4_PULLBACK_ENABLED: return True, 'pullback_disabled', {}
    candles = _fetch_pb_candles(clean_ticker)
    if not candles or len(candles) < PB_EMA + 3:
        return True, f'no_candles (got {len(candles) if candles else 0})', {'candles': len(candles) if candles else 0}
    closes = [c[4] for c in candles]
    # 1H EMA20
    k = 2 / (PB_EMA + 1)
    ema = sum(closes[:PB_EMA]) / PB_EMA
    for cv in closes[PB_EMA:]:
        ema = cv*k + ema*(1-k)
    last_c = closes[-1]
    if ema <= 0: return True, 'bad_ema', {}
    dist = abs(last_c - ema) / ema
    # RSI(14) on 1h close
    gains=[]; losses=[]
    for i in range(1, len(closes)):
        d = closes[i]-closes[i-1]
        gains.append(max(d,0)); losses.append(max(-d,0))
    pp = 14
    if len(gains) < pp: return True, 'insufficient_rsi', {'candles': len(candles)}
    ag = sum(gains[:pp])/pp; al = sum(losses[:pp])/pp
    for i in range(pp, len(gains)):
        ag = (ag*(pp-1)+gains[i])/pp
        al = (al*(pp-1)+losses[i])/pp
    rs = ag/al if al > 0 else 999
    rsi = 100 - 100/(1+rs)
    meta = {'dist_ema_pct': round(dist*100, 3), 'rsi1h': round(rsi, 1), 'ema1h': round(ema, 5), 'price': round(last_c, 5), 'candles': len(candles)}
    # Proximity check
    if dist > PB_PROXIMITY:
        return False, f'PB_FAR ({dist*100:.2f}% from EMA20, limit {PB_PROXIMITY*100:.1f}%)', meta
    # RSI cool check  
    if direction == 'BUY' and rsi >= PB_RSI_HI:
        return False, f'PB_RSI_HOT ({rsi:.0f} >= {PB_RSI_HI})', meta
    if direction == 'SELL' and rsi <= PB_RSI_LO:
        return False, f'PB_RSI_COLD ({rsi:.0f} <= {PB_RSI_LO})', meta
    return True, f'PB_OK (d={dist*100:.2f}% rsi={rsi:.0f})', meta

# ============================================================
# v4.10: OANDA fxOrderBook retail-sentiment fade gate
# Free data from OANDA fxlabs/positionbook CSV. Extreme retail
# positioning = contrarian signal.
# ============================================================
_oanda_cache = {}  # {pair: (ts, data)}
_oanda_ttl = 600  # 10min — data updates hourly
MT4_OANDA_ENABLED = os.environ.get('MT4_OANDA_ENABLED', 'true').lower() == 'true'
# v4.14: TradingView scanner symbol mapping (clean_ticker → (tv_symbol, endpoint))
# Endpoint determines which scanner API to hit (forex / cfd / america / global)
_TV_SYMBOL_MAP = {
    # Forex majors & crosses
    'EURUSD': ('FX_IDC:EURUSD','forex'), 'GBPUSD': ('FX_IDC:GBPUSD','forex'),
    'USDJPY': ('FX_IDC:USDJPY','forex'), 'USDCHF': ('FX_IDC:USDCHF','forex'),
    'USDCAD': ('FX_IDC:USDCAD','forex'), 'AUDUSD': ('FX_IDC:AUDUSD','forex'),
    'NZDUSD': ('FX_IDC:NZDUSD','forex'),
    'EURJPY': ('FX_IDC:EURJPY','forex'), 'GBPJPY': ('FX_IDC:GBPJPY','forex'),
    'EURGBP': ('FX_IDC:EURGBP','forex'), 'EURAUD': ('FX_IDC:EURAUD','forex'),
    'AUDJPY': ('FX_IDC:AUDJPY','forex'), 'CADJPY': ('FX_IDC:CADJPY','forex'),
    'CHFJPY': ('FX_IDC:CHFJPY','forex'),
    'AUDCAD': ('FX_IDC:AUDCAD','forex'), 'AUDCHF': ('FX_IDC:AUDCHF','forex'),
    'AUDNZD': ('FX_IDC:AUDNZD','forex'), 'CADCHF': ('FX_IDC:CADCHF','forex'),
    'EURCAD': ('FX_IDC:EURCAD','forex'), 'EURCHF': ('FX_IDC:EURCHF','forex'),
    'GBPAUD': ('FX_IDC:GBPAUD','forex'), 'GBPCHF': ('FX_IDC:GBPCHF','forex'),
    'GBPNZD': ('FX_IDC:GBPNZD','forex'), 'NZDCAD': ('FX_IDC:NZDCAD','forex'),
    'NZDJPY': ('FX_IDC:NZDJPY','forex'),
    # Metals
    'XAUUSD': ('OANDA:XAUUSD','cfd'),     'XAGUSD': ('TVC:SILVER','cfd'),
    'XPTUSD': ('TVC:PLATINUM','cfd'),     'XPDUSD': ('TVC:PALLADIUM','cfd'),
    # Energy
    'SPOTCRUDE': ('NYMEX:CL1!','global'), 'SPOTBRENT': ('ICEEUR:BRN1!','global'),
    'NATGAS': ('OANDA:NATGASUSD','cfd'),
    # Soft commodities & grains
    'COPPER': ('OANDA:XCUUSD','cfd'),
    'CORN': ('CBOT:ZC1!','global'),       'WHEAT': ('CBOT:ZW1!','global'),
    'SOYBEANS': ('CBOT:ZS1!','global'),
    'SUGAR': ('ICEUS:SB1!','global'),     'COFFEE': ('ICEUS:KC1!','global'),
    # Indices
    'US30': ('OANDA:US30USD','cfd'),      'US500': ('SP:SPX','cfd'),
    'NAS100': ('NASDAQ:NDX','america'),   'US2000': ('TVC:RUT','cfd'),
    'GER40': ('OANDA:DE30EUR','cfd'),     'UK100': ('TVC:UKX','cfd'),
    'JPN225': ('TVC:NI225','cfd'),        'HK50': ('OANDA:HK33HKD','cfd'),
    # Volatility & dollar index
    'VIX': ('CBOE:VIX','cfd'),            'USDX': ('TVC:DXY','cfd'),
}

OANDA_PAIRS = {
    'EURUSD':'EUR_USD','GBPUSD':'GBP_USD','USDJPY':'USD_JPY','USDCHF':'USD_CHF',
    'USDCAD':'USD_CAD','AUDUSD':'AUD_USD','NZDUSD':'NZD_USD',
    'EURJPY':'EUR_JPY','GBPJPY':'GBP_JPY','EURGBP':'EUR_GBP','EURAUD':'EUR_AUD',
    'AUDJPY':'AUD_JPY','CADJPY':'CAD_JPY','CHFJPY':'CHF_JPY',
    'AUDCAD':'AUD_CAD','AUDCHF':'AUD_CHF','AUDNZD':'AUD_NZD',
    'CADCHF':'CAD_CHF','EURCAD':'EUR_CAD','EURCHF':'EUR_CHF',
    'GBPAUD':'GBP_AUD','GBPCHF':'GBP_CHF','GBPNZD':'GBP_NZD',
    'NZDCAD':'NZD_CAD','NZDJPY':'NZD_JPY',
    'XAUUSD':'XAU_USD','XAGUSD':'XAG_USD',
    'SPOTCRUDE':'WTICO_USD','SPOTBRENT':'BCO_USD','NATGAS':'NATGAS_USD',
    'US30':'US30_USD','US500':'SPX500_USD','NAS100':'NAS100_USD',
    'UK100':'UK100_GBP','GER40':'DE30_EUR','JPN225':'JP225_USD',
}

def _fetch_oanda_sentiment(clean_ticker):
    """Fetch market sentiment. Returns tagged tuple or None:
    - ('tv', recommend_all) where recommend_all ∈ [-1, +1] (strong sell→strong buy)
    - (long_pct, short_pct) from MyFXBook/DailyFX retail positioning

    Sources tried in order:
    1. TradingView Scanner API (tech-indicator confluence) — works from cloud IPs
    2. MyFXBook community outlook (retail positioning) — often blocked on cloud
    3. DailyFX sentiment feed (retail positioning) — fallback

    Never blocks trades; returns None if all sources fail.
    """
    pair = OANDA_PAIRS.get(clean_ticker)
    if not pair: return None
    now = time.time()
    cached = _oanda_cache.get(pair)
    if cached and (now - cached[0] < _oanda_ttl):
        return cached[1]
    import urllib.request as _ur
    import re as _re

    # Source 1: TradingView Scanner (PRIMARY — works from cloud)
    tv_sym, tv_ep = _TV_SYMBOL_MAP.get(clean_ticker, (None, None))
    if tv_sym and tv_ep:
        try:
            url = f'https://scanner.tradingview.com/{tv_ep}/scan'
            payload = _json.dumps({
                "symbols":{"tickers":[tv_sym],"query":{"types":[]}},
                "columns":["Recommend.All"]
            }).encode()
            req = _ur.Request(url, data=payload, headers={
                'User-Agent':'Mozilla/5.0','Content-Type':'application/json'
            })
            resp = _ur.urlopen(req, timeout=4)
            data = _json.loads(resp.read())
            if data.get('totalCount', 0) > 0:
                rec_all = data['data'][0]['d'][0]
                if rec_all is not None:
                    # Clamp to [-1, +1]
                    rec_all = max(-1.0, min(1.0, float(rec_all)))
                    result = ('tv', rec_all)
                    _oanda_cache[pair] = (now, result)
                    return result
        except Exception:
            pass

    # Source 2: MyFXBook community outlook (retail positioning — often blocked on cloud)
    pair_url = pair.replace("_","")  # EUR_USD → EURUSD
    try:
        url = f'https://www.myfxbook.com/community/outlook/{pair_url}'
        req = _ur.Request(url, headers={
            'User-Agent':'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Version/15.6.1 Safari/605.1.15',
            'Accept':'text/html,application/xhtml+xml,application/xml;q=0.9',
            'Accept-Language':'en-US,en;q=0.5',
            'DNT':'1',
            'Connection':'keep-alive',
            'Upgrade-Insecure-Requests':'1',
        })
        resp = _ur.urlopen(req, timeout=5)
        html = resp.read().decode('utf-8', errors='ignore')
        m_short = _re.search(r'Short[^\d]*(\d+)\s*%', html)
        m_long  = _re.search(r'Long[^\d]*(\d+)\s*%', html)
        if m_short and m_long:
            long_pct = float(m_long.group(1))
            short_pct = float(m_short.group(1))
            if 0 < long_pct < 100 and 0 < short_pct < 100:
                result = (long_pct, short_pct)
                _oanda_cache[pair] = (now, result)
                return result
    except Exception:
        pass

    # Source 3: DailyFX retail positioning feed
    try:
        url2 = 'https://www.dailyfx.com/api/market-overview/sentiment'
        req = _ur.Request(url2, headers={
            'User-Agent':'Mozilla/5.0',
            'Accept':'application/json',
        })
        resp = _ur.urlopen(req, timeout=4)
        data = _json.loads(resp.read())
        for item in data.get('data', []):
            symbol = item.get('symbol','').replace('/','').upper()
            if symbol == pair_url:
                long_pct = item.get('longPercent') or item.get('long_pct')
                short_pct = item.get('shortPercent') or item.get('short_pct')
                if long_pct and short_pct:
                    result = (float(long_pct), float(short_pct))
                    _oanda_cache[pair] = (now, result)
                    return result
    except Exception:
        pass
    return None

_sent_log_throttle = {}  # {ticker: ts} — avoid log spam

def _mt4_sentiment_mult(clean_ticker, direction):
    """Returns size multiplier based on market sentiment.

    Handles two data formats from _fetch_oanda_sentiment:
    1. ('tv', recommend_all) — TradingView tech-indicator confluence ∈ [-1, +1]
       ALIGN with consensus → boost (confluence trade)
       COUNTER to consensus → reduce (fighting the tape)
    2. (long_pct, short_pct) — MyFXBook/DailyFX retail positioning
       Contrarian fade: extreme crowd long → don't BUY, don't SELL against extreme short

    Returns 1.0 if no data (never blocks).
    """
    if not MT4_OANDA_ENABLED: return 1.0
    data = _fetch_oanda_sentiment(clean_ticker)
    if not data:
        _now = time.time()
        if _now - _sent_log_throttle.get(clean_ticker, 0) > 60:
            _sent_log_throttle[clean_ticker] = _now
            log(f"SENT no_data for {clean_ticker} (all sources failed)")
        return 1.0

    # Format 1: TradingView tech consensus (CONFLUENCE logic — align with tape)
    if isinstance(data, tuple) and len(data) == 2 and data[0] == 'tv':
        rec = data[1]  # -1 strong sell ... +1 strong buy
        label = ('STRONG_BUY' if rec >= 0.5 else 'BUY' if rec >= 0.1
                 else 'STRONG_SELL' if rec <= -0.5 else 'SELL' if rec <= -0.1
                 else 'NEUTRAL')
        log(f"SENT TV {clean_ticker} {direction}: rec={rec:+.2f} ({label})")
        if direction == 'BUY':
            if rec >= 0.5:  return 1.5   # strong align with tape
            if rec >= 0.25: return 1.3
            if rec >= 0.1:  return 1.15
            if rec <= -0.5: return 0.5   # strong counter-trend
            if rec <= -0.25: return 0.7
            if rec <= -0.1: return 0.85
            return 1.0
        else:  # SELL
            if rec <= -0.5:  return 1.5
            if rec <= -0.25: return 1.3
            if rec <= -0.1:  return 1.15
            if rec >= 0.5:   return 0.5
            if rec >= 0.25:  return 0.7
            if rec >= 0.1:   return 0.85
            return 1.0

    # Format 2: Retail positioning % (CONTRARIAN fade at extremes)
    long_pct, short_pct = data
    log(f"SENT RETAIL {clean_ticker} {direction}: long={long_pct:.0f}% short={short_pct:.0f}%")
    if long_pct + short_pct == 0: return 1.0
    long_frac = long_pct / (long_pct + short_pct)
    if direction == 'BUY':
        if long_frac >= 0.80: return 0.5
        if long_frac >= 0.65: return 0.75
        if long_frac <= 0.30: return 1.3
        if long_frac <= 0.20: return 1.5
    else:
        if long_frac <= 0.20: return 0.5
        if long_frac <= 0.35: return 0.75
        if long_frac >= 0.70: return 1.3
        if long_frac >= 0.80: return 1.5
    return 1.0

MT4_QUEUE = []  # EA polls /mt4/signals every 10s
# v4.15: live PnL feedback from EA v5 trade-closed reports
MT4_CLOSED_RING = []
MT4_LIVE_STATS = {}
MT4_TICKET_META = {}
try:
    import os as _os_stats
    if _os_stats.path.exists('/var/data/mt4_stats.json'):
        with open('/var/data/mt4_stats.json') as _f:
            _saved = _json.load(_f)
            MT4_LIVE_STATS = _saved.get('stats', {})
except Exception: pass
MT4_BIAS = {'direction': '', 'ts': 0}

# --- MT4 queue persistence (HL-isolated; writes /var/data/mt4_queue.json) ---
MT4_QUEUE_FILE = '/var/data/mt4_queue.json'
MT4_STALE_SEC = 30   # v4.9: aggressive stale drop — signals older than 30s dropped

# ===== Webhook filter (HL-isolated) =====
MT4_FILTERS_ENABLED = os.environ.get('MT4_FILTERS_ENABLED', 'true').lower() == 'true'
MT4_COOLDOWN_SEC = 15 * 60  # 15min per-ticker cooldown
MT4_ATR_MIN_PCT = 0.08      # reject dead market
MT4_ATR_MAX_PCT = 2.50      # reject news spike
_mt4_last_signal = {}       # {clean_ticker: ts_seconds}
_mt4_atr_cache = {}         # {clean_ticker: (ts, atr_pct)}
_mt4_atr_cache_ttl = 600    # 10min TTL on ATR

def _mt4_atr_pct(clean_ticker):
    """Fetch 14-period ATR% via Yahoo. Returns None on failure (filter passes through)."""
    now = time.time()
    cached = _mt4_atr_cache.get(clean_ticker)
    if cached and (now - cached[0] < _mt4_atr_cache_ttl):
        return cached[1]
    YAHOO_MAP = {
        'XAUUSD':'GC=F','XAGUSD':'SI=F','XPTUSD':'PL=F','XPDUSD':'PA=F',
        'SPOTCRUDE':'CL=F','SPOTBRENT':'BZ=F','NATGAS':'NG=F',
        'COPPER':'HG=F','CORN':'ZC=F','WHEAT':'ZW=F','SOYBEANS':'ZS=F',
        'EURUSD':'EURUSD=X','GBPUSD':'GBPUSD=X','USDJPY':'JPY=X',
        'EURGBP':'EURGBP=X','GBPNZD':'GBPNZD=X','AUDCAD':'AUDCAD=X',
        'AUDUSD':'AUDUSD=X','USDCAD':'CAD=X','USDCHF':'CHF=X',
        'AUDCHF':'AUDCHF=X','AUDNZD':'AUDNZD=X','AUDJPY':'AUDJPY=X',
        'CADCHF':'CADCHF=X','CADJPY':'CADJPY=X','CHFJPY':'CHFJPY=X',
        'EURAUD':'EURAUD=X','EURCAD':'EURCAD=X','EURCHF':'EURCHF=X',
        'GBPAUD':'GBPAUD=X','GBPCHF':'GBPCHF=X','NZDUSD':'NZDUSD=X',
        'NZDCAD':'NZDCAD=X','NAS100':'^NDX','US30':'^DJI','US500':'^GSPC',
        'US2000':'^RUT','GER40':'^GDAXI','UK100':'^FTSE',
        'JPN225':'^N225','HK50':'^HSI','VIX':'^VIX',
    }
    ysym = YAHOO_MAP.get(clean_ticker)
    if not ysym:
        return None
    try:
        import urllib.request as _ur
        end_ts = int(now)
        start_ts = end_ts - 86400 * 3  # 3 days back
        url = f'https://query1.finance.yahoo.com/v8/finance/chart/{ysym}?period1={start_ts}&period2={end_ts}&interval=1h'
        req = _ur.Request(url, headers={'User-Agent':'Mozilla/5.0'})
        resp = _ur.urlopen(req, timeout=5)
        data = _json.loads(resp.read())
        result = data.get('chart',{}).get('result',[{}])[0]
        q = result.get('indicators',{}).get('quote',[{}])[0]
        highs = [h for h in q.get('high',[]) if h is not None]
        lows = [l for l in q.get('low',[]) if l is not None]
        closes = [c for c in q.get('close',[]) if c is not None]
        if len(closes) < 15:
            return None
        # ATR14 on most recent 14 bars
        trs = []
        for i in range(len(closes)-14, len(closes)):
            if i <= 0: continue
            tr = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
            trs.append(tr)
        if not trs:
            return None
        atr = sum(trs) / len(trs)
        atr_pct = (atr / closes[-1]) * 100
        _mt4_atr_cache[clean_ticker] = (now, atr_pct)
        return atr_pct
    except Exception as _e:
        return None

# v4.8: Full per-ticker gate pipeline (from grid optimization)
# Supports: invert, trail params, SL, session, VIX buckets, anchor correlation,
# RSI, counter-trend fade, time cut, hour block, VIX overlay (sentiment)

# === VIX sentiment cache ===
_vix_cache = {'ts': 0, 'value': None}
_vix_ttl = 300  # 5min

def _get_vix():
    now = time.time()
    if _vix_cache.get('value') is not None and (now - _vix_cache['ts'] < _vix_ttl):
        return _vix_cache['value']
    try:
        import urllib.request as _ur
        url = 'https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX?interval=1h&range=1d'
        req = _ur.Request(url, headers={'User-Agent':'Mozilla/5.0'})
        data = _json.loads(_ur.urlopen(req, timeout=5).read())
        r = data['chart']['result'][0]
        closes = [c for c in r['indicators']['quote'][0].get('close',[]) if c is not None]
        if not closes: return None
        v = closes[-1]
        _vix_cache['ts'] = now
        _vix_cache['value'] = v
        return v
    except Exception:
        return None

def _vix_regime(v):
    if v is None: return 'unknown'
    if v < 15: return 'complacent'
    if v < 25: return 'normal'
    if v < 35: return 'elevated'
    if v < 50: return 'panic'
    return 'crisis'

# === Anchor asset cache (for correlation align filter) ===
_anchor_cache = {}  # {symbol: (ts, [closes])}
_anchor_ttl = 600  # 10min

_ANCHOR_MAP = {
    # Ticker -> anchor symbol (Yahoo)
    'EURAUD': ['EURUSD=X','AUDUSD=X'],
    'GBPNZD': ['GBPUSD=X','NZDUSD=X'],
    'GER40': ['^GSPC','GC=F'],
    'US500': ['^NDX','^DJI'],
    'XAUUSD': ['SI=F','DX-Y.NYB'],
    'XAGUSD': ['GC=F','DX-Y.NYB'],
    'XPTUSD': ['GC=F','SI=F'],
    'XPDUSD': ['GC=F','SI=F'],
    'SPOTCRUDE': ['BZ=F','DX-Y.NYB'],
    'SPOTBRENT': ['CL=F','DX-Y.NYB'],
    'NATGAS': ['CL=F'],
    'COPPER': ['^GSPC','GC=F'],
    'CORN': ['ZW=F','ZS=F'],
    'WHEAT': ['ZC=F','ZS=F'],
    'SOYBEANS': ['ZW=F','ZC=F'],
    'NAS100': ['^GSPC','^DJI'],
    'US30': ['^GSPC','^NDX'],
    'US2000': ['^GSPC','^DJI'],
    'UK100': ['^GSPC','^GDAXI'],
    'JPN225': ['^GSPC','JPY=X'],
    'HK50': ['^GSPC','^N225'],
    'SUGAR': ['KC=F'],
    'COFFEE': ['SB=F'],
}

def _fetch_anchor_6h_change(ticker):
    """Returns % change of anchor asset over last 6 hours, None if unavailable."""
    anchors = _ANCHOR_MAP.get(ticker.upper(), [])
    if not anchors: return None
    now = time.time()
    for anc in anchors:
        cached = _anchor_cache.get(anc)
        if cached and (now - cached[0] < _anchor_ttl):
            closes = cached[1]
            if len(closes) >= 7:
                return (closes[-1] - closes[-7]) / closes[-7] * 100 if closes[-7] > 0 else None
            continue
        try:
            import urllib.request as _ur
            url = f'https://query1.finance.yahoo.com/v8/finance/chart/{anc}?interval=1h&range=1d'
            req = _ur.Request(url, headers={'User-Agent':'Mozilla/5.0'})
            data = _json.loads(_ur.urlopen(req, timeout=5).read())
            r = data['chart']['result'][0]
            closes = [c for c in r['indicators']['quote'][0].get('close',[]) if c is not None]
            if len(closes) >= 7:
                _anchor_cache[anc] = (now, closes)
                return (closes[-1] - closes[-7]) / closes[-7] * 100 if closes[-7] > 0 else None
        except Exception:
            continue
    return None

# === Per-ticker gate filter pipeline ===
def _pass_session(hour_utc, sf):
    if sf == 'all' or not sf: return True
    if sf == 'london_only': return 7 <= hour_utc < 12
    if sf == 'london_ny': return 7 <= hour_utc < 17
    if sf == 'london_ny_pm': return 7 <= hour_utc < 21
    if sf == 'ny_only': return 12 <= hour_utc < 17
    if sf == 'ny_pm_only': return 17 <= hour_utc < 21
    if sf == 'asia_only': return hour_utc < 7 or hour_utc >= 21
    if sf == 'skip_asia': return 7 <= hour_utc < 21
    return True

def _pass_vix(v, b):
    if v is None or b in (None, 'any','none'): return True
    if b == 'sub15': return v < 15
    if b == 'over15': return v > 15
    if b == 'over18' or b == 'over18_only': return v > 18
    if b == 'over20': return v > 20
    if b == 'over25': return v > 25
    if b == 'over30': return v > 30
    if b == '15to25' or b == 'normal_only': return 15 <= v <= 25
    if b == '15to20': return 15 <= v <= 20
    if b == '20to25': return 20 <= v <= 25
    if b == 'skip_high':
        regime = _vix_regime(v)
        return regime not in ('panic','crisis')
    if b == 'skip_low':
        return _vix_regime(v) != 'complacent'
    if b == 'only_elevated':
        return _vix_regime(v) in ('elevated','normal')
    return True

def _pass_anchor(ticker, direction, af):
    if not af or af == 'none': return True
    move = _fetch_anchor_6h_change(ticker)
    if move is None: return True  # fail open if anchor unavailable
    sig_bull = direction.upper() == 'BUY'
    if af in ('align_6h','align_3h'):
        return (sig_bull and move > 0) or (not sig_bull and move < 0)
    if af in ('counter_6h','counter_3h'):
        return (sig_bull and move < 0) or (not sig_bull and move > 0)
    return True

def _pass_rsi(r, b):
    if r is None or b in (None, 'any','none'): return True
    if b == 'rsi_under30': return r < 30
    if b == 'rsi_30_70': return 30 <= r <= 70
    if b == 'rsi_over70': return r > 70
    if b == 'rsi_under50': return r < 50
    if b == 'rsi_over50': return r > 50
    if b == 'rsi_40_60': return 40 <= r <= 60
    return True

def _pass_hour_block(hour_utc, hb):
    if not hb or hb == 'any': return True
    if hb == 'skip_dst_rollover': return not (21 <= hour_utc < 24)
    return True

def _mt4_filter_pass(clean_ticker, direction='BUY'):
    # v4.19: per-ticker kill switch — disabled tickers never trade
    _gate_check = MT4_TICKER_GATES.get(clean_ticker, {})
    if not _gate_check.get('enabled', True):
        return False, f"DISABLED ({_gate_check.get('disabled_reason','manual_kill')})"
    """v4.8 full per-ticker gate pipeline.
    Returns (passed: bool, reason: str). Reason is 'ok' on pass.
    Never drops — filters per-ticker using MT4_TICKER_GATES config.
    """
    if not MT4_FILTERS_ENABLED:
        return True, 'filters_disabled'
    t = clean_ticker.upper()
    gate = MT4_TICKER_GATES.get(t, {})
    # Disabled (VIX sentiment-only)
    if gate.get('enabled') is False:
        return False, 'DISABLED_GATE'
    now = time.time()
    import datetime as _dt
    hour_utc = _dt.datetime.utcnow().hour
    # Session
    sf = gate.get('session_filter', 'all')
    if not _pass_session(hour_utc, sf):
        return False, f'SESSION ({sf} h={hour_utc})'
    # Hour block
    hb = gate.get('hour_block', 'any')
    if not _pass_hour_block(hour_utc, hb):
        return False, f'HOUR_BLOCK ({hb})'
    # Per-ticker cooldown
    cooldown_sec = gate.get('cooldown_sec', 900)
    last = _mt4_last_signal.get(clean_ticker)
    if cooldown_sec > 0 and last and (now - last) < cooldown_sec:
        return False, f'COOLDOWN ({int((now-last)/60)}min)'
    # ATR
    atr_min = gate.get('atr_min', 0.0)
    atr_max = gate.get('atr_max', 999.0)
    atr = _mt4_atr_pct(clean_ticker)
    if atr is not None:
        if atr_min > 0 and atr < atr_min:
            return False, f'ATR_LOW ({atr:.2f}% < {atr_min})'
        if atr_max < 999 and atr > atr_max:
            return False, f'ATR_HIGH ({atr:.2f}% > {atr_max})'
    # VIX filter
    vf = gate.get('vix_filter', 'any')
    if vf not in ('any','none', None):
        vix = _get_vix()
        if not _pass_vix(vix, vf):
            return False, f'VIX_FILTER ({vf}, vix={vix})'
    # Anchor alignment
    af = gate.get('anchor_align', 'none')
    if af and af != 'none':
        if not _pass_anchor(t, direction, af):
            return False, f'ANCHOR_{af.upper()}'
    # RSI filter (requires ATR fetch to have populated; best-effort)
    # (RSI fetched separately, skipping for now — filter passes unless gate requires)
    return True, 'ok'

def _mt4_daily_pnl_pct():
    """v4.21: rolling today's total PnL % across all MT4 tickers.
    Reads MT4_CLOSED_RING, sums exit_pct for trades closed in last 24h.
    Used for daily drawdown kill switch.
    """
    import datetime
    now = time.time()
    cutoff_today_utc = now - (now % 86400)  # start of UTC day
    total = 0.0
    for r in MT4_CLOSED_RING:
        if r['ts'] >= cutoff_today_utc:
            total += float(r.get('exit_pct', 0))
    return total

MT4_DAILY_DD_LIMIT = -9999.0  # v4.22: DISABLED — user directive: no kill switches

def _mt4_live_wr_mult(clean_ticker):
    """v4.21: adaptive sizing from live WR. Reads last 20 trades from MT4_LIVE_STATS.
    Returns size multiplier:
      - WR >= 65% and PF >= 1.5 over 20+ trades → 1.3x (hot streak, scale up)
      - WR 55-64% or PF 1.1-1.5 → 1.1x (decent)
      - WR 45-54% or PF 0.9-1.1 → 1.0x (neutral)
      - WR 35-44% or PF 0.6-0.9 → 0.7x (cold, scale down)
      - WR < 35% or PF < 0.6 → 0.4x (very cold, barely size)
    With < 5 trades returns 1.0 (insufficient data).
    Trades counted: only last 20 entries in recent[] ring.
    """
    ss = MT4_LIVE_STATS.get(clean_ticker)
    if not ss or not ss.get('recent'): return 1.0
    recent = ss['recent'][-20:]
    n = len(recent)
    if n < 5: return 1.0  # insufficient sample
    wins = sum(1 for r in recent if r['pnl'] > 0)
    losses = n - wins
    wr = wins / n * 100.0
    gross_wins = sum(r['pnl'] for r in recent if r['pnl'] > 0)
    gross_losses = abs(sum(r['pnl'] for r in recent if r['pnl'] <= 0))
    pf = gross_wins / gross_losses if gross_losses > 0 else 9.0
    # Combined gating
    if wr >= 65 and pf >= 1.5: mult = 1.3
    elif wr >= 55 or pf >= 1.1: mult = 1.1
    elif wr >= 45 or pf >= 0.9: mult = 1.0
    elif wr >= 35 or pf >= 0.6: mult = 0.7
    else: mult = 0.4
    log(f"MT4 LIVE_WR {clean_ticker}: n={n} wr={wr:.0f}% pf={pf:.2f} mult={mult}")
    return mult

def _mt4_max_spread_for(clean_ticker):
    """Per-instrument-class max spread % for EA spread gate.
    Pepperstone typical spreads (points / base price * 100):
    - FX majors: 0.01-0.05%
    - FX crosses (NZDJPY, GBPCAD): 0.05-0.12%
    - Gold/Silver: 0.05-0.25%
    - Platinum/Palladium: 0.3-0.8% (wide due to low liquidity)
    - Oil: 0.15-0.35%
    - Indices: 0.05-0.30%
    - Exotics: 0.20-0.50%
    Return value is CEILING; EA rejects if live spread exceeds this.
    """
    # Tight majors
    if clean_ticker in {'EURUSD','GBPUSD','USDJPY','AUDUSD','USDCAD','USDCHF','NZDUSD'}:
        return 0.05
    # FX crosses
    if clean_ticker in {'EURJPY','GBPJPY','EURGBP','EURAUD','AUDJPY','CADJPY','CHFJPY',
                        'AUDCAD','AUDCHF','AUDNZD','CADCHF','EURCAD','EURCHF',
                        'GBPAUD','GBPCHF','GBPNZD','NZDCAD','NZDJPY'}:
        return 0.12
    # Gold / Silver
    if clean_ticker in {'XAUUSD','XAGUSD'}:
        return 0.25
    # Platinum / Palladium (low liquidity, wide spreads normal)
    if clean_ticker in {'XPTUSD','XPDUSD'}:
        return 0.80
    # Oil
    if clean_ticker in {'SPOTCRUDE','SPOTBRENT'}:
        return 0.35
    # NatGas
    if clean_ticker in {'NATGAS'}:
        return 0.50
    # Major indices
    if clean_ticker in {'US30','US500','NAS100','GER40','UK100','JPN225'}:
        return 0.30
    # Smaller indices
    if clean_ticker in {'US2000','HK50'}:
        return 0.40
    # Soft commodities
    if clean_ticker in {'COPPER','CORN','WHEAT','SOYBEANS','SUGAR','COFFEE'}:
        return 0.50
    # Vol/dollar index
    if clean_ticker in {'VIX','USDX','EURX'}:
        return 0.50
    return 0.20  # default

def _mt4_vix_overlay_mult(clean_ticker):
    """VIX sentiment-based size multiplier (never blocks, only scales)."""
    gate = MT4_TICKER_GATES.get(clean_ticker.upper(), {})
    overlay = gate.get('vix_overlay')
    if not overlay:
        return 1.0
    vix = _get_vix()
    regime = _vix_regime(vix)
    if regime == 'complacent': return overlay.get('low_vix_mult', 1.0)
    if regime == 'normal': return overlay.get('normal_mult', 1.0)
    if regime == 'elevated': return overlay.get('elevated_mult', 1.0)
    if regime in ('panic','crisis'): return overlay.get('panic_mult', 0.5)
    return 1.0
# ===== end webhook filter =====

def _mt4_save():
    try:
        with open(MT4_QUEUE_FILE, 'w') as _f:
            _json.dump({'queue': MT4_QUEUE, 'bias': MT4_BIAS}, _f)
    except Exception as _e:
        pass  # never let disk IO break HL

def _mt4_load():
    global MT4_QUEUE, MT4_BIAS
    try:
        if os.path.exists(MT4_QUEUE_FILE):
            with open(MT4_QUEUE_FILE) as _f:
                _d = _json.load(_f)
            _q = _d.get('queue', [])
            _now = time.time()
            # drop stale on boot
            MT4_QUEUE[:] = [_s for _s in _q if (_now - _s.get('ts', 0)) < MT4_STALE_SEC]
            _b = _d.get('bias')
            if isinstance(_b, dict):
                MT4_BIAS.update(_b)
            try:
                log(f"MT4 QUEUE RESTORED: {len(MT4_QUEUE)} signals from disk")
            except Exception:
                pass
    except Exception as _e:
        try:
            log(f"MT4 QUEUE LOAD ERR: {_e}")
        except Exception:
            pass
# --- end MT4 persistence block ---


PEPPERSTONE_TICKERS = {
    'XAUUSD','XAGUSD','SPOTCRUDE','SPOTBRENT','NATGAS',
    'EURUSD','GBPUSD','USDJPY','EURGBP','GBPNZD',
    'AUDCAD','AUDUSD','USDCAD','USDCHF','AUDCHF',
    'AUDNZD','AUDJPY','CADCHF','CADJPY','CHFJPY',
    'EURAUD','EURCAD','EURCHF','GBPAUD','GBPCHF',
    'NZDUSD','NZDCAD','NAS100','US30','US500','US2000',
    'GER40','UK100','JPN225','HK50','XPTUSD','XPDUSD',
    'COPPER','CORN','WHEAT','SOYBEANS','COFFEE','SUGAR',
    'VIX','USDX','EURX'
}

TV_TO_MT4 = {
    'XAUUSD':'XAUUSD.a','XAGUSD':'XAGUSD.a','XPTUSD':'XPTUSD.a','XPDUSD':'XPDUSD.a',
    'SPOTCRUDE':'SpotCrude.a','SPOTBRENT':'SpotBrent.a','NATGAS':'NatGas.a',
    'EURUSD':'EURUSD.a','GBPUSD':'GBPUSD.a','USDJPY':'USDJPY.a',
    'EURGBP':'EURGBP.a','GBPNZD':'GBPNZD.a','AUDCAD':'AUDCAD.a',
    'AUDUSD':'AUDUSD.a','USDCAD':'USDCAD.a','USDCHF':'USDCHF.a',
    'AUDCHF':'AUDCHF.a','AUDNZD':'AUDNZD.a','AUDJPY':'AUDJPY.a',
    'CADCHF':'CADCHF.a','CADJPY':'CADJPY.a','CHFJPY':'CHFJPY.a',
    'EURAUD':'EURAUD.a','EURCAD':'EURCAD.a','EURCHF':'EURCHF.a',
    'GBPAUD':'GBPAUD.a','GBPCHF':'GBPCHF.a','NZDUSD':'NZDUSD.a',
    'NZDCAD':'NZDCAD.a','NAS100':'NAS100.a','US30':'US30.a',
    'US500':'US500.a','US2000':'US2000.a','GER40':'GER40.a',
    'UK100':'UK100.a','JPN225':'JPN225.a','HK50':'HK50.a',
    'COPPER':'Copper.a','CORN':'Corn.a','WHEAT':'Wheat.a',
    'SOYBEANS':'Soybeans.a','COFFEE':'Coffee.a','SUGAR':'Sugar.a',
    'VIX':'VIX.a','USDX':'USDX.a','EURX':'EURX.a'
}

def is_pepperstone(ticker):
    clean = ticker.upper().replace('PEPPERSTONE:','').replace('USDT','').replace('.P','')
    return clean in PEPPERSTONE_TICKERS

def get_mt4_symbol(ticker):
    clean = ticker.upper().replace('PEPPERSTONE:','').replace('USDT','').replace('.P','')
    return TV_TO_MT4.get(clean, clean + '.a')


# Map TradingView ticker → HL coin name
def tv_to_hl(ticker):
    """BTCUSD→BTC, SOLUSDT→SOL, BONKUSDT→kBONK, etc."""
    t = ticker.upper().replace('USDT.P','').replace('.P','').replace('USDT','').replace('USD','').replace('PERP','')
    # k-prefix for 1000x tokens
    remap = {'BONK':'kBONK','PEPE':'kPEPE','SHIB':'kSHIB','MATIC':'POL',
             '1000BONK':'kBONK','1000PEPE':'kPEPE','1000SHIB':'kSHIB'}
    return remap.get(t, t)

app = Flask(__name__)

# Shared navigation — injected into every HTML endpoint for consistent UX
PRECOG_NAV = '''<style>
.precog-nav{position:sticky;top:0;z-index:1000;background:rgba(7,8,10,0.92);backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);border-bottom:1px solid rgba(200,204,212,0.08);padding:10px 16px;display:flex;flex-wrap:wrap;gap:6px;align-items:center;font-family:'JetBrains Mono',monospace;margin:-20px -20px 20px -20px}
.precog-nav .nav-brand{font-family:'Cormorant Garamond',serif;font-size:15px;color:#d9d6cd;padding-right:12px;margin-right:4px;letter-spacing:0.15em;text-transform:uppercase;font-weight:300;opacity:0.85;border-right:1px solid rgba(200,204,212,0.1);text-decoration:none}
.precog-nav a{display:inline-flex;align-items:center;padding:6px 12px;font-size:10px;letter-spacing:0.25em;text-transform:uppercase;color:#c8ccd4;text-decoration:none;border:1px solid rgba(200,204,212,0.12);background:transparent;transition:all 0.12s ease;white-space:nowrap;font-weight:500}
.precog-nav a:hover{border-color:#b8ff2f;color:#b8ff2f;background:rgba(184,255,47,0.06)}
.precog-nav a.current{border-color:#b8ff2f;color:#b8ff2f;background:rgba(184,255,47,0.1)}
.precog-nav .nav-sep{flex:1}
.precog-nav .nav-tag{font-size:9px;color:#64748b;letter-spacing:0.25em;opacity:0.6;padding-left:6px}
@media(max-width:700px){.precog-nav{padding:8px 10px;gap:4px}.precog-nav .nav-brand{font-size:13px;width:100%;padding:0 0 6px 0;margin:0 0 4px 0;border-right:none;border-bottom:1px solid rgba(200,204,212,0.1)}.precog-nav a{padding:5px 8px;font-size:9px;letter-spacing:0.18em}.precog-nav .nav-tag{width:100%;padding:6px 0 0 0;text-align:center}}
</style>
<nav class="precog-nav">
<a href="/" class="nav-brand">PRECOG</a>
<a href="/" data-page="dashboard">Dash</a>
<a href="/violations" data-page="violations">Audit</a>
<a href="/audit/deep?format=html" data-page="deep">Deep</a>
<a href="/audit/elasticity?format=html" data-page="elasticity">Elasticity</a>
<a href="/shadow/compare?format=html" data-page="shadow">Shadow</a>
<a href="/enforce" data-page="enforce">Enforce</a>
<a href="/experiment" data-page="experiment">Experiment</a>
<div class="nav-sep"></div>
<span class="nav-tag">v8.28 · phase 1</span>
</nav>
<script>(function(){var p=location.pathname,c="";if(p==="/"||p==="")c="dashboard";else if(p.indexOf("/violations")===0||p==="/audit")c="violations";else if(p.indexOf("/audit/deep")===0)c="deep";else if(p.indexOf("/audit/elasticity")===0)c="elasticity";else if(p.indexOf("/shadow")===0)c="shadow";else if(p.indexOf("/enforce")===0)c="enforce";else if(p.indexOf("/experiment")===0)c="experiment";document.querySelectorAll(".precog-nav a[data-page]").forEach(function(a){if(a.getAttribute("data-page")===c)a.classList.add("current")})})();</script>
'''

# Register post-mortem endpoints (no-op if module failed to import)
if _POSTMORTEM_OK and _postmortem is not None:
    try:
        from postmortem.endpoints import register_endpoints as _pm_register
        _pm_register(app)
        # Also initialize DB eagerly so first close doesn't pay the cost
        try:
            _postmortem.init_db()
        except Exception as _e:
            print(f'[postmortem] db init deferred: {_e}', flush=True)
        # Auto-start trade finder daemon if env says so
        try:
            _started = _postmortem.trade_finder_module.start_auto()
            if _started:
                print('[postmortem] trade finder auto-started', flush=True)
        except Exception as _e:
            print(f'[postmortem] finder auto-start err (non-fatal): {_e}', flush=True)
        # Auto-start macro puller daemon (Stooq + Massive.io → tv_cache)
        # Requires no user config. Refreshes every POSTMORTEM_AUTOMACRO_TTL sec (default 1800).
        # Disable by setting POSTMORTEM_AUTOMACRO_ENABLED=0.
        try:
            if os.environ.get('POSTMORTEM_AUTOMACRO_ENABLED', '1') == '1':
                from postmortem import auto_macro as _auto_macro
                if _auto_macro.start_daemon():
                    print('[postmortem] auto_macro daemon started (Stooq + Massive.io)', flush=True)
        except Exception as _e:
            print(f'[postmortem] auto_macro start err (non-fatal): {_e}', flush=True)
    except Exception as _e:
        print(f'[postmortem] endpoint registration failed (non-fatal): {_e}', flush=True)


_LANDING_HTML = None
def _load_landing():
    """Load landing.html from disk."""
    try:
        with open(os.path.join(os.path.dirname(__file__), 'landing.html'), 'r') as f:
            return f.read()
    except Exception as e:
        return f"<h1>landing load err: {e}</h1>"

def _load_violations_page():
    """Load violations.html from disk."""
    try:
        with open(os.path.join(os.path.dirname(__file__), 'violations.html'), 'r') as f:
            return f.read()
    except Exception as e:
        return f"<h1>violations page load err: {e}</h1>"

@app.route('/', methods=['GET'])
@app.route('/landing', methods=['GET'])
def landing():
    resp = Response(_load_landing(), mimetype='text/html')
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp

@app.route('/violations', methods=['GET'])
@app.route('/audit', methods=['GET'])
def violations_audit():
    """Execution violations audit dashboard — integrity monitoring."""
    resp = Response(_load_violations_page(), mimetype='text/html')
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp

@app.route('/stats/reset', methods=['GET'])
def stats_reset():
    if flask_request.args.get('k') != WEBHOOK_SECRET[:16]: return jsonify({'err':'unauthorized'}), 401
    state = load_state()
    state['stats'] = {'by_engine': {}, 'by_hour': {}, 'by_side': {}, 'by_coin': {},
                      'by_conf': {}, 'total_wins': 0, 'total_losses': 0, 'total_pnl': 0.0}
    save_state(state)
    return jsonify({'status':'stats reset'})

@app.route('/stats', methods=['GET'])
def stats_endpoint():
    """Live stats: per-engine, per-hour, per-side, per-coin, per-conf."""
    try:
        state = load_state()
        stats = state.get('stats', {})
        def summarize(bucket):
            out = {}
            for k, v in bucket.items():
                w = v.get('w',0); l = v.get('l',0); n = w + l
                wr = (w/n) if n else 0
                out[k] = {'n': n, 'wr': round(wr*100,1), 'pnl_pct': round(v.get('pnl',0),2)}
            return out
        return jsonify({
            'total_wins': stats.get('total_wins',0),
            'total_losses': stats.get('total_losses',0),
            'total_n': stats.get('total_wins',0) + stats.get('total_losses',0),
            'overall_wr': round(stats.get('total_wins',0) / max(1, stats.get('total_wins',0)+stats.get('total_losses',0)) * 100, 1),
            'total_pnl_pct': round(stats.get('total_pnl',0), 2),
            'by_engine': summarize(stats.get('by_engine', {})),
            'by_hour':   summarize(stats.get('by_hour', {})),
            'by_side':   summarize(stats.get('by_side', {})),
            'by_coin':   summarize(stats.get('by_coin', {})),
            'by_conf':   summarize(stats.get('by_conf', {})),
        })
    except Exception as e:
        return jsonify({'err': str(e)})

@app.route('/conf/test/<coin>', methods=['GET'])
def conf_test(coin):
    """Test confidence scoring on current coin state (no trade fired)."""
    try:
        candles = fetch(coin.upper())
        if len(candles) < 50:
            return jsonify({'err': f'insufficient candles: {len(candles)}'})
        btc = btc_correlation.get_state()
        btc_d = btc.get('btc_dir', 0)
        buy_score, buy_brk = confidence.score(candles, [], coin.upper(), 'BUY', btc_d)
        sell_score, sell_brk = confidence.score(candles, [], coin.upper(), 'SELL', btc_d)
        return jsonify({
            'coin': coin.upper(),
            'n_candles': len(candles),
            'btc_dir': btc_d,
            'btc_move_15m': btc.get('btc_move', 0),
            'btc_move_1h': btc.get('btc_1h_move', 0),
            'BUY':  {'score': buy_score,  'mult': confidence.size_multiplier(buy_score),  'breakdown': buy_brk},
            'SELL': {'score': sell_score, 'mult': confidence.size_multiplier(sell_score), 'breakdown': sell_brk},
        })
    except Exception as e:
        return jsonify({'err': str(e)})

@app.route('/engines', methods=['GET'])
def engines_status():
    """Live engine + guard + venue state."""
    try:
        btc = btc_correlation.get_state()
        btc_fresh = (time.time() - btc.get('ts',0)) < 120 if btc.get('ts') else False
    except Exception: btc_fresh = False
    try:
        venues = orderbook_ws.get_venue_status()
    except Exception: venues = {}
    def v_ok(name):
        age = venues.get(name)
        return age is not None and age < 60
    return jsonify({
        'signal_engines': {
            'PIVOT': True,  # always core
            'PULLBACK': True,
            'WALL_BNC': v_ok('by') or v_ok('bn'),
            'WALL_EXH': v_ok('by') or v_ok('bn'),
            'WALL_ABSORB': wall_absorption.status().get('enabled', False),
            'FUNDING_MR': funding_engine.status().get('enabled', False),
            'LIQ_CSCD': True,
            'CVD_DIV': True,
        },
        'guards': {
            'V3_TREND': True,
            'ATR_MIN': True,
            'BTC_CORR': btc_fresh,
            'FUNDING': True,
            'CHASE': True,
            'SPOOF': True,
            'NEWS': True,
            'POS_CAPS': True,
            'DD_BRK': True,
        },
        'sizing': {'CONF_SIZE': True},
        'venues': {
            'BYBIT':    v_ok('by'),
            'BINANCE':  v_ok('bn'),
            'OKX':      v_ok('okx'),
            'COINBASE': v_ok('cb'),
            'BITGET':   v_ok('bg'),
            'KRAKEN':   v_ok('kr'),
        },
        'venue_ages': venues,
    })

@app.route('/orderbook/<coin>', methods=['GET'])
def orderbook_depth(coin):
    try:
        agg = orderbook_ws.get_aggregated_depth(coin.upper()) if hasattr(orderbook_ws,'get_aggregated_depth') else None
        if not agg:
            # Fallback: build from _DEPTH
            from orderbook_ws import _DEPTH, _LOCK
            with _LOCK:
                d = _DEPTH.get(coin.upper(), {})
                bids_raw = d.get('bids', {})
                asks_raw = d.get('asks', {})
                mid = d.get('mid', 0)
            # _DEPTH uses venue_px keys
            bids = {}; asks = {}
            for k,v in bids_raw.items():
                if isinstance(v, tuple): px, sz = v; bids[px] = bids.get(px,0)+sz
            for k,v in asks_raw.items():
                if isinstance(v, tuple): px, sz = v; asks[px] = asks.get(px,0)+sz
            agg = {'bids':bids,'asks':asks,'mid':mid,'venue_count':0}
        mid = agg.get('mid', 0)
        # Build depth levels within 2% of mid, bucketed
        if not mid: return jsonify({'mid':0,'bids':[],'asks':[]})
        bids = sorted([(p,s) for p,s in agg['bids'].items() if p > mid*0.97 and p <= mid], reverse=True)
        asks = sorted([(p,s) for p,s in agg['asks'].items() if p < mid*1.03 and p >= mid])
        # Bucket into 40 levels
        import math
        def bucket(orders, N=40):
            if not orders: return []
            out = []
            for px, sz in orders:
                usd = px * sz
                out.append({'price': px, 'size': sz, 'usd': usd})
            return out[:N]
        return jsonify({'coin':coin.upper(),'mid':mid,
                        'bids':bucket(bids,40),
                        'asks':bucket(asks,40),
                        'venues':agg.get('venue_count',0)})
    except Exception as e:
        return jsonify({'err':str(e)})

@app.route('/funding', methods=['GET'])
def funding_diagnostics():
    """Funding mean-reversion engine diagnostics. Shows top extreme funding
    rates across the universe and which would fire if FUNDING_MR_ENABLED=1.

    in_universe: coins in the trade universe — what would actually fire.
    global:      all HL coins — broader market crowd signal.
    """
    try:
        return jsonify({
            'engine_status': funding_engine.status(),
            'in_universe': funding_engine.get_top_funding_extremes(15, universe=COINS),
            'global': funding_engine.get_top_funding_extremes(15),
            'funding_arb_status': funding_arb.status(),
        })
    except Exception as e:
        return jsonify({'err': str(e)})

@app.route('/signals', methods=['GET'])
def signals_feed():
    with _SIGNAL_LOG_LOCK:
        items = list(_SIGNAL_LOG)[-30:][::-1]
    return jsonify({'items': items})

@app.route('/whales', methods=['GET'])
def whales_feed():
    try:
        from collections import deque
        items = []
        if hasattr(whale_filter, '_WHALES'):
            now = time.time()
            with whale_filter._LOCK:
                for coin, dq in whale_filter._WHALES.items():
                    for ts, side, usd in list(dq)[-5:]:
                        if now - ts < 300:
                            items.append({'coin':coin,'side':side,'usd':usd,'ts':ts})
        items.sort(key=lambda x: x['ts'], reverse=True)
        return jsonify({'items': items[:20]})
    except Exception as e:
        return jsonify({'items': [], 'err': str(e)})

@app.route('/news', methods=['GET'])
def news_feed():
    try:
        items = news_filter.get_recent_items(limit=10) if hasattr(news_filter, 'get_recent_items') else []
    except Exception:
        items = []
    state = news_filter.get_state() if hasattr(news_filter, 'get_state') else {}
    return jsonify({'items': items, 'state': state})

@app.route('/drift', methods=['GET'])
def drift_diagnostic():
    """Identify the exchange/ledger mismatch driving the reconciler halt.
    Lists open trade_ids per side and flags the discrepancies.

    Admin actions (require token=WEBHOOK_SECRET):
      ?dedupe=1   — call trade_ledger.dedupe_open_trades() to close duplicates
    """
    from flask import request
    try:
        # Optional admin: dedupe duplicate-open trade_ids
        dedupe_result = None
        if request.args.get('dedupe') == '1':
            if request.args.get('token') != WEBHOOK_SECRET:
                return jsonify({'err': 'unauthorized — dedupe requires token'}), 401
            try:
                import trade_ledger as _tl
                dedupe_result = _tl.dedupe_open_trades()
            except Exception as e:
                dedupe_result = {'err': str(e)}

        # Exchange positions
        us = _cached_user_state()
        exch_coins = set()
        exch_details = {}
        for ap in us.get('assetPositions', []):
            p = ap.get('position', {})
            try:
                sz = float(p.get('szi', 0))
            except Exception:
                sz = 0
            if sz == 0:
                continue
            c = (p.get('coin', '') or '').upper()
            exch_coins.add(c)
            exch_details[c] = {
                'size': sz,
                'side': 'BUY' if sz > 0 else 'SELL',
                'entry_px': float(p.get('entryPx', 0) or 0),
            }

        # Trade ledger open trades — group by coin to find duplicates
        ledger_open = []
        ledger_coins = set()
        ledger_by_coin = {}  # coin -> list of trade dicts
        try:
            import trade_ledger as _tl
            for t in _tl.open_trades():
                c = (t.get('coin') or '').upper()
                rec = {
                    'trade_id': t.get('trade_id'),
                    'coin': c,
                    'side': t.get('side'),
                    'engine': t.get('engine'),
                    'entry_price': t.get('entry_price'),
                    'timestamp': t.get('timestamp'),
                    'event_seq': t.get('event_seq'),
                }
                ledger_open.append(rec)
                if c:
                    ledger_coins.add(c)
                    ledger_by_coin.setdefault(c, []).append(rec)
        except Exception as e:
            ledger_open = [{'err': str(e)}]

        # Discrepancies
        only_exchange = sorted(exch_coins - ledger_coins)
        only_ledger = sorted(ledger_coins - exch_coins)
        # Duplicates: coins with >1 open trade_id in ledger
        duplicates_in_ledger = {c: ts for c, ts in ledger_by_coin.items() if len(ts) > 1}

        return jsonify({
            'exchange_count': len(exch_coins),
            'ledger_open_count': len(ledger_open),
            'ledger_unique_coins': len(ledger_coins),
            'drift_pct': abs(len(exch_coins) - len(ledger_open)) / max(len(exch_coins), 1),
            'orphans_on_exchange': only_exchange,
            'orphan_details': {c: exch_details[c] for c in only_exchange},
            'phantoms_in_ledger': only_ledger,
            'phantom_details': [t for t in ledger_open if (t.get('coin') or '').upper() in only_ledger],
            'duplicates_in_ledger': duplicates_in_ledger,
            'duplicate_count': len(duplicates_in_ledger),
            'all_exchange_coins': sorted(exch_coins),
            'all_ledger_coins': sorted(ledger_coins),
            'reconciler_state': (_reconciler.status() if (_RECONCILER_OK and _reconciler is not None) else {}),
            'dedupe_result': dedupe_result,
        })
    except Exception as e:
        import traceback as _tb
        return jsonify({'err': str(e), 'trace': _tb.format_exc()[-500:]}), 500


@app.route('/backtest', methods=['GET'])
def backtest_endpoint():
    """Run historical backtest of confluence_engine on a list of coins.

    Query params:
      ?coins=BTC,ETH,SOL    — comma-separated coin list
      ?top=20               — auto-select top N from shadow universe (by volume)
      ?bars=300             — history depth (max 300 = ~3 days of 15m)
      ?min_n=8              — promotion candidate threshold (sample size)
      ?min_wr=60            — promotion candidate threshold (WR pct)

    Returns:
      per_coin stats + ranked promotion candidates list.
    """
    from flask import request
    try:
        import backtest as _bt
        coins_param = (request.args.get('coins') or '').strip()
        top_n = int(request.args.get('top', '0') or 0)
        n_bars = int(request.args.get('bars', '300'))
        min_n = int(request.args.get('min_n', '8'))
        min_wr = float(request.args.get('min_wr', '60.0'))

        # Resolve coin list
        if coins_param:
            coins = [c.strip().upper() for c in coins_param.split(',') if c.strip()]
        elif top_n > 0:
            # Pull top N by volume from HL meta_and_asset_ctxs (same as shadow tier)
            try:
                meta_ctxs = info.meta_and_asset_ctxs()
                meta = meta_ctxs[0]
                ctxs = meta_ctxs[1]
                # Exclude live coins
                live = set(COINS)
                ranked = []
                for i, u in enumerate(meta.get('universe', [])):
                    name = (u.get('name', '') or '').upper()
                    if not name or name in live:
                        continue
                    if name.startswith('k') and len(name) >= 4 and name[1].isupper():
                        continue
                    if i < len(ctxs):
                        try:
                            vol = float(ctxs[i].get('dayNtlVlm', 0) or 0)
                        except (TypeError, ValueError):
                            vol = 0
                    else:
                        vol = 0
                    ranked.append((name, vol))
                ranked.sort(key=lambda kv: -kv[1])
                coins = [c for c, v in ranked[:top_n] if v > 0]
            except Exception as _e:
                return jsonify({'err': f'top-N resolve failed: {_e}'}), 500
        else:
            return jsonify({'err': 'specify ?coins=A,B,C or ?top=N'}), 400

        # Run backtest (sequential — bounded by OKX rate limit)
        results = _bt.backtest_universe(coins, n_bars=n_bars)
        promotion = _bt.rank_promotion_candidates(results, min_n=min_n, min_wr=min_wr)

        # Aggregate stats across universe
        total_signals = sum(r.get('n_signals', 0) for r in results.values() if not r.get('err'))
        total_wins = sum(r.get('wins', 0) for r in results.values() if not r.get('err'))
        total_losses = sum(r.get('losses', 0) for r in results.values() if not r.get('err'))
        decided = total_wins + total_losses
        agg_wr = (total_wins / decided * 100) if decided else None

        return jsonify({
            'config': {
                'n_coins': len(coins),
                'n_bars': n_bars,
                'min_n': min_n,
                'min_wr': min_wr,
                'note': 'orthogonal systems (LIQ/CVD/OI/SPOOF/WHALE/WALL_ABS/FUND_ARB/NEWS) fail-soft → backtest is price-action stack only (SNIPER/DAY/SWING + FUNDING)',
            },
            'aggregate': {
                'total_signals': total_signals,
                'total_wins': total_wins,
                'total_losses': total_losses,
                'aggregate_wr_pct': round(agg_wr, 1) if agg_wr is not None else None,
            },
            'promotion_candidates': promotion,
            'per_coin': results,
        })
    except Exception as e:
        import traceback as _tb
        return jsonify({'err': str(e), 'trace': _tb.format_exc()[-500:]}), 500


@app.route('/health', methods=['GET'])
def health():
    eq = 0
    try: eq = get_balance()
    except Exception: pass
    cur_regime = None
    try:
        import regime_detector
        cur_regime = regime_detector.get_regime()
    except Exception: pass
    return jsonify({'status':'ok','version':'v8.28','equity':eq,
                    'queue_size':WEBHOOK_QUEUE.qsize(),
                    'mt4_queue':len(MT4_QUEUE),
                    'coins':len(COINS),
                    'risk':INITIAL_RISK_PCT,
                    'trail':TRAIL_PCT,
                    'gates_loaded':len(TICKER_GATES),
                    'regime':cur_regime,
                    'snapshot': (_candle_snap.snapshot_status() if _SNAPSHOT_OK else {'enabled': False}),
                    'unknown_coins': sorted(_UNKNOWN_COINS),
                    'sl_state': (_sl_state_tracker.status() if _SL_STATE_OK else {'enabled': False}),
                    'execution_state': (_exec_state.status() if _EXEC_STATE_OK else {'enabled': False}),
                    'order_finality': (_order_finality.status() if _ORDER_FINALITY_OK else {'enabled': False}),
                    'flight_guard': flight_guard.status(),
                    'order_state': order_state.status(),
                    'ledger':       position_ledger.status(),
                    'hl_user_ws':   hl_user_ws.status(),
                    # 2026-04-27: surface external data-source feeds for diagnostics.
                    # These power the new orthogonal confluence systems (LIQ, CVD,
                    # WHALE). If any are dead, those engines fail-soft silent.
                    'liquidation_ws': (liquidation_ws.status() if hasattr(liquidation_ws, 'status') else {}),
                    'cvd_ws':         (cvd_ws.status() if hasattr(cvd_ws, 'status') else {}),
                    'whale_filter':   (whale_filter.status() if hasattr(whale_filter, 'status') else {}),
                    'spoof_detection':(spoof_detection.status() if hasattr(spoof_detection, 'status') else {}),
                    'oi_tracker':     (oi_tracker.status() if hasattr(oi_tracker, 'status') else {}),
                    'enforce_throttle': {
                        **_ENFORCE_STATS,
                        'cooldown_sec': _ENFORCE_COOLDOWN_SEC,
                        'tracked_coins': len(_LAST_ENFORCE),
                    },
                    'atomic_reconciler': atomic_reconciler.status(),
                    'lifecycle_reconciler': (
                        {k: _reconciler.status().get(k) for k in (
                            'halt_flag', 'entry_limiter', 'drift_tier',
                            'last_drift_pct', 'healthy_streak', 'unsafe_streak',
                            'circuit_breaker_tripped', 'cycles_total',
                            'last_cycle_ts', 'cycle_stale', 'reconciler_lag_s')}
                        if (_RECONCILER_OK and _reconciler is not None) else {'enabled': False}
                    ),
                    'wall_exhaustion': wall_exhaustion.status(),
                    'wall_absorption': wall_absorption.status(),
                    'wall_bounce': wall_bounce.status() if hasattr(wall_bounce, 'status') else {},
                    'funding_mr': funding_engine.status(),
                    'engine_health': _compute_engine_health(),
                    'use_atomic_exec': USE_ATOMIC_EXEC,
                    'use_ledger_for_size': USE_LEDGER_FOR_SIZE,
                    'recent_logs':LOG_BUFFER[-20:]})

@app.route('/confluence', methods=['GET'])
def confluence_status():
    """System B status: fires, WR, PnL, open positions."""
    try:
        import confluence_worker as cw
        st = cw.status()
        wr = 0
        total_closed = st.get('wins', 0) + st.get('losses', 0)
        if total_closed > 0:
            wr = st['wins'] * 100 // total_closed
        # Last-bar diagnostics — if empty, worker isn't fetching candles
        last_bars = st.get('last_bar_ts', {})
        return jsonify({
            'enabled': cw.ENABLED,
            'dry_run': cw.DRY_RUN,
            'scan_interval_s': cw.SCAN_INTERVAL_S,
            'max_positions': cw.MAX_POSITIONS,
            'risk_pct': cw.RISK_PCT,
            'total_fires': st.get('total_fires', 0),
            'wins': st.get('wins', 0),
            'losses': st.get('losses', 0),
            'timeouts': st.get('timeouts', 0),
            'wr_pct': wr,
            'total_pnl_pct': st.get('total_pnl_pct', 0.0),
            'open_positions': st.get('open_positions', {}),
            'last_fire_ts': st.get('last_fire_ts', {}),
            # Diagnostics
            'last_bar_coins': len(last_bars),
            'last_bar_sample': dict(list(last_bars.items())[:5]),
            'killed_coins': list(st.get('killed_coins', {}).keys())[:20],
            # Per-filter rejection counters (surgical diagnosis for "why 0 fires")
            'engine_stats': st.get('engine_stats', {}),
            # 2026-04-26: per-gate reject counters across the scan→fire pipeline.
            # signals_yielded - sum(rejects_last_scan) - last_scan_fires should = 0.
            # Whichever bucket dominates rejects_last_scan IS the blocker.
            'rejects_cumulative': st.get('rejects', {}),
            'rejects_last_scan': st.get('rejects_last_scan', {}),
            'last_scan_at': st.get('last_scan_at', 0),
            'last_scan_signals': st.get('last_scan_signals', 0),
            'last_scan_fires': st.get('last_scan_fires', 0),
            # 2026-04-26: fire-stage diagnostics (why post-gate signals don't fire)
            'place_attempts': st.get('place_attempts', 0),
            'place_filled':   st.get('place_filled', 0),
            'place_no_fill':  st.get('place_no_fill', 0),
            'place_error':    st.get('place_error', 0),
            # 2026-04-26: stale-flat rotation diagnostics
            'stale_flat_marked':  st.get('stale_flat_marked', 0),    # times marked eligible
            'stale_flat_rotated': st.get('stale_flat_rotated', 0),   # times actually evicted
        })
    except Exception as e:
        return jsonify({'err': str(e)}), 500


@app.route('/shadow_tier', methods=['GET'])
def shadow_tier_status():
    """Shadow-tier screening results. Tracks coins NOT currently in the
    confluence universe — eval'd every scan, hypothetical TP/SL outcomes
    resolved as candles advance.

    Promotion candidates (n >= 15 AND wr >= 65%) appear flagged.
    Add coins by editing percoin_configs.py once promoted.
    """
    try:
        import shadow_trades as _st
        per_coin = _st.per_coin_stats(reason_filter='shadow_screen_tier', min_n=1)
        promotion_candidates = [c for c, d in per_coin.items() if d.get('promotion_candidate')]
        # Also surface confluence_worker shadow scan diagnostics
        try:
            import confluence_worker as cw
            sw = cw._state
            shadow_diag = {
                'shadow_universe_size': sw.get('shadow_universe_size', 0),
                'shadow_signals_last_scan': sw.get('shadow_signals_last_scan', 0),
            }
        except Exception:
            shadow_diag = {}
        return jsonify({
            'overall': _st.status(),
            'shadow_tier_screen': {
                'per_coin': per_coin,
                'promotion_candidates': promotion_candidates,
                'criteria': {'min_n': 15, 'min_wr_pct': 65.0},
                **shadow_diag,
            },
        })
    except Exception as e:
        import traceback as _tb
        return jsonify({'err': str(e), 'trace': _tb.format_exc()[-500:]}), 500


@app.route('/analyze', methods=['GET'])
def analyze_endpoint():
    """Run the full trade analysis against /var/data/trades.csv and return
    structured JSON. Same numbers as `analyze_trades.py` CLI but on demand.

    Query params:
      ?since=<unix_ts>  — drop trades with entry_ts older than this
      ?since=24h        — convenience: drop trades older than 24 hours
      ?path=<file>      — override CSV path (default /var/data/trades.csv)
    """
    from flask import request
    try:
        import analyze_trades as _at
        path = request.args.get('path', '/var/data/trades.csv')
        since_raw = request.args.get('since')
        since_ts = None
        if since_raw:
            s = since_raw.strip().lower()
            try:
                if s.endswith('h'):
                    since_ts = time.time() - float(s[:-1]) * 3600.0
                elif s.endswith('m'):
                    since_ts = time.time() - float(s[:-1]) * 60.0
                elif s.endswith('d'):
                    since_ts = time.time() - float(s[:-1]) * 86400.0
                else:
                    since_ts = float(s)
            except Exception:
                since_ts = None
        out = _at.analyze_to_dict(path=path, since_ts=since_ts)
        out['generated_at'] = time.time()
        return jsonify(out)
    except Exception as e:
        import traceback as _tb
        return jsonify({'err': str(e), 'trace': _tb.format_exc()[-500:]}), 500


@app.route('/confluence/reset', methods=['POST', 'GET'])
def confluence_reset():
    """Reset System B state. Used after universe alignment.
    Requires WEBHOOK_SECRET token.
    Optional ?preserve=1 to snapshot pre-reset stats.
    """
    from flask import request
    token = request.args.get('token') or (request.json or {}).get('token') if request.is_json else request.args.get('token')
    if token != WEBHOOK_SECRET:
        return jsonify({'err': 'unauthorized'}), 401
    preserve = request.args.get('preserve') == '1'
    try:
        import confluence_worker as cw
        ok = cw.reset(preserve_history=preserve)
        return jsonify({'action': 'reset', 'preserve_history': preserve, 'ok': ok})
    except Exception as e:
        return jsonify({'err': str(e)}), 500


@app.route('/confluence/trades', methods=['GET'])
def confluence_trades():
    """Per-trade telemetry: closed-trade ring buffer with summary breakdown.
    Use ?n=<int> to limit (default 50, max 500).
    Returns: items + summary by exit_reason + by confluence_score + by coin.
    """
    from flask import request
    n = min(int(request.args.get('n', '50')), 500)
    try:
        import confluence_worker as cw
        st = cw.status()
        trades = st.get('closed_trades', [])
        recent = trades[-n:] if trades else []
        # Summary aggregations
        from collections import Counter, defaultdict
        by_reason = Counter(t.get('exit_reason', '?') for t in trades)
        by_score = Counter(t.get('confluence_score', 0) for t in trades)
        by_coin = defaultdict(lambda: {'n': 0, 'w': 0, 'l': 0, 'pnl': 0.0})
        durations = []
        wins = 0
        for t in trades:
            c = t.get('coin', '?')
            pnl = t.get('pnl_pct', 0.0)
            by_coin[c]['n'] += 1
            if pnl > 0:
                by_coin[c]['w'] += 1
                wins += 1
            else:
                by_coin[c]['l'] += 1
            by_coin[c]['pnl'] += pnl
            durations.append(t.get('duration_min', 0))
        n_total = len(trades)
        avg_dur = (sum(durations) / len(durations)) if durations else 0
        wr = (wins / n_total * 100) if n_total > 0 else 0
        return jsonify({
            'total_closed': n_total,
            'wr_pct': round(wr, 1),
            'avg_duration_min': round(avg_dur, 1),
            'by_exit_reason': dict(by_reason),
            'by_confluence_score': {str(k): v for k, v in by_score.items()},
            'top_coins': dict(sorted(by_coin.items(), key=lambda kv: -kv[1]['n'])[:20]),
            'recent_trades': recent,
        })
    except Exception as e:
        return jsonify({'err': str(e)}), 500


@app.route('/regime', methods=['GET'])
def regime_status():
    """Return current regime detector state + per-coin coverage."""
    try:
        import regime_detector
        import regime_configs
        return jsonify({
            'detector': regime_detector.status(),
            'config_coverage': regime_configs.coverage_stats(),
            'total_coins_with_regime_configs': len(regime_configs.REGIME_CONFIGS),
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/microstructure', methods=['GET'])
def microstructure_status():
    """Microstructure annotator — post-hoc classification of closes.
    Accumulates data silently; at 200 closes flags discussion trigger."""
    try:
        if not _MS_OK or _microstructure is None:
            return jsonify({'error': 'microstructure module not loaded'}), 503
        return jsonify(_microstructure.get_stats())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/signal_log', methods=['GET'])
def signal_log_status():
    """Signal state logger — parallel telemetry for mutual information analysis.
    At 1000+ states + 500+ outcomes, flags MI discussion trigger."""
    try:
        if not _SL_OK or _signal_logger is None:
            return jsonify({'error': 'signal_logger not loaded'}), 503
        return jsonify(_signal_logger.get_stats())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/convex', methods=['GET'])
def convex_status():
    """Convexity scorer — payoff asymmetry telemetry.
    At 100+ outcomes, flags trigger for sizing activation decision."""
    try:
        if not _CX_OK or _convex is None:
            return jsonify({'error': 'convex_scorer not loaded'}), 503
        return jsonify(_convex.get_stats())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/counterfactual', methods=['GET'])
def counterfactual_status():
    """Counterfactual engine — per-trade alternative replay.
    Computes regret for delayed/skipped/resized/signal-removed paths.
    At 50+ analyses, flags trigger for decision evaluation."""
    try:
        if not _CF_OK or _counterfactual is None:
            return jsonify({'error': 'counterfactual not loaded'}), 503
        return jsonify(_counterfactual.get_stats())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/red_team', methods=['GET'])
def red_team_status():
    """Red team defenses — cheap structural safeguards.
    Chop cooldown extension, regime staleness check, funding exit audit."""
    try:
        if not _RT_OK or _red_team is None:
            return jsonify({'error': 'red_team not loaded'}), 503
        return jsonify(_red_team.status())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/abstain', methods=['GET'])
def abstain_status():
    """Optimal inaction — silent abstain scorer.
    Records no-trade-zone risk at signal fire. At 100 closes, evaluates
    whether abstain-bucket WR justifies live activation."""
    try:
        if not _IA_OK or _inaction is None:
            return jsonify({'error': 'optimal_inaction not loaded'}), 503
        return jsonify(_inaction.get_stats())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/edge_decay', methods=['GET'])
def edge_decay_status():
    """Edge decay monitor — live WR drift / R:R compression / hold expansion.
    Returns current trend (increasing/stable/decaying_slow/decaying_fast/broken)
    and half-life estimate for current edge."""
    try:
        if not _ED_OK or _edge_decay is None:
            return jsonify({'error': 'edge_decay not loaded'}), 503
        return jsonify(_edge_decay.status())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/path_dep', methods=['GET'])
def path_dep_status():
    """Path dependency — live streak tracking + adaptive sizing.
    Emits size multiplier on consecutive losses. Entry pause at 7+ losses.
    This module DOES modify live trading behavior."""
    try:
        if not _PD_OK or _path_dep is None:
            return jsonify({'error': 'path_dep not loaded'}), 503
        return jsonify(_path_dep.status())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/calibration', methods=['GET'])
def calibration_status():
    """Conviction calibration — reliability curve + Brier score + per-bucket expectancy.
    Reads existing signal_logger jsonl. No new data source."""
    try:
        if not _SL_OK or _signal_logger is None:
            return jsonify({'error': 'signal_logger not loaded'}), 503
        return jsonify(_signal_logger.calibration_report())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/reality_gap', methods=['GET'])
def reality_gap_status():
    """Reality gap — backtest-to-live drift audit.
    Correction factors for slippage, regime transitions, overfit tax, leverage."""
    try:
        if not _RG_OK or _reality_gap is None:
            return jsonify({'error': 'reality_gap not loaded'}), 503
        return jsonify(_reality_gap.status())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/contract', methods=['GET'])
def contract_status():
    """Execution contract — exit hierarchy enforcement status.
    Shows authorized reasons, queued reversals, rejected closes."""
    try:
        if not _EC_OK or _contract is None:
            return jsonify({'error': 'exec_contract not loaded'}), 503
        return jsonify(_contract.status())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/tf_isolation', methods=['GET'])
def tf_isolation_status():
    """Timeframe isolation contract — per-TF independence + HTF gating rules."""
    try:
        if not _TFI_OK or _tf_iso is None:
            return jsonify({'error': 'tf_isolation not loaded'}), 503
        return jsonify(_tf_iso.status())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/invariants', methods=['GET'])
def invariants_status():
    """Contract invariants — deadman check, entry/exit invariants, audit log."""
    try:
        if not _INV_OK or _invariants is None:
            return jsonify({'error': 'invariants not loaded'}), 503
        return jsonify(_invariants.status())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

_AUDIT_FILLS_CACHE = {'data': None, 'ts': 0}
_AUDIT_CACHE_TTL = 30  # seconds

def _fetch_user_fills_cached():
    """Fetch HL userFills with 30s cache. On 429, serve stale cache.
    Returns (fills, cache_age_sec) or (None, None) on permanent failure."""
    import urllib.request as _ureq
    now = time.time()
    if _AUDIT_FILLS_CACHE['data'] is not None and now - _AUDIT_FILLS_CACHE['ts'] < _AUDIT_CACHE_TTL:
        return _AUDIT_FILLS_CACHE['data'], int(now - _AUDIT_FILLS_CACHE['ts'])
    try:
        body = json.dumps({'type': 'userFills', 'user': WALLET}).encode()
        req = _ureq.Request('https://api.hyperliquid.xyz/info', method='POST',
                            data=body, headers={'Content-Type': 'application/json'})
        with _ureq.urlopen(req, timeout=10) as resp:
            fills = json.loads(resp.read())
        _AUDIT_FILLS_CACHE['data'] = fills
        _AUDIT_FILLS_CACHE['ts'] = now
        return fills, 0
    except Exception as e:
        # On any error, fall back to stale cache if we have one
        if _AUDIT_FILLS_CACHE['data'] is not None:
            age = int(now - _AUDIT_FILLS_CACHE['ts'])
            return _AUDIT_FILLS_CACHE['data'], age
        raise


@app.route('/audit/deep', methods=['GET'])
def audit_deep():
    """One-click deep audit over the past N hours (default 5)."""
    try:
        hours = float(flask_request.args.get('hours', '5'))
    except Exception:
        hours = 5.0
    fmt = flask_request.args.get('format', 'json').lower()

    import urllib.request as _ureq
    from collections import defaultdict as _dd

    now_ms = int(time.time() * 1000)
    cutoff = now_ms - int(hours * 3600 * 1000)

    # 1. HL fills (cached, falls back to stale on 429)
    fills = []
    cache_age = 0
    try:
        fills, cache_age = _fetch_user_fills_cached()
    except Exception as e:
        return jsonify({'error': f'HL fills fetch: {e}'}), 503

    recent = [f for f in fills if f.get('time', 0) >= cutoff]
    closes = [f for f in recent if float(f.get('closedPnl', 0)) != 0]
    wins = [c for c in closes if float(c.get('closedPnl', 0)) > 0]
    losses = [c for c in closes if float(c.get('closedPnl', 0)) < 0]
    pnl = sum(float(c.get('closedPnl', 0)) for c in closes)
    fees = sum(float(f.get('fee', 0)) for f in recent)
    net = pnl - fees
    n_concluded = len(wins) + len(losses)
    wr = (len(wins) / n_concluded * 100) if n_concluded > 0 else 0
    avg_win = (sum(float(c['closedPnl']) for c in wins) / len(wins)) if wins else 0
    avg_loss = (sum(float(c['closedPnl']) for c in losses) / len(losses)) if losses else 0
    rr = (avg_win / abs(avg_loss)) if avg_loss else 0
    max_win = max((float(c['closedPnl']) for c in wins), default=0)
    max_loss = min((float(c['closedPnl']) for c in losses), default=0)

    # Per-coin
    by_coin = _dd(lambda: {'n': 0, 'pnl': 0.0, 'w': 0, 'l': 0})
    for c in closes:
        k = c.get('coin', '?')
        pnl_c = float(c.get('closedPnl', 0))
        by_coin[k]['n'] += 1
        by_coin[k]['pnl'] += pnl_c
        if pnl_c > 0: by_coin[k]['w'] += 1
        elif pnl_c < 0: by_coin[k]['l'] += 1
    top_coins = sorted(by_coin.items(), key=lambda x: -x[1]['n'])[:15]

    # Per-hour
    by_hour = _dd(lambda: {'fills': 0, 'closes': 0, 'pnl': 0.0})
    for f in recent:
        h = int((now_ms - f.get('time', 0)) / 3600_000)
        by_hour[h]['fills'] += 1
        if float(f.get('closedPnl', 0)) != 0:
            by_hour[h]['closes'] += 1
            by_hour[h]['pnl'] += float(f.get('closedPnl', 0))
    hourly = [(h, by_hour[h]) for h in sorted(by_hour)]

    # 2. System state snapshots
    equity = None
    try: equity = get_balance()
    except Exception: pass

    contract_state = None
    if _EC_OK and _contract is not None:
        try: contract_state = _contract.status()
        except Exception: pass

    invariants_state = None
    if _INV_OK and _invariants is not None:
        try: invariants_state = _invariants.status()
        except Exception: pass

    tf_state = None
    if _TFI_OK and _tf_iso is not None:
        try: tf_state = _tf_iso.status()
        except Exception: pass

    shadow_state = None
    if _SR_OK and _shadow_rej is not None:
        try: shadow_state = _shadow_rej.status()
        except Exception: pass

    # 3. Verdict
    if n_concluded < 10:
        verdict = {
            'label': 'SAMPLE TOO SMALL',
            'desc': f'Only {n_concluded} concluded trades. Need 10+ to read signal.',
            'color': 'cool',
        }
    elif wr >= 55 and net > 0:
        verdict = {
            'label': 'EDGE PRESENT',
            'desc': f'WR {wr:.1f}%, NET +${net:.2f}. Keep running.',
            'color': 'acid',
        }
    elif net > 0 and wr < 45:
        verdict = {
            'label': 'POSITIVE EXPECTANCY',
            'desc': f'NET +${net:.2f} despite low WR {wr:.1f}% — R:R {rr:.2f}:1 carrying it.',
            'color': 'amber',
        }
    elif net > 0:
        verdict = {
            'label': 'MILDLY POSITIVE',
            'desc': f'WR {wr:.1f}%, NET +${net:.2f}. Early signal.',
            'color': 'amber',
        }
    else:
        verdict = {
            'label': 'NEGATIVE',
            'desc': f'WR {wr:.1f}%, NET ${net:+.2f}. Filter or signal issue.',
            'color': 'hot',
        }

    audit = {
        'window_hours': hours,
        'as_of_utc': time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(now_ms/1000)),
        'trading': {
            'fills': len(recent),
            'closes': len(closes),
            'wins': len(wins),
            'losses': len(losses),
            'win_rate_pct': round(wr, 2),
            'pnl_realized': round(pnl, 2),
            'fees': round(fees, 2),
            'net': round(net, 2),
            'avg_win': round(avg_win, 2),
            'avg_loss': round(avg_loss, 2),
            'risk_reward': round(rr, 2),
            'max_win': round(max_win, 2),
            'max_loss': round(max_loss, 2),
            'closes_per_hour': round(len(closes) / hours, 2) if hours > 0 else 0,
            'projected_daily_trades': round(len(closes) / hours * 24) if hours > 0 else 0,
            'projected_daily_net': round(net / hours * 24, 2) if hours > 0 else 0,
        },
        'per_coin': [{'coin': c, **d} for c, d in top_coins],
        'per_hour': [{'hrs_ago': h, **d} for h, d in hourly],
        'live_state': {
            'equity': equity,
            'contract': {
                'queue_size': len(contract_state.get('reversal_queue', {})) if contract_state else None,
                'violations': contract_state.get('violations_counters') if contract_state else None,
            } if contract_state else None,
            'invariants': {
                'daemon_running': invariants_state.get('daemon_running') if invariants_state else None,
                'positions_protected': invariants_state.get('positions_protected') if invariants_state else None,
                'positions_naked': invariants_state.get('positions_naked_now') if invariants_state else None,
                'violations': invariants_state.get('violations') if invariants_state else None,
            } if invariants_state else None,
            'tf_isolation': tf_state.get('violations') if tf_state else None,
            'shadow': {
                'pending': shadow_state.get('pending_count') if shadow_state else None,
                'resolved': shadow_state.get('resolved_total') if shadow_state else None,
                'by_class': shadow_state.get('by_class') if shadow_state else None,
            } if shadow_state else None,
        },
        'verdict': verdict,
    }

    if fmt != 'html':
        return jsonify(audit)

    # HTML rendering
    def _c(v, pos='ok', neg='hot'):
        if v is None: return ''
        return pos if v >= 0 else neg

    coin_rows = ''.join(
        f'<tr><td class="mono">{c["coin"]}</td><td class="num">{c["n"]}</td>'
        f'<td class="num">{c["w"]}/{c["l"]}</td>'
        f'<td class="num {_c(c["pnl"])}">${c["pnl"]:+.2f}</td></tr>'
        for c in audit['per_coin']
    )
    hour_rows = ''.join(
        f'<tr><td>{h["hrs_ago"]}h</td><td class="num">{h["fills"]}</td>'
        f'<td class="num">{h["closes"]}</td>'
        f'<td class="num {_c(h["pnl"])}">${h["pnl"]:+.2f}</td></tr>'
        for h in audit['per_hour']
    )
    shadow_rows = ''
    if shadow_state:
        by_cls = shadow_state.get('by_class', {})
        for cls, d in by_cls.items():
            wr_v = d.get('win_rate')
            wr_s = f"{wr_v*100:.0f}%" if wr_v is not None else '—'
            shadow_rows += (
                f'<tr><td>{cls}</td>'
                f'<td class="num">{d.get("pending", 0)}</td>'
                f'<td class="num">{d.get("n_resolved", 0)}</td>'
                f'<td class="num">{wr_s}</td>'
                f'<td class="num {_c(d.get("expectancy_pct"))}">{d.get("expectancy_pct", 0):+.2f}%</td></tr>'
            )

    equity_str = f"${equity:.2f}" if isinstance(equity, (int, float)) else "—"

    html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AUDIT · {hours}h · {verdict['label']}</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;600&family=Cormorant+Garamond:wght@300;400&display=swap" rel="stylesheet">
<style>
:root {{ --void:#07080a;--carbon:#0d0e11;--chrome:#c8ccd4;--bone:#d9d6cd;--acid:#b8ff2f;--hot:#ef4444;--amber:#f59e0b;--cool:#64748b;--line:rgba(200,204,212,0.08); }}
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:var(--void);color:var(--chrome);font-family:'JetBrains Mono',monospace;font-size:12px;padding:20px;line-height:1.5}}
h1{{font-family:'Cormorant Garamond',serif;font-weight:300;font-size:28px;color:var(--bone);margin-bottom:4px}}
.sub{{font-size:10px;letter-spacing:0.3em;text-transform:uppercase;color:var(--chrome);opacity:0.5;margin-bottom:20px}}
.verdict{{padding:20px;border:1px solid var(--{verdict['color']});margin-bottom:24px;background:linear-gradient(180deg,rgba(184,255,47,0.04),transparent)}}
.verdict .label{{font-size:11px;letter-spacing:0.3em;text-transform:uppercase;color:var(--{verdict['color']});font-weight:600}}
.verdict .v{{font-family:'Cormorant Garamond',serif;font-size:34px;color:var(--bone);margin:6px 0}}
.verdict .desc{{font-size:12px;color:var(--chrome);opacity:0.8}}
.grid{{display:grid;grid-template-columns:repeat(6,1fr);gap:0;margin-bottom:28px;padding:18px 0;border-top:1px solid var(--acid);border-bottom:1px solid var(--line)}}
.stat{{padding:0 18px;border-left:1px solid var(--line)}}
.stat:first-child{{border-left:none}}
.stat-val{{font-family:'Cormorant Garamond',serif;font-size:24px;color:var(--bone);line-height:1}}
.stat-lbl{{font-size:9px;letter-spacing:0.3em;text-transform:uppercase;opacity:0.55;margin-top:6px}}
.ok{{color:var(--acid)}}.hot{{color:var(--hot)}}.amber{{color:var(--amber)}}.cool{{color:var(--cool)}}
.section{{margin-bottom:24px}}
.section h2{{font-size:11px;letter-spacing:0.35em;text-transform:uppercase;color:var(--bone);padding-bottom:8px;border-bottom:1px solid var(--line);margin-bottom:12px}}
table{{width:100%;border-collapse:collapse;font-size:11px}}
th{{text-align:left;padding:6px 10px;font-size:9px;letter-spacing:0.25em;text-transform:uppercase;opacity:0.6;border-bottom:1px solid var(--line);font-weight:400}}
td{{padding:6px 10px;border-bottom:1px solid rgba(200,204,212,0.04);color:var(--bone)}}
.num{{text-align:right;font-variant-numeric:tabular-nums}}
.mono{{font-family:'JetBrains Mono',monospace}}
.two{{display:grid;grid-template-columns:1fr 1fr;gap:20px}}
.kv{{display:grid;grid-template-columns:160px 1fr;gap:8px;font-size:11px}}
.kv dt{{color:var(--chrome);opacity:0.55}}.kv dd{{color:var(--bone)}}
a{{color:var(--acid)}}
@media(max-width:700px){{.grid{{grid-template-columns:repeat(3,1fr)}}.two{{grid-template-columns:1fr}}}}
</style></head><body>
{PRECOG_NAV}
<div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:8px">
<h1>Deep Audit · {hours}h</h1>
<a href="/violations" style="font-size:10px;letter-spacing:0.3em">VIOLATIONS →</a>
</div>
<div class="sub">as of {audit['as_of_utc']} UTC · window: past {hours} hours</div>

<div class="verdict">
    <div class="label">Verdict</div>
    <div class="v {verdict['color']}">{verdict['label']}</div>
    <div class="desc">{verdict['desc']}</div>
</div>

<div class="grid">
    <div class="stat"><div class="stat-val">{audit['trading']['closes']}</div><div class="stat-lbl">Closes</div></div>
    <div class="stat"><div class="stat-val {('ok' if wr>=55 else 'amber' if wr>=45 else 'hot')}">{wr:.0f}%</div><div class="stat-lbl">Win Rate</div></div>
    <div class="stat"><div class="stat-val {_c(net)}">${net:+.2f}</div><div class="stat-lbl">Net PnL</div></div>
    <div class="stat"><div class="stat-val">{rr:.2f}</div><div class="stat-lbl">R:R</div></div>
    <div class="stat"><div class="stat-val">{audit['trading']['closes_per_hour']:.1f}</div><div class="stat-lbl">Closes/Hr</div></div>
    <div class="stat"><div class="stat-val">{equity_str}</div><div class="stat-lbl">Equity</div></div>
</div>

<div class="two">
    <div class="section">
        <h2>Per Coin</h2>
        <table><thead><tr><th>Coin</th><th class="num">N</th><th class="num">W/L</th><th class="num">PnL</th></tr></thead>
        <tbody>{coin_rows or '<tr><td colspan=4 style="opacity:0.5;padding:20px">no closes</td></tr>'}</tbody></table>
    </div>
    <div class="section">
        <h2>Hourly</h2>
        <table><thead><tr><th>Age</th><th class="num">Fills</th><th class="num">Closes</th><th class="num">PnL</th></tr></thead>
        <tbody>{hour_rows or '<tr><td colspan=4 style="opacity:0.5;padding:20px">no activity</td></tr>'}</tbody></table>
    </div>
</div>

<div class="section">
    <h2>Shadow Rejections · Edge vs Capacity</h2>
    <table><thead><tr><th>Class</th><th class="num">Pending</th><th class="num">Resolved</th><th class="num">WR</th><th class="num">Exp</th></tr></thead>
    <tbody>{shadow_rows or '<tr><td colspan=5 style="opacity:0.5;padding:20px">no shadow data</td></tr>'}</tbody></table>
</div>

<div class="section">
    <h2>System State</h2>
    <dl class="kv">
    <dt>Contract queue</dt><dd>{len(contract_state.get('reversal_queue', {})) if contract_state else '—'} pending</dd>
    <dt>Contract violations</dt><dd>{contract_state.get('violations_counters') if contract_state else '—'}</dd>
    <dt>Invariants daemon</dt><dd>{invariants_state.get('daemon_running') if invariants_state else '—'}</dd>
    <dt>Positions protected</dt><dd>{invariants_state.get('positions_protected') if invariants_state else '—'}</dd>
    <dt>Positions naked</dt><dd>{invariants_state.get('positions_naked_now') if invariants_state else '—'}</dd>
    <dt>Invariant violations</dt><dd>{invariants_state.get('violations') if invariants_state else '—'}</dd>
    <dt>TF isolation</dt><dd>{tf_state.get('violations') if tf_state else '—'}</dd>
    <dt>Shadow pending</dt><dd>{shadow_state.get('pending_count') if shadow_state else '—'}</dd>
    <dt>Shadow resolved</dt><dd>{shadow_state.get('resolved_total') if shadow_state else '—'}</dd>
    </dl>
</div>

<div style="margin-top:40px;padding-top:12px;border-top:1px solid var(--line);font-size:9px;opacity:0.5;letter-spacing:0.2em;text-transform:uppercase">
    Projected daily at current pace: {audit['trading']['projected_daily_trades']} trades · ${audit['trading']['projected_daily_net']:+.2f} · 
    <a href="?hours=1&format=html">1h</a> ·
    <a href="?hours=5&format=html">5h</a> ·
    <a href="?hours=12&format=html">12h</a> ·
    <a href="?hours=24&format=html">24h</a> ·
    <a href="?hours=48&format=html">48h</a> ·
    <a href="?format=json">json</a>
</div>

</body></html>"""
    resp = Response(html, mimetype='text/html')
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return resp


@app.route('/audit/elasticity', methods=['GET'])
def audit_elasticity():
    """Filter elasticity analysis.

    Computes expectancy for each of: TAKEN (live fills), CAPACITY_REJECTED
    (max_pos/margin/same_side), EDGE_REJECTED (conf/regime/whitelist/
    correlation/funding/htf). Compares them to surface whether each filter
    is removing bad trades (FVS < 0, correct) or destroying edge (FVS > 0).

    Query params:
      ?hours=24  (window for live fills comparison; shadow is all-time)
      ?format=html or json
    """
    try:
        try:
            hours = float(flask_request.args.get('hours', '24'))
        except Exception:
            hours = 24.0
        fmt = flask_request.args.get('format', 'json').lower()

        import urllib.request as _ureq
        from collections import defaultdict as _dd
        import statistics as _stat

        now_ms = int(time.time() * 1000)
        cutoff = now_ms - int(hours * 3600 * 1000)

        # ─── TAKEN TRADES: HL fills ─────────────────────────────
        taken_rmults = []       # R-multiples: pnl% / sl%
        taken_pnl_pcts = []
        try:
            fills, _ = _fetch_user_fills_cached()
            closes = [f for f in fills
                      if f.get('time', 0) >= cutoff and float(f.get('closedPnl', 0)) != 0]
            # Approximate R = pnl_pct / sl_pct. Use fallback 2.5% SL if unknown.
            DEFAULT_SL = 0.025
            for c in closes:
                pnl_usd = float(c.get('closedPnl', 0))
                try:
                    entry_px = float(c.get('px', 0))
                    sz = abs(float(c.get('sz', 0)))
                    notional = entry_px * sz
                    if notional > 0:
                        pnl_pct = pnl_usd / notional
                        r_mult = pnl_pct / DEFAULT_SL
                        taken_pnl_pcts.append(pnl_pct * 100)
                        taken_rmults.append(r_mult)
                except Exception:
                    pass
        except Exception:
            pass

        # ─── SHADOW RESOLVED: rejected trades with outcomes ─────
        shadow_by_reason = _dd(list)  # reason -> list of (pnl_pct, r_mult, outcome)
        shadow_by_class = _dd(list)

        if _SR_OK and _shadow_rej is not None:
            try:
                # Access internals directly for full resolved list
                # This is idiomatic for this audit — status() truncates
                with _shadow_rej._LOCK:
                    resolved = list(_shadow_rej._RESOLVED)
                for rec in resolved:
                    outcome = rec.get('outcome')
                    if outcome not in ('tp', 'sl'):  # skip timeouts for expectancy
                        continue
                    reason = rec.get('reason', 'unknown')
                    cls = rec.get('rejection_class') or _shadow_rej.classify_reason(reason)
                    pnl_pct = rec.get('pnl_pct', 0.0)  # already percent
                    sl_pct = rec.get('sl_pct', 0.025) * 100
                    r_mult = pnl_pct / sl_pct if sl_pct > 0 else 0
                    shadow_by_reason[reason].append({
                        'pnl_pct': pnl_pct, 'r_mult': r_mult, 'outcome': outcome
                    })
                    shadow_by_class[cls].append({
                        'pnl_pct': pnl_pct, 'r_mult': r_mult, 'outcome': outcome
                    })
            except Exception as e:
                print(f"[elasticity] shadow access err: {e}", flush=True)

        # ─── COMPUTE EXPECTANCY STATS ────────────────────────────
        def _stats(samples_pnl, samples_r):
            n = len(samples_pnl)
            if n == 0:
                return {'n': 0, 'wr': None, 'ev_pct': None, 'ev_r': None,
                        'std_r': None, 'mean_win_r': None, 'mean_loss_r': None}
            wins = [r for r in samples_r if r > 0]
            losses = [r for r in samples_r if r < 0]
            wr = len(wins) / n if n > 0 else 0
            ev_pct = sum(samples_pnl) / n
            ev_r = sum(samples_r) / n
            std_r = _stat.pstdev(samples_r) if n > 1 else 0
            return {
                'n': n,
                'wr': round(wr, 3),
                'ev_pct': round(ev_pct, 3),
                'ev_r': round(ev_r, 3),
                'std_r': round(std_r, 3),
                'mean_win_r': round(sum(wins)/len(wins), 3) if wins else None,
                'mean_loss_r': round(sum(losses)/len(losses), 3) if losses else None,
                'wins': len(wins),
                'losses': len(losses),
            }

        taken_stats = _stats(taken_pnl_pcts, taken_rmults)

        # Per-class
        class_stats = {}
        for cls, recs in shadow_by_class.items():
            pnls = [r['pnl_pct'] for r in recs]
            rs = [r['r_mult'] for r in recs]
            class_stats[cls] = _stats(pnls, rs)

        # Per-reason (the filters themselves)
        reason_stats = {}
        for reason, recs in shadow_by_reason.items():
            pnls = [r['pnl_pct'] for r in recs]
            rs = [r['r_mult'] for r in recs]
            s = _stats(pnls, rs)
            # FVS = EV(blocked) - EV(taken). Positive = filter destroying edge.
            fvs = None
            if s['ev_r'] is not None and taken_stats['ev_r'] is not None:
                fvs = round(s['ev_r'] - taken_stats['ev_r'], 3)
            s['fvs_r'] = fvs
            s['rejection_class'] = _shadow_rej.classify_reason(reason) if _SR_OK and _shadow_rej else 'UNCLASSIFIED'
            reason_stats[reason] = s

        # ─── DELTAS ────────────────────────────────────────────
        ev_taken_r = taken_stats['ev_r']
        ev_capacity_r = class_stats.get('CAPACITY', {}).get('ev_r')
        ev_edge_r = class_stats.get('EDGE', {}).get('ev_r')
        delta_capacity = (ev_capacity_r - ev_taken_r) if (ev_capacity_r is not None and ev_taken_r is not None) else None
        delta_edge = (ev_edge_r - ev_taken_r) if (ev_edge_r is not None and ev_taken_r is not None) else None

        # ─── OPPORTUNITY LOSS ─────────────────────────────────
        # R per day suppressed. Pending rejections × per-reason EV × (hours_window/24) factor.
        opp_loss = {'capacity_r_per_day': None, 'edge_r_per_day': None,
                    'capacity_n_pending': 0, 'edge_n_pending': 0}
        if _SR_OK and _shadow_rej is not None:
            try:
                with _shadow_rej._LOCK:
                    pending = list(_shadow_rej._PENDING)
                    resolved_count = len(_shadow_rej._RESOLVED)
                # Get age of oldest resolved record for daily rate extrapolation
                if pending:
                    oldest_ts = min(p['created_ts'] for p in pending)
                    age_days = max((time.time() - oldest_ts) / 86400, 0.0001)
                else:
                    age_days = 1

                cap_pending = sum(1 for p in pending
                                  if _shadow_rej.classify_reason(p.get('reason', '')) == 'CAPACITY')
                edge_pending = sum(1 for p in pending
                                   if _shadow_rej.classify_reason(p.get('reason', '')) == 'EDGE')
                opp_loss['capacity_n_pending'] = cap_pending
                opp_loss['edge_n_pending'] = edge_pending

                if ev_capacity_r is not None:
                    # Assume pending resolve to same EV as historical resolved
                    opp_loss['capacity_r_per_day'] = round(
                        (cap_pending / age_days) * ev_capacity_r, 2)
                if ev_edge_r is not None:
                    opp_loss['edge_r_per_day'] = round(
                        (edge_pending / age_days) * ev_edge_r, 2)
            except Exception:
                pass

        # ─── DECISION SIGNALS ─────────────────────────────────
        decisions = {
            'filters_optimal': 'UNCLEAR',
            'most_restrictive_on_positive_ev': None,
            'most_effective_remover_negative_ev': None,
            'expand_whitelist': 'UNCLEAR',
            'raise_max_positions': 'UNCLEAR',
            'notes': [],
        }

        # Check elite whitelist directly
        whitelist = reason_stats.get('not_elite_whitelisted', {})
        if whitelist.get('n', 0) >= 10:  # min sample
            fvs = whitelist.get('fvs_r', 0) or 0
            ev = whitelist.get('ev_r', 0) or 0
            if ev > 0 and fvs > 0.1:
                decisions['expand_whitelist'] = 'YES'
                decisions['notes'].append(
                    f"not_elite_whitelisted: n={whitelist['n']}, EV={ev:+.2f}R > taken "
                    f"EV, FVS={fvs:+.2f}R. Filter is blocking winners.")
            elif ev < 0 and fvs < -0.1:
                decisions['expand_whitelist'] = 'NO'
                decisions['notes'].append(
                    f"not_elite_whitelisted: EV={ev:+.2f}R, correctly blocking losers.")
            else:
                decisions['expand_whitelist'] = 'CONDITIONAL'
        else:
            decisions['notes'].append(
                f"not_elite_whitelisted sample too small ({whitelist.get('n', 0)}). Need 10+.")

        # Max positions assessment
        if ev_capacity_r is not None and class_stats.get('CAPACITY', {}).get('n', 0) >= 10:
            if ev_capacity_r > 0 and (delta_capacity or 0) > 0.1:
                decisions['raise_max_positions'] = 'YES'
                decisions['notes'].append(
                    f"CAPACITY: EV={ev_capacity_r:+.2f}R, better than taken "
                    f"EV={ev_taken_r:+.2f}R. Leaving money on table.")
            elif ev_capacity_r < 0:
                decisions['raise_max_positions'] = 'NO'
                decisions['notes'].append(
                    f"CAPACITY: EV={ev_capacity_r:+.2f}R, cap correctly blocking losers.")
            else:
                decisions['raise_max_positions'] = 'CONDITIONAL'
        else:
            decisions['notes'].append(
                f"CAPACITY sample too small (n={class_stats.get('CAPACITY', {}).get('n', 0)}).")

        # Most restrictive filter on positive EV
        positive_fvs = [
            (r, s) for r, s in reason_stats.items()
            if s.get('n', 0) >= 5 and (s.get('fvs_r') or 0) > 0
        ]
        if positive_fvs:
            positive_fvs.sort(key=lambda x: -(x[1]['fvs_r'] or 0))
            most_restrictive = positive_fvs[0]
            decisions['most_restrictive_on_positive_ev'] = {
                'reason': most_restrictive[0],
                'fvs_r': most_restrictive[1]['fvs_r'],
                'n': most_restrictive[1]['n'],
            }

        # Most effective at removing negative EV
        negative_fvs = [
            (r, s) for r, s in reason_stats.items()
            if s.get('n', 0) >= 5 and (s.get('fvs_r') or 0) < 0
        ]
        if negative_fvs:
            negative_fvs.sort(key=lambda x: (x[1]['fvs_r'] or 0))
            most_effective = negative_fvs[0]
            decisions['most_effective_remover_negative_ev'] = {
                'reason': most_effective[0],
                'fvs_r': most_effective[1]['fvs_r'],
                'n': most_effective[1]['n'],
            }

        # Overall optimality
        if decisions['expand_whitelist'] == 'YES' or decisions['raise_max_positions'] == 'YES':
            decisions['filters_optimal'] = 'NO'
        elif decisions['expand_whitelist'] == 'NO' and decisions['raise_max_positions'] == 'NO':
            decisions['filters_optimal'] = 'YES'
        else:
            decisions['filters_optimal'] = 'UNCLEAR'

        result = {
            'as_of_utc': time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(now_ms/1000)),
            'live_window_hours': hours,
            'shadow_all_time': True,
            'taken': taken_stats,
            'by_class': class_stats,
            'by_reason': reason_stats,
            'deltas': {
                'ev_taken_r': ev_taken_r,
                'ev_capacity_r': ev_capacity_r,
                'ev_edge_r': ev_edge_r,
                'delta_capacity': round(delta_capacity, 3) if delta_capacity is not None else None,
                'delta_edge': round(delta_edge, 3) if delta_edge is not None else None,
            },
            'opportunity_loss': opp_loss,
            'decisions': decisions,
        }

        if fmt != 'html':
            return jsonify(result)

        # ─── HTML RENDERING ────────────────────────────────────
        def _fmt_r(v):
            if v is None: return '—'
            s = f"{v:+.2f}R"
            return s

        def _color(v):
            if v is None: return 'cool'
            if v > 0.1: return 'ok'
            if v < -0.1: return 'hot'
            return 'amber'

        # Reason rows, sorted by FVS (most destructive first)
        reason_items = sorted(
            reason_stats.items(),
            key=lambda x: -(x[1].get('fvs_r') or -999)
        )
        reason_rows = ''
        for reason, s in reason_items:
            fvs = s.get('fvs_r')
            fvs_color = _color(fvs)
            verdict = (
                'DESTROYING EDGE' if fvs and fvs > 0.1 else
                'NEUTRAL' if fvs is not None and abs(fvs) <= 0.1 else
                'REMOVING LOSERS' if fvs and fvs < -0.1 else
                '—'
            )
            wr_s = f"{s.get('wr')*100:.0f}%" if s.get('wr') is not None else '—'
            ev_r = s.get('ev_r')
            ev_s = _fmt_r(ev_r)
            reason_rows += (
                f'<tr><td class="mono">{reason}</td>'
                f'<td style="font-size:9px;letter-spacing:0.2em">{s.get("rejection_class", "—")}</td>'
                f'<td class="num">{s.get("n", 0)}</td>'
                f'<td class="num">{wr_s}</td>'
                f'<td class="num {_color(ev_r)}">{ev_s}</td>'
                f'<td class="num {fvs_color}" style="font-weight:600">{_fmt_r(fvs)}</td>'
                f'<td style="font-size:10px;color:var(--{fvs_color})">{verdict}</td></tr>'
            )

        # Decision pills
        def _decision_pill(label, value):
            if value in ('YES',):
                color = 'hot'
            elif value in ('NO',):
                color = 'ok'
            elif value == 'CONDITIONAL':
                color = 'amber'
            else:
                color = 'cool'
            return f'<span style="padding:3px 10px;border:1px solid var(--{color});color:var(--{color});font-size:11px;letter-spacing:0.2em">{value}</span>'

        opt_color = {'YES': 'ok', 'NO': 'hot', 'UNCLEAR': 'amber'}.get(decisions['filters_optimal'], 'cool')

        mr = decisions.get('most_restrictive_on_positive_ev')
        mr_str = f"<strong class='mono hot'>{mr['reason']}</strong> (FVS {mr['fvs_r']:+.2f}R, n={mr['n']})" if mr else '<span style="opacity:0.5">none detected</span>'

        me = decisions.get('most_effective_remover_negative_ev')
        me_str = f"<strong class='mono ok'>{me['reason']}</strong> (FVS {me['fvs_r']:+.2f}R, n={me['n']})" if me else '<span style="opacity:0.5">none detected</span>'

        notes_html = ''.join(f'<li style="margin-bottom:6px">{n}</li>' for n in decisions.get('notes', []))

        html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Filter Elasticity · {result['as_of_utc']}</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;600&family=Cormorant+Garamond:wght@300;400&display=swap" rel="stylesheet">
<style>
:root{{--void:#07080a;--carbon:#0d0e11;--chrome:#c8ccd4;--bone:#d9d6cd;--acid:#b8ff2f;--hot:#ef4444;--amber:#f59e0b;--cool:#64748b;--oxide:#d97706;--line:rgba(200,204,212,0.08)}}
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:var(--void);color:var(--chrome);font-family:'JetBrains Mono',monospace;font-size:12px;padding:20px;line-height:1.5}}
h1{{font-family:'Cormorant Garamond',serif;font-weight:300;font-size:28px;color:var(--bone);margin-bottom:4px}}
h2{{font-size:11px;letter-spacing:0.35em;text-transform:uppercase;color:var(--bone);padding-bottom:8px;border-bottom:1px solid var(--line);margin-bottom:12px}}
.sub{{font-size:10px;letter-spacing:0.3em;text-transform:uppercase;color:var(--chrome);opacity:0.5;margin-bottom:20px}}
.grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:0;margin-bottom:24px;padding:18px 0;border-top:1px solid var(--acid);border-bottom:1px solid var(--line)}}
.stat{{padding:0 18px;border-left:1px solid var(--line)}}
.stat:first-child{{border-left:none}}
.stat-val{{font-family:'Cormorant Garamond',serif;font-size:28px;line-height:1;color:var(--bone)}}
.stat-sub{{font-size:10px;color:var(--chrome);opacity:0.55;margin-top:3px}}
.stat-lbl{{font-size:9px;letter-spacing:0.3em;text-transform:uppercase;opacity:0.55;margin-top:8px}}
.ok{{color:var(--acid)}}.hot{{color:var(--hot)}}.amber{{color:var(--amber)}}.cool{{color:var(--cool)}}
.section{{margin-bottom:24px}}
table{{width:100%;border-collapse:collapse;font-size:11px}}
th{{text-align:left;padding:6px 10px;font-size:9px;letter-spacing:0.25em;text-transform:uppercase;opacity:0.6;border-bottom:1px solid var(--line);font-weight:400}}
td{{padding:6px 10px;border-bottom:1px solid rgba(200,204,212,0.04);color:var(--bone)}}
.num{{text-align:right;font-variant-numeric:tabular-nums}}
.mono{{font-family:'JetBrains Mono',monospace}}
.decisions{{padding:20px;border:1px solid var(--{opt_color});background:linear-gradient(180deg,rgba(184,255,47,0.03),transparent);margin-bottom:24px}}
.decisions h3{{font-size:11px;letter-spacing:0.3em;text-transform:uppercase;color:var(--{opt_color});font-weight:600}}
.decisions .v{{font-family:'Cormorant Garamond',serif;font-size:34px;margin:6px 0;color:var(--bone)}}
.dec-row{{display:grid;grid-template-columns:240px 1fr;gap:12px;padding:6px 0;border-bottom:1px solid var(--line);font-size:11px}}
.dec-row:last-child{{border:none}}
.dec-lbl{{color:var(--chrome);opacity:0.7;text-transform:uppercase;font-size:10px;letter-spacing:0.2em}}
ul{{padding-left:18px;font-size:11px}}
a{{color:var(--acid)}}
@media(max-width:700px){{.grid{{grid-template-columns:1fr}}.dec-row{{grid-template-columns:1fr}}}}
</style></head><body>
{PRECOG_NAV}
<div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:8px">
<h1>Filter Elasticity Analysis</h1>
<a href="/violations" style="font-size:10px;letter-spacing:0.3em">VIOLATIONS →</a>
</div>
<div class="sub">{result['as_of_utc']} UTC · live window {hours}h · shadow all-time</div>

<div class="decisions">
    <h3>Decision Signal</h3>
    <div class="v {opt_color}">{decisions['filters_optimal']}</div>
    <div style="font-size:11px;color:var(--chrome);opacity:0.8;margin-bottom:16px">Are filters optimal?</div>

    <div class="dec-row"><div class="dec-lbl">Expand whitelist?</div><div>{_decision_pill('', decisions['expand_whitelist'])}</div></div>
    <div class="dec-row"><div class="dec-lbl">Raise max positions?</div><div>{_decision_pill('', decisions['raise_max_positions'])}</div></div>
    <div class="dec-row"><div class="dec-lbl">Most restrictive on +EV</div><div>{mr_str}</div></div>
    <div class="dec-row"><div class="dec-lbl">Best at removing -EV</div><div>{me_str}</div></div>
</div>

<div class="grid">
    <div class="stat">
        <div class="stat-val">{_fmt_r(ev_taken_r)}</div>
        <div class="stat-sub">n={taken_stats['n']} · WR {taken_stats.get('wr', 0)*100 if taken_stats.get('wr') else 0:.0f}%</div>
        <div class="stat-lbl">EV TAKEN</div>
    </div>
    <div class="stat">
        <div class="stat-val {_color(ev_capacity_r)}">{_fmt_r(ev_capacity_r)}</div>
        <div class="stat-sub">n={class_stats.get('CAPACITY',{}).get('n',0)} · Δ {_fmt_r(delta_capacity)}</div>
        <div class="stat-lbl">EV CAPACITY</div>
    </div>
    <div class="stat">
        <div class="stat-val {_color(ev_edge_r)}">{_fmt_r(ev_edge_r)}</div>
        <div class="stat-sub">n={class_stats.get('EDGE',{}).get('n',0)} · Δ {_fmt_r(delta_edge)}</div>
        <div class="stat-lbl">EV EDGE</div>
    </div>
</div>

<div class="section">
    <h2>Per-Filter Value Score (FVS = EV_blocked − EV_taken)</h2>
    <table>
        <thead>
            <tr>
                <th>FILTER</th>
                <th>CLASS</th>
                <th class="num">N</th>
                <th class="num">WR</th>
                <th class="num">EV/trade</th>
                <th class="num">FVS</th>
                <th>VERDICT</th>
            </tr>
        </thead>
        <tbody>{reason_rows or '<tr><td colspan=7 style="opacity:0.5;padding:20px">No resolved shadow data yet. Wait for shadow rejections to hit TP or SL.</td></tr>'}</tbody>
    </table>
    <div style="margin-top:10px;font-size:10px;opacity:0.55;line-height:1.6">
        <strong style="color:var(--hot)">FVS &gt; +0.1R</strong> = filter is destroying edge (blocking winners) ·
        <strong style="color:var(--amber)">|FVS| ≤ 0.1R</strong> = neutral ·
        <strong style="color:var(--acid)">FVS &lt; -0.1R</strong> = filter is correctly removing losers
    </div>
</div>

<div class="section">
    <h2>Opportunity Loss (Pending Pipeline)</h2>
    <div style="font-size:11px;line-height:1.8">
        <div>CAPACITY rejections pending: <strong>{opp_loss['capacity_n_pending']}</strong> · R/day at historical EV: <strong>{opp_loss['capacity_r_per_day'] if opp_loss['capacity_r_per_day'] is not None else '—'}</strong></div>
        <div>EDGE rejections pending: <strong>{opp_loss['edge_n_pending']}</strong> · R/day at historical EV: <strong>{opp_loss['edge_r_per_day'] if opp_loss['edge_r_per_day'] is not None else '—'}</strong></div>
    </div>
</div>

<div class="section">
    <h2>Notes</h2>
    <ul>{notes_html or '<li style="opacity:0.5">(no notes)</li>'}</ul>
</div>

<div style="margin-top:40px;padding-top:12px;border-top:1px solid var(--line);font-size:9px;opacity:0.5;letter-spacing:0.2em;text-transform:uppercase">
    <a href="/audit/deep?format=html">DEEP AUDIT</a> ·
    <a href="?hours=24&format=html">24h</a> ·
    <a href="?hours=48&format=html">48h</a> ·
    <a href="?hours=168&format=html">7d</a> ·
    <a href="?format=json">json</a>
</div>

</body></html>"""
        resp = Response(html, mimetype='text/html')
        resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        return resp
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'tb': traceback.format_exc()[-800:]}), 500


@app.route('/enforce', methods=['GET'])
def enforce_status():
    """Enforce Protection stats — replacement/verification counts,
    deadline breach counters, currently-halted coins."""
    try:
        if not _EP_OK or _ep is None:
            return jsonify({'error': 'enforce_protection not loaded'}), 503
        data = _ep.stats()
        if flask_request.args.get('format') == 'json':
            return jsonify(data)
        # HTML view
        halted = data.get('currently_halted', {})
        halted_rows = ''.join(
            f'<tr><td class="mono hot">{c}</td><td class="num">{t:.0f}s</td></tr>'
            for c, t in halted.items()
        ) or '<tr><td colspan=2 style="opacity:0.5;padding:20px">no halted coins</td></tr>'
        by_coin = data.get('by_coin', {})
        coin_rows = ''.join(
            f'<tr><td class="mono">{c}</td><td class="num">{v.get("enforced")}</td>'
            f'<td class="num">{v.get("replaced")}</td><td class="num">{v.get("failed")}</td></tr>'
            for c, v in sorted(by_coin.items(), key=lambda x: -x[1].get("enforced", 0))[:20]
        ) or '<tr><td colspan=4 style="opacity:0.5;padding:20px">no enforcement events</td></tr>'
        dl = data.get('deadlines', {})
        html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"><title>ENFORCE · Protection Contract</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;600&family=Cormorant+Garamond:wght@300;400&display=swap" rel="stylesheet">
<style>
:root{{--void:#07080a;--chrome:#c8ccd4;--bone:#d9d6cd;--acid:#b8ff2f;--hot:#ef4444;--amber:#f59e0b;--cool:#64748b;--line:rgba(200,204,212,0.08)}}
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:var(--void);color:var(--chrome);font-family:'JetBrains Mono',monospace;font-size:12px;padding:20px;line-height:1.5}}
h1{{font-family:'Cormorant Garamond',serif;font-weight:300;font-size:28px;color:var(--bone)}}
h2{{font-size:11px;letter-spacing:0.35em;text-transform:uppercase;color:var(--bone);padding-bottom:8px;border-bottom:1px solid var(--line);margin-bottom:12px}}
.sub{{font-size:10px;letter-spacing:0.3em;text-transform:uppercase;opacity:0.5;margin-bottom:20px}}
.grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:0;margin-bottom:24px;padding:18px 0;border-top:1px solid var(--acid);border-bottom:1px solid var(--line)}}
.stat{{padding:0 18px;border-left:1px solid var(--line)}}
.stat:first-child{{border:none}}
.stat-val{{font-family:'Cormorant Garamond',serif;font-size:26px;color:var(--bone)}}
.stat-lbl{{font-size:9px;letter-spacing:0.3em;opacity:0.55;margin-top:6px}}
.ok{{color:var(--acid)}}.hot{{color:var(--hot)}}.amber{{color:var(--amber)}}.cool{{color:var(--cool)}}
.section{{margin-bottom:24px}}
table{{width:100%;border-collapse:collapse;font-size:11px}}
th{{text-align:left;padding:6px 10px;font-size:9px;letter-spacing:0.25em;opacity:0.6;border-bottom:1px solid var(--line);font-weight:400}}
td{{padding:6px 10px;border-bottom:1px solid rgba(200,204,212,0.04);color:var(--bone)}}
.num{{text-align:right;font-variant-numeric:tabular-nums}}
.mono{{font-family:'JetBrains Mono',monospace}}
.two{{display:grid;grid-template-columns:1fr 1fr;gap:20px}}
a{{color:var(--acid)}}
@media(max-width:700px){{.grid{{grid-template-columns:1fr 1fr}}.two{{grid-template-columns:1fr}}}}
</style></head><body>
{PRECOG_NAV}
<h1>Enforce Protection Contract</h1>
<div class="sub">Deadlines: SL ≤ {dl.get('sl_sec')}s (critical) · TP ≤ {dl.get('tp_sec')}s (repair) · FULL ≤ {dl.get('full_sec')}s</div>

<div class="grid">
    <div class="stat"><div class="stat-val">{data.get('enforced_total', 0)}</div><div class="stat-lbl">Enforced</div></div>
    <div class="stat"><div class="stat-val ok">{data.get('verified_ok', 0)}</div><div class="stat-lbl">Verified OK</div></div>
    <div class="stat"><div class="stat-val amber">{data.get('replaced', 0)}</div><div class="stat-lbl">Replaced</div></div>
    <div class="stat"><div class="stat-val hot">{data.get('failed', 0)}</div><div class="stat-lbl">Failed</div></div>
    <div class="stat"><div class="stat-val hot">{data.get('sl_deadline_breach', 0)}</div><div class="stat-lbl">SL Breaches</div></div>
    <div class="stat"><div class="stat-val amber">{data.get('tp_deadline_breach', 0)}</div><div class="stat-lbl">TP Breaches</div></div>
    <div class="stat"><div class="stat-val hot">{data.get('emergency_closes', 0)}</div><div class="stat-lbl">Emergency Closes</div></div>
    <div class="stat"><div class="stat-val">{data.get('coin_halts_total', 0)}</div><div class="stat-lbl">Halt Total</div></div>
</div>

<div class="two">
    <div class="section">
        <h2>Currently Halted Coins</h2>
        <table><thead><tr><th>COIN</th><th class="num">SECONDS LEFT</th></tr></thead>
        <tbody>{halted_rows}</tbody></table>
    </div>
    <div class="section">
        <h2>By Coin (top 20)</h2>
        <table><thead><tr><th>COIN</th><th class="num">ENFORCED</th><th class="num">REPLACED</th><th class="num">FAILED</th></tr></thead>
        <tbody>{coin_rows}</tbody></table>
    </div>
</div>

<div style="margin-top:40px;padding-top:12px;border-top:1px solid var(--line);font-size:9px;opacity:0.5;letter-spacing:0.2em;text-transform:uppercase">
Auto-refreshes on navigation · <a href="?format=json">json</a>
</div>
<script>setTimeout(function(){{location.reload()}},30000);</script>
</body></html>"""
        resp = Response(html, mimetype='text/html')
        resp.headers['Cache-Control'] = 'no-cache'
        return resp
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/shadow/compare', methods=['GET'])
def shadow_compare():
    """Live vs Shadow expectancy comparison — the scientific validation endpoint.

    Pulls live trades from past N hours (default 48), computes R-multiples,
    then invokes compare_live_vs_shadow with shadow resolved trades.

    Returns EV delta + per-reason breakdown.
    Query: ?hours=N&format=html|json
    """
    try:
        try: hours = float(flask_request.args.get('hours', '48'))
        except: hours = 48.0
        fmt = flask_request.args.get('format', 'json').lower()
        if not _SR_OK or _shadow_rej is None:
            return jsonify({'error': 'shadow_trades not loaded'}), 503

        # Fetch live fills, compute R-multiples using DEFAULT SL 2.5% (matches shadow friction model)
        fills, _ = _fetch_user_fills_cached()
        now_ms = int(time.time() * 1000)
        cutoff = now_ms - int(hours * 3600 * 1000)
        DEFAULT_SL_PCT = 2.5  # percent
        live_rmults = []
        for f in fills:
            if f.get('time', 0) < cutoff: continue
            pnl_usd = float(f.get('closedPnl', 0) or 0)
            if pnl_usd == 0: continue
            try:
                entry_px = float(f.get('px', 0) or 0)
                sz = abs(float(f.get('sz', 0) or 0))
                notional = entry_px * sz
                if notional <= 0: continue
                pnl_pct_pct = (pnl_usd / notional) * 100.0  # percent
                r_mult = pnl_pct_pct / DEFAULT_SL_PCT
                live_rmults.append(r_mult)
            except Exception:
                continue

        cmp_result = _shadow_rej.compare_live_vs_shadow(live_rmults)
        cmp_result['as_of_utc'] = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())
        cmp_result['window_hours'] = hours
        cmp_result['live_sample_note'] = 'R-mult assumes DEFAULT_SL=2.5%; shadow uses exact sl_pct'

        if fmt != 'html':
            return jsonify(cmp_result)

        # HTML rendering
        live = cmp_result.get('live', {})
        shadow = cmp_result.get('shadow', {})
        ev_delta = cmp_result.get('ev_delta')
        confidence = cmp_result.get('confidence', 'insufficient')

        def _c(v):
            if v is None: return 'cool'
            if v > 0.05: return 'ok'
            if v < -0.05: return 'hot'
            return 'amber'

        def _fmt_r(v):
            return f"{v:+.2f}R" if v is not None else "—"

        def _fmt_wr(v):
            return f"{v*100:.0f}%" if v is not None else "—"

        # Interpretation
        if ev_delta is None or confidence == 'insufficient':
            verdict_label = 'INSUFFICIENT DATA'
            verdict_color = 'cool'
            verdict_desc = f'Need min 30 samples (live={live.get("n",0)}, shadow={shadow.get("n",0)}).'
        elif ev_delta > 0.1:
            verdict_label = 'FILTERS CREATING EDGE'
            verdict_color = 'ok'
            verdict_desc = f'Live EV is {ev_delta:+.2f}R better than rejected trades. Keep filters.'
        elif ev_delta < -0.1:
            verdict_label = 'FILTERS DESTROYING EDGE'
            verdict_color = 'hot'
            verdict_desc = f'Live EV is {ev_delta:.2f}R WORSE than rejected trades. Loosen filters.'
        else:
            verdict_label = 'FILTERS NEUTRAL'
            verdict_color = 'amber'
            verdict_desc = f'EV delta {ev_delta:+.2f}R within noise. Simpler = better; consider removing weakest filters.'

        reason_rows = ''
        for reason, s in sorted(cmp_result.get('by_reason', {}).items(), key=lambda x: -(x[1].get('fvs_r') or -999)):
            reason_rows += (
                f'<tr>'
                f'<td class="mono">{reason}</td>'
                f'<td style="font-size:9px;letter-spacing:0.2em">{s.get("class", "—")}</td>'
                f'<td class="num">{s.get("n")}</td>'
                f'<td class="num">{_fmt_wr(s.get("wr"))}</td>'
                f'<td class="num {_c(s.get("avg_r"))}">{_fmt_r(s.get("avg_r"))}</td>'
                f'<td class="num {_c(s.get("fvs_r"))}" style="font-weight:600">{_fmt_r(s.get("fvs_r"))}</td>'
                f'</tr>'
            )

        class_cards = ''
        for cls, s in cmp_result.get('by_class', {}).items():
            avg_r = s.get('avg_r')
            color = _c(avg_r)
            class_cards += (
                f'<div style="padding:14px;border:1px solid var(--{color})">'
                f'<div style="font-size:10px;letter-spacing:0.3em;color:var(--{color})">{cls}</div>'
                f'<div style="font-family:\'Cormorant Garamond\',serif;font-size:22px;color:var(--bone);margin-top:6px">{_fmt_r(avg_r)}</div>'
                f'<div style="font-size:10px;opacity:0.5;margin-top:4px">n={s.get("n")} · WR={_fmt_wr(s.get("wr"))}</div>'
                f'</div>'
            )

        html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Shadow vs Live · Expectancy</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;600&family=Cormorant+Garamond:wght@300;400&display=swap" rel="stylesheet">
<style>
:root{{--void:#07080a;--carbon:#0d0e11;--chrome:#c8ccd4;--bone:#d9d6cd;--acid:#b8ff2f;--hot:#ef4444;--amber:#f59e0b;--cool:#64748b;--line:rgba(200,204,212,0.08)}}
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:var(--void);color:var(--chrome);font-family:'JetBrains Mono',monospace;font-size:12px;padding:20px;line-height:1.5}}
h1{{font-family:'Cormorant Garamond',serif;font-weight:300;font-size:28px;color:var(--bone)}}
h2{{font-size:11px;letter-spacing:0.35em;text-transform:uppercase;color:var(--bone);padding-bottom:8px;border-bottom:1px solid var(--line);margin-bottom:12px}}
.sub{{font-size:10px;letter-spacing:0.3em;text-transform:uppercase;opacity:0.5;margin-bottom:20px}}
.verdict{{padding:20px;border:1px solid var(--{verdict_color});margin-bottom:24px}}
.verdict .lbl{{font-size:11px;letter-spacing:0.3em;color:var(--{verdict_color});font-weight:600}}
.verdict .v{{font-family:'Cormorant Garamond',serif;font-size:32px;color:var(--bone);margin:6px 0}}
.verdict .desc{{font-size:12px;opacity:0.8}}
.pair{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:0;padding:18px 0;border-top:1px solid var(--acid);border-bottom:1px solid var(--line);margin-bottom:24px}}
.col{{padding:0 18px;border-left:1px solid var(--line)}}
.col:first-child{{border:none}}
.col-lbl{{font-size:9px;letter-spacing:0.3em;color:var(--chrome);opacity:0.5;margin-bottom:8px;text-transform:uppercase}}
.col-val{{font-family:'Cormorant Garamond',serif;font-size:28px;color:var(--bone)}}
.col-sub{{font-size:10px;opacity:0.55;margin-top:4px}}
.ok{{color:var(--acid)}}.hot{{color:var(--hot)}}.amber{{color:var(--amber)}}.cool{{color:var(--cool)}}
table{{width:100%;border-collapse:collapse;font-size:11px}}
th{{text-align:left;padding:6px 10px;font-size:9px;letter-spacing:0.25em;opacity:0.6;border-bottom:1px solid var(--line);font-weight:400}}
td{{padding:6px 10px;border-bottom:1px solid rgba(200,204,212,0.04);color:var(--bone)}}
.num{{text-align:right;font-variant-numeric:tabular-nums}}
.mono{{font-family:'JetBrains Mono',monospace}}
.classes{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:24px}}
.section{{margin-bottom:24px}}
a{{color:var(--acid)}}
@media(max-width:700px){{.pair{{grid-template-columns:1fr}}.classes{{grid-template-columns:1fr}}}}
</style></head><body>
{PRECOG_NAV}
<div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:8px">
<h1>Shadow vs Live · Expectancy Validation</h1>
<a href="/violations" style="font-size:10px;letter-spacing:0.3em">AUDIT →</a>
</div>
<div class="sub">{cmp_result.get('as_of_utc')} UTC · live {hours}h · shadow all-time · confidence: {confidence}</div>

<div class="verdict">
    <div class="lbl">Verdict</div>
    <div class="v">{verdict_label}</div>
    <div class="desc">{verdict_desc}</div>
</div>

<div class="pair">
    <div class="col">
        <div class="col-lbl">LIVE</div>
        <div class="col-val {_c(live.get('avg_r'))}">{_fmt_r(live.get('avg_r'))}</div>
        <div class="col-sub">n={live.get('n',0)} · WR={_fmt_wr(live.get('wr'))} · σ={live.get('std_r',0):.2f}</div>
    </div>
    <div class="col">
        <div class="col-lbl">SHADOW</div>
        <div class="col-val {_c(shadow.get('avg_r'))}">{_fmt_r(shadow.get('avg_r'))}</div>
        <div class="col-sub">n={shadow.get('n',0)} · WR={_fmt_wr(shadow.get('wr'))} · σ={shadow.get('std_r',0):.2f}</div>
    </div>
    <div class="col">
        <div class="col-lbl">EV DELTA</div>
        <div class="col-val {_c(ev_delta)}">{_fmt_r(ev_delta)}</div>
        <div class="col-sub">live − shadow</div>
    </div>
</div>

<h2>By Rejection Class</h2>
<div class="classes">{class_cards or '<div style="opacity:0.5">no resolved shadow data yet</div>'}</div>

<h2>By Reason (min 5 samples)</h2>
<table>
<thead><tr><th>REASON</th><th>CLASS</th><th class="num">N</th><th class="num">WR</th><th class="num">EV/trade</th><th class="num">FVS (vs live)</th></tr></thead>
<tbody>{reason_rows or '<tr><td colspan=6 style="opacity:0.5;padding:20px">No reasons with enough samples yet</td></tr>'}</tbody>
</table>
<div style="margin-top:10px;font-size:10px;opacity:0.55">
<strong class="hot">FVS &gt; 0</strong> = filter blocks winners (loosen) · <strong class="ok">FVS &lt; 0</strong> = filter blocks losers (keep)
</div>

<div style="margin-top:40px;padding-top:12px;border-top:1px solid var(--line);font-size:9px;opacity:0.5;letter-spacing:0.2em;text-transform:uppercase">
Friction model: {cmp_result['friction']['fee_round_trip_pct']:.2f}% fees + {cmp_result['friction']['slippage_round_trip_pct']:.2f}% slip ·
<a href="?hours=24&format=html">24h</a> · <a href="?hours=48&format=html">48h</a> · <a href="?hours=168&format=html">7d</a> · <a href="?format=json">json</a>
</div>
</body></html>"""
        resp = Response(html, mimetype='text/html')
        resp.headers['Cache-Control'] = 'no-cache'
        return resp
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'tb': traceback.format_exc()[-500:]}), 500


@app.route('/experiment', methods=['GET'])
def experiment_status():
    """Promotion engine status — experimental bucket tracking, A/B test state."""
    try:
        if not _PROMO_OK or _promo is None:
            return jsonify({'error': 'promotion_engine not loaded'}), 503
        data = _promo.status()
        if flask_request.args.get('format') == 'json':
            return jsonify(data)

        def _fmt_r(v):
            if v is None: return '—'
            return f"{v:+.2f}R"

        active = data.get('active_positions', {})
        active_rows = ''.join(
            f'<tr><td class="mono">{c}</td><td>{info.get("side")}</td>'
            f'<td class="num">{int(time.time()-info.get("promoted_ts", time.time()))}s</td></tr>'
            for c, info in active.items()
        ) or '<tr><td colspan=3 style="opacity:0.5;padding:20px">no active experimental positions</td></tr>'

        recent = data.get('recent_resolved', [])[-12:]
        recent_rows = ''.join(
            f'<tr><td class="mono">{r.get("coin")}</td><td>{r.get("side")}</td>'
            f'<td class="num {"ok" if r.get("pnl_usd",0)>0 else "hot"}">${r.get("pnl_usd",0):+.2f}</td>'
            f'<td class="num">{_fmt_r(r.get("pnl_r"))}</td>'
            f'<td class="num">{int(r.get("held_sec") or 0)}s</td></tr>'
            for r in reversed(recent)
        ) or '<tr><td colspan=5 style="opacity:0.5;padding:20px">no resolutions yet</td></tr>'

        kill_banner = ''
        if data.get('kill_active'):
            secs = data.get('kill_paused_remaining_sec', 0)
            kill_banner = (
                f'<div style="padding:16px;border:1px solid var(--hot);margin-bottom:20px;background:rgba(239,68,68,0.05)">'
                f'<div style="font-size:11px;letter-spacing:0.3em;color:var(--hot);font-weight:600">KILL SWITCH ACTIVE</div>'
                f'<div style="margin-top:8px;font-size:12px">{data.get("kill_reason")} · resumes in {secs}s</div>'
                f'</div>'
            )

        enabled_label = 'ENABLED' if data.get('enabled') else 'DISABLED'
        enabled_color = 'ok' if data.get('enabled') else 'cool'

        html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Experiment · Whitelist Leak</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;600&family=Cormorant+Garamond:wght@300;400&display=swap" rel="stylesheet">
<style>
:root{{--void:#07080a;--chrome:#c8ccd4;--bone:#d9d6cd;--acid:#b8ff2f;--hot:#ef4444;--amber:#f59e0b;--cool:#64748b;--line:rgba(200,204,212,0.08)}}
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:var(--void);color:var(--chrome);font-family:'JetBrains Mono',monospace;font-size:12px;padding:20px;line-height:1.5}}
h1{{font-family:'Cormorant Garamond',serif;font-weight:300;font-size:28px;color:var(--bone)}}
h2{{font-size:11px;letter-spacing:0.35em;text-transform:uppercase;color:var(--bone);padding-bottom:8px;border-bottom:1px solid var(--line);margin-bottom:12px}}
.sub{{font-size:10px;letter-spacing:0.3em;text-transform:uppercase;opacity:0.5;margin-bottom:20px}}
.grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:0;margin-bottom:24px;padding:18px 0;border-top:1px solid var(--acid);border-bottom:1px solid var(--line)}}
.stat{{padding:0 18px;border-left:1px solid var(--line)}}
.stat:first-child{{border:none}}
.stat-val{{font-family:'Cormorant Garamond',serif;font-size:26px;color:var(--bone)}}
.stat-lbl{{font-size:9px;letter-spacing:0.3em;opacity:0.55;margin-top:6px}}
.ok{{color:var(--acid)}}.hot{{color:var(--hot)}}.amber{{color:var(--amber)}}.cool{{color:var(--cool)}}
.two{{display:grid;grid-template-columns:1fr 1fr;gap:20px}}
.section{{margin-bottom:24px}}
table{{width:100%;border-collapse:collapse;font-size:11px}}
th{{text-align:left;padding:6px 10px;font-size:9px;letter-spacing:0.25em;opacity:0.6;border-bottom:1px solid var(--line);font-weight:400}}
td{{padding:6px 10px;border-bottom:1px solid rgba(200,204,212,0.04);color:var(--bone)}}
.num{{text-align:right;font-variant-numeric:tabular-nums}}
.mono{{font-family:'JetBrains Mono',monospace}}
a{{color:var(--acid)}}
.kv{{display:grid;grid-template-columns:180px 1fr;gap:8px;font-size:11px}}
.kv dt{{opacity:0.55}}
@media(max-width:700px){{.grid{{grid-template-columns:1fr 1fr}}.two{{grid-template-columns:1fr}}}}
</style></head><body>
{PRECOG_NAV}
<h1>Whitelist Leak Experiment</h1>
<div class="sub">Status: <span class="{enabled_color}">{enabled_label}</span> · Leak {data.get('leak_rate',0)*100:.0f}% · Size {data.get('size_mult',0)*100:.0f}% · Kill at {data.get('kill_r_threshold')}R</div>

{kill_banner}

<div class="grid">
    <div class="stat"><div class="stat-val">{data.get('total_promoted', 0)}</div><div class="stat-lbl">Promoted</div></div>
    <div class="stat"><div class="stat-val">{data.get('total_resolved', 0)}</div><div class="stat-lbl">Resolved</div></div>
    <div class="stat"><div class="stat-val">{data.get('wins', 0)}/{data.get('losses', 0)}</div><div class="stat-lbl">W / L</div></div>
    <div class="stat"><div class="stat-val {('ok' if (data.get('bucket_r') or 0)>=0 else 'hot')}">{_fmt_r(data.get('bucket_r'))}</div><div class="stat-lbl">Bucket R</div></div>
    <div class="stat"><div class="stat-val {('ok' if (data.get('bucket_pnl_usd') or 0)>=0 else 'hot')}">${data.get('bucket_pnl_usd', 0):+.2f}</div><div class="stat-lbl">Bucket USD</div></div>
    <div class="stat"><div class="stat-val">{_fmt_r(data.get('avg_r_per_trade'))}</div><div class="stat-lbl">Avg R</div></div>
    <div class="stat"><div class="stat-val">{data.get('concurrent', 0)}/{data.get('max_concurrent', 1)}</div><div class="stat-lbl">Concurrent</div></div>
    <div class="stat"><div class="stat-val">{(data.get('win_rate') or 0)*100:.0f}%</div><div class="stat-lbl">Win Rate</div></div>
</div>

<div class="two">
    <div class="section">
        <h2>Active Experimental Positions</h2>
        <table><thead><tr><th>COIN</th><th>SIDE</th><th class="num">HELD</th></tr></thead>
        <tbody>{active_rows}</tbody></table>
    </div>
    <div class="section">
        <h2>Recent Resolutions (last 12)</h2>
        <table><thead><tr><th>COIN</th><th>SIDE</th><th class="num">USD</th><th class="num">R</th><th class="num">HELD</th></tr></thead>
        <tbody>{recent_rows}</tbody></table>
    </div>
</div>

<div class="section">
    <h2>Parameters</h2>
    <dl class="kv">
    <dt>Equity floor</dt><dd>${data.get('equity_floor')}</dd>
    <dt>Per-coin cooldown</dt><dd>{data.get('per_coin_cooldown_sec')}s</dd>
    <dt>Kill pause</dt><dd>24h (after threshold breach)</dd>
    </dl>
</div>

<div style="margin-top:40px;padding-top:12px;border-top:1px solid var(--line);font-size:9px;opacity:0.5;letter-spacing:0.2em;text-transform:uppercase">
Auto-refreshes every 30s · <a href="?format=json">json</a>
</div>
<script>setTimeout(function(){{location.reload()}},30000);</script>
</body></html>"""
        resp = Response(html, mimetype='text/html')
        resp.headers['Cache-Control'] = 'no-cache'
        return resp
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/experiment/reset_kill', methods=['POST'])
def experiment_reset_kill():
    """Manual kill-switch reset. POST-only to avoid accidental browser resets."""
    try:
        if not _PROMO_OK or _promo is None:
            return jsonify({'error': 'promotion_engine not loaded'}), 503
        _promo.reset_kill_switch()
        return jsonify({'status': 'kill_switch_reset', 'new_state': _promo.status()})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/shadow_trades', methods=['GET'])
def shadow_trades_status():
    """Shadow trades — rejected-trade outcome tracking. Would-have-been
    expectancy by rejection reason."""
    try:
        if not _SR_OK or _shadow_rej is None:
            return jsonify({'error': 'shadow_trades not loaded'}), 503
        return jsonify(_shadow_rej.status())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/shadow', methods=['GET'])
def shadow_status():
    """Shadow thresholds — silent variant evaluation (no LLM routing).
    Logs would-fire state for relaxed RSI/pivot variants per bar."""
    try:
        if not _SH_OK or _shadow is None:
            return jsonify({'error': 'shadow not loaded'}), 503
        return jsonify(_shadow.get_stats())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/funding_sig', methods=['GET'])
def funding_sig_status():
    """Funding rate standalone signal — extreme funding mean-reversion trigger."""
    try:
        if not _FS_OK or _funding_sig is None:
            return jsonify({'error': 'funding_sig not loaded'}), 503
        return jsonify(_funding_sig.status())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/reflexivity', methods=['GET'])
def reflexivity_status():
    """Reflexivity detector — crowding + move position + echo scoring.
    LEAD/FOLLOW/SKEPTICAL/AVOID recommendation logged silently.
    At 75+ outcomes, flags trigger for LEAD vs AVOID bucket analysis."""
    try:
        if not _RX_OK or _reflex is None:
            return jsonify({'error': 'reflexivity not loaded'}), 503
        return jsonify(_reflex.get_stats())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/monitor', methods=['GET'])
def monitor_status():
    """Live monitoring: rolling 50-trade WR/expectancy/avg$win/loss + alerts."""
    try:
        import monitor
        return jsonify(monitor.status())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/alerts', methods=['GET'])
def get_alerts():
    """All pending alerts. Optional ?severity=CRITICAL|WARN filter."""
    try:
        import monitor
        from flask import request
        sev = request.args.get('severity')
        return jsonify({'alerts': monitor.get_alerts(sev)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

LOG_BUFFER = []


@app.route('/trades', methods=['GET'])
def get_trades():
    """Return trade log CSV as JSON for analysis. Tolerates None/empty pnl values."""
    try:
        import csv
        def safe_pnl(v):
            if v is None or v == '': return None
            try: return float(v)
            except (ValueError, TypeError): return None
        trades = []
        with open(TRADE_LOG) as f:
            reader = csv.DictReader(f)
            for row in reader:
                trades.append(row)
        wins = sum(1 for t in trades if (p := safe_pnl(t.get('pnl'))) is not None and p > 0)
        losses = sum(1 for t in trades if (p := safe_pnl(t.get('pnl'))) is not None and p < 0)
        total_pnl = sum(p for t in trades if (p := safe_pnl(t.get('pnl'))) is not None)
        return jsonify({'trades': trades[-50:], 'total': len(trades),
                        'wins': wins, 'losses': losses, 'total_pnl': round(total_pnl, 4)})
    except Exception as e:
        return jsonify({'error': str(e), 'trades': []})

@app.route('/trades/recent', methods=['GET'])
def get_trades_recent():
    """Trade log filtered to recent N hours (default 12), with per-engine/side breakdown."""
    try:
        import csv
        from datetime import datetime, timedelta, timezone
        hours = int(flask_request.args.get('hours', '12'))
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        def safe_pnl(v):
            if v is None or v == '': return None
            try: return float(v)
            except (ValueError, TypeError): return None
        def parse_ts(s):
            try:
                # ISO format from datetime.utcnow().isoformat() — naive UTC
                return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
            except Exception: return None
        trades = []
        with open(TRADE_LOG) as f:
            reader = csv.DictReader(f)
            for row in reader:
                ts = parse_ts(row.get('timestamp', ''))
                if not ts or ts < cutoff: continue
                row['_ts'] = ts.isoformat()
                row['_pnl_f'] = safe_pnl(row.get('pnl'))
                trades.append(row)
        # Aggregate
        by_engine = {}
        by_side = {}
        by_coin = {}
        by_hour = {}
        # 2026-04-26: noise engines (RECONCILED + untagged_legacy) are
        # bookkeeping artifacts, not strategy decisions. They still get
        # bucketed but flagged so the user can ignore them when judging
        # engine quality. RECONCILED = adopted from exchange or
        # missing-close cleanup. untagged_legacy = pre-attribution-fix
        # rows that didn't carry engine through CLOSE.
        NOISE_ENGINES = {'RECONCILED', 'untagged_legacy'}
        def _engine_label(raw):
            r = (raw or '').strip()
            if not r:
                return 'untagged_legacy'
            return r
        for t in trades:
            p = t.get('_pnl_f')
            if p is None: continue
            # 2026-04-26: by_side was using `direction` which is always
            # 'CLOSE' on close events — gave a single useless bucket.
            # Use `side` (BUY/SELL preserved through close) instead.
            for bucket, key in [(by_engine, _engine_label(t.get('engine'))),
                                (by_side, t.get('side','?')),
                                (by_coin, t.get('coin','?')),
                                (by_hour, t['_ts'][:13])]:
                b = bucket.setdefault(key, {'n':0,'w':0,'l':0,'b':0,'pnl':0.0,
                                            'mfe_sum':0.0, 'mae_sum':0.0,
                                            'mfe_count':0, 'mae_count':0})
                b['n'] += 1
                if p > 0: b['w'] += 1
                elif p < 0: b['l'] += 1
                else: b['b'] += 1   # exact 0 = breakeven, distinct from null/unknown
                b['pnl'] += p
                # MFE/MAE aggregation — surfaces "side X consistently goes
                # against us before recovering" patterns at the breakdown level
                _mfe = t.get('mfe_pct', '')
                _mae = t.get('mae_pct', '')
                try:
                    if _mfe not in (None, ''):
                        b['mfe_sum'] += float(_mfe); b['mfe_count'] += 1
                except Exception: pass
                try:
                    if _mae not in (None, ''):
                        b['mae_sum'] += float(_mae); b['mae_count'] += 1
                except Exception: pass
        def fmt(b, mark_noise=False):
            # 2026-04-26: WR = w / (w+l). Breakevens excluded from denominator
            # — a breakeven is not a loss. Without this, a 1W/1L/1BE trade set
            # showed wr=33% which made the engine look much worse than it is.
            for k, v in b.items():
                decided = v['w'] + v['l']
                v['wr'] = round(v['w']/decided*100, 1) if decided else None
                v['pnl'] = round(v['pnl'], 4)
                # Average MFE/MAE per bucket. "avg_mfe near 0 + avg_mae deep
                # negative" = systematic bad-entry pattern for that bucket.
                v['avg_mfe_pct'] = round(v['mfe_sum']/v['mfe_count']*100, 3) if v['mfe_count'] else None
                v['avg_mae_pct'] = round(v['mae_sum']/v['mae_count']*100, 3) if v['mae_count'] else None
                # Cleanup raw sums from response
                v.pop('mfe_sum', None); v.pop('mae_sum', None)
                v.pop('mfe_count', None); v.pop('mae_count', None)
                # Flag noise engines so the user can ignore them when judging
                # actual strategy quality.
                if mark_noise:
                    v['_is_noise'] = (k in NOISE_ENGINES)
            return b
        closed = [t for t in trades if t.get('_pnl_f') is not None]
        no_pnl = [t for t in trades if t.get('_pnl_f') is None]
        wins = sum(1 for t in closed if t['_pnl_f'] > 0)
        losses = sum(1 for t in closed if t['_pnl_f'] < 0)
        be = sum(1 for t in closed if t['_pnl_f'] == 0)
        decided = wins + losses
        # Real-engine subtotals (excluding RECONCILED + untagged_legacy)
        real_closed = [t for t in closed if _engine_label(t.get('engine')) not in NOISE_ENGINES]
        real_wins = sum(1 for t in real_closed if t['_pnl_f'] > 0)
        real_losses = sum(1 for t in real_closed if t['_pnl_f'] < 0)
        real_decided = real_wins + real_losses
        return jsonify({
            'window_hours': hours,
            'cutoff_utc': cutoff.isoformat(),
            'total_logged': len(trades),
            'closed_with_pnl': len(closed),
            'no_pnl_recorded': len(no_pnl),
            'wins': wins,
            'losses': losses,
            'breakeven': be,
            'overall_wr_pct': round(wins/decided*100, 1) if decided else None,
            'total_pnl': round(sum(t['_pnl_f'] for t in closed), 4),
            # Strategy quality view — RECONCILED/untagged excluded so engine
            # judgement isn't polluted by bookkeeping artifacts.
            'real_engines_wins': real_wins,
            'real_engines_losses': real_losses,
            'real_engines_wr_pct': round(real_wins/real_decided*100, 1) if real_decided else None,
            'real_engines_pnl': round(sum(t['_pnl_f'] for t in real_closed), 4),
            'noise_engines': sorted(list(NOISE_ENGINES)),
            'by_engine': fmt(by_engine, mark_noise=True),
            'by_side': fmt(by_side),
            'by_coin': fmt(by_coin),
            'by_hour': fmt(by_hour),
            'last_20': trades[-20:],
        })
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()[:500]})

@app.route('/reset', methods=['GET'])
def reset_cb():
    """Reset circuit breaker and consecutive losses."""
    state = load_state()
    state['cb_pause_until'] = 0
    state['consec_losses'] = 0
    save_state(state)
    log("CIRCUIT BREAKER RESET via /reset endpoint")
    return jsonify({'status':'reset','cb_pause_until':0,'consec_losses':0})

@app.route('/closeall', methods=['GET'])
def close_all_positions():
    """Force close ALL — requires ?secret="""
    if flask_request.args.get('secret') != WEBHOOK_SECRET: return jsonify({'err':'unauthorized'}), 401
    state = load_state()
    positions = get_all_positions_live()
    closed = []
    for coin, pos in positions.items():
        try:
            pnl = close(coin)
            closed.append({'coin':coin,'pnl':pnl})
            state['positions'].pop(coin, None)
        except Exception as e:
            closed.append({'coin':coin,'error':str(e)})
    state['consec_losses'] = 0
    state['cb_pause_until'] = 0
    save_state(state)
    log(f"FORCE CLOSE ALL: {len(closed)} positions closed")
    return jsonify({'status':'closed_all','positions':closed})

@app.route('/close/<coin>', methods=['GET', 'POST'])
def close_one_position(coin):
    """Force close a single coin. Requires ?secret="""
    if flask_request.args.get('secret') != WEBHOOK_SECRET: return jsonify({'err':'unauthorized'}), 401
    coin = coin.upper()
    try:
        pnl = close(coin)
        state = load_state()
        state['positions'].pop(coin, None)
        save_state(state)
        return jsonify({'status':'closed','coin':coin,'pnl':pnl})
    except Exception as e:
        return jsonify({'status':'error','coin':coin,'error':str(e)}), 500


@app.route('/cancel_stale_makers', methods=['GET', 'POST'])
def cancel_stale_makers():
    """Cancel maker-limit orders that are stale — i.e. there's already a
    filled position of the same size and side, meaning the order either
    got double-placed or the bot missed the fill. If ?coin=X is given,
    only scan that coin; else scan all.

    Args:
      ?secret= (required)
      ?coin=  (optional: restrict to one coin)
      ?dryrun=1  (optional: report what WOULD be cancelled, no action)

    Returns list of cancelled oids with reasons."""
    if flask_request.args.get('secret') != WEBHOOK_SECRET:
        return jsonify({'err':'unauthorized'}), 401
    coin_filter = (flask_request.args.get('coin') or '').upper() or None
    dryrun = flask_request.args.get('dryrun') == '1'
    try:
        us = _cached_user_state()
        pos_by_coin = {}
        for ap in us.get('assetPositions', []):
            p = ap.get('position', {})
            sz = float(p.get('szi', 0))
            if sz == 0: continue
            pos_by_coin[p.get('coin','').upper()] = sz

        fo = _cached_frontend_orders()
        cancelled = []; skipped = []
        for o in fo:
            c = o.get('coin','').upper()
            if coin_filter and c != coin_filter: continue
            # Skip trigger-type orders (SL/TP); only target vanilla limits
            if o.get('isTrigger'): continue
            if o.get('orderType','') != 'Limit': continue
            # We only flag STALE makers — where there's an open position
            # in the same direction AND same size already
            pos_sz = pos_by_coin.get(c, 0)
            if pos_sz == 0: continue  # no position → limit is legit (entry)
            side_is_buy = o.get('side') == 'B'
            pos_is_long = pos_sz > 0
            if side_is_buy != pos_is_long: continue  # opposite side → legit reduce
            order_sz = float(o.get('sz', 0))
            # Stale if order size matches position size (within 1%)
            if abs(order_sz - abs(pos_sz)) / abs(pos_sz) > 0.01: continue
            oid = o.get('oid')
            info_dict = {'coin':c,'oid':oid,'side':o.get('side'),
                         'px':o.get('limitPx'),'sz':order_sz,'pos_sz':pos_sz,
                         'reason':'stale_maker_duplicate_of_filled_position'}
            if dryrun:
                skipped.append(info_dict)
                continue
            try:
                r = exchange.cancel(c, oid)
                status = (r or {}).get('response',{}).get('data',{}).get('statuses',[{}])[0] if r else {}
                info_dict['hl_response'] = status
                cancelled.append(info_dict)
                log(f"STALE-MAKER CANCELLED {c} oid={oid} px={o.get('limitPx')} sz={order_sz} (pos={pos_sz})")
            except Exception as e:
                info_dict['err'] = str(e)
                cancelled.append(info_dict)
                log(f"STALE-MAKER CANCEL ERR {c} oid={oid}: {e}")
        return jsonify({'status':'done','dryrun':dryrun,
                        'cancelled': cancelled, 'would_cancel': skipped})
    except Exception as e:
        log(f"CANCEL_STALE ERR: {e}")
        return jsonify({'err': str(e)}), 500


@app.route('/protect_sl/<coin>', methods=['GET', 'POST'])
def protect_sl_endpoint(coin):
    """Place a native SL on an existing HL position that lacks one.
    ?secret=  (required)
    ?sl_pct=0.05  (optional override; else uses per-coin OOS config / tuner)
    Reads the current HL position directly — no dependency on local state."""
    if flask_request.args.get('secret') != WEBHOOK_SECRET:
        return jsonify({'err':'unauthorized'}), 401
    coin = coin.upper()
    try:
        us = _cached_user_state()
        # Find this coin's open position on HL
        target = None
        for ap in us.get('assetPositions', []):
            p = ap.get('position', {})
            if p.get('coin','').upper() == coin:
                sz = float(p.get('szi', 0))
                if sz == 0: continue
                target = {'sz': sz, 'entry': float(p.get('entryPx', 0))}
                break
        if not target:
            return jsonify({'err':'no open position', 'coin':coin}), 404

        is_long = target['sz'] > 0
        size_abs = abs(target['sz'])
        entry = target['entry']

        # Optional override of sl_pct
        override = flask_request.args.get('sl_pct')
        if override:
            try:
                sl_pct = float(override)
                trigger_px = entry * (1 - sl_pct) if is_long else entry * (1 + sl_pct)
                trigger_px = float(round_price(coin, trigger_px))
                limit_px = float(round_price(coin, trigger_px * (0.98 if not is_long else 1.02)))
                sl_size = float(round_size(coin, size_abs))
                sl_side = not is_long  # reduce short by buying, reduce long by selling
                r = exchange.order(coin, sl_side, sl_size, limit_px,
                                   {"trigger":{"triggerPx": trigger_px, "isMarket": True, "tpsl":"sl"}},
                                   reduce_only=True)
                status = r.get('response',{}).get('data',{}).get('statuses',[{}])[0] if r else {}
                log(f"{coin} MANUAL SL placed @ {trigger_px} (sl_pct={sl_pct*100:.1f}%, entry={entry}, size={sl_size})")
                return jsonify({'status':'placed','coin':coin,'sl_pct':sl_pct,'trigger_px':trigger_px,
                                'entry':entry,'size':sl_size,'hl_response':status})
            except Exception as e:
                return jsonify({'err': f'override place failed: {e}'}), 500
        else:
            # Use normal SL pipeline (per-coin config + postmortem tuner)
            sl_pct_used = place_native_sl(coin, is_long, entry, size_abs)
            return jsonify({'status':'placed','coin':coin,'sl_pct_used':sl_pct_used,
                            'entry':entry,'size':size_abs,'side':'LONG' if is_long else 'SHORT'})
    except Exception as e:
        log(f"PROTECT_SL ERR {coin}: {e}")
        return jsonify({'err': str(e)}), 500


@app.route('/retune_exits', methods=['GET', 'POST'])
def retune_exits_endpoint():
    """For every open position, cancel existing TP and SL trigger orders
    and re-place them at the config-derived TP/SL percentages. Fixes
    positions that were opened when the tuner was still overriding TP/SL
    to micro values (~2.6%).

    Args:
      ?secret= (required)
      ?coin=   (optional: restrict to one coin)
      ?dryrun=1 (optional: report what WOULD happen, no changes)
      ?rate_delay=2.5 (default gap between HL calls; raise if you see 429s)
      ?new_sl_pct= (optional: force SL pct for all; else uses per-coin config TP)
      ?new_tp_pct= (optional: force TP pct for all; else uses per-coin config TP)

    Policy-safe: only operates on trigger orders (isTrigger=True), does NOT
    touch resting entry limits or other order types.
    """
    if flask_request.args.get('secret') != WEBHOOK_SECRET:
        return jsonify({'err':'unauthorized'}), 401
    coin_filter = (flask_request.args.get('coin') or '').upper() or None
    dryrun = flask_request.args.get('dryrun') == '1'
    new_sl = flask_request.args.get('new_sl_pct')
    new_tp = flask_request.args.get('new_tp_pct')
    try: rate_delay = float(flask_request.args.get('rate_delay','2.5'))
    except: rate_delay = 2.5

    try:
        us = _cached_user_state()
        fo = _cached_frontend_orders()
    except Exception as e:
        return jsonify({'err': f'HL fetch failed: {e}'}), 500

    # Existing trigger orders per coin
    from collections import defaultdict
    triggers = defaultdict(list)
    for o in fo:
        c = o.get('coin','').upper()
        ot = o.get('orderType','')
        if 'Stop' in ot or 'Take' in ot:
            triggers[c].append({
                'oid': o.get('oid'),
                'type': 'sl' if 'Stop' in ot else 'tp',
                'triggerPx': o.get('triggerPx'),
                'side': o.get('side'),
            })

    results = []
    for ap in us.get('assetPositions', []):
        p = ap.get('position', {})
        sz = float(p.get('szi', 0))
        if sz == 0: continue
        coin = p.get('coin','').upper()
        if coin_filter and coin != coin_filter: continue
        entry = float(p.get('entryPx', 0))
        if not entry: continue
        is_long = sz > 0
        size_abs = abs(sz)

        # Source intended SL/TP from config
        cfg = None
        try:
            if percoin_configs.ELITE_MODE and percoin_configs.is_elite(coin):
                cfg = percoin_configs.get_config(coin)
        except Exception: pass
        cfg_sl = (cfg or {}).get('SL', 0.02)
        cfg_tp = (cfg or {}).get('TP', 0.06)
        sl_pct = float(new_sl) if new_sl else cfg_sl
        sl_pct = _apply_sl_cap(sl_pct)
        tp_pct = float(new_tp) if new_tp else cfg_tp

        # Inspect existing triggers
        existing = triggers.get(coin, [])
        existing_tp = [t for t in existing if t['type'] == 'tp']
        existing_sl = [t for t in existing if t['type'] == 'sl']

        # Compute existing trigger distances from entry
        def dist_pct(trigger_px):
            try:
                tp = float(trigger_px)
                return abs(tp - entry) / entry
            except: return None

        existing_tp_pct = dist_pct(existing_tp[0]['triggerPx']) if existing_tp else None
        existing_sl_pct = dist_pct(existing_sl[0]['triggerPx']) if existing_sl else None

        result = {
            'coin': coin,
            'side': 'LONG' if is_long else 'SHORT',
            'size': size_abs,
            'entry': entry,
            'existing_tp_pct': round(existing_tp_pct*100,2) if existing_tp_pct is not None else None,
            'existing_sl_pct': round(existing_sl_pct*100,2) if existing_sl_pct is not None else None,
            'target_tp_pct': round(tp_pct*100,2),
            'target_sl_pct': round(sl_pct*100,2),
        }

        # Decide what to do
        # Retune TP if existing differs meaningfully from target (>0.5% delta)
        needs_tp_retune = (existing_tp_pct is None) or abs(existing_tp_pct - tp_pct) > 0.005
        needs_sl_retune = (existing_sl_pct is None) or abs(existing_sl_pct - sl_pct) > 0.005

        if not needs_tp_retune and not needs_sl_retune:
            result['status'] = 'no_change_needed'
            results.append(result); continue

        if dryrun:
            result['would_cancel_tps'] = [t['oid'] for t in existing_tp] if needs_tp_retune else []
            result['would_cancel_sls'] = [t['oid'] for t in existing_sl] if needs_sl_retune else []
            result['status'] = 'dryrun_would_retune'
            results.append(result); continue

        # Execute: cancel old TP, re-place
        if needs_tp_retune:
            # Cancel old TP(s)
            for t in existing_tp:
                try:
                    exchange.cancel(coin, t['oid'])
                    log(f"RETUNE {coin} cancelled old TP oid={t['oid']} @ {t['triggerPx']}")
                except Exception as e:
                    log(f"RETUNE {coin} cancel TP err: {e}")
            time.sleep(rate_delay)
            # Place new TP
            try:
                trigger_px = entry * (1 + tp_pct) if is_long else entry * (1 - tp_pct)
                trigger_px = float(round_price(coin, trigger_px))
                limit_px = float(round_price(coin, trigger_px * (0.998 if is_long else 1.002)))
                tp_size = float(round_size(coin, size_abs))
                r = exchange.order(coin, not is_long, tp_size, limit_px,
                                   {"trigger":{"triggerPx": trigger_px, "isMarket": True, "tpsl":"tp"}},
                                   reduce_only=True)
                s = (r or {}).get('response',{}).get('data',{}).get('statuses',[{}])[0]
                result['new_tp_placed_at'] = trigger_px
                result['new_tp_response'] = s
                log(f"RETUNE {coin} new TP placed @ {trigger_px} ({tp_pct*100:.1f}%)")
            except Exception as e:
                result['tp_err'] = str(e)
                log(f"RETUNE {coin} new TP err: {e}")
            time.sleep(rate_delay)

        if needs_sl_retune:
            for t in existing_sl:
                try:
                    exchange.cancel(coin, t['oid'])
                    log(f"RETUNE {coin} cancelled old SL oid={t['oid']} @ {t['triggerPx']}")
                except Exception as e:
                    log(f"RETUNE {coin} cancel SL err: {e}")
            time.sleep(rate_delay)
            try:
                trigger_px = entry * (1 - sl_pct) if is_long else entry * (1 + sl_pct)
                trigger_px = float(round_price(coin, trigger_px))
                limit_px = float(round_price(coin, trigger_px * (0.98 if not is_long else 1.02)))
                sl_size = float(round_size(coin, size_abs))
                r = exchange.order(coin, not is_long, sl_size, limit_px,
                                   {"trigger":{"triggerPx": trigger_px, "isMarket": True, "tpsl":"sl"}},
                                   reduce_only=True)
                s = (r or {}).get('response',{}).get('data',{}).get('statuses',[{}])[0]
                result['new_sl_placed_at'] = trigger_px
                result['new_sl_response'] = s
                log(f"RETUNE {coin} new SL placed @ {trigger_px} ({sl_pct*100:.1f}%)")
            except Exception as e:
                result['sl_err'] = str(e)
                log(f"RETUNE {coin} new SL err: {e}")
            time.sleep(rate_delay)

        result['status'] = 'retuned'
        results.append(result)

    return jsonify({'status':'done','dryrun':dryrun,'n_positions':len(results),
                    'rate_delay_sec':rate_delay,'results':results})


@app.route('/protect_all', methods=['GET', 'POST'])
def protect_all_endpoint():
    """Scan every open position on HL. For any naked (no SL or no TP)
    position, attach the missing protection using the coin's per-coin
    config (or ?sl_pct / ?tp_pct overrides). Returns per-coin outcome.

    Args:
      ?secret= (required)
      ?sl_pct= (optional: force this SL pct for all; else uses per-coin config)
      ?tp_pct= (optional: force this TP pct for all; else uses per-coin config)
      ?dryrun=1 (optional: report what WOULD be placed, no orders fire)
      ?rate_delay=2.0 (seconds between calls to avoid HL 429)

    Idempotent — already-protected positions are skipped.
    """
    if flask_request.args.get('secret') != WEBHOOK_SECRET:
        return jsonify({'err':'unauthorized'}), 401
    sl_override = flask_request.args.get('sl_pct')
    tp_override = flask_request.args.get('tp_pct')
    dryrun = flask_request.args.get('dryrun') == '1'
    try: rate_delay = float(flask_request.args.get('rate_delay','2.0'))
    except: rate_delay = 2.0

    try:
        us = _cached_user_state()
        fo = _cached_frontend_orders()
    except Exception as e:
        return jsonify({'err': f'HL fetch failed: {e}'}), 500

    # Coverage map per coin
    from collections import defaultdict
    cov = defaultdict(lambda: {'sl': False, 'tp': False})
    for o in fo:
        c = o.get('coin','').upper()
        ot = o.get('orderType','')
        if 'Stop' in ot: cov[c]['sl'] = True
        elif 'Take' in ot: cov[c]['tp'] = True

    results = []
    for ap in us.get('assetPositions', []):
        p = ap.get('position', {})
        sz = float(p.get('szi', 0))
        if sz == 0: continue
        coin = p.get('coin','').upper()
        entry = float(p.get('entryPx', 0))
        if not entry: continue
        is_long = sz > 0
        size_abs = abs(sz)
        c = cov[coin]
        if c['sl'] and c['tp']:
            results.append({'coin': coin, 'status': 'already_protected'})
            continue

        # Figure out SL/TP pct from config if not overridden
        cfg = None
        try:
            if percoin_configs.ELITE_MODE and percoin_configs.is_elite(coin):
                cfg = percoin_configs.get_config(coin)
        except Exception: pass
        default_sl = (cfg or {}).get('SL', 0.02)
        default_tp = (cfg or {}).get('TP', 0.06)
        sl_pct = float(sl_override) if sl_override else default_sl
        tp_pct = float(tp_override) if tp_override else default_tp

        result = {'coin': coin, 'entry': entry, 'size': size_abs,
                  'side': 'LONG' if is_long else 'SHORT',
                  'sl_pct': sl_pct, 'tp_pct': tp_pct,
                  'had_sl': c['sl'], 'had_tp': c['tp']}

        if dryrun:
            result['status'] = 'dryrun_would_attach'
            results.append(result); continue

        # Attach SL if missing
        if not c['sl']:
            try:
                trigger_px = entry * (1 - sl_pct) if is_long else entry * (1 + sl_pct)
                trigger_px = float(round_price(coin, trigger_px))
                limit_px = float(round_price(coin, trigger_px * (0.98 if not is_long else 1.02)))
                sl_size = float(round_size(coin, size_abs))
                r = exchange.order(coin, not is_long, sl_size, limit_px,
                                   {"trigger":{"triggerPx": trigger_px, "isMarket": True, "tpsl":"sl"}},
                                   reduce_only=True)
                s = (r or {}).get('response',{}).get('data',{}).get('statuses',[{}])[0]
                result['sl_placed_at'] = trigger_px
                result['sl_hl_response'] = s
                log(f"PROTECT_ALL {coin} SL placed @ {trigger_px} ({sl_pct*100:.1f}%)")
            except Exception as e:
                result['sl_err'] = str(e)
                log(f"PROTECT_ALL {coin} SL err: {e}")
            time.sleep(rate_delay)

        # Attach TP if missing
        if not c['tp']:
            try:
                trigger_px = entry * (1 + tp_pct) if is_long else entry * (1 - tp_pct)
                trigger_px = float(round_price(coin, trigger_px))
                limit_px = float(round_price(coin, trigger_px * (0.98 if is_long else 1.02)))
                tp_size = float(round_size(coin, size_abs))
                r = exchange.order(coin, not is_long, tp_size, limit_px,
                                   {"trigger":{"triggerPx": trigger_px, "isMarket": True, "tpsl":"tp"}},
                                   reduce_only=True)
                s = (r or {}).get('response',{}).get('data',{}).get('statuses',[{}])[0]
                result['tp_placed_at'] = trigger_px
                result['tp_hl_response'] = s
                log(f"PROTECT_ALL {coin} TP placed @ {trigger_px} ({tp_pct*100:.1f}%)")
            except Exception as e:
                result['tp_err'] = str(e)
                log(f"PROTECT_ALL {coin} TP err: {e}")
            time.sleep(rate_delay)

        result['status'] = 'attached'
        results.append(result)

    return jsonify({'status':'done','dryrun':dryrun,'n_positions':len(results),
                    'rate_delay_sec':rate_delay,'results': results})


@app.route('/transfer', methods=['POST'])
def transfer_funds():
    """Transfer USDC internally on HL. POST {amount, to_wallet}"""
    try:
        data = flask_request.get_json(force=True, silent=True)
        if not data or 'amount' not in data:
            return jsonify({'error': 'POST {amount, to_wallet} required'}), 400
        amount = float(data['amount'])
        to_wallet = data.get('to_wallet', WALLET)
        log(f"TRANSFER REQUEST: {amount} USDC to {to_wallet}")
        result = exchange.usd_transfer(amount, to_wallet)
        log(f"TRANSFER RESULT: {result}")
        return jsonify({'status': 'transferred', 'amount': amount, 'to': to_wallet, 'result': str(result)}), 200
    except Exception as e:
        log(f"TRANSFER ERROR: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/webhook', methods=['POST'])
def webhook():
    """Receive DynaPro signal from TradingView.
    Expected JSON: {"ticker":"BTCUSD","action":"buy|sell|exit_buy|exit_sell","price":12345.67}
    Optional: {"secret":"...","tf":"15"} 
    Also accepts plain text: 'buy BTCUSD 12345.67' format.
    """
    # Parse flexibly — TV sends various formats
    raw_body = flask_request.get_data(as_text=True)
    log(f"WEBHOOK RAW: content_type={flask_request.content_type} body={raw_body[:300]}")
    
    data = None
    try:
        data = flask_request.get_json(force=True, silent=True)
    except Exception: pass
    
    if not data:
        text = raw_body.strip()
        
        # DynaPro pattern: "Double Top Pattern Detected | timeframe : 15 | ENSUSDT"
        if '|' in text:
            parts = [p.strip() for p in text.split('|')]
            ticker_part = parts[-1] if len(parts) >= 2 else ''
            pt = parts[0].lower()
            bearish = any(b in pt for b in ['double top','head and shoulders','rising wedge','descending triangle','bearish','evening star','shooting star','dark cloud','hanging man','three black'])
            bullish = any(b in pt for b in ['double bottom','inverted h&s','inverse head','falling wedge','ascending triangle','bullish','morning star','hammer','piercing','three white'])
            if (bearish or bullish) and ticker_part:
                data = {'action': 'sell' if bearish else 'buy', 'ticker': ticker_part}
            else:
                log(f"WEBHOOK PATTERN SKIP: {text[:100]}")
                return jsonify({'status':'received','type':'pattern'}), 200
        
        # "long entry" / "short entry" — broadcast to ALL Pepperstone tickers
        elif text.lower() in ('long entry','short entry','long exit','short exit'):
            direction = 'BUY' if 'long' in text.lower() else 'SELL'
            MT4_BIAS['direction'] = direction
            MT4_BIAS['ts'] = time.time()
            mt4_count = 0
            for tv_sym, mt4_sym in TV_TO_MT4.items():
                MT4_QUEUE.append({'symbol': mt4_sym, 'direction': direction, 'price': 0, 'ts': time.time()})
                mt4_count += 1
            if len(MT4_QUEUE) > 200: MT4_QUEUE[:] = MT4_QUEUE[-200:]
            _mt4_save()
            log(f"MT4 BROADCAST: {direction} → {mt4_count} tickers (from '{text}')")
            return jsonify({'status':'broadcast','direction':direction,'count':mt4_count}), 200
        
        else:
            parts = text.replace('\n',' ').split()
            if len(parts) >= 2:
                first = parts[0].lower()
                if first in ('long','short'):
                    data = {'action': 'buy' if first=='long' else 'sell', 'ticker': parts[-1]}
                else:
                    data = {'action': parts[0].lower(), 'ticker': parts[1]}
                if len(parts) >= 3:
                    try: data['price'] = float(parts[-1])
                    except Exception: pass
    
    if not data:
        # Last resort — just log and accept, don't 400
        log(f"WEBHOOK UNPARSEABLE: {raw_body[:200]}")
        return jsonify({'status':'received','parsed':False}), 200
    
    # If no ticker, try to extract from raw body
    if 'ticker' not in data or not data['ticker']:
        # Search for anything that looks like a ticker symbol
        import re as _re
        m = _re.search(r'([A-Z]{2,}(?:USDT|USD)?(?:\.P)?)', raw_body)
        if m: data['ticker'] = m.group(1)
    
    if 'action' not in data or not data.get('action'):
        # Infer from body text
        lower = raw_body.lower()
        if 'long' in lower or 'buy' in lower: data['action'] = 'buy'
        elif 'short' in lower or 'sell' in lower: data['action'] = 'sell'
    
    if not data.get('ticker') or not data.get('action'):
        # No ticker — log and skip (Trend Buy/Sell alerts include tickers)
        action_text = str(data.get('action','')).lower()
        direction = None
        if 'long' in action_text or 'buy' in action_text: direction = 'BUY'
        elif 'short' in action_text or 'sell' in action_text: direction = 'SELL'
        if direction:
            MT4_BIAS['direction'] = direction
            MT4_BIAS['ts'] = time.time()
            log(f"MT4 BIAS: {direction} (condition alert, no ticker)")
        return jsonify({'status':'bias_only','direction':direction or ''}), 200

    # Optional secret check
    if WEBHOOK_SECRET and data.get('secret') and data['secret'] != WEBHOOK_SECRET:
        return jsonify({'error':'bad secret'}), 403

    coin = tv_to_hl(data['ticker'])
    action_raw = str(data.get('action','')).lower().replace(' ','_')
    price = data.get('price', 0)

    # Normalize action from DynaPro's various alert texts
    if action_raw in ('buy','sell','exit_buy','exit_sell'):
        action = action_raw
    elif 'long_entry' in action_raw or 'long entry' in str(data.get('action','')).lower():
        action = 'buy'
    elif 'short_entry' in action_raw or 'short entry' in str(data.get('action','')).lower():
        action = 'sell'
    elif 'long_exit' in action_raw or 'exit_buy' in action_raw:
        action = 'exit_buy'
    elif 'short_exit' in action_raw or 'exit_sell' in action_raw:
        action = 'exit_sell'
    else:
        # Check for pattern names in action field
        act = str(data.get('action','')).lower()
        bearish = any(b in act for b in ['double top','head and shoulders','rising wedge','descending triangle','evening star','shooting star','dark cloud','hanging man','three black'])
        bullish = any(b in act for b in ['double bottom','inverted h&s','inverse head','falling wedge','ascending triangle','morning star','hammer','piercing','three white'])
        if bearish: action = 'sell'
        elif bullish: action = 'buy'
        else:
            log(f"WEBHOOK UNKNOWN ACTION: {data.get('action','')[:100]} — skipped")
            return jsonify({'status':'received','unknown_action':True}), 200

    signal = {'coin': coin, 'action': action, 'price': price, 'ts': time.time(), 'source': 'dynapro'}
    
    # DEDUP REMOVED — was blocking legitimate re-entries
    
    # Route: Pepperstone tickers → MT4, crypto tickers → HL
    raw_ticker = data.get('ticker','').upper().replace('PEPPERSTONE:','')
    if is_pepperstone(raw_ticker):
        mt4_sym = get_mt4_symbol(raw_ticker)
        clean = raw_ticker.upper().replace('PEPPERSTONE:','').replace('.A','')
        gate = MT4_TICKER_GATES.get(clean, {})
        direction = action.upper()
        # FILTER: v4.8 per-ticker gate pipeline (direction passed for anchor-align)
        _passed, _reason = _mt4_filter_pass(clean, direction)
        if not _passed:
            log(f"MT4 FILTERED {clean} {direction}: {_reason}")
            return jsonify({'status':'filtered','symbol':clean,'reason':_reason}), 200
        # Inversion BEFORE pullback check so pullback sees the actual direction we'll trade
        if gate.get('invert', False) or gate.get('inverted', False):
            direction = 'SELL' if direction == 'BUY' else 'BUY'
            log(f"MT4 INVERTED {clean}: {action.upper()} → {direction}")
        # v4.10: pullback gate (must be near 1h EMA20, RSI cooled)
        _pb_ok, _pb_reason, _pb_meta = _mt4_pullback_check(clean, direction)
        if not _pb_ok:
            log(f"MT4 FILTERED {clean} {direction}: {_pb_reason} meta={_pb_meta}")
            return jsonify({'status':'filtered','symbol':clean,'reason':_pb_reason,'meta':_pb_meta}), 200
        _mt4_last_signal[clean] = time.time()
        log(f"MT4 PULLBACK {clean} {direction}: {_pb_reason}")
        # VIX sentiment size multiplier (scales, never blocks)
        size_mult = _mt4_vix_overlay_mult(clean)
        # v4.10: OANDA retail sentiment multiplier (contrarian fade at extremes)
        sent_mult = _mt4_sentiment_mult(clean, direction)
        # v4.9: zone confluence boost/reduce
        zone_boost = 1.0
        zone_info = {}
        if ZONES_ENABLED and _zones:
            try:
                zone_info = _zones.zone_confluence(clean, direction, price)
                zone_boost = zone_info.get('size_boost', 1.0)
                if zone_info.get('aligned') == 'contradicted':
                    log(f"MT4 ZONE CONTRA {clean} {direction} @ {price}: {zone_info.get('zones_hit',[])[:3]} — size×{zone_boost}")
                elif zone_info.get('aligned') == 'aligned':
                    log(f"MT4 ZONE ALIGN {clean} {direction} @ {price}: {zone_info.get('zones_hit',[])[:3]} — size×{zone_boost}")
            except Exception as _ze:
                log(f"MT4 zone err {clean}: {_ze}")
        live_wr_mult = _mt4_live_wr_mult(clean)
        final_mult = round(size_mult * zone_boost * sent_mult * live_wr_mult, 2)
        rec = {
            'symbol': mt4_sym,
            'direction': direction,
            'price': price,
            'ts': time.time(),
            'trail_activate': gate.get('trail_activate', 0.4),
            'trail_distance': gate.get('trail_distance', 0.2),
            'sl_pct': gate.get('sl_pct', 1.4),
            'time_cut_hours': gate.get('time_cut_hours'),
            'size_mult': final_mult,
            'vix_mult': round(size_mult, 2),
            'zone_boost': round(zone_boost, 2),
            'zone_status': zone_info.get('aligned') if zone_info else None,
            'sent_mult': round(sent_mult, 2),
            'live_wr_mult': round(live_wr_mult, 2),
            'pullback_meta': _pb_meta,
            'max_spread_pct': _mt4_max_spread_for(clean),
            'tp_pct': gate.get('tp_pct', round(gate.get('sl_pct', 1.0) * 2.0, 2)),
            'max_slip_pct': 0.3,  # EA rejects market fallback if slip > this
        }
        MT4_QUEUE.append(rec)
        if len(MT4_QUEUE) > 200: MT4_QUEUE[:] = MT4_QUEUE[-200:]
        _mt4_save()
        log(f"MT4 QUEUED: {direction} {mt4_sym} @ {price} trail={rec['trail_activate']}/{rec['trail_distance']} sl={rec['sl_pct']} vix×{size_mult} zone×{zone_boost} sent×{sent_mult} = {rec['size_mult']} pb={_pb_meta} slip_max={rec['max_slip_pct']}%")
        log_trade('MT4', clean, direction, price, 0, 'webhook')
        return jsonify({'status':'mt4_queued','symbol':mt4_sym,'action':direction}), 200

    # Per-ticker gate for webhook signals (non-blocking — don't fetch candles in webhook handler)
    try:
        wh_coin = signal.get('coin','').upper()
        gate = TICKER_GATES.get(wh_coin, {})
        # Quick gate checks that don't need candles (body/cloud need candles, skip here)
        # Full gate check happens in the main loop when signal executes
    except Exception as e:
        log(f"webhook gate err: {e}")

    WEBHOOK_QUEUE.put(signal)
    log(f"WEBHOOK: {action} {coin} @ {price} (queued, size={WEBHOOK_QUEUE.qsize()})")
    return jsonify({'status':'queued','coin':coin,'action':action}), 200

@app.route('/signal', methods=['POST'])
def signal_alias():
    """Alias for /webhook — backwards compatible with old cyber-psycho webhook URL."""
    return webhook()


@app.route('/mt4/signals', methods=['GET'])
def mt4_signals():
    """EA polls this every 10s. Returns one signal, removes from queue. Drops stale."""
    global MT4_QUEUE
    _now = time.time()
    # drop stale signals (older than MT4_STALE_SEC)
    while MT4_QUEUE and (_now - MT4_QUEUE[0].get('ts', 0)) >= MT4_STALE_SEC:
        _drop = MT4_QUEUE.pop(0)
        log(f"MT4 STALE DROP: {_drop.get('direction','')} {_drop.get('symbol','')} age={int(_now - _drop.get('ts',0))}s")
    if MT4_QUEUE:
        sig = MT4_QUEUE.pop(0)
        _mt4_save()
        log(f"MT4 SERVED: {sig['direction']} {sig['symbol']}")
        return jsonify(sig)
    return ('', 204)  # v4.16: empty body when no signal — EA's StringLen(body)<5 check bails cleanly

@app.route('/mt4/status', methods=['GET'])
def mt4_status():
    bias_age = time.time() - MT4_BIAS.get('ts', 0)
    bias_active = bias_age < 300  # 5min validity
    return jsonify({
        'queue_size':len(MT4_QUEUE),'queue':MT4_QUEUE[:5],
        'bias': MT4_BIAS.get('direction','') if bias_active else '',
        'bias_age_sec': round(bias_age)
    })

# FLATTEN BROADCAST: server sets a flag, EA polls, closes all magic-matched + deletes pendings, acks
MT4_FLATTEN_FLAG = {'pending': False, 'ts': 0, 'reason': ''}

@app.route('/mt4/flatten', methods=['POST', 'GET'])
def mt4_flatten_set():
    """Arm flatten broadcast. EA will act on next poll."""
    global MT4_QUEUE, MT4_FLATTEN_FLAG
    reason = flask_request.args.get('reason', 'user_request')
    # Clear the server queue too
    cleared = len(MT4_QUEUE)
    MT4_QUEUE.clear()
    _mt4_save()
    MT4_FLATTEN_FLAG = {'pending': True, 'ts': time.time(), 'reason': reason}
    log(f"MT4 FLATTEN ARMED: reason={reason}, cleared_queue={cleared}")
    return jsonify({'status': 'armed', 'reason': reason, 'queue_cleared': cleared})

@app.route('/mt4/flatten/check', methods=['GET'])
def mt4_flatten_check():
    """EA polls. Returns flag + timestamp. EA decides to act."""
    return jsonify(MT4_FLATTEN_FLAG)

@app.route('/mt4/flatten/ack', methods=['POST', 'GET'])
def mt4_flatten_ack():
    """EA acks after flatten complete. Clears flag."""
    global MT4_FLATTEN_FLAG
    closed = flask_request.args.get('closed', '0')
    deleted = flask_request.args.get('deleted', '0')
    log(f"MT4 FLATTEN ACK: closed={closed}, deleted={deleted}")
    MT4_FLATTEN_FLAG = {'pending': False, 'ts': time.time(), 'reason': ''}
    return jsonify({'status': 'acked'})

@app.route('/mt4/trade-opened', methods=['POST'])
def mt4_trade_opened():
    """EA v5.1 reports OrderSend success so server tracks direction per ticket."""
    try:
        d = flask_request.get_json(force=True, silent=True) or {}
        ticket = int(d.get('ticket', 0))
        symbol = (d.get('symbol') or '').replace('.a', '').upper()
        side = (d.get('side') or '').upper()
        entry = float(d.get('entry', 0))
        lots = float(d.get('lots', 0))
        if ticket <= 0: return jsonify({'ok': False, 'err':'no_ticket'}), 200
        MT4_TICKET_META[ticket] = {
            'direction': side, 'entry_ts': time.time(),
            'symbol': symbol, 'entry': entry, 'lots': lots,
        }
        cutoff = time.time() - 86400
        stale = [t for t,m in MT4_TICKET_META.items() if m['entry_ts'] < cutoff]
        for t in stale: MT4_TICKET_META.pop(t, None)
        log(f"MT4 OPEN #{ticket} {side} {symbol} @ {entry} lots={lots}")
        return jsonify({'ok': True})
    except Exception as e:
        log(f"MT4 trade-opened err: {e}")
        return jsonify({'ok': False, 'err': str(e)}), 200

@app.route('/mt4/trade-closed', methods=['POST'])
def mt4_trade_closed():
    """EA v5 reports every trade exit. Records PnL, rolls stats, queues retest if TRAIL."""
    try:
        raw_body = flask_request.get_data(as_text=True)
        d = flask_request.get_json(force=True, silent=True) or {}
        log(f"MT4 trade-closed RAW body={raw_body[:300]} parsed={d}")
        ticket = int(d.get('ticket', 0))
        symbol = (d.get('symbol') or '').replace('.a', '').upper()
        exit_type = (d.get('exit_type') or '').upper()
        entry = float(d.get('entry', 0))
        peak_pct = float(d.get('peak_pct', 0))
        exit_pct = float(d.get('exit_pct', 0))
        if entry <= 0:
            return jsonify({'ok': False, 'err': 'no_entry'}), 200

        # v4.15b: handle OPEN events piggybacked on /mt4/trade-closed (saves MT4 whitelist slot)
        if exit_type == 'OPEN':
            side = (d.get('side') or '').upper()
            lots = float(d.get('lots', 0))
            MT4_TICKET_META[ticket] = {
                'direction': side, 'entry_ts': time.time(),
                'symbol': symbol, 'entry': entry, 'lots': lots,
            }
            cutoff = time.time() - 86400
            stale = [t for t,m in MT4_TICKET_META.items() if m['entry_ts'] < cutoff]
            for t in stale: MT4_TICKET_META.pop(t, None)
            log(f"MT4 OPEN #{ticket} {side} {symbol} @ {entry} lots={lots}")
            return jsonify({'ok': True, 'event': 'open'})

        rec_exit = {
            'ts': time.time(), 'symbol': symbol, 'ticket': ticket,
            'exit_type': exit_type, 'entry': entry,
            'peak_pct': round(peak_pct, 3), 'exit_pct': round(exit_pct, 3),
            'win': exit_pct > 0,
        }
        MT4_CLOSED_RING.append(rec_exit)
        if len(MT4_CLOSED_RING) > 500:
            MT4_CLOSED_RING[:] = MT4_CLOSED_RING[-500:]

        ss = MT4_LIVE_STATS.setdefault(symbol, {'wins':0,'losses':0,'sum_pnl':0.0,'trades':0,'recent':[]})
        ss['trades'] += 1
        ss['sum_pnl'] += exit_pct
        if exit_pct > 0: ss['wins'] += 1
        else: ss['losses'] += 1
        ss['recent'].append({'ts':rec_exit['ts'],'pnl':exit_pct,'exit_type':exit_type,'peak':peak_pct})
        if len(ss['recent']) > 50:
            ss['recent'] = ss['recent'][-50:]

        try:
            with open('/var/data/mt4_stats.json','w') as f:
                _json.dump({'stats': MT4_LIVE_STATS, 'ring_len': len(MT4_CLOSED_RING)}, f, default=str)
        except Exception: pass

        outcome = 'WIN' if exit_pct > 0 else 'LOSS'
        wr50 = sum(1 for r in ss['recent'] if r['pnl']>0) / max(1, len(ss['recent'])) * 100
        log(f"MT4 CLOSE {symbol} #{ticket} {exit_type} peak={peak_pct:+.2f}% exit={exit_pct:+.2f}% {outcome} [n={ss['trades']} wr50={wr50:.0f}% totPnL={ss['sum_pnl']:+.2f}%]")

        if exit_type != 'TRAIL' or peak_pct < 0.3:
            return jsonify({'ok': True, 'recorded': True, 'retest': False})

        side = MT4_TICKET_META.get(ticket, {}).get('direction')
        if not side:
            return jsonify({'ok': True, 'recorded': True, 'retest': False, 'note':'no_side'})

        if side == 'BUY':
            peak_price = entry * (1 + peak_pct / 100.0)
            retest = peak_price - (peak_price - entry) * 0.382
        else:
            peak_price = entry * (1 - peak_pct / 100.0)
            retest = peak_price + (entry - peak_price) * 0.382

        broker_sym = symbol + '.a'
        rec = {
            'symbol': broker_sym, 'direction': side, 'price': round(retest, 5),
            'type': 'LIMIT', 'ts': int(time.time() * 1000), 'ttl_sec': 1800,
            'is_retest': True, 'origin_ticket': ticket,
            'origin_entry': entry, 'origin_peak_pct': peak_pct,
        }
        global MT4_LATEST_SIGNAL
        MT4_LATEST_SIGNAL = rec
        log(f"MT4 RETEST QUEUED: {side} {broker_sym} retest={retest:.5f} (peak was {peak_pct:.2f}% from entry {entry})")
        return jsonify({'ok': True, 'recorded': True, 'retest': True, 'retest_price': retest, 'ttl_sec': 1800})
    except Exception as e:
        log(f"MT4 trade-closed err: {e}")
        return jsonify({'ok': False, 'err': str(e)}), 200

@app.route('/mt4/stats', methods=['GET'])
def mt4_stats():
    """Per-ticker live WR/PnL dashboard."""
    out = {}
    for sym, ss in MT4_LIVE_STATS.items():
        recent = ss.get('recent', [])
        wr_all = ss['wins'] / max(1, ss['trades']) * 100
        wr50 = sum(1 for r in recent if r['pnl']>0) / max(1, len(recent)) * 100
        avg_pnl = ss['sum_pnl'] / max(1, ss['trades'])
        wins_pnl = sum(r['pnl'] for r in recent if r['pnl']>0)
        losses_pnl = sum(r['pnl'] for r in recent if r['pnl']<=0)
        pf = (wins_pnl / abs(losses_pnl)) if losses_pnl else 99.0
        out[sym] = {
            'trades': ss['trades'], 'wr_all_pct': round(wr_all, 1),
            'wr_last50_pct': round(wr50, 1), 'avg_pnl_pct': round(avg_pnl, 3),
            'total_pnl_pct': round(ss['sum_pnl'], 2), 'profit_factor': round(pf, 2),
        }
    sorted_out = dict(sorted(out.items(), key=lambda x: -x[1]['total_pnl_pct']))
    return jsonify({'tickers': sorted_out, 'ring_len': len(MT4_CLOSED_RING),
                    'total_closed': sum(s['trades'] for s in MT4_LIVE_STATS.values())})

@app.route('/mt4/stats/reset', methods=['POST'])
def mt4_stats_reset():
    """Wipe MT4 live stats. Intended for wiping test data before real trading begins."""
    global MT4_LIVE_STATS, MT4_CLOSED_RING, MT4_TICKET_META
    MT4_LIVE_STATS = {}
    MT4_CLOSED_RING = []
    MT4_TICKET_META = {}
    try:
        with open('/var/data/mt4_stats.json','w') as f:
            _json.dump({'stats': {}, 'ring_len': 0}, f)
    except Exception: pass
    log("MT4 STATS RESET")
    return jsonify({'ok': True, 'wiped': True})

@app.route('/mt4/stats/summary', methods=['GET'])
def mt4_stats_summary():
    total = sum(s['trades'] for s in MT4_LIVE_STATS.values())
    if total == 0:
        return jsonify({'trades':0,'msg':'no closures yet'})
    wins = sum(s['wins'] for s in MT4_LIVE_STATS.values())
    tot_pnl = sum(s['sum_pnl'] for s in MT4_LIVE_STATS.values())
    return jsonify({
        'trades': total, 'wins': wins, 'losses': total - wins,
        'wr_pct': round(wins / total * 100, 1),
        'total_pnl_pct_sum': round(tot_pnl, 2),
        'avg_pnl_pct': round(tot_pnl/total, 3),
        'tickers_traded': len(MT4_LIVE_STATS),
    })


# UNIVERSE: elite-tier coins only (71 = 7 PURE + 20 NINETY_99 + 34 EIGHTY_89 + 10 SEVENTY_79).
# Sourced dynamically from percoin_configs — adding/removing a tier coin auto-updates the scan.
# Reasoning: non-elite coins were getting -15 conf penalty + soft-gates → 95%+ never cleared
# the conviction floor anyway. Scanning 142 coins wasted API calls (HL 429s on every tick),
# slowed candle fetches, and added zero alpha. 50% smaller universe = no rate-limit hits,
# faster ticks, identical trade decisions.
COINS = sorted(set(
    list(percoin_configs.PURE_14.keys()) +
    list(percoin_configs.NINETY_99.keys()) +
    list(percoin_configs.EIGHTY_89.keys()) +
    list(percoin_configs.SEVENTY_79.keys())
))

CHASE_GATE_COINS = {'BTC','BNB','DOT','ATOM','SUI','LDO','INJ','UMA','ALGO',
                    'BLUR','VVV','APE','OP','TON','TIA','LTC','MOODENG',
                    'AR','GALA','VIRTUAL'}
CHASE_LOOKBACK = 20


# ═══════════════════════════════════════════════════════
# PER-TICKER GATES — grid-optimized for 90%+ WR
# Each ticker has: gate_buy, gate_sell, cloud, body, lookback
# ═══════════════════════════════════════════════════════
# json already imported at top
_gates_path = os.path.join(os.path.dirname(__file__), 'ticker_gates.json')
if os.path.exists(_gates_path):
    TICKER_GATES = json.load(open(_gates_path))
    print(f"Loaded {len(TICKER_GATES)} per-ticker gate configs", flush=True)
else:
    TICKER_GATES = {}
    print("WARNING: ticker_gates.json not found, running without per-ticker gates", flush=True)

# V3 trend gate (4H EMA9 direction) — applied first in apply_ticker_gate
_HTF_CACHE = {}
HTF_CACHE_SEC = 900  # 15 min — 4h bars close every 4h, 15m cache fine

def fetch_htf(coin, interval='4h', bars=30):
    now = time.time()
    k = f"{coin}_{interval}"
    c = _HTF_CACHE.get(k)
    if c and now - c['ts'] < HTF_CACHE_SEC:
        return c['data']
    sec_map = {'1h':3600,'4h':14400,'15m':900,'5m':300}
    sec = sec_map.get(interval, 14400)
    end = int(time.time()*1000)
    start = end - bars*sec*1000
    try:
        d = okx_fetch.fetch_klines(coin, interval, bars)
        result = [(int(x['t']), float(x['o']), float(x['h']), float(x['l']), float(x['c']), float(x['v'])) for x in d]
        _HTF_CACHE[k] = {'data': result, 'ts': now}
        return result
    except Exception as e:
        if '429' in str(e) and c: return c['data']
        log(f"htf err {coin} {interval}: {e}")
        return []

V3_ENABLED = False  # OOS 14d: V3 ON +108% gain, V3 OFF +173% (+65pp). Regime-aware system already handles trend context per-coin per-regime; V3 was double-filtering and blocking valid signals.
V3_HTF = '4h'
V3_EMA = 9

V3_BUFFER = 0.01  # 2% — only block extreme trend — only block when clearly in opposite trend

def trend_gate(coin, side):
    """V3: block BUY if 4H close < 4H EMA9 * (1-buffer), SELL if above EMA * (1+buffer)."""
    if not V3_ENABLED: return True
    htf = fetch_htf(coin, V3_HTF, V3_EMA * 3 + 5)
    if len(htf) < V3_EMA + 2: return True
    closes = [b[4] for b in htf]
    k = 2/(V3_EMA+1)
    ema = sum(closes[:V3_EMA])/V3_EMA
    for c in closes[V3_EMA:]:
        ema = c*k + ema*(1-k)
    last = closes[-1]
    if side == 'BUY' and last < ema * (1 - V3_BUFFER): return False
    if side == 'SELL' and last > ema * (1 + V3_BUFFER): return False
    return True

USE_GRID_GATE = False  # overfit layer disabled; V3 + ATR-min do the filtering

def apply_ticker_gate(coin, side, price, candles, return_reasons=False):
    """V3 trend + ATR-min filter. Returns True/False bool, OR (passed, reasons) if return_reasons=True.
    Reasons is a list of strings explaining why it failed (empty if passed)."""
    reasons = []
    # CROWDING: directional imbalance.
    # 2026-04-27: bumped 10 → 20. With 33-position cap, threshold of 10 was
    # tripping on every SELL after first chop session. Real concern is "we're
    # piled into shorts" which only happens >half the book.
    try:
        if side == 'SELL':
            lp = get_all_positions_live()
            shorts = sum(1 for k,v in lp.items() if v.get('size',0) < 0)
            if shorts >= 20:
                btc_state = btc_correlation.get_state()
                if btc_state.get('btc_move', 0) > 0.002 or btc_state.get('btc_dir', 0) > 0:
                    reasons.append('crowding_shorts_btc_up')
    except Exception: pass
    if not trend_gate(coin, side):
        reasons.append('v3_trend')
        _shadow_record_rejection(coin, 'BUY' if side.upper() in ('B','BUY','L') else 'SELL', 'v3_trend_block')
    if candles and len(candles) >= 15:
        trs = []
        for j in range(1, min(15, len(candles))):
            h,l,c = candles[-j][2], candles[-j][3], candles[-j][4]
            pc = candles[-j-1][4]
            trs.append(max(h-l, abs(h-pc), abs(l-pc)))
        if trs:
            atr_val = sum(trs)/len(trs)
            last_c = candles[-1][4]
            if last_c>0 and atr_val/last_c < 0.001:
                reasons.append(f'atr_low_{atr_val/last_c*100:.2f}%')
                _shadow_record_rejection(coin, 'BUY' if side.upper() in ('B','BUY','L') else 'SELL', 'atr_min_block',
                                         {'atr_pct': round(atr_val/last_c*100, 4)})
    if not funding_filter.allow_side(coin, side):
        reasons.append('funding')
        _shadow_record_rejection(coin, 'BUY' if side.upper() in ('B','BUY','L') else 'SELL', 'funding_block')
    if not btc_correlation.allow_alt_trade(coin, side):
        reasons.append('btc_corr')
        _shadow_record_rejection(coin, 'BUY' if side.upper() in ('B','BUY','L') else 'SELL', 'btc_correlation_block')
    passed = len(reasons) == 0
    if return_reasons:
        return passed, reasons
    if not passed:
        log(f"{coin} {side} BLOCKED by {','.join(reasons)}")
    return passed
    if not USE_GRID_GATE:
        return True
    key = coin.upper().replace('.P','')
    # Try: exact, +USDT, strip k prefix +USDT (kBONK→BONKUSDT, kPEPE→PEPEUSDT)
    gate = TICKER_GATES.get(key) or TICKER_GATES.get(key + 'USDT')
    if not gate and key.startswith('K'):
        gate = TICKER_GATES.get(key[1:] + 'USDT')
    if not gate:
        log(f"{coin} NO GATE CONFIG — signal passes ungated")
        return True
    
    glb = gate.get('glb', 20)
    
    # Chase gate buy
    if gate.get('gb') and side == 'BUY' and candles and len(candles) > glb:
        window = candles[-glb:]
        hi = max(c[2] for c in window)
        if price > hi:
            return False
    
    # Chase gate sell
    if gate.get('gs') and side == 'SELL' and candles and len(candles) > glb:
        window = candles[-glb:]
        lo = min(c[3] for c in window)
        if price < lo:
            return False
    
    # Cloud filter
    if gate.get('cloud') and candles and len(candles) >= 50:
        closes = [c[4] for c in candles]
        k = 2/51; ema50 = sum(closes[:50])/50
        for j in range(50, len(closes)):
            ema50 = closes[j]*k + ema50*(1-k)
        k2 = 2/21; ema20 = sum(closes[:20])/20
        for j in range(20, len(closes)):
            ema20 = closes[j]*k2 + ema20*(1-k2)
        if side == 'BUY' and ema20 < ema50:
            return False
        if side == 'SELL' and ema20 > ema50:
            return False
    
    # Body filter
    if gate.get('body', 0) > 0 and candles and len(candles) > 0:
        last = candles[-1]
        br = last[2] - last[3]
        if br > 0 and abs(last[4] - last[1]) / br < gate['body']:
            return False
    
    return True

GRID = {'sens':1, 'rsi':10, 'wick':1, 'ext':1, 'block':1, 'vol':1, 'cd':3}  # tuner-overridden below

def derive(s):
    return {'rsi_hi': 50 + s['rsi']*3, 'rsi_lo': 50 - s['rsi']*3,
            'pivot_lb': max(2, 9 - s['sens']), 'cd': s['cd']}
SP = derive(GRID); BP = derive(GRID)
# TUNER WINNER OVERRIDE — plb=36 rsi=70/35
SP['pivot_lb'] = 15  # OOS: plb=15 lifts PnL +5%, matches trail 0.8% winner
BP['pivot_lb'] = 15
SP['rsi_hi'] = 70  # tight: quality over quantity in chop
BP['rsi_lo'] = 30  # 2026-04-22: 35→30. Previous 35 was asymmetric (15 below 50) vs
                   # sell threshold at 70 (20 above 50). Made BUY signals easier to
                   # trigger than SELL → structural long bias. Now both thresholds
                   # are 20 points from 50 — neutral signal generation.


INITIAL_RISK_PCT = float(os.environ.get('RISK_PCT', '0.01'))  # 1% per user directive (was 0.03 DIAL MODE: 1.5x base. Conf multiplier 0.2x-5.0x. Max conf = 15% equity at SL (hard cap).
SCALED_RISK_PCT  = 0.005
SCALE_DOWN_AT    = 50000
LEV = 10
LOOP_SEC = 60  # 2026-04-23: raised 15s → 60s for 15m signal generation.
               # Signals are 15m-candle based — candle closes every 900s.
               # 60s loop = 15 ticks per 15m candle. Catches all signals at
               # candle close without over-polling HL (rate limits) or racing
               # the grid (signal evaluation takes ~30-50s across 120 coins).
USE_ISOLATED_MARGIN = True

TP_MULTIPLIER = 1.0  # Set to 1.0 — TPs now OOS-tuned PER COIN (no global multiplier needed).
                     # Per-coin 15m OOS optimization: PROMPT 10%, ETH 10%, ALT 6%, ASTER 6%, etc.
                     # Prior value 2.0 was bandaid before per-coin tuning existed.
MAX_POSITIONS = int(os.environ.get('MAX_POSITIONS', '10'))  # 10 default (was 25  # 2026-04-22: raised 8 → 25 for data-gathering phase.
                    # With the signal-generator bias fix live, signals are
                    # now properly filtered at 3 layers (conv floor, MTF gate,
                    # R:R floor). The tight filtering means the remaining
                    # signals that actually fire are high-quality, so we want
                    # to capture most of them rather than artificially capping.
                    # At $660 equity with 25 slots and 3-5% risk per trade,
                    # per-slot notional = $200-$500 (meaningful, covers LLM
                    # cost ~$0.03/trade with >500x headroom). MAX_TOTAL_RISK
                    # at 92% remains the real safety ceiling — position count
                    # is now advisory, not the primary risk gate.
MAX_SAME_SIDE = int(os.environ.get('MAX_SAME_SIDE', '25'))  # 2026-04-24: raised 12→25
                    # during data-gathering phase. HTF alignment multiplier +
                    # contract + invariants make the old same-side protection
                    # redundant — drawdown circuit breaker is the real safety.
MAX_TOTAL_RISK = float(os.environ.get('MAX_TOTAL_RISK', '0.97'))  # 3% reserve for data mode
STOP_LOSS_PCT = 0.02      # 2% — tuner winner config
BTC_VOL_THRESHOLD = 0.03

MAX_HOLD_SEC = 99999 * 3600  # max hold disabled — OOS showed forced exits cost performance
CB_CONSEC_LOSSES = 999  # disabled per user principle
CB_PAUSE_SEC = 600  # 10min (was 60min — too long, cloud exit was triggering it)
FUNDING_CUT_RATIO = 0.50

TRAIL_PCT = 0.015          # OOS winner: +250% vs +40% at 0.3%
TRAIL_TIGHTEN_AFTER_SEC = 7200  # 2h: tighten trail to 0.9% (OOS +77% PnL vs static)
TRAIL_TIGHTEN_PCT = 0.009          # OOS winner: +250% vs +40% at 0.3%
MAKER_FALLBACK_SEC = 10
MAKER_OFFSET = 0.0015  # OOS winner: +21.22%/day  # 0.1% entry split — OOS +127% PnL (better avg entry)

def _init_hl_with_retry(max_attempts=8):
    """Retry Info() init with exponential backoff — Hyperliquid 429s on cold deploys."""
    import time as _t
    for attempt in range(max_attempts):
        try:
            return Info(constants.MAINNET_API_URL, skip_ws=True)
        except Exception as e:
            msg = str(e)
            if '429' in msg or 'rate' in msg.lower():
                wait = min(60, 3 * (2 ** attempt))
                print(f"[HL init] 429 rate-limited, retry {attempt+1}/{max_attempts} in {wait}s", flush=True)
                _t.sleep(wait)
                continue
            raise
    raise RuntimeError("Hyperliquid Info() init failed after retries")

def _init_exchange_with_retry(account, max_attempts=8):
    """Retry Exchange() init with exponential backoff — HL 429s on cold deploys."""
    import time as _t
    for attempt in range(max_attempts):
        try:
            return Exchange(account, constants.MAINNET_API_URL, account_address=WALLET)
        except Exception as e:
            msg = str(e)
            if '429' in msg or 'rate' in msg.lower():
                wait = min(60, 3 * (2 ** attempt))
                print(f"[HL exch init] 429 rate-limited, retry {attempt+1}/{max_attempts} in {wait}s", flush=True)
                _t.sleep(wait)
                continue
            raise
    raise RuntimeError("Hyperliquid Exchange() init failed after retries")

info = _init_hl_with_retry()
account = Account.from_key(PRIV_KEY)
exchange = _init_exchange_with_retry(account)

# 2026-04-25: start HL user-channel WebSocket subscriber. Feeds position_ledger
# from webData2/userFills/orderUpdates. Always-on (passive observer until
# USE_LEDGER_FOR_SIZE=1 is set, then becomes authoritative for size queries).
# Wrapped to prevent any WS init bug from blocking startup.
try:
    hl_user_ws.init(info, WALLET)
    print(f"[hl_user_ws] subscribed for wallet {WALLET[:10]}…", flush=True)
except Exception as _wse:
    print(f"[hl_user_ws] init failed (non-fatal, fallback to REST): {_wse}", flush=True)

# ─── ORDER RATE GOVERNOR ───────────────────────────────────────────────
# 2026-04-22: HL IP-throttles the order endpoint when we burst too many
# exchange.order calls. Symptom this session: 2000 fills/hour, $57/hour
# fees, and 429s on every protect_sl/protect_all retry for 30+ minutes.
# The governor enforces a min-interval between order calls (default 120ms
# = ~8 orders/sec max) and an absolute burst cap (max 20 orders in any
# rolling 5-sec window). When either limit is about to trip, the call
# sleeps just enough to stay within the limit.
# Can be tuned via env: ORDER_MIN_INTERVAL_MS, ORDER_BURST_WINDOW_SEC,
# ORDER_BURST_MAX. Default values are conservative — tune down once we
# see the actual HL order rate that doesn't trigger 429s.
# 2026-04-22: Raised 125→300ms min interval, 20→10 burst cap. Observed
# 429s on SL/TP placement even with 125ms spacing during multi-position
# open bursts. HL's per-IP rate limit appears ~5-6 orders/sec sustained;
# 300ms gives 3.3/sec steady, 10-in-5s burst = 2/sec average. Tighter
# but reliable. Can retighten once HL WS userEvents replaces REST polling.
_ORDER_MIN_INTERVAL = float(os.environ.get('ORDER_MIN_INTERVAL_MS', '300')) / 1000.0
_ORDER_BURST_WINDOW = float(os.environ.get('ORDER_BURST_WINDOW_SEC', '5'))
_ORDER_BURST_MAX = int(os.environ.get('ORDER_BURST_MAX', '10'))
_order_times = []
_order_lock = threading.Lock()
_last_order_ts = [0.0]  # mutable holder for last order time

_orig_exchange_order = exchange.order
def _rate_limited_order(*args, **kwargs):
    """Rate-limit wrapper around exchange.order.
    Enforces: (1) min-interval between consecutive orders, (2) absolute
    burst cap in a rolling window, (3) per-coin spacing via flight_guard
    to prevent same-coin write collisions (cancel→taker 429 pattern).
    Waits just enough to stay legal."""
    # Per-coin spacing first — cheap, prevents same-coin burst BEFORE
    # we hit the global burst cap.
    coin = args[0] if args else kwargs.get('coin')
    if coin:
        flight_guard.acquire(coin)
    with _order_lock:
        now = time.time()
        # Min-interval check
        gap = now - _last_order_ts[0]
        if gap < _ORDER_MIN_INTERVAL:
            delay = _ORDER_MIN_INTERVAL - gap
            time.sleep(delay)
            now = time.time()
        # Burst cap check — trim window
        cutoff = now - _ORDER_BURST_WINDOW
        while _order_times and _order_times[0] < cutoff: _order_times.pop(0)
        # If at burst cap, sleep until oldest order drops out of window
        if len(_order_times) >= _ORDER_BURST_MAX:
            sleep_for = _order_times[0] + _ORDER_BURST_WINDOW - now
            if sleep_for > 0:
                time.sleep(sleep_for)
                now = time.time()
                # Re-trim after sleep
                cutoff = now - _ORDER_BURST_WINDOW
                while _order_times and _order_times[0] < cutoff: _order_times.pop(0)
        # Record + fire
        _order_times.append(now)
        _last_order_ts[0] = now
    return _orig_exchange_order(*args, **kwargs)

exchange.order = _rate_limited_order

# 2026-04-25: cancel wrapper. exchange.cancel was bypassing the order
# rate limiter entirely, so cancel→taker on the same coin within ~600ms
# kept tripping CloudFront (verified in logs: WLFI cancel + taker both
# 429'd 619ms apart). Wrapping cancel in flight_guard fixes it.
_orig_exchange_cancel = exchange.cancel
def _rate_limited_cancel(*args, **kwargs):
    """Apply per-coin spacing + global min-interval to cancels.
    Same write-class as orders from CloudFront's perspective."""
    coin = args[0] if args else kwargs.get('coin')
    if coin:
        flight_guard.acquire(coin)
    # Also burn one slot in the global order rate limiter — cancel
    # contributes to the same per-IP window as order placement.
    with _order_lock:
        now = time.time()
        gap = now - _last_order_ts[0]
        if gap < _ORDER_MIN_INTERVAL:
            time.sleep(_ORDER_MIN_INTERVAL - gap)
            now = time.time()
        _last_order_ts[0] = now
        _order_times.append(now)
        cutoff = now - _ORDER_BURST_WINDOW
        while _order_times and _order_times[0] < cutoff: _order_times.pop(0)
    return _orig_exchange_cancel(*args, **kwargs)

exchange.cancel = _rate_limited_cancel

# 2026-04-26: update_leverage was bypassing the rate limiter entirely. Live
# logs showed `lev set err HBAR: (429, ...)` cascading into failed entries
# because set_isolated_leverage runs immediately before exchange.order.
# Wrap it through the same global window + flight_guard, and add a small
# 429-retry so transient throttles don't kill the whole entry.
def _retry_on_429(fn, *args, max_retries=3, base_delay=0.4, **kwargs):
    """Call fn with up to max_retries on HTTP 429, exponential backoff with jitter."""
    import random as _rand
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            last_exc = e
            msg = str(e)
            if '429' not in msg and 'rate' not in msg.lower():
                raise
            if attempt >= max_retries:
                raise
            wait = base_delay * (2 ** attempt) + _rand.uniform(0, 0.2)
            time.sleep(wait)
    if last_exc:
        raise last_exc

_orig_exchange_update_leverage = exchange.update_leverage
def _rate_limited_update_leverage(*args, **kwargs):
    """Rate-limit + 429-retry wrapper around exchange.update_leverage.
    update_leverage runs in the same per-IP write window as orders/cancels,
    so it must share the global token bucket and per-coin spacing."""
    # update_leverage(leverage, coin, is_cross) — coin is positional arg[1]
    coin = args[1] if len(args) >= 2 else kwargs.get('coin')
    if coin:
        flight_guard.acquire(coin)
    with _order_lock:
        now = time.time()
        gap = now - _last_order_ts[0]
        if gap < _ORDER_MIN_INTERVAL:
            time.sleep(_ORDER_MIN_INTERVAL - gap)
            now = time.time()
        _last_order_ts[0] = now
        _order_times.append(now)
        cutoff = now - _ORDER_BURST_WINDOW
        while _order_times and _order_times[0] < cutoff: _order_times.pop(0)
    return _retry_on_429(_orig_exchange_update_leverage, *args, **kwargs)

exchange.update_leverage = _rate_limited_update_leverage

# Also wrap the existing order/cancel with 429-retry. The rate limiter
# itself prevents most 429s but transient bursts still leak through;
# without retry, a single 429 turns into a missing SL/TP placement.
def _rate_limited_order_with_retry(*args, **kwargs):
    return _retry_on_429(_rate_limited_order, *args, **kwargs)

def _rate_limited_cancel_with_retry(*args, **kwargs):
    return _retry_on_429(_rate_limited_cancel, *args, **kwargs)

exchange.order = _rate_limited_order_with_retry
exchange.cancel = _rate_limited_cancel_with_retry
# ───────────────────────────────────────────────────────────────────────

_META_CACHE = None
def _get_sz_decimals(coin):
    """Perps: price <= 5 sig figs AND <= (MAX_DECIMALS - szDecimals) decimals. MAX_DECIMALS=6 for perps."""
    global _META_CACHE
    if _META_CACHE is None:
        try:
            m = info.meta()
            _META_CACHE = {u['name']: int(u.get('szDecimals',0)) for u in m['universe']}
        except Exception: _META_CACHE = {}
    return _META_CACHE.get(coin, 2)

def round_price(coin, px):
    """HL-compliant price rounding: max 5 sig figs AND max (6 - szDecimals) decimals."""
    szD = _get_sz_decimals(coin)
    max_dec = max(0, 6 - szD)
    # First: 5 significant figures
    if px > 0:
        import math
        sig_scale = 10 ** (5 - int(math.floor(math.log10(abs(px)))) - 1)
        px_sig = round(px * sig_scale) / sig_scale
    else: px_sig = px
    # Then: max_dec decimal places
    return round(px_sig, max_dec)

def round_size(coin, sz):
    szD = _get_sz_decimals(coin)
    return round(sz, szD)

def log(m):
    msg = f"[{datetime.utcnow().isoformat()}] {m}"
    print(msg, flush=True)
    LOG_BUFFER.append(msg)
    if len(LOG_BUFFER) > 200: LOG_BUFFER.pop(0)

def current_risk_pct(equity):
    return SCALED_RISK_PCT if equity >= SCALE_DOWN_AT else INITIAL_RISK_PCT

# ═══════════════════════════════════════════════════════
# STATE — atomic write, rich position tracking (FIX #4)
# ═══════════════════════════════════════════════════════
def load_state():
    default = {'positions':{}, 'cooldowns':{}, 'consec_losses':0, 'cb_pause_until':0, 'last_pnl_close':None, 'cd_format':'ts'}
    try:
        # Try primary path, fall back to backup
        path = STATE_PATH if os.path.exists(STATE_PATH) else STATE_PATH + '.bak'
        with open(path) as f:
            loaded = json.load(f)
        if loaded.get('cd_format') != 'ts':
            loaded['cooldowns'] = {}
            loaded['cd_format'] = 'ts'
        for k,v in default.items():
            if k not in loaded: loaded[k]=v
        # Auto-scrub bogus stats (any bucket pnl > 100% is impossible, drop it)
        s = loaded.get('stats', {})
        if s:
            for bucket_name in ['by_engine','by_hour','by_side','by_coin','by_conf']:
                bucket = s.get(bucket_name, {})
                for k,v in list(bucket.items()):
                    if abs(v.get('pnl',0)) > 100:
                        bucket.pop(k, None)
            if abs(s.get('total_pnl',0)) > 200:
                s['total_pnl'] = 0; s['total_wins'] = 0; s['total_losses'] = 0
        return loaded
    except Exception: return default

def save_state(s):
    """Atomic write with backup for deploy resilience."""
    os.makedirs('/var/data', exist_ok=True)
    tmp = STATE_PATH + '.tmp'
    with open(tmp,'w') as f: json.dump(s,f)
    os.replace(tmp, STATE_PATH)
    # Backup copy survives if primary is lost on deploy
    try:
        import shutil; shutil.copy2(STATE_PATH, STATE_PATH + '.bak')
    except Exception: pass

def kill_switch_active():
    return os.path.exists(KILL_FILE)

# ═══════════════════════════════════════════════════════
# INDICATORS (unchanged from v7)
# ═══════════════════════════════════════════════════════
def rma(a,n):
    r=[None]*len(a); seed=[x for x in a[:n] if x is not None]
    if len(seed)<n: return r
    s=sum(seed)/n; r[n-1]=s
    for i in range(n,len(a)):
        if a[i] is None: r[i]=s; continue
        s=(s*(n-1)+a[i])/n; r[i]=s
    return r

def rsi_calc(c,n=14):
    g=[0]*len(c); lo=[0]*len(c)
    for i in range(1,len(c)): d=c[i]-c[i-1]; g[i]=max(d,0); lo[i]=max(-d,0)
    ag=rma(g,n); al=rma(lo,n); r=[None]*len(c)
    for i in range(len(c)):
        if ag[i] is None: continue
        r[i]=100 if al[i]==0 else 100-100/(1+ag[i]/al[i])
    return r

_CANDLE_CACHE = {}  # {coin: {'data': [...], 'ts': float}}
_CANDLE_COLD = {}   # {coin: ts_unfrozen} — skip HL calls for coins recently 429'd
_LAST_CLOSE_FILL = {}  # {coin: {'fill_px', 'pnl_usd', 'pct', 'ts'}} — exchange-confirmed exit fill
_UNKNOWN_COINS = set()  # coins that don't exist in HL universe — auto-quarantined
# 2026-04-25: cache TTL 300s → 600s. On a 15m base timeframe, cache only needs
# to refresh once per bar (every 900s). 600s gives 1.5× headroom for bar-close
# capture while halving HL load. Diagnosis: 78-coin universe was producing
# CloudFront 429 cascades, signal engines running on partial data, throughput
# dropping to zero. Pure data-availability fix; no signal logic touched.
CANDLE_CACHE_SEC = 600  # was 300; was 120 originally
CANDLE_COLD_SEC = 180   # was 60; longer back-off after 429 to break cascade

# Global HL throttle: shared mutex enforces min-gap across ALL worker threads.
# 2026-04-25: 120ms → 250ms (~4 req/s ceiling). 8 req/s was triggering
# CloudFront's per-IP rate limit on burst. 4 req/s stays under the radar.
# 78 coins × 250ms = 19.5s of cumulative throttle but cache hits absorb most.
_HL_THROTTLE_LOCK = threading.Lock()
_HL_LAST_CALL = [0.0]   # mutable container for thread-shared state
HL_MIN_GAP_SEC = 2.0    # bumped to 2s gap = 0.5 req/s sustained
                        # CloudFront PoP HIO52-P5 still rate-limited
                        # specific coins. 1.0s = 1 req/s sustained,
                        # ~80s build for 78 coins (within 180s TTL).

def _hl_throttle():
    """Block until enough time has elapsed since the last HL call (any thread)."""
    with _HL_THROTTLE_LOCK:
        now = time.time()
        gap = now - _HL_LAST_CALL[0]
        if gap < HL_MIN_GAP_SEC:
            # 2026-04-25: jitter widened 0.02→0.20 to break burst-synchronous
            # patterns. CloudFront seems to fingerprint regular intervals.
            time.sleep(HL_MIN_GAP_SEC - gap + random.uniform(0, 0.20))
        _HL_LAST_CALL[0] = time.time()

def fetch(coin, n_bars=100, retries=3):
    """SNAPSHOT-FIRST candle reader. NO REST during tick path.

    2026-04-25 (final form): hard boundary. fetch() is now O(1) lookup only.
    Order: snapshot → LKG (via candle_snapshot) → Bybit WS → empty.
    Background snapshot builder is the ONLY path that calls HL REST.

    This eliminates the residual REST escape that was causing CloudFront
    cascades during tick pressure. Trade-off: deterministic stale data
    (≤60s on 15m timeframe = ~6% drift) > real-time partial data + instability.

    Returns [] only if no data has ever been fetched for this coin.
    """
    # 1. SNAPSHOT (primary — atomic, full-universe, coverage-gated)
    if _SNAPSHOT_OK and _candle_snap is not None:
        try:
            snap_candles = _candle_snap.get_candles(coin, '15m')
            if snap_candles and len(snap_candles) > 0:
                # Return whatever the snapshot has, capped at n_bars
                return snap_candles[-n_bars:] if len(snap_candles) >= n_bars else snap_candles
        except Exception:
            pass
    # 2. BYBIT WS (already streamed, no REST cost) — kept as best-effort fallback
    try:
        if bybit_ws.has_coin(coin):
            by_candles = bybit_ws.get_candles(coin, limit=n_bars + 50)
            if len(by_candles) >= n_bars:
                return by_candles[-n_bars:]
    except Exception:
        pass
    # 3. LEGACY CACHE (stale-acceptable read only — NO new fetch)
    cached = _CANDLE_CACHE.get(coin)
    if cached:
        return cached['data']
    # 4. HARD STOP — no REST escape during tick
    return []

SCAN_BARS = 12  # scan last 12 bars to catch signals after warmup
CD_MS = 30 * 60 * 1000  # 30 min cooldown — prevents rapid signal re-fire + opposite-exit storm

def chase_gate_ok(side, price, candles, i):
    """Reject entries chasing extended moves.
    Returns True if entry is allowed, False if it should be skipped.
    Only called for coins in CHASE_GATE_COINS."""
    if i < CHASE_LOOKBACK: return True  # not enough history yet
    window = candles[max(0, i-CHASE_LOOKBACK):i]
    if not window: return True
    hi20 = max(c[2] for c in window)
    lo20 = min(c[3] for c in window)
    if hi20 <= lo20: return True
    if side == 'BUY' and price > hi20:
        return False  # chasing upside breakout
    if side == 'SELL' and price < lo20:
        return False  # chasing downside breakdown
    return True


# Trend-pullback signal engine (OOS: n=279 WR=84.9% PnL=+105.83% PF=9.83 on 14d)
# HL-specific 5m-based constants (distinct from MT4 1h PB_* above)
HL_PB_EMA = 20
HL_PB_RSI_HI = 55
HL_PB_RSI_LO = 45
HL_PB_PROXIMITY = 0.003  # within 0.3% of 1H EMA20 derived from 5m resampled

def pullback_signal(coin, candles15, last_pb_buy_ts, last_pb_sell_ts):
    """Returns (side, bar_ts) or (None, None). Entry: 15m near 1H EMA20 + cooled RSI + 4H trend aligned.

    15m candles resample to 1h in groups of 4 (4 × 15m = 1h).
    """
    if len(candles15) < 80: return None, None
    # Resample 15m → 1h (groups of 4)
    n1h = len(candles15) // 4
    if n1h < HL_PB_EMA + 3: return None, None
    c1h = []
    for i in range(n1h):
        g = candles15[i*4:(i+1)*4]
        c1h.append(g[-1][4])
    # 1H EMA20
    k = 2/(HL_PB_EMA+1)
    ema1h = sum(c1h[:HL_PB_EMA])/HL_PB_EMA
    for cv in c1h[HL_PB_EMA:]:
        ema1h = cv*k + ema1h*(1-k)
    last_c = candles15[-1][4]
    if ema1h<=0: return None, None
    dist = abs(last_c - ema1h) / ema1h
    if dist > HL_PB_PROXIMITY: return None, None
    # RSI(14) on 15m
    closes = [b[4] for b in candles15]
    gains=[]; losses=[]
    for i in range(1,len(closes)):
        d=closes[i]-closes[i-1]
        gains.append(max(d,0)); losses.append(max(-d,0))
    p=14
    ag=sum(gains[:p])/p; al=sum(losses[:p])/p
    for i in range(p,len(gains)):
        ag=(ag*(p-1)+gains[i])/p; al=(al*(p-1)+losses[i])/p
    rs = ag/al if al>0 else 999
    r_last = 100-100/(1+rs)
    bar_ts = candles15[-1][0]
    # 4H trend — delegate to trend_gate (V3 already implements)
    trend_up = trend_gate(coin, 'SELL') == False  # if V3 blocks SELL, trend is up
    trend_dn = trend_gate(coin, 'BUY')  == False  # if V3 blocks BUY, trend is down
    buy_ok  = trend_up and r_last < HL_PB_RSI_HI and (bar_ts - last_pb_buy_ts) > CD_MS
    sell_ok = trend_dn and r_last > HL_PB_RSI_LO and (bar_ts - last_pb_sell_ts) > CD_MS
    if buy_ok:  return 'BUY', bar_ts
    if sell_ok: return 'SELL', bar_ts
    return None, None

def signal(candles, last_sell_ts, last_buy_ts, coin=None):
    """Scan last SCAN_BARS closed bars. Cooldown tracked by bar timestamp.
    Applies chase_gate for coins in CHASE_GATE_COINS.

    Tuned params (read via postmortem, fall through to SP/BP defaults):
      rsi.sell_threshold, rsi.buy_threshold, pivot.lookback
    """
    if len(candles)<60: return None, None
    h=[c[2] for c in candles]; l=[c[3] for c in candles]; cl=[c[4] for c in candles]
    N=len(cl); r14=rsi_calc(cl,14)
    # Read tuned params with SP/BP as defaults (per-coin tuner overrides if set)
    if _POSTMORTEM_OK and _postmortem is not None and coin:
        try:
            LB = int(_postmortem.get_param(coin, 'pivot', 'lookback', default=SP['pivot_lb']))
            RH_ = float(_postmortem.get_param(coin, 'rsi', 'sell_threshold', default=SP['rsi_hi']))
            RL_ = float(_postmortem.get_param(coin, 'rsi', 'buy_threshold', default=BP['rsi_lo']))
        except Exception:
            LB = SP['pivot_lb']; RH_ = SP['rsi_hi']; RL_ = BP['rsi_lo']
    else:
        LB = SP['pivot_lb']; RH_ = SP['rsi_hi']; RL_ = BP['rsi_lo']
    apply_gate = coin in CHASE_GATE_COINS
    for i in range(max(LB, N-SCAN_BARS), N):
        if r14[i] is None: continue
        br = h[i]-l[i]
        if br <= 0: continue
        bar_ts = candles[i][0]
        is_pivot_high = h[i] == max(h[max(0,i-LB):i+1])
        is_pivot_low  = l[i] == min(l[max(0,i-LB):i+1])
        sell_ok = is_pivot_high and r14[i] > RH_ and (bar_ts - last_sell_ts) > CD_MS
        buy_ok  = is_pivot_low  and r14[i] < RL_ and (bar_ts - last_buy_ts)  > CD_MS
        if apply_gate:
            if sell_ok and not chase_gate_ok('SELL', cl[i], candles, i):
                sell_ok = False
            if buy_ok and not chase_gate_ok('BUY', cl[i], candles, i):
                buy_ok = False
        if sell_ok: return 'SELL', bar_ts
        if buy_ok:  return 'BUY',  bar_ts
    return None, None

def bb_signal(candles, coin=None, last_buy_ts=0, last_sell_ts=0):
    """Bollinger Band rejection signal. Mirrors OOS tuner logic.
    BUY: low breaks lower BB (2 SD), close back above lower BB, RSI near oversold
    SELL: high breaks upper BB (2 SD), close back below upper BB, RSI near overbought
    Returns (side, bar_ts) or (None, None).
    Enforces CD_MS cooldown from last_buy_ts/last_sell_ts to prevent signal storms.

    Tuned params (postmortem overrides):
      bollinger.period, bollinger.std_mult, bollinger.rsi_buffer,
      rsi.buy_threshold, rsi.sell_threshold
    """
    if len(candles) < 40: return None, None
    h = [c[2] for c in candles]; l = [c[3] for c in candles]; cl = [c[4] for c in candles]
    N = len(cl)
    # Defaults
    BB_P_default = 20
    std_mult_default = 2.0
    rsi_buffer_default = 0.0  # 2026-04-22: was 5.0. Buffer widened RSI thresholds
                                # asymmetrically (BUY at RSI<40, SELL at RSI>65 with
                                # 35/70 defaults). Zero buffer = BB signal uses the
                                # raw RSI threshold, no directional widening.
    r14 = rsi_calc(cl, 14)
    # Per-coin RL/RH if available, else globals
    RL_def = BP['rsi_lo']; RH_def = SP['rsi_hi']
    try:
        if coin and percoin_configs.ELITE_MODE and percoin_configs.is_elite(coin):
            cfg = percoin_configs.get_config(coin)
            if cfg:
                RL_def = cfg.get('RL', RL_def); RH_def = cfg.get('RH', RH_def)
    except Exception: pass
    # Read tuner overrides
    if _POSTMORTEM_OK and _postmortem is not None and coin:
        try:
            BB_P = int(_postmortem.get_param(coin, 'bollinger', 'period', default=BB_P_default))
            std_mult = float(_postmortem.get_param(coin, 'bollinger', 'std_mult', default=std_mult_default))
            rsi_buffer = float(_postmortem.get_param(coin, 'bollinger', 'rsi_buffer', default=rsi_buffer_default))
            RL = float(_postmortem.get_param(coin, 'rsi', 'buy_threshold', default=RL_def))
            RH = float(_postmortem.get_param(coin, 'rsi', 'sell_threshold', default=RH_def))
        except Exception:
            BB_P, std_mult, rsi_buffer, RL, RH = BB_P_default, std_mult_default, rsi_buffer_default, RL_def, RH_def
    else:
        BB_P, std_mult, rsi_buffer, RL, RH = BB_P_default, std_mult_default, rsi_buffer_default, RL_def, RH_def
    for i in range(max(BB_P+5, N-SCAN_BARS), N):
        if r14[i] is None: continue
        window = cl[i-BB_P:i]
        mean = sum(window)/BB_P
        var = sum((x-mean)**2 for x in window)/BB_P
        sd = var**0.5
        if sd <= 0: continue
        upper = mean + std_mult*sd; lower = mean - std_mult*sd
        bar_ts = candles[i][0]
        # BUY: pierced lower BB, closed back above, RSI in oversold zone
        if l[i] <= lower and cl[i] > lower and r14[i] < RL + rsi_buffer and (bar_ts - last_buy_ts) > CD_MS:
            return 'BUY', bar_ts
        # SELL: pierced upper BB, closed back below, RSI in overbought zone
        if h[i] >= upper and cl[i] < upper and r14[i] > RH - rsi_buffer and (bar_ts - last_sell_ts) > CD_MS:
            return 'SELL', bar_ts
    return None, None

def ib_signal(candles, coin=None, last_buy_ts=0, last_sell_ts=0):
    """Inside Bar breakout signal. Two consecutive inside bars, then breakout.
    BUY: close breaks above prior inner bar high
    SELL: close breaks below prior inner bar low
    Returns (side, bar_ts) or (None, None).
    Enforces CD_MS cooldown from last_buy_ts/last_sell_ts to prevent signal storms."""
    if len(candles) < 10: return None, None
    h = [c[2] for c in candles]; l = [c[3] for c in candles]; cl = [c[4] for c in candles]
    N = len(cl)
    for i in range(max(5, N-SCAN_BARS), N):
        if i < 4: continue
        inside1 = h[i-1] < h[i-2] and l[i-1] > l[i-2]
        inside2 = h[i-2] < h[i-3] and l[i-2] > l[i-3]
        if not (inside1 and inside2): continue
        bar_ts = candles[i][0]
        if cl[i] > h[i-1] and (bar_ts - last_buy_ts) > CD_MS: return 'BUY', bar_ts
        if cl[i] < l[i-1] and (bar_ts - last_sell_ts) > CD_MS: return 'SELL', bar_ts
    return None, None

def pass_per_coin_filter(coin, side, candles, i):
    """Apply per-coin ema200/adx25/adx20 filter from percoin_configs.
    Returns True if signal passes, False otherwise.
    For non-elite coins or coins without filter configured, always returns True."""
    try:
        if not (percoin_configs.ELITE_MODE and percoin_configs.is_elite(coin)):
            return True
        cfg = percoin_configs.get_config(coin)
        if not cfg: return True
        flt = cfg.get('flt', 'none')
        if flt == 'none': return True
        cl = [c[4] for c in candles]
        h = [c[2] for c in candles]
        l = [c[3] for c in candles]
        N = len(cl)
        if 'ema200' in flt and N >= 200:
            k = 2/201
            e = sum(cl[:200])/200
            for j in range(200, i+1): e = cl[j]*k + e*(1-k)
            if side == 'BUY' and cl[i] < e: return False
            if side == 'SELL' and cl[i] > e: return False
        if 'ema50' in flt and N >= 50:
            k = 2/51
            e = sum(cl[:50])/50
            for j in range(50, i+1): e = cl[j]*k + e*(1-k)
            if side == 'BUY' and cl[i] < e: return False
            if side == 'SELL' and cl[i] > e: return False
        if 'adx' in flt and N >= 28:
            # Minimum ADX threshold (14-period Wilder's)
            threshold = 25 if 'adx25' in flt else 20
            P = 14
            # Compute recent ADX at index i
            tr_s = []; pdm_s = []; ndm_s = []
            for j in range(1, i+1):
                tr = max(h[j]-l[j], abs(h[j]-cl[j-1]), abs(l[j]-cl[j-1]))
                up = h[j]-h[j-1]; dn = l[j-1]-l[j]
                pdm = up if (up > dn and up > 0) else 0
                ndm = dn if (dn > up and dn > 0) else 0
                tr_s.append(tr); pdm_s.append(pdm); ndm_s.append(ndm)
            if len(tr_s) < 2*P: return False
            atr = sum(tr_s[:P])/P; spdm = sum(pdm_s[:P]); sndm = sum(ndm_s[:P])
            for j in range(P, len(tr_s)):
                atr = (atr*(P-1) + tr_s[j])/P
                spdm = spdm - spdm/P + pdm_s[j]
                sndm = sndm - sndm/P + ndm_s[j]
            pdi = 100*spdm/atr if atr>0 else 0
            ndi = 100*sndm/atr if atr>0 else 0
            dx = 100*abs(pdi-ndi)/(pdi+ndi) if (pdi+ndi)>0 else 0
            # Single-bar ADX approximation — compare DX vs threshold directly (noisy but fast)
            if dx < threshold: return False
        if 'conv' in flt and N >= 50:
            # CONVICTION STACK: adx_low + ema21_far + deep_os
            # OOS 15m: lifts Exp from $1.86 → $2.39 (+29%), WR 48.5% → 49.1%
            # Trade count drops 62% but every trade is higher quality
            # 1. ADX_LOW: only trade when ADX < 30 (mean-reversion regime)
            P = 14
            tr_s = []; pdm_s = []; ndm_s = []
            for j in range(max(1, i-60), i+1):
                tr = max(h[j]-l[j], abs(h[j]-cl[j-1]), abs(l[j]-cl[j-1]))
                up = h[j]-h[j-1]; dn = l[j-1]-l[j]
                pdm = up if (up > dn and up > 0) else 0
                ndm = dn if (dn > up and dn > 0) else 0
                tr_s.append(tr); pdm_s.append(pdm); ndm_s.append(ndm)
            if len(tr_s) >= 2*P:
                atr = sum(tr_s[:P])/P; spdm = sum(pdm_s[:P]); sndm = sum(ndm_s[:P])
                for j in range(P, len(tr_s)):
                    atr = (atr*(P-1) + tr_s[j])/P
                    spdm = spdm - spdm/P + pdm_s[j]
                    sndm = sndm - sndm/P + ndm_s[j]
                pdi = 100*spdm/atr if atr>0 else 0
                ndi = 100*sndm/atr if atr>0 else 0
                dx = 100*abs(pdi-ndi)/(pdi+ndi) if (pdi+ndi)>0 else 0
                if dx >= 30: return False  # ADX too high, trend regime — skip mean-rev
            # 2. EMA21_FAR: require >1% distance from 21 EMA
            k21 = 2/22
            if N >= 21:
                e21 = sum(cl[:21])/21
                for j in range(21, i+1): e21 = cl[j]*k21 + e21*(1-k21)
                if cl[i] > 0 and abs(cl[i]-e21)/cl[i] < 0.01: return False
            # 3. DEEP_OS: price must be outside 1.5σ band (not just touching 2σ)
            BB_P = 20
            if N >= BB_P:
                window = cl[i-BB_P:i]
                mu = sum(window)/BB_P
                var = sum((x-mu)**2 for x in window)/BB_P
                sd = var**0.5
                if sd > 0:
                    if side == 'BUY' and cl[i] > mu - 1.5*sd: return False
                    if side == 'SELL' and cl[i] < mu + 1.5*sd: return False
        return True
    except Exception:
        return True  # fail-open to avoid blocking trades on filter bugs

# ═══════════════════════════════════════════════════════
# HL INTERFACE
# ═══════════════════════════════════════════════════════
def get_balance():
    try: return float(_cached_user_state()['marginSummary']['accountValue'])
    except Exception: return 0

def get_total_margin():
    try: return float(_cached_user_state()['marginSummary'].get('totalMarginUsed', 0))
    except Exception: return 0

# ═══════════════════════════════════════════════════════
# API CACHE — reduces HL API calls from 100+/cycle to ~3/cycle
# ═══════════════════════════════════════════════════════
_cache = {'mids': None, 'mids_ts': 0, 'state': None, 'state_ts': 0}
CACHE_TTL = 5  # seconds
CACHE_TTL_ORDERS = 3  # orders change more — shorter cache

def _cached_mids():
    now = time.time()
    if _cache['mids'] is None or now - _cache['mids_ts'] > CACHE_TTL:
        try:
            _cache['mids'] = info.all_mids()
            _cache['mids_ts'] = now
        except Exception: pass
    return _cache['mids'] or {}

def _cached_user_state():
    now = time.time()
    if _cache['state'] is None or now - _cache['state_ts'] > CACHE_TTL:
        try:
            _cache['state'] = info.user_state(WALLET)
            _cache['state_ts'] = now
        except Exception: pass
    return _cache['state'] or {}

_cache['fo'] = None; _cache['fo_ts'] = 0
def _cached_frontend_orders():
    """Cache frontend_open_orders to avoid hammering HL. 4 call sites in this
    file hit this endpoint — protect_sl, protect_all, retune_exits, mt4
    flatten-check — often in rapid succession. Unbounded requests → 429.
    3-sec cache is still fresh enough for trigger-order checks without
    missing rapidly-placed SL/TP."""
    now = time.time()
    if _cache['fo'] is None or now - _cache['fo_ts'] > CACHE_TTL_ORDERS:
        try:
            _cache['fo'] = info.frontend_open_orders(WALLET)
            _cache['fo_ts'] = now
        except Exception: pass
    return _cache['fo'] or []

def get_mid(coin):
    """Get latest mid price. Bybit WS FIRST (no rate limit, ~500ms lead on HL),
    HL cached mids as fallback. This is the hot path — called by every SL/TP
    placement, every gate check, every position PnL computation.

    FIXED 2026-04-22: previous impl went straight to HL info.all_mids() with
    a 5-sec cache. At 7+ positions × ~10 gate checks per tick × ticks every
    30s × 120 coins, this hammered HL /info and triggered 429s on the very
    endpoints we need for SL placement. Root cause of the 'naked position'
    symptom: protect_sl and place_native_sl both call this path.

    The 500ms lead on Bybit is also a minor alpha leak — SL/TP placement at
    a lagging price misses the real mid by ~0.05-0.1%.
    """
    # Bybit WS — fresh if age <= 3s
    try:
        if bybit_ws.has_coin(coin):
            by_px, age_ms = bybit_ws.get_price(coin)
            if by_px and age_ms is not None and age_ms <= 3000:
                return float(by_px)
    except Exception: pass
    # HL mids fallback (still cached 5s at _cached_mids level)
    try: return float(_cached_mids()[coin])
    except Exception: return None

_POSITIONS_CACHE = {'data': {}, 'ts': 0}
_SIGNAL_LOG = []  # ring buffer
_SIGNAL_LOG_LOCK = threading.Lock()
def log_signal(coin, kind, side=None):
    import datetime
    with _SIGNAL_LOG_LOCK:
        _SIGNAL_LOG.append({'coin':coin,'kind':kind,'side':side,
                            'ts': datetime.datetime.utcnow().strftime('%H:%M:%S')})
        if len(_SIGNAL_LOG) > 50: del _SIGNAL_LOG[:len(_SIGNAL_LOG)-50]

def get_all_positions_live(force=False):
    """Cached — refreshes once per tick (5s). Force=True for critical ops."""
    now = time.time()
    if not force and now - _POSITIONS_CACHE['ts'] < 4:
        return _POSITIONS_CACHE['data']
    """Returns dict of coin -> {size, entry, pnl, mark} for all actual positions on HL."""
    out={}
    try:
        for p in _cached_user_state().get('assetPositions',[]):
            pos=p['position']
            sz=float(pos.get('szi',0))
            if sz!=0:
                out[pos['coin']] = {
                    'size':sz,
                    'entry':float(pos['entryPx']),
                    'pnl':float(pos['unrealizedPnl']),
                    'mark':float(pos.get('positionValue',0)) / abs(sz) if sz else 0,
                    'lev':int(pos.get('leverage',{}).get('value',10)),
                    'upnl':float(pos['unrealizedPnl']),
                }
    except Exception as e:
        log(f"positions fetch err: {e}")
    _POSITIONS_CACHE['data'] = out
    _POSITIONS_CACHE['ts'] = time.time()
    return out

_FUNDING_CACHE = {'data': {}, 'ts': 0}
def get_funding_rate(coin):
    now = __import__('time').time()
    if now - _FUNDING_CACHE['ts'] < 900:  # cache 15 min
        return _FUNDING_CACHE['data'].get(coin, 0)
    """Fetch current funding rate for a coin (per hour). Negative = shorts pay, positive = longs pay."""
    try:
        meta = info.meta_and_asset_ctxs()
        asset_ctxs = meta[1]
        universe = meta[0]['universe']
        for i, u in enumerate(universe):
            if u['name']==coin and i<len(asset_ctxs):
                return float(asset_ctxs[i].get('funding', 0))
    except Exception: pass
    return 0

def calc_size(equity, px, risk_pct, risk_mult=1.0, coin=None, side='BUY'):
    # Per-coin leverage (BTC/ETH 20x, alts 3-10x)
    actual_lev = leverage_map.get_max(coin, default=LEV) if coin else LEV
    # News risk multiplier
    try: news_mult = news_filter.get_risk_mult()
    except Exception: news_mult = 1.0
    try: news_dir = news_filter.get_state().get('direction_bias', 0)
    except Exception: news_dir = 0
    # News + orderbook composite boost
    try: confluence = wall_confluence.composite_boost(coin, side, px, news_dir) if coin else 1.0
    except Exception: confluence = 1.0
    # Session scaler (London/NY 1.0x, Asia 0.7x)
    try: session_mult = session_scaler.get_mult()
    except Exception: session_mult = 1.0
    confluence *= session_mult
    try: whale_mult = whale_filter.confluence_boost(coin, side) if coin else 1.0
    except Exception: whale_mult = 1.0
    confluence *= whale_mult
    # CVD confluence: aligned buy/sell pressure boost
    try:
        cvd_sig = cvd_ws.cvd_signal(coin) if coin else None
        if cvd_sig == side: confluence *= 1.3
        elif cvd_sig and cvd_sig != side: confluence *= 0.7
    except Exception: pass
    # OI confluence: position-adding on our side = trend continuation
    try:
        if coin:
            # Simple price direction from recent candles not available here — use side as intent
            oi_delta = oi_tracker.get_delta(coin) if coin else 0
            if oi_delta > 0.02:  # OI rising >2%
                confluence *= 1.2
    except Exception: pass
    # Risk ladder override
    try: tier_risk = risk_ladder.get_risk()
    except Exception: tier_risk = risk_pct
    raw = equity * tier_risk * risk_mult * news_mult * confluence * actual_lev / px
    # 2026-04-25: DEBUG mode — force fixed notional regardless of sizing logic.
    # Bypasses risk_mult, leverage, confluence, session, news, etc. Pure fixed
    # USD per trade for clean data collection during bug-hunt phase.
    if FORCE_NOTIONAL_USD > 0:
        raw = FORCE_NOTIONAL_USD / px
    if raw>=100: return round(raw,0)
    if raw>=10:  return round(raw,1)
    if raw>=1:   return round(raw,2)
    if raw>=0.1: return round(raw,3)
    return round(raw,4)

def set_isolated_leverage(coin):
    """Set isolated margin + per-coin tier leverage before opening.
    Uses TIER_SIZING from percoin_configs (not global LEV).
    HL caps to coin's max leverage automatically."""
    try:
        # Get tier leverage (15 PURE / 12 NINETY_99 / 10 EIGHTY_89 / 10 SEVENTY_79)
        tier_lev, _ = percoin_configs.get_sizing(coin)
        # HL update_leverage(leverage, coin, is_cross). is_cross MUST be False for isolated.
        exchange.update_leverage(tier_lev, coin, is_cross=False)
    except Exception as e:
        log(f"lev set err {coin}: {e}")
        # Fallback: try global LEV if tier lookup fails
        try: exchange.update_leverage(LEV, coin, is_cross=False)
        except: pass

# ═══════════════════════════════════════════════════════
# TIER-PRIORITY BUMP — free margin from lower-tier positions for higher-tier signals
# ═══════════════════════════════════════════════════════
TIER_PRIO = {'PURE': 4, 'NINETY_99': 3, 'EIGHTY_89': 2, 'SEVENTY_79': 1}

def try_tier_bump(incoming_coin, state, live_positions):
    """If margin would reject incoming trade, bump the lowest-priority active positions below incoming tier.
    Only called when elite_mode and incoming coin is in whitelist.
    Returns (freed_margin_estimate_usd, count_bumped). Closes positions as side effect.

    Safety:
    - Only bumps coins with STRICTLY LOWER tier than incoming (PURE never bumped)
    - Stops bumping once enough margin freed
    - Never bumps more than 3 positions per signal (cascade guard)
    - Fail-safe on close() error — stops bumping, returns what was freed
    """
    if not percoin_configs.ELITE_MODE or not percoin_configs.is_elite(incoming_coin):
        return 0, 0
    incoming_tier = percoin_configs.get_tier(incoming_coin)
    if not incoming_tier: return 0, 0
    incoming_prio = TIER_PRIO.get(incoming_tier, 0)

    # Check current margin state
    try:
        us = _cached_user_state()
        total_margin = float(us['marginSummary'].get('totalMarginUsed', 0))
        account_value = float(us['marginSummary'].get('accountValue', 0))
        withdrawable = float(us.get('withdrawable', 0))
    except Exception:
        return 0, 0

    # Rough size of what we want to open (use risk_pct × equity as margin proxy)
    try:
        risk_pct = current_risk_pct(account_value)
        cfg = percoin_configs.get_config(incoming_coin) or {}
        # Use tier target risk matching new TIER_SIZING (5/3/3/3 — see percoin_configs.py)
        target_risk = {'PURE': 0.02, 'NINETY_99': 0.012, 'EIGHTY_89': 0.012, 'SEVENTY_79': 0.012}.get(incoming_tier, 0.012)
        needed_margin = account_value * target_risk
    except Exception:
        needed_margin = account_value * 0.15

    # If we have >= needed margin available, no bump needed
    if withdrawable >= needed_margin * 1.05:
        return 0, 0

    # Find bump candidates: active positions with lower tier than incoming
    candidates = []
    for coin, lp in live_positions.items():
        if coin == incoming_coin: continue
        sz = lp.get('size', 0)
        if sz == 0: continue
        tier = percoin_configs.get_tier(coin)
        if not tier: continue
        prio = TIER_PRIO.get(tier, 0)
        if prio >= incoming_prio: continue  # same or higher, skip
        # Estimate margin freed by closing this position
        entry = lp.get('entry', 0)
        if not entry: continue
        notional = abs(sz) * entry
        margin_used = notional / 5  # assume 5x avg lev post-resolver
        pnl = lp.get('pnl', 0)
        # Prefer bumping losers first (rank: lowest prio first, then most negative pnl)
        candidates.append((prio, pnl, margin_used, coin, tier))

    if not candidates:
        return 0, 0

    candidates.sort(key=lambda x: (x[0], x[1]))  # lowest tier first, then worst pnl first

    freed = 0
    count = 0
    MAX_BUMPS = 3
    for prio, pnl, margin, coin, tier in candidates:
        if count >= MAX_BUMPS: break
        if freed + withdrawable >= needed_margin * 1.05: break
        try:
            close(coin, state_ref=state)
            log(f"TIER-BUMP closed {coin} (tier={tier} pnl=${pnl:+.3f}) to free ~${margin:.0f} for incoming {incoming_coin} ({incoming_tier})")
            state.get('positions', {}).pop(coin, None)
            freed += margin
            count += 1
        except Exception as e:
            log(f"tier-bump close err {coin}: {e}")
            break

    if count > 0:
        log(f"TIER-BUMP: freed ~${freed:.0f} by closing {count} lower-tier positions for {incoming_coin}")
    return freed, count

# ─── ATOMIC ENTRY DISPATCHER ─────────────────────────────────────────
# 2026-04-25: when USE_ATOMIC_EXEC=1, route entry through atomic_entry which
# submits entry+SL+TP in one bulk_orders call. Eliminates the fill-before-SL
# race that was triggering the audit→enforce cascade. Falls back to legacy
# place() if disabled or if atomic submission fails.

MAX_SL_PCT = float(os.environ.get('MAX_SL_PCT', '0.025'))
# 2026-04-26: global hard ceiling on SL distance. Per-coin OOS configs ranged
# 1.5%-5% (RSR=4%, JTO=3%, etc.). At min notional, a 4% stop is bounded loss,
# but observed RSR -$0.45 + JTO -$0.34 events drove the engine PnL deep into
# the red. 2.5% cap halves the worst-case per-trade loss without changing
# trade flow — same coins still trigger, just with smaller tail.
# Disable cap entirely with MAX_SL_PCT=0.

def _apply_sl_cap(sl_pct):
    """Clamp SL distance to MAX_SL_PCT (env-tunable). 0 disables the cap."""
    if not sl_pct or sl_pct <= 0:
        return sl_pct
    if MAX_SL_PCT > 0 and sl_pct > MAX_SL_PCT:
        return MAX_SL_PCT
    return sl_pct


def _compute_sl_px(coin, is_long, entry):
    """Compute SL trigger price for atomic entry. Pure — no I/O.
    Returns (sl_px_rounded, sl_pct_used) or (None, None)."""
    sl_pct = STOP_LOSS_PCT  # global fallback
    try:
        if percoin_configs.ELITE_MODE and percoin_configs.is_elite(coin):
            cfg = percoin_configs.get_config(coin)
            if cfg and 'SL' in cfg:
                sl_pct = cfg['SL']
    except Exception: pass
    sl_pct = _apply_sl_cap(sl_pct)
    if not sl_pct or sl_pct <= 0:
        return None, None
    entry = float(entry)
    trigger_px = entry * (1 - sl_pct) if is_long else entry * (1 + sl_pct)
    try:
        return float(round_price(coin, trigger_px)), float(sl_pct)
    except Exception:
        return float(trigger_px), float(sl_pct)


def _compute_tp_px(coin, is_long, entry):
    """Compute TP trigger price for atomic entry. Pure — no I/O.
    Returns (tp_px_rounded, tp_pct_used) or (None, None)."""
    cfg = None
    try:
        if percoin_configs.ELITE_MODE and percoin_configs.is_elite(coin):
            cfg = percoin_configs.get_config(coin)
    except Exception: pass
    if not cfg or 'TP' not in cfg:
        # Contract fallback for non-elite coins
        if _EC_OK and _contract is not None:
            try:
                cfg = _contract.get_fallback_config(coin)
            except Exception:
                return None, None
        else:
            return None, None
    tp_pct = cfg.get('TP') if cfg else None
    if not tp_pct or tp_pct <= 0:
        return None, None
    if MAX_TP_PCT > 0 and tp_pct > MAX_TP_PCT:
        tp_pct = MAX_TP_PCT
    entry = float(entry)
    trigger_px = entry * (1 + tp_pct) if is_long else entry * (1 - tp_pct)
    try:
        return float(round_price(coin, trigger_px)), float(tp_pct)
    except Exception:
        return float(trigger_px), float(tp_pct)


def _dispatch_entry(coin, is_buy, size, cloid=None, trade_id=None):
    """Single entry-point that routes to atomic OR legacy based on flag.

    Returns dict:
      {
        'fill_px':       float or None,
        'sl_pct':        float or None,    # populated if atomic succeeded
        'tp_pct':        float or None,    # populated if atomic succeeded
        'atomic_used':   bool,             # True if bulk_orders succeeded
        'reason':        str or None,
      }

    On atomic-success: SL+TP are already placed atomically. Caller should
    SKIP enforce_position_protection (it would just be a redundant verifier).

    On atomic-fail or flag-off: falls back to legacy place(). Caller should
    run enforce_position_protection as before.
    """
    out = {'fill_px': None, 'sl_pct': None, 'tp_pct': None,
           'atomic_used': False, 'reason': None,
           # 2026-04-26: expected_px = pre-order mid; realized_slippage_pct
           # = signed (fill - expected)/expected * sign(side). Positive =
           # unfavorable (paid more for buy / received less for sell).
           'expected_px': None, 'realized_slippage_pct': None}

    # 2026-04-26: k-prefix coin block. HL's k-coins (kFLOKI/kPEPE/kSHIB/kBONK/
    # kNEIRO) report prices in mismatched scales between get_mid() and order
    # fill paths, producing 1000x-off PnL math (saw kFLOKI fake +$998 on $11
    # trade). Skip until the unit handling is properly normalized.
    if coin and coin.startswith('k') and len(coin) >= 4 and coin[1].isupper():
        out['reason'] = 'k_coin_blocked'
        log(f"{coin} blocked: k-prefix coin (1000x scale mismatch — see TODO)")
        return out

    # 2026-04-26: per-coin blocklist (RSR, JTO by default). These two had
    # outsized single-trade losses (-$0.45 and -$0.34) that ate the PnL
    # of otherwise-winning engines (PIVOT 70% WR, FUNDING_MR 86% WR).
    # Block at the dispatcher so all engine paths skip them.
    if coin and coin.upper() in COIN_BLOCKLIST:
        out['reason'] = 'coin_blocklist'
        log(f"{coin} blocked: in COIN_BLOCKLIST={sorted(COIN_BLOCKLIST)}")
        return out

    # 2026-04-26: side filter — defaults to BOTH sides. The earlier 30-trade
    # SELL-bias signal dissipated by trade 33 (BUY 50% / SELL 54.5% over 40
    # decided, Wilson CIs heavily overlap). Don't cut signals on weak evidence.
    # Override explicitly via ALLOWED_SIDES=BUY or =SELL if needed.
    _side_label = 'BUY' if is_buy else 'SELL'
    _sides_env = os.environ.get('ALLOWED_SIDES', 'BUY,SELL').upper()
    _allowed = {s.strip() for s in _sides_env.split(',') if s.strip() in ('BUY', 'SELL')}
    if not _allowed:
        _allowed = {'BUY', 'SELL'}
    if _side_label not in _allowed:
        out['reason'] = 'side_filter'
        log(f"{coin} {_side_label} blocked by ALLOWED_SIDES={sorted(_allowed)}")
        return out

    # ─── Legacy path (flag off, or atomic skipped) ────────────────────
    if not USE_ATOMIC_EXEC:
        # Capture expected (mid) BEFORE place() so we can compute slippage.
        # place() returns the limit price (not actual fill) so this is best-
        # effort for legacy — atomic path computes from real fill avgPx.
        try:
            _expected = get_mid(coin)
            if _expected:
                out['expected_px'] = _expected
        except Exception:
            pass
        out['fill_px'] = place(coin, is_buy, size, cloid=cloid)
        # Best-effort slippage from place() return (which is limit_px on maker
        # fills, mid on taker). Approximate but still useful.
        if out['expected_px'] and out['fill_px']:
            try:
                _drift = (float(out['fill_px']) - float(out['expected_px'])) / float(out['expected_px'])
                out['realized_slippage_pct'] = _drift if is_buy else -_drift
            except Exception:
                pass
        out['reason'] = 'flag_off'
        return out

    # ─── Atomic path ──────────────────────────────────────────────────
    # Lifecycle lock — same dedup as place()
    if cloid is not None:
        if not order_state.acquire(cloid):
            log(f"atomic_entry({coin}) SKIP: cloid={cloid} already in flight")
            out['reason'] = 'cloid_in_flight'
            return out

    try:
        # Pre-checks shared with place()
        size = round_size(coin, size)
        if size <= 0:
            out['reason'] = 'size_rounded_zero'
            log(f"{coin} size rounded to 0 — skip atomic"); return out

        try:
            set_isolated_leverage(coin)
        except Exception as _le:
            log(f"{coin} leverage set err (non-fatal): {_le}")

        mark_px = get_mid(coin)
        if not mark_px:
            out['reason'] = 'no_mark_px'
            log(f"{coin} no mid price — skip atomic"); return out

        is_long = bool(is_buy)
        sl_px, sl_pct = _compute_sl_px(coin, is_long, mark_px)
        tp_px, tp_pct = _compute_tp_px(coin, is_long, mark_px)
        if sl_px is None or tp_px is None:
            # Can't form a complete bracket — fall back to legacy.
            log(f"{coin} atomic skipped: sl_px={sl_px} tp_px={tp_px} → legacy")
            out['fill_px'] = place(coin, is_buy, size, cloid=cloid)
            out['reason'] = 'no_bracket_pcts'
            return out

        # Submit atomically
        tid = trade_id or cloid or f"{coin}_{'B' if is_buy else 'S'}_{int(time.time())}"
        result = atomic_entry.submit_atomic(
            exchange=exchange,
            coin=coin, is_buy=is_buy, size=size,
            mark_px=mark_px, sl_px=sl_px, tp_px=tp_px,
            trade_id=tid,
            log_fn=log,
            price_rounder=round_price,  # fixes float_to_wire on small-tick coins
        )

        if result.get('success'):
            out['fill_px'] = result.get('fill_px') or mark_px
            out['sl_pct'] = sl_pct
            out['tp_pct'] = tp_pct
            out['atomic_used'] = True
            out['reason'] = 'atomic_ok'
            # mark_px = expected; result.fill_px = actual avgPx from HL.
            # This path produces EXACT slippage (vs the legacy approximation).
            out['expected_px'] = mark_px
            try:
                _drift = (float(out['fill_px']) - float(mark_px)) / float(mark_px)
                out['realized_slippage_pct'] = _drift if is_buy else -_drift
            except Exception:
                pass
            log(f"{coin} ATOMIC ENTRY ok: fill={out['fill_px']} "
                f"sl={sl_px}({sl_pct*100:.2f}%) tp={tp_px}({tp_pct*100:.2f}%) "
                f"oids: e={result.get('entry_oid')} sl={result.get('sl_oid')} tp={result.get('tp_oid')}")
            return out

        # Atomic failed — try legacy as last resort
        log(f"{coin} ATOMIC FAILED ({result.get('reason')}) → legacy place()")
        # If atomic placed orphan triggers, cancel them before legacy retry
        for _kind, _oid in (('sl', result.get('sl_oid')), ('tp', result.get('tp_oid'))):
            if _oid:
                try:
                    exchange.cancel(coin, _oid)
                    log(f"{coin} cleaned up orphan {_kind}_oid={_oid}")
                except Exception as _ce:
                    log(f"{coin} orphan {_kind}_oid={_oid} cleanup err: {_ce}")
        # Use _place_impl (lock-free) — we already hold the cloid lock in this scope
        # mark_px from above is the expected reference; place() returns the
        # limit/mid (best-effort slippage on this fallback path).
        out['expected_px'] = mark_px
        out['fill_px'] = _place_impl(coin, is_buy, size, cloid=cloid)
        if out['fill_px']:
            try:
                _drift = (float(out['fill_px']) - float(mark_px)) / float(mark_px)
                out['realized_slippage_pct'] = _drift if is_buy else -_drift
            except Exception:
                pass
        out['reason'] = f"atomic_fail_legacy_fallback:{result.get('reason')}"
        return out
    finally:
        if cloid is not None:
            order_state.release(cloid)


def place(coin, is_buy, size, cloid=None):
    """HL-compliant price rounding + maker/taker handling.

    Step 2: accepts optional cloid for exchange-side identity binding.
    cloid derives from trade_id by caller; see entry sites in process()/webhook.

    2026-04-25: lifecycle lock — if a place() for this cloid is already in
    flight (e.g. webhook re-fired while previous attempt still running),
    skip and return None. Prevents duplicate fills and cancel/taker race.
    """
    # Lifecycle lock — prevent duplicate dispatch on same trade_id
    if cloid is not None:
        if not order_state.acquire(cloid):
            log(f"place({coin}) SKIP: cloid={cloid} already in flight")
            return None
    try:
        return _place_impl(coin, is_buy, size, cloid)
    finally:
        if cloid is not None:
            order_state.release(cloid)


def _place_impl(coin, is_buy, size, cloid=None):
    """Original place() body — wrapped by place() with lifecycle lock."""
    # Convert cloid string to hyperliquid Cloid object if provided
    _cloid_obj = None
    if cloid:
        try:
            from hyperliquid.utils.types import Cloid
            # HL requires cloid as 16-byte hex padded: '0x' + 32 hex chars
            hex_str = cloid.encode().hex()[:32].ljust(32, '0')
            _cloid_obj = Cloid.from_str('0x' + hex_str)
        except Exception as _ce:
            log(f"cloid construction err {coin}: {_ce} — placing without cloid")
            _cloid_obj = None

    px = get_mid(coin)
    if not px: return None
    set_isolated_leverage(coin)
    size = round_size(coin, size)
    if size <= 0:
        log(f"{coin} size rounded to 0 — skip"); return None

    # MIN-NOTIONAL GUARD. Refuse to open positions below $50 notional.
    # Below this, the $0.10 dust-sweep threshold fires on <0.2% price moves
    # and closes the position before TP/SL has a chance. Every trade in the
    # post-mortem log from the first 40 runs dust-swept at 30min because
    # notional was \$13-\$37 — winners and losers both killed prematurely.
    # This guard is the cheapest, cleanest fix: if calc_size produces a dust
    # position, we skip rather than trade. Post-mortem log stops filling with
    # garbage. Entry sizing that's too small to matter is still too small to
    # trade. Clean cutoff.
    notional_usd = size * px
    # 2026-04-25: $50 floor → $10 floor.
    # The original $50 was protective padding when dust-sweep at $0.10 was killing
    # small positions in <0.2% moves. Dust-sweep was disabled 2026-04-22.
    # $50 is now blocking small-account testing on low-priced coins (MAV, kFLOKI,
    # HMSTR, BIGTIME, etc.) — at $560 equity × 0.5% risk × 10x lev = ~$28 notional
    # max for some coins. We need every trade we can get for data, not protective
    # filtering. $10 = HL's actual exchange minimum; below this orders bounce.
    MIN_NOTIONAL_USD = 10.0
    if notional_usd < MIN_NOTIONAL_USD:
        log(f"{coin} {'BUY' if is_buy else 'SELL'} SKIP: notional ${notional_usd:.2f} below ${MIN_NOTIONAL_USD:.0f} HL min (size={size}, px={px})")
        return None

    # Bybit-lead limit: capture HL lag using Bybit's current price
    side = 'BUY' if is_buy else 'SELL'
    edge = bybit_lead.compute_edge_price(coin, side, px)
    if edge:
        maker_px = round_price(coin, edge)
    else:
        maker_px = round_price(coin, px * (1 - MAKER_OFFSET) if is_buy else px * (1 + MAKER_OFFSET))
    try:
        r = exchange.order(coin, is_buy, size, maker_px, {'limit':{'tif':'Alo'}}, reduce_only=False, cloid=_cloid_obj)
        status = r.get('response',{}).get('data',{}).get('statuses',[{}])[0] if r else {}
        if 'error' in status:
            log(f"MAKER {coin} rejected: {status['error']} @ {maker_px}")
        elif 'resting' in status or 'filled' in status:
            log(f"MAKER {coin} {'BUY' if is_buy else 'SELL'} {size}@{maker_px}: {status}")
            oid = status.get('resting',{}).get('oid') or status.get('filled',{}).get('oid')
            if 'filled' in status: return maker_px

            # Poll for fill. Check by BOTH order status (preferred) AND position
            # presence (backup). Polling by oid is more precise and immune to
            # ambient positions from other fills.
            # FIXED 2026-04-22: previous logic polled for 'any position on this
            # coin' which was correct but led to a false-negative race — if
            # the maker filled AFTER the last poll but BEFORE the cancel call,
            # the cancel would succeed (cancelling a just-filled order on HL
            # returns 'success' but is a no-op if already filled), but we'd
            # have already decided no fill happened and fall through to TAKER,
            # creating a double-entry. Now we explicitly verify no fill after
            # cancel and re-check position state one last time.
            filled_via_poll = False
            for wait_s in range(MAKER_FALLBACK_SEC):
                time.sleep(1)
                try:
                    state_now = info.user_state(WALLET)
                    has_pos = any(p['position'].get('coin')==coin and float(p['position'].get('szi',0))!=0
                                  for p in state_now.get('assetPositions',[]))
                    if has_pos:
                        log(f"MAKER fill {coin} after {wait_s+1}s")
                        filled_via_poll = True
                        return maker_px
                except Exception:
                    pass  # transient HL error, keep polling

            # Poll window expired. Try to cancel the maker.
            try:
                exchange.cancel(coin, oid)
                log(f"MAKER unfilled {coin}, canceling oid={oid} -> TAKER fallback")
                # Sanity check: did the cancel race against a fill? Recheck.
                time.sleep(0.5)
                try:
                    state_now = info.user_state(WALLET)
                    has_pos = any(p['position'].get('coin')==coin and float(p['position'].get('szi',0))!=0
                                  for p in state_now.get('assetPositions',[]))
                    if has_pos:
                        log(f"MAKER {coin} filled during cancel race — returning maker_px={maker_px}, skipping TAKER")
                        return maker_px
                except Exception: pass
            except Exception as ce:
                # Cancel failed. Most common cause: order already filled. Verify.
                log(f"cancel err {coin}: {ce}")
                try:
                    time.sleep(0.3)
                    state_now = info.user_state(WALLET)
                    has_pos = any(p['position'].get('coin')==coin and float(p['position'].get('szi',0))!=0
                                  for p in state_now.get('assetPositions',[]))
                    if has_pos:
                        log(f"MAKER cancel-failed but position exists — treating as filled, maker_px={maker_px}")
                        return maker_px
                except Exception: pass
    except Exception as e:
        log(f"maker place err {coin}: {e}")

    # TAKER fallback (Ioc) — refresh price in case market moved
    px = get_mid(coin) or px
    slip_px = round_price(coin, px * (1.005 if is_buy else 0.995))
    try:
        r = exchange.order(coin, is_buy, size, slip_px, {'limit':{'tif':'Ioc'}}, reduce_only=False, cloid=_cloid_obj)
        status = r.get('response',{}).get('data',{}).get('statuses',[{}])[0] if r else {}
        if 'error' in status:
            log(f"TAKER {coin} rejected: {status['error']} @ {slip_px}"); return None
        log(f"TAKER {coin} {'BUY' if is_buy else 'SELL'} {size}@{slip_px}: {status}")
        return px
    except Exception as e:
        log(f"taker err {coin}: {e}"); return None

def cancel_trigger_orders(coin):
    """Cancel any native SL/TP trigger orders for a coin — prevents orphaned stops."""
    try:
        open_orders = info.open_orders(WALLET)
        cancelled_count = 0
        for o in open_orders:
            if o.get('coin') == coin:
                oid = o.get('oid')
                if oid:
                    exchange.cancel(coin, oid)
                    log(f"{coin} cancelled orphaned order {oid}")
                    cancelled_count += 1
        if cancelled_count and _INV_OK and _invariants is not None:
            try: _invariants.record_action(coin, 'cancel_trigger', origin='cancel_trigger_orders', detail={'count': cancelled_count})
            except Exception: pass
    except Exception as e:
        log(f"{coin} cancel triggers err: {e}")


def partial_close(coin, fraction, reason='partial_exit_tp1'):
    """Close a fraction of the current position via reduce-only IOC market.
    Existing TP/SL trigger orders auto-adjust to the reduced remaining size
    (HL honors the szi at trigger time).

    Args:
        coin: coin symbol
        fraction: 0-1 portion of position to close (0.5 = close half)
        reason: contract reason (must be in AUTHORIZED_REASONS)

    Returns:
        dict with {status, closed_size, remaining_size, pnl_pct} or None on failure
    """
    try:
        live = get_all_positions_live(force=True).get(coin)
        if not live: return None
        is_long = live['size'] > 0
        total_size = abs(live['size'])
        close_size = total_size * fraction
        close_size = float(round_size(coin, close_size))
        if close_size <= 0 or close_size >= total_size:
            log(f"{coin} partial_close skipped: invalid fraction (size={total_size}, close={close_size})")
            return None
        px = get_mid(coin)
        if not px: return None
        slip = float(round_price(coin, px * (0.995 if is_long else 1.005)))
        # Reduce-only IOC market close (is_buy=False for long, True for short)
        r = exchange.order(coin, not is_long, close_size, slip,
                           {'limit': {'tif': 'Ioc'}}, reduce_only=True)
        status = r.get('response',{}).get('data',{}).get('statuses',[{}])[0] if r else {}
        if 'error' in status:
            log(f"{coin} PARTIAL_CLOSE FAILED: {status['error']}")
            return None
        entry = live['entry']
        pct = ((px-entry)/entry*100) if is_long else ((entry-px)/entry*100)
        remaining = total_size - close_size
        log(f"{coin} PARTIAL_CLOSE {close_size}/{total_size} ({fraction*100:.0f}%) "
            f"@ {slip} | entry={entry} | {pct:+.2f}% | reason={reason} | remaining={remaining}")
        if _INV_OK and _invariants is not None:
            try: _invariants.record_action(coin, 'partial_close', size_before=total_size,
                                            size_after=remaining, origin=reason,
                                            detail={'close_size': close_size, 'fraction': fraction})
            except Exception: pass
        return {'status':'closed','closed_size': close_size,
                'remaining_size': remaining, 'pnl_pct': pct}
    except Exception as e:
        log(f"{coin} partial_close err: {e}")
        return None


def modify_sl_to_breakeven(coin, entry, size, is_long, buffer_pct=0.003):
    """Cancel existing SL and place new SL at breakeven + buffer (covers fees + slip).

    30bps buffer = ~12.5bps round-trip friction + ~17.5bps safety margin.
    Caller may override buffer_pct (e.g. partial closes pass new_sl_pct).

    Args:
        coin: coin symbol
        entry: original entry price
        size: remaining position size after partial close
        is_long: direction
        buffer_pct: how far above entry (for long) / below (for short) to set SL

    Returns:
        new_sl_price or None
    """
    try:
        # Cancel only SL trigger orders (leave TP intact)
        open_orders = info.open_orders(WALLET)
        for o in open_orders:
            if o.get('coin') != coin: continue
            ot = o.get('orderType', '')
            # Match SL trigger orders (Stop variants)
            if 'Stop' in ot or 'Sl' in ot:
                oid = o.get('oid')
                if oid:
                    try: exchange.cancel(coin, oid)
                    except Exception: pass
        # Place new SL at breakeven + buffer
        if is_long:
            trigger_px = entry * (1 + buffer_pct)
        else:
            trigger_px = entry * (1 - buffer_pct)
        trigger_px = float(round_price(coin, trigger_px))
        limit_px = float(round_price(coin, trigger_px * (0.98 if not is_long else 1.02)))
        sl_size = float(round_size(coin, size))
        sl_side = not is_long
        r = exchange.order(coin, sl_side, sl_size, limit_px,
                           {'trigger': {'triggerPx': trigger_px, 'isMarket': True, 'tpsl': 'sl'}},
                           reduce_only=True)
        status = r.get('response',{}).get('data',{}).get('statuses',[{}])[0] if r else {}
        if 'error' in status:
            log(f"{coin} BE-SL REJECTED: {status['error']}")
            return None
        log(f"{coin} SL → BREAKEVEN @ {trigger_px} (entry={entry}, buffer={buffer_pct*100:.2f}%)")
        return trigger_px
    except Exception as e:
        log(f"{coin} be-sl err: {e}")
        return None


def modify_tp_extended(coin, entry, size, is_long, new_tp_pct):
    """Cancel existing TP and place new TP at extended target.

    Args:
        coin: coin symbol
        entry: original entry
        size: current remaining size
        is_long: direction
        new_tp_pct: new TP as decimal (0.10 = 10%)
    """
    try:
        open_orders = info.open_orders(WALLET)
        for o in open_orders:
            if o.get('coin') != coin: continue
            ot = o.get('orderType', '')
            if 'Take' in ot or 'Tp' in ot:
                oid = o.get('oid')
                if oid:
                    try: exchange.cancel(coin, oid)
                    except Exception: pass
        if is_long:
            trigger_px = entry * (1 + new_tp_pct)
        else:
            trigger_px = entry * (1 - new_tp_pct)
        trigger_px = float(round_price(coin, trigger_px))
        limit_px = float(round_price(coin, trigger_px * (0.998 if is_long else 1.002)))
        tp_size = float(round_size(coin, size))
        tp_side = not is_long
        r = exchange.order(coin, tp_side, tp_size, limit_px,
                           {'trigger': {'triggerPx': trigger_px, 'isMarket': True, 'tpsl': 'tp'}},
                           reduce_only=True)
        status = r.get('response',{}).get('data',{}).get('statuses',[{}])[0] if r else {}
        if 'error' in status:
            log(f"{coin} EXTENDED-TP REJECTED: {status['error']}")
            return None
        log(f"{coin} TP EXTENDED @ {trigger_px} (new_tp_pct={new_tp_pct*100:.2f}%, entry={entry})")
        return trigger_px
    except Exception as e:
        log(f"{coin} extend-tp err: {e}")
        return None


def run_profit_management(state, live_positions):
    """Per-tick hook: check each open 15m position for TP1 partial exit or
    TP extension. Called from main loop.

    ONLY acts on positions tagged tf='15m'. HTF positions managed by their
    own engines.
    """
    if not _PM_OK or _pm is None: return
    for coin, lp in live_positions.items():
        try:
            pos_state = state.get('positions', {}).get(coin, {})
            if not pos_state: continue
            if pos_state.get('tf') and pos_state.get('tf') != '15m': continue
            entry = float(pos_state.get('entry', 0))
            tp_pct = pos_state.get('tp_pct')
            if not entry or not tp_pct: continue
            mark = get_mid(coin)
            if not mark: continue
            is_long = lp['size'] > 0

            # POLICY 1: partial exit at TP1
            partial = _pm.check_partial_exit_tp1(coin, pos_state, mark)
            if partial.get('execute'):
                log(f"{coin} TP1 partial trigger: {partial['reason']}")
                result = partial_close(coin, partial['close_fraction'], reason='partial_exit_tp1')
                if result:
                    # Move remainder's SL to breakeven
                    remaining = result['remaining_size']
                    be_price = modify_sl_to_breakeven(coin, entry, remaining, is_long,
                                                       buffer_pct=partial['new_sl_pct'])
                    _pm.mark_partial_done(coin)
                    pos_state['partial_done'] = True
                    pos_state['sl_pct'] = partial['new_sl_pct']  # update state
                    log(f"{coin} TP1 policy complete: 50% booked, remainder at BE+{partial['new_sl_pct']*100:.2f}%")
                continue  # only one policy per tick per coin

            # POLICY 2: extend winners (only if partial already done OR position
            # is cleanly in profit and trend-aligned)
            # Requires 1h + 4h bias. Cache via HL REST.
            import urllib.request as _ureq
            try:
                # 1h bias
                _c1h = _CANDLE_CACHE.get(f'htf_1h_{coin}')
                if not _c1h or time.time() - _c1h['ts'] > 600:
                    _hl_throttle()
                    _data_1h = okx_fetch.fetch_klines(coin, '1h', 30)
                    _CANDLE_CACHE[f'htf_1h_{coin}'] = {'data': _data_1h, 'ts': time.time()}
                    _c1h = _CANDLE_CACHE[f'htf_1h_{coin}']
                # 4h bias
                _c4h = _CANDLE_CACHE.get(f'htf_4h_{coin}')
                if not _c4h or time.time() - _c4h['ts'] > 600:
                    _hl_throttle()
                    _data_4h = okx_fetch.fetch_klines(coin, '4h', 30)
                    _CANDLE_CACHE[f'htf_4h_{coin}'] = {'data': _data_4h, 'ts': time.time()}
                    _c4h = _CANDLE_CACHE[f'htf_4h_{coin}']
            except Exception:
                continue

            def _to_list(bars):
                return [[b['t'],b['o'],b['h'],b['l'],b['c'],b['v']] for b in bars]

            htf_1h_data = _c1h['data']
            htf_4h_data = _c4h['data']
            if not htf_1h_data or not htf_4h_data: continue

            bias_1h = _tf_iso.derive_htf_bias(_to_list(htf_1h_data)) if _TFI_OK else None
            bias_4h = _tf_iso.derive_htf_bias(_to_list(htf_4h_data)) if _TFI_OK else None
            if not bias_1h or not bias_4h: continue

            extend = _pm.check_extend_winner(
                coin, pos_state, mark,
                htf_1h_bias=bias_1h['bias'],
                htf_4h_bias=bias_4h['bias'],
                htf_4h_strength=bias_4h['strength'])
            if extend.get('extend'):
                log(f"{coin} EXTEND-WINNER trigger: {extend['reason']}")
                size_abs = abs(lp['size'])
                new_tp_price = modify_tp_extended(coin, entry, size_abs, is_long,
                                                    extend['new_tp_pct'])
                if new_tp_price:
                    pos_state['tp_pct'] = extend['new_tp_pct']
                    pos_state['tp_extended'] = True
                    log(f"{coin} TP extended: {extend['original_tp_pct']*100:.1f}% → "
                        f"{extend['new_tp_pct']*100:.1f}%")
        except Exception as e:
            log(f"profit_mgmt err {coin}: {e}")


def close(coin, state_ref=None, reason=None):
    """Close position. Behavior depends on RECONCILER_AUTHORITATIVE env.

    OBSERVE mode (default, RECONCILER_AUTHORITATIVE=0):
        Legacy behavior — directly market-closes on exchange, returns pnl_pct.
        Reconciler observes but doesn't execute.

    AUTHORITATIVE mode (RECONCILER_AUTHORITATIVE=1):
        Becomes a shim. Emits FORCE_CLOSE intent for the reconciler to resolve.
        Returns None. All callers must treat close() as fire-and-forget.
        Reconciler is the sole writer (uses _close_direct() to bypass shim).
    """
    # SL state cleanup — when position closes, no SL needed; clear tracking
    if _SL_STATE_OK and _sl_state_tracker is not None:
        try:
            _sl_state_tracker.cleanup(coin, reason or 'close')
        except Exception:
            pass
    # STEP 3: shim routing
    if os.environ.get('RECONCILER_AUTHORITATIVE', '0') == '1':
        if _INTENTS_OK and _intents is not None:
            # CRITICAL: read trade_id from ledger, NOT state. The state dict
            # can hold a stale trade_id from a prior closed ledger entry —
            # in that case reconciler hits is_closed() and skips idempotently,
            # leaving the exchange position bleeding indefinitely. Ledger is
            # the single source of truth for "what trade_id is currently open
            # for this coin." Mirrors the fix in admin close_coin (3c87643).
            tid = None
            if _LEDGER_OK and _ledger is not None:
                try:
                    tid = _ledger.latest_open_trade_id_for_coin(coin)
                except Exception:
                    tid = None
            if not tid:
                # Fall back to state if ledger has nothing (e.g. ledger module down)
                tid = (state.get('positions', {}).get(coin) or {}).get('trade_id')
            _intents.emit('FORCE_CLOSE', coin, trade_id=tid,
                          reason=reason or 'legacy_close_shim')
        # DO NOT execute — reconciler handles it.
        return None
    return _close_direct(coin, state_ref)


def _close_direct(coin, state_ref=None):
    """Direct market-close with escalating slip + fill verification.

    Escalation: 0.5% -> 1.5% -> 3.0% slip (IOC each). First attempt is tight
    for clean fills; wider attempts handle fast moves where tight IOC gets
    skipped by the matching engine.

    Returns pct (positive on success) or None on complete failure.

    CRITICAL: only callers are (a) close() in observe mode, (b) reconciler's
    execute_close_fn in authoritative mode. Strategy/process logic must NEVER
    call this — use close() which routes through the intent queue.
    """
    live = get_all_positions_live(force=True).get(coin)
    if not live: return None
    is_buy = live['size'] < 0   # close direction = opposite of position
    size = abs(live['size'])
    entry = live['entry']
    size = round_size(coin, size)
    if size <= 0:
        log(f"CLOSE {coin} SKIP: size rounded to 0")
        return None

    # Escalating slip tiers
    SLIP_TIERS = [0.005, 0.015, 0.030]  # 0.5%, 1.5%, 3.0%
    for attempt, slip_pct in enumerate(SLIP_TIERS, 1):
        px = get_mid(coin)
        if not px:
            log(f"CLOSE {coin} no mid px on attempt {attempt}")
            continue
        slip = round_price(coin, px * (1 + slip_pct) if is_buy else px * (1 - slip_pct))
        try:
            r = exchange.order(coin, is_buy, size, slip,
                               {'limit':{'tif':'Ioc'}}, reduce_only=True)
            status = r.get('response',{}).get('data',{}).get('statuses',[{}])[0] if r else {}

            if 'error' in status:
                log(f"CLOSE {coin} attempt {attempt}/{len(SLIP_TIERS)} @ slip={slip_pct*100:.1f}% "
                    f"rejected: {status['error']}")
                continue  # try next tier

            # Verify fill actually happened (not just 'resting' or 'canceled')
            if 'filled' not in status:
                # IOC was accepted but didn't match. Try wider.
                log(f"CLOSE {coin} attempt {attempt}/{len(SLIP_TIERS)} @ slip={slip_pct*100:.1f}% "
                    f"no fill (status={status})")
                continue

            # Success
            fill_info = status['filled']
            # 2026-04-25 (final): EXIT PRICE COMES FROM EXCHANGE FILL ONLY.
            # Prior code: fill_px = float(fill_info.get('avgPx', px))  ← FALLED BACK to limit price.
            # That caused fake exit prices (and worst case $0.01 closes) when
            # the exchange ack arrived without avgPx populated.
            # Now: if avgPx is missing/zero/invalid, DO NOT close the trade
            # in the ledger — log the corruption and let the reconciler retry.
            avg_raw = fill_info.get('avgPx')
            try:
                fill_px = float(avg_raw) if avg_raw is not None else 0.0
            except (TypeError, ValueError):
                fill_px = 0.0
            if fill_px <= 0:
                log(f"CLOSE {coin} attempt {attempt}/{len(SLIP_TIERS)} INVALID FILL PRICE "
                    f"avgPx={avg_raw!r} — refusing to record fake exit, will retry tier")
                continue  # do not return; try next slip tier
            pct = ((fill_px - entry) / entry * 100) if live['size'] > 0 \
                  else ((entry - fill_px) / entry * 100)
            pnl_usd = live['pnl']
            log(f"CLOSE {coin} FILLED attempt {attempt}/{len(SLIP_TIERS)} @ slip={slip_pct*100:.1f}% "
                f"size={size} px={fill_px} | entry={entry} | {pct:+.2f}% | ${pnl_usd:+.3f}")
            log_trade('HL', coin, 'CLOSE', fill_px, pnl_usd, 'close')
            cancel_trigger_orders(coin)
            # Stash real fill for reconciler to read (no get_mid inference)
            try:
                _LAST_CLOSE_FILL[coin] = {
                    'fill_px': fill_px, 'pnl_usd': pnl_usd, 'pct': pct,
                    'ts': time.time(),
                }
            except Exception:
                pass
            try:
                import monitor
                monitor.record_close(coin, pct/100, pnl_usd, 0, 'close')
            except Exception: pass
            return pct
        except Exception as e:
            log(f"CLOSE {coin} attempt {attempt}/{len(SLIP_TIERS)} err: {e}")
            continue

    # All tiers exhausted
    log(f"CLOSE {coin} FAILED: all {len(SLIP_TIERS)} slip tiers exhausted "
        f"(0.5%/1.5%/3%) — position remains open, reconciler will retry")
    return None

# ═══════════════════════════════════════════════════════
# STEP 1 — close_trade() defined, NOT YET WIRED.
# Reconciler (Step 2-3) will be the sole caller. Nothing calls this yet.
# Purpose: single-writer authority for trade close events.
# ═══════════════════════════════════════════════════════
def close_trade(trade_id, close_reason, exit_price=None, exchange_fill_id=None,
                pnl=None, source='reconcile', close_size=None):
    """Single-writer close. Idempotent. Returns True on recorded close, False on duplicate.

    Contract: THIS FUNCTION WILL EVENTUALLY BE THE ONLY WRITER OF CLOSE STATE.
    All other close paths must route through here via the reconciler.

    PnL computation:
      - If `pnl` is supplied, used as-is (USD).
      - Else if `close_size` and `exit_price` and entry_price are all known,
        pnl = (exit - entry) * size_with_sign  (USD, signed by side).
      - Else falls back to position_ledger.get_size() lookup.
      - Last resort: legacy price-delta approximation (small but nonzero).
    """
    if not _LEDGER_OK or _ledger is None:
        log(f'[close_trade] LEDGER UNAVAILABLE trade_id={trade_id}')
        return False

    trade = _ledger.get_by_trade_id(trade_id)
    if not trade:
        log(f'[close_trade] unknown trade_id={trade_id} reason={close_reason}')
        return False

    if trade.get('event_type') == 'CLOSE':
        return False  # already closed — idempotent

    coin = trade.get('coin', '')
    try:
        entry_price_f = float(trade.get('entry_price') or 0)
    except (TypeError, ValueError):
        entry_price_f = 0
    side = trade.get('side', '')

    # Compute PnL if not supplied — prefer USD via size, fall back to price-delta
    if pnl is None and exit_price is not None and entry_price_f > 0:
        try:
            exit_f = float(exit_price)
            # Resolve size: explicit param > position_ledger lookup > None
            size_f = None
            if close_size is not None:
                try: size_f = float(close_size)
                except Exception: pass
            if size_f is None:
                try:
                    import position_ledger as _pl
                    size_f = abs(float(_pl.get_size(coin) or 0))
                except Exception: pass
            if size_f and size_f > 0:
                # USD pnl: (exit - entry) * size, signed by side
                price_delta = exit_f - entry_price_f
                if side in ('SELL', 'S'):
                    price_delta = -price_delta
                pnl = round(price_delta * size_f, 4)
            else:
                # Fallback: legacy price-delta approximation (logging only — not USD)
                move_pct = (exit_f - entry_price_f) / entry_price_f
                if side in ('SELL', 'S'):
                    move_pct = -move_pct
                pnl = round(move_pct * entry_price_f, 4)
        except Exception:
            pnl = 0

    # Funding accrual: signed cost on notional from entry_ts to now.
    # Best-effort — doesn't block close on failure.
    _funding_pct = _funding_for_close(trade)

    # MFE/MAE: pulled from state['positions'][coin] which the profit_lock
    # loop updates every tick. Signed fractions of entry price.
    _mfe = None; _mae = None
    try:
        _live_pos = state.get('positions', {}).get(coin, {})
        _mfe = _live_pos.get('mfe_pct')
        _mae = _live_pos.get('mae_pct')
    except Exception:
        pass

    ok = _ledger.append_close(
        trade_id=trade_id,
        exit_price=exit_price,
        pnl=pnl,
        close_reason=close_reason,
        exchange_fill_id=exchange_fill_id,
        source=source,
        funding_paid_pct=_funding_pct,
        mfe_pct=_mfe,
        mae_pct=_mae,
    )
    if not ok:
        return False

    # Update derivative state — this is where risk_ladder/coin_wr get wired
    try:
        if coin and coin in state.get('positions', {}):
            state['positions'].pop(coin, None)
        if pnl is not None:
            state['last_pnl_close'] = pnl
            if close_reason not in ('timeout', 'max_hold', 'reconcile_missing'):
                won = pnl > 0
                try: update_coin_wr(coin, won, state)
                except Exception as _e: log(f'[close_trade] coin_wr err: {_e}')
                try: risk_ladder.record_trade(won)
                except Exception as _e: log(f'[close_trade] risk_ladder err: {_e}')
                if not won:
                    state['consec_losses'] = state.get('consec_losses', 0) + 1
                else:
                    state['consec_losses'] = 0
    except Exception as _e:
        log(f'[close_trade] state update err: {_e}')

    log(f'[close_trade] {coin} trade_id={trade_id[:8]} reason={close_reason} '
        f'pnl={pnl} exit={exit_price}')
    return True

def flatten_all(reason='KILL'):
    live = get_all_positions_live()
    log(f"FLATTEN ALL ({reason}): {len(live)} positions")
    for coin in live:
        close(coin)
        time.sleep(0.3)


# ═══════════════════════════════════════════════════════
# ENFORCE PROTECTION — adapter helpers for enforce_protection.py
# ═══════════════════════════════════════════════════════

def _ep_fetch_size(coin):
    """Fetch authoritative position size.

    2026-04-25: now uses cached state when fresh (≤2s) to avoid 429 cascade
    during rapid SL/TP placement retries. Direct user_state() call only when
    cache is genuinely stale or missing. Was: every call hits HL → DYM-style
    429 storm during retry loops.

    2026-04-25 (event-sourced): when USE_LEDGER_FOR_SIZE=1 AND WS feed is
    fresh (<30s), reads from position_ledger — zero REST cost. This kills
    the residual 429 source on the enforce_protection size-fetch path
    (15+ size fetches per enforcement during settlement loop).
    """
    # ─── PATH 0: ledger (event-sourced) ─────────────────────
    # 2026-04-25: ledger fast path. Returns None ONLY when ledger has the
    # row but state is not LIVE — but immediately after a fresh entry,
    # the row may not yet exist (webData2 hasn't ticked) OR may still be
    # PENDING_ENTRY. In those cases we MUST fall through to REST so the
    # caller doesn't conclude "no position" on a freshly-opened trade.
    # Only return early if ledger has authoritative non-None size.
    if USE_LEDGER_FOR_SIZE and position_ledger.ws_is_fresh(max_age_sec=30):
        _led_sz = position_ledger.get_size(coin)
        if _led_sz is not None:
            return _led_sz
        # else: ledger doesn't have a LIVE row yet — fall through to REST

    try:
        # Prefer cached state if fresh — protects against retry-storm 429s
        try:
            cache_ts = _cache.get('state_ts', 0)
            if time.time() - cache_ts < 2.0:  # fresh enough
                cached = _cache.get('state')
                if cached:
                    for ap in cached.get('assetPositions', []):
                        p = ap.get('position', {})
                        if p.get('coin', '').upper() == coin.upper():
                            szi = float(p.get('szi', 0))
                            if abs(szi) > 1e-12:
                                return abs(szi)
                    # Not in cache → position not held (don't fall through)
                    return None
        except Exception:
            pass
        # Cache stale or unavailable — direct call (with throttle)
        try:
            _hl_throttle()
        except Exception:
            pass
        us = info.user_state(WALLET)
        # Refresh cache while we're here
        try:
            _cache['state'] = us
            _cache['state_ts'] = time.time()
        except Exception:
            pass
        for ap in us.get('assetPositions', []):
            p = ap.get('position', {})
            if p.get('coin', '').upper() == coin.upper():
                szi = float(p.get('szi', 0))
                if abs(szi) > 1e-12:
                    return abs(szi)
        return None
    except Exception as e:
        log(f'[ep_fetch_size] {coin} err: {e}')
        return None


def _ep_fetch_orders(coin):
    """Fetch open trigger orders for coin. Uses frontend_open_orders which
    returns a richer payload including 'isTrigger', 'triggerPx', 'tpsl'."""
    try:
        fo = _cached_frontend_orders() or []
        out = []
        for o in fo:
            if o.get('coin', '').upper() != coin.upper():
                continue
            # Trigger orders have isTrigger=True, and tpsl in {'tp','sl',None}
            if not o.get('isTrigger'):
                continue
            tpsl = o.get('tpsl') or ''
            if tpsl.lower() not in ('tp', 'sl'):
                continue
            out.append({
                'oid': o.get('oid'),
                'sz': float(o.get('sz', 0) or 0),
                'tpsl': tpsl,
                'triggerPx': float(o.get('triggerPx', 0) or 0),
                'side': o.get('side'),
            })
        return out
    except Exception as e:
        log(f'[ep_fetch_orders] {coin} err: {e}')
        return []


def _ep_cancel_order(coin, oid):
    """Cancel a specific order by oid."""
    try:
        r = exchange.cancel(coin, oid)
        # Invalidate cache so fetch_orders returns fresh state
        try:
            _cache['fo'] = None
            _cache['fo_ts'] = 0
        except Exception:
            pass
        return True
    except Exception as e:
        log(f'[ep_cancel] {coin} oid={oid} err: {e}')
        return False


# 2026-04-25 (audit decoupling): the enforce_position_protection function fires
# 20-25 REST calls per invocation (15 settlement polls + 3 order fetches +
# 2 cancels + 2 places + 3 verify fetches). It's called after every entry AND
# every 15s by lifecycle_reconciler. The audit→enforce→audit feedback loop
# was the actual amplifier on CloudFront 429s.
#
# Surgical fix: per-coin cooldown. If we enforced this coin within the last
# N seconds, return the cached result instead of re-running the full audit.
# Reconciler hits cooldown → no-op. Real entries always run (different coin
# OR cooldown expired). Loop dies. Latency unchanged on first enforcement.
_ENFORCE_COOLDOWN_SEC = float(os.environ.get('ENFORCE_COOLDOWN_SEC', '8.0'))
_LAST_ENFORCE = {}  # coin -> (ts, result_dict)
_ENFORCE_TS_LOCK = threading.Lock()
_ENFORCE_STATS = {'invocations': 0, 'cache_hits': 0, 'fresh_runs': 0}


def enforce_position_protection(coin, is_long, entry_px, origin='unknown'):
    """Single call to protect a position: idempotent, atomic, authoritative.

    Phase 1 contract:
    - SL verified ≤ 5s or emergency close
    - TP verified ≤ 15s or repair (no close)
    - Cloid idempotency
    - Coin halted 5min after critical execution failure

    2026-04-25: per-coin cooldown wrapper. If enforced within last N sec,
    return cached result. Breaks audit→enforce→audit→enforce feedback loop.
    """
    _ENFORCE_STATS['invocations'] += 1

    # ─── Per-coin cooldown gate ──────────────────────────────────────
    with _ENFORCE_TS_LOCK:
        last = _LAST_ENFORCE.get(coin)
        if last is not None:
            age = time.time() - last[0]
            if age < _ENFORCE_COOLDOWN_SEC:
                _ENFORCE_STATS['cache_hits'] += 1
                # Return shallow copy with annotation so callers can detect
                cached = dict(last[1])
                cached['from_cache'] = True
                cached['cache_age_sec'] = round(age, 2)
                return cached

    _ENFORCE_STATS['fresh_runs'] += 1
    result = _enforce_position_protection_impl(coin, is_long, entry_px, origin)

    # Cache only successful results — failures should be retried on next call
    if result.get('success'):
        with _ENFORCE_TS_LOCK:
            _LAST_ENFORCE[coin] = (time.time(), dict(result))

    return result


def _enforce_position_protection_impl(coin, is_long, entry_px, origin='unknown'):
    if not _EP_OK or _ep is None:
        # Fallback: legacy pattern
        sl_pct = place_native_sl(coin, is_long, entry_px, _ep_fetch_size(coin) or 0)
        tp_pct = place_native_tp(coin, is_long, entry_px, _ep_fetch_size(coin) or 0)
        return {
            'success': (sl_pct is not None and tp_pct is not None),
            'tp_pct': tp_pct, 'sl_pct': sl_pct,
            'replaced': False, 'tp_placed': tp_pct is not None,
            'sl_placed': sl_pct is not None, 'actual_size': None,
            'reason': 'legacy_fallback' if not _EP_OK else None,
            'emergency_closed': False, 'coin_halted': False,
        }
    # Wire emergency_close to precog's close() which is the authorized path
    def _emergency_close_fn(c, reason):
        try:
            log(f"{c} EMERGENCY CLOSE triggered by enforce_protection: {reason}")
            # Use authorized path through contract if available
            if _EC_OK and _contract is not None:
                _contract.authorize_close(c, f'enforce_{reason}', ttl_sec=30)
            close(c)
            state.get('positions', {}).pop(c, None)
            return True
        except Exception as _ece:
            log(f"{c} emergency close err: {_ece}")
            return False

    return _ep.enforce_protection(
        coin=coin,
        is_long=is_long,
        entry_px=entry_px,
        fetch_size_fn=_ep_fetch_size,
        fetch_orders_fn=_ep_fetch_orders,
        cancel_order_fn=_ep_cancel_order,
        place_tp_fn=place_native_tp,
        place_sl_fn=place_native_sl,
        log_fn=log,
        emergency_close_fn=_emergency_close_fn,
        origin=origin,
    )


def is_coin_execution_halted(coin):
    """Check if coin is halted from new entries due to critical execution failure."""
    if not _EP_OK or _ep is None:
        return False
    try: return _ep.is_coin_halted(coin)
    except Exception: return False


# ═══════════════════════════════════════════════════════
# PROCESS — one coin per tick
# ═══════════════════════════════════════════════════════
def place_native_sl(coin, is_long, entry, size):
    """Place HL native stop-loss order — executes server-side, no tick delay.
    Uses per-coin SL from percoin_configs (OOS-tuned), then postmortem tuner overrides,
    else global fallback. Returns the sl_pct that was used (for pos-dict enrichment).

    CRITICAL: we do NOT trust the caller's `size` param. A reduce_only SL sized
    larger than the actual HL position is rejected. Between place() returning and
    this call firing, partial fills and settlement delays mean the true position
    size can differ from the requested `size`. We query HL for the authoritative
    current position size and use that instead. If HL has no position yet, we
    retry for up to 3 seconds before giving up (settlement delay on taker fills).
    """
    try:
        # Per-coin SL from OOS tuning (5% default — validated WR)
        sl_pct = STOP_LOSS_PCT  # global fallback 2%
        try:
            if percoin_configs.ELITE_MODE and percoin_configs.is_elite(coin):
                cfg = percoin_configs.get_config(coin)
                if cfg and 'SL' in cfg:
                    sl_pct = cfg['SL']  # OOS-validated per-coin SL
        except Exception: pass
        sl_pct = _apply_sl_cap(sl_pct)
        # TUNER OVERRIDE REMOVED 2026-04-22. See place_native_tp for rationale.
        # SL bounds in postmortem/bounds.py allow 0.3%-5% drift from closures,
        # which the tuner was using to tighten stops down below swing structure.
        # SL is policy (derived from config + swing structure), not a learnable
        # param. Operational closes don't indicate whether the stop was correct.

        # AUTHORITATIVE POSITION SIZE: query HL, don't trust caller.
        # Retry up to 3s for settlement delay on taker fills.
        # CRITICAL 2026-04-22: bypass _cached_user_state here. The cache has
        # a 5-sec TTL, but this function runs ~500ms after exchange.order fill,
        # when the new position is NOT yet in the cached response. Using the
        # cached value would return "no position found" for EVERY fresh fill,
        # causing the bot to open positions without SL attachment — exactly the
        # 'naked position' bug we've been chasing all session. Direct HL read
        # is required here; we only pay the /info cost on actual fills, not
        # every gate check. Fallback path keeps cache for tuner bounds etc.
        actual_size = None
        for attempt in range(6):
            try:
                us = info.user_state(WALLET)  # DIRECT — no cache
                for ap in us.get('assetPositions', []):
                    p = ap.get('position', {})
                    if p.get('coin','').upper() == coin.upper():
                        szi = float(p.get('szi', 0))
                        if abs(szi) > 1e-12:
                            actual_size = abs(szi)
                            # Also refresh the shared cache while we're here
                            _cache['state'] = us
                            _cache['state_ts'] = time.time()
                            break
                if actual_size is not None:
                    break
            except Exception:
                pass
            time.sleep(0.5)
        if actual_size is None:
            log(f"{coin} NATIVE SL ABORT: no position found after 3s retry (size requested={size})")
            return None
        if abs(actual_size - abs(float(size))) > 1e-9:
            log(f"{coin} NATIVE SL size correction: caller said {size}, HL shows {actual_size} (partial fill or pre-existing)")

        entry = float(entry)
        trigger_px = entry * (1 - sl_pct) if is_long else entry * (1 + sl_pct)
        trigger_px = float(round_price(coin, trigger_px))
        # Limit price: aggressive to ensure fill (2% past trigger for slippage room)
        limit_px = float(round_price(coin, trigger_px * (0.98 if not is_long else 1.02)))
        sl_size = float(round_size(coin, actual_size))
        sl_side = not is_long
        # 2026-04-22: retry on 429/transient errors. Previously a single HL 429
        # at SL placement left positions naked indefinitely. Protect_all would
        # try again later but the window between fill and SL attachment is
        # where flash crashes wipe accounts. Inline retry keeps positions safe.
        # Deterministic cloid for HL-level idempotency: retries with same
        # (coin, side, purpose, size) get same cloid → HL dedups.
        _sl_cloid = None
        if _EP_OK and _ep is not None:
            try: _sl_cloid = _ep.cloid_for(coin, 'SHORT' if not is_long else 'LONG', 'sl', sl_size)
            except Exception: pass
        r = None
        last_err = None
        for attempt in range(4):
            try:
                order_kwargs = {
                    'reduce_only': True,
                }
                if _sl_cloid:
                    order_kwargs['cloid'] = _sl_cloid
                r = exchange.order(coin, sl_side, sl_size, limit_px,
                               {"trigger": {"triggerPx": trigger_px, "isMarket": True, "tpsl": "sl"}},
                               **order_kwargs)
                # success — break out
                break
            except Exception as _e:
                last_err = _e
                err_str = str(_e).lower()
                # Cloid collision = HL already has this order → treat as success
                if 'cloid' in err_str or 'duplicate' in err_str:
                    log(f"{coin} SL cloid dedup'd by HL (idempotency worked): {_e}")
                    r = {'response': {'data': {'statuses': [{'resting': {'oid': 'dedup'}}]}}}
                    break
                if '429' in err_str or 'rate' in err_str or 'timeout' in err_str or 'connection' in err_str:
                    # Exponential backoff: 0.5s, 1.5s, 3.5s
                    backoff = 0.5 * (3 ** attempt)
                    log(f"{coin} SL attempt {attempt+1} hit transient err ({_e}) — retry in {backoff:.1f}s")
                    time.sleep(backoff)
                else:
                    # Non-transient — don't retry
                    log(f"{coin} SL attempt {attempt+1} non-transient err: {_e}")
                    break
        if r is None:
            log(f"{coin} NATIVE SL FAILED after 4 attempts, last err: {last_err}")
            return None
        status = r.get('response',{}).get('data',{}).get('statuses',[{}])[0] if r else {}
        if 'error' in status:
            log(f"{coin} NATIVE SL REJECTED: {status['error']} (size={sl_size}, trigger={trigger_px})")
            return None
        else:
            log(f"{coin} NATIVE SL placed @ {trigger_px} (sl_pct={sl_pct*100:.1f}%, size={sl_size})")
            # Invalidate frontend_orders cache so the verify loop's first poll
            # sees the just-placed SL rather than a stale snapshot.
            try:
                _cache['fo'] = None
                _cache['fo_ts'] = 0
            except Exception:
                pass
        return sl_pct
    except Exception as e:
        log(f"{coin} native SL err: {e}")
        return None

def _shadow_record_rejection(coin, side, reason, meta=None):
    """Helper: resolve TP/SL pct + current price, then record rejection to shadow.

    Also calls log_signal() so the dashboard signal feed shows filtered signals
    (not just trades that fired). Silently no-ops on any failure. Non-blocking.
    """
    # Always log to signal feed first (cheap), even if shadow fails
    try:
        log_signal(coin, f'REJECT:{reason}', side)
    except Exception:
        pass

    if not _SR_OK or _shadow_rej is None:
        return
    try:
        # Pull TP/SL config (fallback for non-elite)
        tp_pct = 0.05; sl_pct = 0.025
        try:
            if percoin_configs.ELITE_MODE and percoin_configs.is_elite(coin):
                cfg = percoin_configs.get_config(coin)
                if cfg:
                    tp_pct = cfg.get('TP', tp_pct)
                    sl_pct = cfg.get('SL', sl_pct)
        except Exception:
            pass
        px = get_mid(coin)
        if not px or px <= 0:
            return
        _shadow_rej.record_rejection(
            coin=coin, side=side, entry_price=px,
            tp_pct=tp_pct, sl_pct=sl_pct,
            reason=reason, meta=meta or {},
        )
    except Exception:
        pass


def place_native_tp(coin, is_long, entry, size):
    """Place HL native take-profit order. Uses per-coin config if elite,
    fallback 5% TP otherwise. Contract enforcement: EVERY position gets a TP.
    Returns the tp_pct that was used, or None if placement failed."""
    try:
        cfg = None
        if percoin_configs.ELITE_MODE and percoin_configs.is_elite(coin):
            cfg = percoin_configs.get_config(coin)
        if not cfg or 'TP' not in cfg:
            # Contract fallback — non-elite coins also get TP protection
            if _EC_OK and _contract is not None:
                cfg = _contract.get_fallback_config(coin)
                log(f"{coin} using contract fallback TP={cfg['TP']*100:.1f}%")
            else:
                return None
        tp_pct = cfg['TP']
        # TUNER OVERRIDE REMOVED 2026-04-22. The postmortem tuner's bounds
        # (bounds.py line 103: tp.pct bounded 0.004-0.20) allow it to drift
        # TP all the way down to 0.4%. In practice it was writing ~2.6% TP
        # overrides based on recent operational closes, which overrode the
        # swing-safe config (6-8% from percoin_configs). Every position in
        # the current session (13 of 13) had TP at 2.6% because of this
        # layer. TP is policy, not a learnable parameter from trade-close
        # telemetry — a loss doesn't prove the TP was too aggressive, it
        # proves the entry was wrong. Let the config/base_tier decide TP.
        # Same change applied to place_native_sl.
        # TP cap (optional, env MAX_TP_PCT). Default 0 = no cap.
        if MAX_TP_PCT > 0 and tp_pct > MAX_TP_PCT:
            log(f"{coin} TP capped: cfg={tp_pct*100:.1f}% → {MAX_TP_PCT*100:.1f}%")
            tp_pct = MAX_TP_PCT
        # AUTHORITATIVE POSITION SIZE: same logic as place_native_sl.
        # Partial fills + settlement delay mean `size` from calc_size can
        # differ from what HL actually has. reduce_only with wrong qty = reject.
        # CRITICAL 2026-04-22: bypass _cached_user_state — cache staleness was
        # blocking TP placement on fresh fills (root cause of naked positions).
        actual_size = None
        for attempt in range(6):
            try:
                us = info.user_state(WALLET)  # DIRECT — no cache
                for ap in us.get('assetPositions', []):
                    p = ap.get('position', {})
                    if p.get('coin','').upper() == coin.upper():
                        szi = float(p.get('szi', 0))
                        if abs(szi) > 1e-12:
                            actual_size = abs(szi)
                            _cache['state'] = us
                            _cache['state_ts'] = time.time()
                            break
                if actual_size is not None:
                    break
            except Exception:
                pass
            time.sleep(0.5)
        if actual_size is None:
            log(f"{coin} NATIVE TP ABORT: no position found after 3s retry")
            return None

        entry = float(entry)
        trigger_px = entry * (1 + tp_pct) if is_long else entry * (1 - tp_pct)
        trigger_px = float(round_price(coin, trigger_px))
        # Limit: slightly worse to ensure fill
        limit_px = float(round_price(coin, trigger_px * (0.998 if is_long else 1.002)))
        tp_size = float(round_size(coin, actual_size))
        tp_side = not is_long
        # Deterministic cloid for HL-level idempotency
        _tp_cloid = None
        if _EP_OK and _ep is not None:
            try: _tp_cloid = _ep.cloid_for(coin, 'SHORT' if not is_long else 'LONG', 'tp', tp_size)
            except Exception: pass
        # 2026-04-22: retry on transient errors, same as SL path
        r = None
        last_err = None
        for attempt in range(4):
            try:
                order_kwargs = {'reduce_only': True}
                if _tp_cloid: order_kwargs['cloid'] = _tp_cloid
                r = exchange.order(coin, tp_side, tp_size, limit_px,
                               {"trigger": {"triggerPx": trigger_px, "isMarket": True, "tpsl": "tp"}},
                               **order_kwargs)
                break
            except Exception as _e:
                last_err = _e
                err_str = str(_e).lower()
                if 'cloid' in err_str or 'duplicate' in err_str:
                    log(f"{coin} TP cloid dedup'd by HL (idempotency worked): {_e}")
                    r = {'response': {'data': {'statuses': [{'resting': {'oid': 'dedup'}}]}}}
                    break
                if '429' in err_str or 'rate' in err_str or 'timeout' in err_str or 'connection' in err_str:
                    backoff = 0.5 * (3 ** attempt)
                    log(f"{coin} TP attempt {attempt+1} hit transient err ({_e}) — retry in {backoff:.1f}s")
                    time.sleep(backoff)
                else:
                    log(f"{coin} TP attempt {attempt+1} non-transient err: {_e}")
                    break
        if r is None:
            log(f"{coin} NATIVE TP FAILED after 4 attempts, last err: {last_err}")
            return None
        status = r.get('response',{}).get('data',{}).get('statuses',[{}])[0] if r else {}
        if 'error' in status:
            log(f"{coin} NATIVE TP REJECTED: {status['error']} (size={tp_size}, trigger={trigger_px})")
            return None
        else:
            log(f"{coin} NATIVE TP placed @ {trigger_px} (tp_pct={tp_pct*100:.2f}%, size={tp_size})")
            # Invalidate frontend_orders cache so the verify loop's first poll
            # sees the just-placed TP rather than a 3s-stale snapshot from
            # before the placement.
            try:
                _cache['fo'] = None
                _cache['fo_ts'] = 0
            except Exception:
                pass
        return tp_pct
    except Exception as e:
        log(f"{coin} native TP err: {e}")
        return None

def process(coin, state, equity, live_positions, risk_mult=1.0):
    global _LAST_OPEN_TS
    if coin_disabled(coin, state): return

    # STEP 3: reconciler halt flag — block new entries if drift is unsafe
    # 2026-04-25: SAFE MODE — drift≥5% no longer hard-blocks entries.
    # Diagnosis: during candle bucket rebuild windows, ledger/exchange snapshots
    # can briefly diverge (ingestion artifact, not real divergence). Hard HALT
    # was reacting to data-pacing noise, blocking valid trades. Now: warn +
    # size×0.5 instead of full block. Real persistent drift (3+ consecutive
    # cycles) still triggers entry suppression via entry_limiter().
    if _RECONCILER_OK and _reconciler is not None:
        try:
            if _reconciler.is_halted():
                # Was: return (hard block). Now: log + size penalty.
                if hash(coin + str(int(time.time() / 300))) % 10 == 0:
                    log(f"{coin} LIFECYCLE WARN — reconciler drift ≥ 5%, sizing×0.5 (safe mode, not blocking)")
                risk_mult = risk_mult * 0.5
                # NO RETURN — let signal proceed at half size
            # STEP 4 spec §2B: degraded-state entry limiter
            limiter = _reconciler.entry_limiter()
            if limiter == 'halted':
                # Reconciler-internal hard halt (3+ unsafe cycles) — still respected
                if hash(coin + str(int(time.time() / 300))) % 10 == 0:
                    log(f"{coin} ENTRY-LIMITER halted: persistent drift across cycles")
                return
            if limiter == 'reduced':
                # Scale risk_mult down by 50% when drift degraded (1-5%)
                risk_mult = risk_mult * 0.5
                if _reconciler.should_skip_high_risk():
                    # High-risk = low-WR or experimental coins; scan gets tighter
                    try:
                        _wr = (_ledger.stats() if _LEDGER_OK else {}) and None  # no-op placeholder
                    except Exception: pass
        except Exception: pass

    # EXECUTION CONTRACT v2: block new activity on halted coins.
    # Halt is set after emergency close from enforce_protection failure.
    # 5-min cooldown; cleared automatically by next successful enforce.
    if is_coin_execution_halted(coin):
        # Only log occasionally to avoid spam (once per ~10 ticks)
        if hash(coin + str(int(time.time() / 300))) % 10 == 0:
            log(f"{coin} EXECUTION HALTED — skipping (critical execution failure)")
        return

    # TF ISOLATION: 15m engine owns only 15m-tagged positions. If a position
    # exists and it's NOT 15m, do NOT interfere. 1h/4h engines manage those.
    # HTF positions' TP/SL already on exchange — they'll resolve on their own.
    _pos_state = state.get('positions', {}).get(coin, {})
    if _pos_state:
        _existing_tf = _pos_state.get('tf')
        if _existing_tf and _existing_tf != '15m':
            # HTF position exists. 15m engine stays out of this coin entirely.
            return

    # CONTRACT: queue drain (15m engine). Only reads queue entries for 15m
    # positions. 1h/4h positions have their own independent engines/queues.
    if _EC_OK and _contract is not None:
        q = _contract.get_queued_reversal(coin, position_tf='15m')
        if q:
            if coin in live_positions:
                # Position still open. Confirm it's a 15m position — otherwise
                # queue stays; this 15m pass should not close an HTF trade.
                _existing_tf = state.get('positions', {}).get(coin, {}).get('tf')
                if _existing_tf and _existing_tf != '15m':
                    # Not our concern — don't drain. HTF engine handles its own.
                    pass
                # else: wait for our 15m TP/SL to resolve it
            else:
                queued_side = q.get('desired_side')
                _contract.clear_reversal_queue(coin, position_tf='15m')
                log(f"{coin} 15m QUEUE DRAIN: firing queued {queued_side} "
                    f"(advisory {q.get('advisory_reason')})")

    candles=fetch(coin)
    last_s=state['cooldowns'].get(coin+'_sell', 0)
    last_b=state['cooldowns'].get(coin+'_buy',  0)
    sig, bar_ts = signal(candles, last_s, last_b, coin=coin)
    signal_engine = 'PIVOT' if sig else None

    # CONTRACT: if no fresh signal but queue had a desire, adopt it
    if not sig and _EC_OK and _contract is not None:
        q2 = _contract.get_queued_reversal(coin, position_tf='15m')
        if q2 and coin not in live_positions:
            sig = q2.get('desired_side')
            signal_engine = 'QUEUED_REVERSAL_15m'
            bar_ts = int(time.time() * 1000)
            _contract.clear_reversal_queue(coin, position_tf='15m')
            log(f"{coin} adopting queued 15m signal: {sig}")

    # RED TEAM: extend cooldown in chop regime (2x) to prevent Zone C signal
    # clustering. Applies after all engine-level CD checks.
    if sig and _RT_OK and _red_team is not None:
        try:
            import regime_detector as _rd
            _cur_reg = _rd.get_regime()
            _mult = _red_team.chop_cooldown_multiplier(_cur_reg)
            if _mult > 1.0:
                _last = last_s if sig == 'SELL' else last_b
                _extended_cd = int(CD_MS * _mult)
                if bar_ts - _last < _extended_cd:
                    log(f"{coin} {sig} SKIP: chop cooldown extension (mult={_mult}x)")
                    sig = None; signal_engine = None
        except Exception:
            pass  # fail-open

    # PER-COIN FILTER: for ELITE coins, apply tuned sigs whitelist + filter
    # Each coin's allowed sigs list and filter (ema200/adx25/etc) comes from OOS tuning
    if percoin_configs.ELITE_MODE and percoin_configs.is_elite(coin):
        elite_cfg_check = percoin_configs.get_config(coin)
        allowed = set(elite_cfg_check.get('sigs', [])) if elite_cfg_check else set()
        # If PIVOT signal fired but coin doesn't allow PV, drop it
        if sig and 'PV' not in allowed:
            # silent — per-coin filter intentionally blocks wrong sig engine
            sig = None; signal_engine = None
        # Try BB_REJ if allowed and nothing fired
        if not sig and 'BB' in allowed:
            try:
                sig, bar_ts = bb_signal(candles, coin=coin, last_buy_ts=last_b, last_sell_ts=last_s)
                if sig: signal_engine = 'BB_REJ'
            except Exception as e:
                log(f"bb_signal err {coin}: {e}")
        # Try INSIDE_BAR if allowed and nothing fired
        if not sig and 'IB' in allowed:
            try:
                sig, bar_ts = ib_signal(candles, coin=coin, last_buy_ts=last_b, last_sell_ts=last_s)
                if sig: signal_engine = 'INSIDE_BAR'
            except Exception as e:
                log(f"ib_signal err {coin}: {e}")
        # Apply per-coin filter (ema200/adx20/adx25 as configured)
        if sig:
            # Find bar index for bar_ts
            idx = len(candles) - 1
            for j in range(len(candles)-1, -1, -1):
                if candles[j][0] == bar_ts: idx = j; break
            if not pass_per_coin_filter(coin, sig, candles, idx):
                flt = elite_cfg_check.get('flt', 'none') if elite_cfg_check else 'none'
                log(f"{coin} {sig} {signal_engine} FILTERED — failed {flt}")
                _shadow_record_rejection(coin, sig, f'per_coin_filter_{flt}')
                sig = None; signal_engine = None
    elif percoin_configs.ELITE_MODE and not percoin_configs.is_elite(coin):
        # SOFT DEMOTE (was hard block): non-elite signals proceed but flagged.
        # Confidence gets -15 penalty downstream; WR/regime/conf filters still apply.
        # Deadlock valve (30min no opens) further loosens WR + regime if all rejecting.
        # Non-elite gets PIVOT (already fired) + BB engine (no IB — keeps elite specialty).
        if not sig:
            try:
                sig, bar_ts = bb_signal(candles, coin=coin, last_buy_ts=last_b, last_sell_ts=last_s)
                if sig: signal_engine = 'BB_REJ'
            except Exception as e:
                log(f"bb_signal err {coin}: {e}")
        if sig:
            log(f"{coin} {sig} NON-ELITE → passed downstream (-15 conf penalty)")
            _shadow_record_rejection(coin, sig, 'non_elite_soft_demote')
        # DO NOT null sig — let downstream filters decide.

    # REVERSAL CONTRACT v2: market-close → confirm closed → allow new entry
    # this tick. Option A per Phase 1 spec. No queueing, no waiting for TP/SL.
    if sig and coin in state.get('positions', {}):
        pos = state['positions'][coin]
        if pos and ((pos.get('side')=='L' and sig=='SELL') or (pos.get('side')=='S' and sig=='BUY')):
            try:
                log(f"{coin} REVERSAL {pos.get('side')} → {sig}: HARD CLOSE")
                # Authorize close through contract (maintains contract invariants)
                if _EC_OK and _contract is not None:
                    try:
                        _contract.authorize_close(coin, 'signal_reversal', ttl_sec=30)
                    except Exception:
                        pass
                # Execute market close
                close(coin)
                # Confirm closed: poll HL until size=0 (5s timeout)
                _rev_confirmed = False
                _rev_start = time.time()
                while time.time() - _rev_start < 5.0:
                    time.sleep(0.5)
                    try:
                        actual = _ep_fetch_size(coin)
                        if actual is None or actual < 1e-9:
                            _rev_confirmed = True
                            break
                    except Exception:
                        pass
                if _rev_confirmed:
                    # Cancel any residual TP/SL (reduce-only orders are now stale)
                    try:
                        residual = _ep_fetch_orders(coin) or []
                        for o in residual:
                            _ep_cancel_order(coin, o.get('oid'))
                    except Exception: pass
                    state['positions'].pop(coin, None)
                    log(f"{coin} REVERSAL confirmed closed in {time.time()-_rev_start:.1f}s; proceeding with new {sig} entry")
                    # sig remains set → new entry flows normally below
                else:
                    log(f"{coin} REVERSAL CLOSE TIMEOUT (size still >0 after 5s) — skipping new entry")
                    sig = None; signal_engine = None
            except Exception as e:
                log(f"{coin} reversal err: {e}")
                sig = None; signal_engine = None
    # Secondary: pullback engine (OOS 84.9% WR / PF 9.83)
    if not sig:
        try:
            pb_s = state['cooldowns'].get(coin+'_pb_sell', 0)
            pb_b = state['cooldowns'].get(coin+'_pb_buy', 0)
            sig, bar_ts = pullback_signal(coin, candles, pb_b, pb_s)
            if sig:
                signal_engine = 'PULLBACK'
                key = coin + ('_pb_buy' if sig=='BUY' else '_pb_sell')
                state['cooldowns'][key] = bar_ts
        except Exception as e:
            log(f"pullback err {coin}: {e}")
    # Tertiary: wall-bounce retest engine (requires verified OB + V3 alignment)
    if not sig:
        try:
            # Infer V3 direction from trend_gate checks
            v3_dir = 0
            if trend_gate(coin, 'BUY') and not trend_gate(coin, 'SELL'): v3_dir = 1
            elif trend_gate(coin, 'SELL') and not trend_gate(coin, 'BUY'): v3_dir = -1
            cur_px = get_mid(coin)
            wb_side, wb_wall = wall_bounce.check(coin, cur_px, v3_dir)
            if wb_side:
                sig = wb_side; bar_ts = int(time.time()*1000); signal_engine = 'WALL_BNC'
                state.setdefault('wall_entries', {})[coin] = {
                    'side': wb_side, 'wall_price': wb_wall['price'],
                    'wall_usd': wb_wall['usd'], 'entry_ts': time.time()}
                log(f"WALL-BOUNCE {coin} {wb_side} @ wall ${wb_wall['usd']/1000:.0f}k p={wb_wall['price']}")
        except Exception as e:
            log(f"wall_bounce err {coin}: {e}")
    # 2026-04-25: WALL_EXHAUSTION engine — detect walls about to fail, trade
    # the breakout direction. Asymmetric to wall_bounce (which trades the hold).
    # Activates in any regime; especially valuable in chop where wall_bounce
    # pullback threshold rarely fires.
    if not sig:
        try:
            cur_px = get_mid(coin)
            we_side, we_ctx = wall_exhaustion.check(coin, cur_px)
            if we_side:
                sig = we_side; bar_ts = int(time.time()*1000); signal_engine = 'WALL_EXH'
                log(f"WALL-EXHAUSTION {coin} {we_side} wall_side={we_ctx['wall_side']} "
                    f"decay={we_ctx['decay_pct']}% dist={we_ctx['distance_pct']}% "
                    f"@ ${we_ctx['wall_usd']/1000:.0f}k")
        except Exception as e:
            log(f"wall_exhaustion err {coin}: {e}")
    # 2026-04-25: WALL_ABSORPTION engine — first-touch BB extreme + stable wall.
    # Fades the move (bounce off support / rejection at resistance). Mutually
    # exclusive with wall_exhaustion via wall classification (STABLE vs
    # EXHAUSTING) and with funding_engine via internal funding-overlap guard.
    # Default DISABLED via WALL_ABSORB_ENABLED=0; flip env to enable.
    if not sig:
        try:
            _wa_regime = 'unknown'
            try:
                import regime_detector as _rd_wa
                _wa_regime = _rd_wa.get_regime() or 'unknown'
            except Exception as _wa_e:
                log(f"[wall_absorb] regime fetch err: {type(_wa_e).__name__}: {_wa_e}")
            # Count active absorption positions for capacity gate
            _wa_active = 0
            try:
                for _c, _p in (state.get('positions') or {}).items():
                    if _p and _p.get('signal_engine') == 'WALL_ABSORB':
                        _wa_active += 1
            except Exception: pass
            cur_px = get_mid(coin)
            wa_side, wa_ctx = wall_absorption.check(coin, cur_px, _wa_regime, _wa_active)
            if wa_side:
                sig = wa_side; bar_ts = int(time.time()*1000); signal_engine = 'WALL_ABSORB'
                state.setdefault('wall_entries', {})[coin] = {
                    'side': wa_side, 'wall_price': wa_ctx['wall_price'],
                    'wall_usd': wa_ctx['wall_usd'], 'entry_ts': time.time(),
                    'engine': 'WALL_ABSORB'}
                log(f"WALL-ABSORPTION {coin} {wa_side} {wa_ctx['bb_position']} "
                    f"decay={wa_ctx['wall_decay_pct']}% dist={wa_ctx['distance_pct']}% "
                    f"@ ${wa_ctx['wall_usd']/1000:.0f}k")
        except Exception as e:
            log(f"wall_absorption err {coin}: {e}")
    # 2026-04-25: FUNDING_MR engine — counter-crowd fade in chop. When HL
    # funding is extreme and Binance confirms, fade the over-positioned side.
    # Default DISABLED via FUNDING_MR_ENABLED=0; flip env to enable. Only
    # fires in chop regime by default (FUNDING_MR_CHOP_ONLY=1).
    if not sig:
        try:
            _fmr_regime = 'unknown'
            try:
                import regime_detector as _rd_fmr
                _fmr_regime = _rd_fmr.get_regime() or 'unknown'
            except Exception as _fmr_e:
                log(f"[funding_mr] regime fetch err: {type(_fmr_e).__name__}: {_fmr_e}")
            fmr_side, fmr_ctx = funding_engine.check(coin, _fmr_regime)
            if fmr_side:
                sig = fmr_side; bar_ts = int(time.time()*1000); signal_engine = 'FUNDING_MR'
                log(f"FUNDING-MR {coin} {fmr_side} hl={fmr_ctx['hl_funding_daily_pct']}%/d "
                    f"bn={fmr_ctx.get('bn_funding_hr_pct')} conf={fmr_ctx['confidence']}")
        except Exception as e:
            log(f"funding_mr err {coin}: {e}")
    # Quaternary: liquidation cascade fade
    if not sig:
        try:
            casc = liquidation_ws.get_cascade(coin, max_age_sec=180)
            if casc:
                sig = casc['fade_direction']; bar_ts = int(time.time()*1000); signal_engine = 'LIQ_CSCD'
                log(f"LIQ-CASCADE {coin} fade {sig} (${casc['total_usd']/1e6:.1f}M liqs)")
        except Exception as e:
            log(f"liq cascade err {coin}: {e}")
    # Quinary: spoof detection fade
    if not sig:
        try:
            sp = spoof_detection.get_spoof_signal(coin)
            if sp:
                sig = sp['direction']; bar_ts = int(time.time()*1000); signal_engine = 'SPOOF'
                spoof_detection.mark_fired(coin)
                log(f"SPOOF-FADE {coin} {sig} (wall ${sp['original_wall']/1000:.0f}k→${sp['remaining']/1000:.0f}k)")
        except Exception as e:
            log(f"spoof err {coin}: {e}")
    # Sextenary: TREND_CONT — buy-pullback / sell-rally in direction of HTF bias.
    # This is the FIRST engine that intentionally aligns with regime instead of
    # fading it. Fixes the 90% SELL bias caused by mean-reversion engines in
    # bull markets. Fires when:
    #   - 1h + 4h bias both strongly in same direction (from mtf_context)
    #   - Price has pulled back / rallied to within 0.6% of 1h EMA20
    #   - 5m RSI is not extreme (20-80) — we want continuation, not reversal
    # R:R: 1h EMA20 as invalidation (tight SL ~1-2%), next swing as TP (3-5%).
    if not sig and _MTF_OK and _mtf is not None:
        try:
            b1, d1 = _mtf.get_bias(coin, '1h')
            b4, d4 = _mtf.get_bias(coin, '4h')
            # Require BOTH TFs to agree strongly (not neutral)
            if b1 == b4 and b1 in ('up', 'down'):
                cur_px = get_mid(coin)
                ema20_1h = (d1 or {}).get('ema20')
                if cur_px and ema20_1h and ema20_1h > 0:
                    dist_to_ema = (cur_px - ema20_1h) / ema20_1h
                    # In uptrend, BUY the pullback (price near/below 1h EMA20).
                    # In downtrend, SELL the rally (price near/above 1h EMA20).
                    pullback_buy = b1 == 'up' and -0.015 < dist_to_ema < 0.006
                    rally_sell  = b1 == 'down' and -0.006 < dist_to_ema < 0.015
                    if pullback_buy or rally_sell:
                        # Sanity: avoid entries when 5m RSI is already extreme (catching falling/rising knife)
                        rsi_arr = rsi_calc([c[4] for c in candles], 14)
                        r_now = rsi_arr[-1] if rsi_arr and rsi_arr[-1] is not None else 50
                        ok_rsi = (pullback_buy and 30 <= r_now <= 70) or (rally_sell and 30 <= r_now <= 70)
                        if ok_rsi:
                            # Cooldown check
                            tc_key = coin + ('_tc_buy' if pullback_buy else '_tc_sell')
                            last_tc = state['cooldowns'].get(tc_key, 0)
                            bar_ts_now = int(time.time()*1000)
                            if (bar_ts_now - last_tc) > CD_MS:
                                sig = 'BUY' if pullback_buy else 'SELL'
                                bar_ts = bar_ts_now
                                signal_engine = 'TREND_CONT'
                                state['cooldowns'][tc_key] = bar_ts
                                log(f"TREND_CONT {coin} {sig}: 1h={b1}({d1.get('dist_pct','?')}%) 4h={b4}({d4.get('dist_pct','?')}%) dist_ema20={dist_to_ema*100:+.2f}% rsi={r_now:.0f}")
        except Exception as _tce:
            log(f"trend_cont err {coin}: {_tce}")

    cur=state['positions'].get(coin)
    live=live_positions.get(coin)

    # Position management under EXECUTION CONTRACT:
    # The exchange-side TP/SL orders are now the PRIMARY protection for ALL
    # positions (elite + non-elite via fallback config). Polling-based exits
    # are advisory-only. This entire block is bypassed when the contract is
    # active — all close() calls below would be contract violations.
    # Kept intact but gated on contract absence for fallback safety during
    # contract module failure.
    _contract_active = bool(_EC_OK and _contract is not None)
    if cur and live and not _contract_active:

        mark = get_mid(coin)
        if mark and cur.get('entry'):
            entry = cur['entry']
            side = cur['side']
            fav = (mark - entry) / entry if side == 'L' else (entry - mark) / entry

            # Per-coin hard stop — uses OOS-validated SL from percoin_configs if available
            sl_pct = STOP_LOSS_PCT  # global 2% fallback
            try:
                if percoin_configs.ELITE_MODE and percoin_configs.is_elite(coin):
                    _cfg = percoin_configs.get_config(coin)
                    if _cfg and 'SL' in _cfg:
                        sl_pct = _cfg['SL']  # OOS-validated per-coin SL (typically 5%)
            except Exception: pass
            sl_pct = _apply_sl_cap(sl_pct)

            if fav <= -sl_pct:
                prev_pos = dict(cur)
                prev_pos['exit_reason'] = 'sl_hit'
                pnl_pct = close(coin)
                if pnl_pct is not None:
                    record_close(prev_pos, coin, pnl_pct, state)
                    state['consec_losses'] += 1
                    state['last_pnl_close'] = pnl_pct
                log(f"{coin} STOP LOSS {fav*100:.2f}% (limit -{sl_pct*100:.1f}%)")
                state['positions'].pop(coin, None)
                return

            # PER-COIN TP-LOCK — once TP reached, it becomes the new SL floor.
            # Price can run ABOVE TP freely, but if it retraces BELOW TP it exits with that locked profit.
            # This lets winners ride while guaranteeing minimum TP gain once reached.
            # TP_MULTIPLIER widens TPs to hit $5+ avg win target (OOS validates ×2 = $25 avg win @ $75 margin).
            tp_pct = None
            try:
                if percoin_configs.ELITE_MODE and percoin_configs.is_elite(coin):
                    _cfg = percoin_configs.get_config(coin)
                    if _cfg and 'TP' in _cfg:
                        tp_pct = _cfg['TP'] * TP_MULTIPLIER
            except Exception: pass

            # HWM tracking for trail
            hwm = cur.get('hwm', fav)
            if fav > hwm:
                hwm = fav
                cur['hwm'] = hwm

            # TP-LOCK state: once TP touched, mark the position as locked
            tp_locked = cur.get('tp_locked', False)
            if tp_pct is not None and not tp_locked and fav >= tp_pct:
                cur['tp_locked'] = True
                tp_locked = True
                log(f"{coin} TP-LOCK armed at +{fav*100:.2f}% (TP={tp_pct*100:.2f}%). Floor locked.")

            # If TP-locked, exit when price retraces back to TP level
            if tp_locked and tp_pct is not None and fav < tp_pct:
                prev_pos = dict(cur)
                prev_pos['exit_reason'] = 'tp_lock_exit'
                pnl_pct = close(coin)
                if pnl_pct is not None:
                    record_close(prev_pos, coin, pnl_pct, state)
                    state['consec_losses'] = 0
                    state['last_pnl_close'] = pnl_pct
                log(f"{coin} TP-LOCK EXIT +{fav*100:.2f}% (peak +{hwm*100:.2f}%, TP floor {tp_pct*100:.2f}%)")
                state['positions'].pop(coin, None)
                return

            # TRAIL (secondary): only active AFTER tp-lock, to capture runs past TP
            # Uses tighter 0.8% trail to not give back too much.
            # Before TP-lock: no trail exit (let it work toward TP or SL).
            # After TP-lock: 0.8% trail from peak on top of locked TP floor.
            if tp_locked and hwm > (tp_pct + TRAIL_PCT):
                age = time.time() - (cur.get('opened_at') or time.time())
                trl = TRAIL_TIGHTEN_PCT if age > TRAIL_TIGHTEN_AFTER_SEC else TRAIL_PCT
                if (hwm - fav) >= trl:
                    prev_pos = dict(cur)
                    prev_pos['exit_reason'] = 'trail_exit'
                    pnl_pct = close(coin)
                    if pnl_pct is not None:
                        record_close(prev_pos, coin, pnl_pct, state)
                        state['consec_losses'] = 0
                        state['last_pnl_close'] = pnl_pct
                    log(f"{coin} TRAIL EXIT +{fav*100:.2f}% (peak +{hwm*100:.2f}%, trail {trl*100:.2f}%, post-TP-lock)")
                    state['positions'].pop(coin, None)
                    return

    # 4h max hold check
    if cur and cur.get('opened_at'):
        age = time.time() - cur['opened_at']
        if age > MAX_HOLD_SEC:
            log(f"{coin} MAX HOLD exceeded ({age/3600:.1f}h) — force close (does NOT count as loss)")
            pnl_pct = close(coin)
            if pnl_pct is not None:
                # MAX HOLD closes never trigger circuit breaker
                state['last_pnl_close'] = pnl_pct
            state['positions'].pop(coin, None)
            return

    # FIX #6: Funding filter — cut if funding eating profits
    if live and live.get('pnl',0) > 0:
        funding_rate = get_funding_rate(coin)  # hourly rate
        # Estimate 1h forward cost: funding * notional (if wrong-side funding)
        pos_size = abs(live['size'])
        mark = live.get('mark', 0)
        notional = pos_size * mark
        # If holding long and funding > 0 → pay. Holding short and funding < 0 → pay.
        is_long = live['size'] > 0
        paying_funding = (is_long and funding_rate > 0) or (not is_long and funding_rate < 0)
        if paying_funding:
            hourly_cost = abs(funding_rate) * notional
            profit = live['pnl']
            # 2026-04-22: Tightened funding cut. Was: hourly_cost > profit × 0.50
            # which closed positions as soon as 1hr forward funding exceeded
            # half of current profit — firing on tiny profits in high-funding
            # windows. New: require position age > 1hr (enough funding has
            # already been paid to justify concern) AND hourly_cost > profit × 2
            # (not just offsetting profit, actively eating it at 2x the rate).
            # Native TP catches real winners long before funding becomes an issue.
            pos_state = state.get('positions', {}).get(coin, {})
            age = time.time() - (pos_state.get('opened_at') or time.time())
            FUNDING_CUT_MIN_AGE_SEC = 3600
            FUNDING_CUT_MIN_COST_RATIO = 2.0
            if (age > FUNDING_CUT_MIN_AGE_SEC and
                hourly_cost > profit * FUNDING_CUT_MIN_COST_RATIO and
                profit > 0):
                log(f"{coin} FUNDING CUT: cost ${hourly_cost:.3f}/h vs profit ${profit:.3f} age={age/3600:.1f}h")
                pnl_pct = close(coin)
                if pnl_pct is not None:
                    state['last_pnl_close'] = pnl_pct
                    state['consec_losses'] = 0  # funding cut = booked win, reset streak
                state['positions'].pop(coin, None)
                return

    if not sig: return

    # Enforce position caps (reconciled via live_positions)
    open_count = len(live_positions)
    if not live and open_count >= MAX_POSITIONS:
        log(f"{coin} {sig} SKIP (max {MAX_POSITIONS} positions)")
        _shadow_record_rejection(coin, sig, 'max_positions_cap', {'open_count': open_count})
        return
    same_side_count = sum(1 for p in live_positions.values() if (p['size']>0 and sig=='BUY') or (p['size']<0 and sig=='SELL'))
    if not live and same_side_count >= MAX_SAME_SIDE:
        log(f"{coin} {sig} SKIP (side cap {MAX_SAME_SIDE})")
        _shadow_record_rejection(coin, sig, 'same_side_cap', {'same_side_count': same_side_count})
        return

    # ─── DRAWDOWN CIRCUIT BREAKER ──────────────────────────────────────
    # 2026-04-22: Added after observing a correlation disaster — bot
    # stacked 21 longs in bull-calm, then intraday pullback turned every
    # single one red simultaneously (uPnL -$16 in 30min, ~2.5% of equity).
    # Individual-position risk checks don't protect against this because
    # each position is sized to its own risk budget; it's the AGGREGATE
    # that turns catastrophic when correlation spikes.
    # Gate logic: if aggregate uPnL across all open positions is worse
    # than DRAWDOWN_FLOOR % of equity, block new entries on the same side
    # as the majority of losing positions. Still allows opposite-side
    # entries (which would be hedging) and allows exits (reduce-only).
    if not live and live_positions:
        aggregate_upnl = sum(p.get('upnl', 0) for p in live_positions.values())
        drawdown_pct = abs(aggregate_upnl) / equity if equity > 0 else 0
        DRAWDOWN_FLOOR = float(os.environ.get('DRAWDOWN_FLOOR', '0.02'))  # 2% default
        if aggregate_upnl < 0 and drawdown_pct > DRAWDOWN_FLOOR:
            # Find which side is the loser. If majority of losing positions
            # are longs, block further BUYs. If shorts, block SELLs.
            losing_longs = sum(1 for p in live_positions.values()
                               if p.get('upnl', 0) < 0 and p.get('size', 0) > 0)
            losing_shorts = sum(1 for p in live_positions.values()
                                if p.get('upnl', 0) < 0 and p.get('size', 0) < 0)
            losing_side = 'BUY' if losing_longs > losing_shorts else 'SELL'
            if sig == losing_side:
                log(f"{coin} {sig} SKIP (drawdown circuit: uPnL=${aggregate_upnl:.2f} "
                    f"= {drawdown_pct*100:.1f}% > floor {DRAWDOWN_FLOOR*100:.0f}%, "
                    f"losing side = {losing_side})")
                return
    # ───────────────────────────────────────────────────────────────────

    risk_pct = current_risk_pct(equity)
    total_locked = get_total_margin()
    proposed = equity * risk_pct * risk_mult
    if not live and (total_locked + proposed)/equity > MAX_TOTAL_RISK:
        # Before hard-skipping: try to close positions to make room
        if percoin_configs.ELITE_MODE and percoin_configs.is_elite(coin):
            # CONTRACT: tier-bump closes violate the hierarchy. Existing positions
            # must run to TP/SL. If margin tight, skip the new signal instead.
            if _EC_OK and _contract is not None:
                log(f"{coin} {sig} SKIP (margin tight; contract forbids tier-bump close)")
                return
            incoming_tier = percoin_configs.get_tier(coin)
            DUST_USD = 0.10
            # Tier priority for closing: SEVENTY_79 → EIGHTY_89 → NINETY_99 → PURE
            # Higher-tier incoming signals can claim lower-tier positions.
            # PURE: never close anything (100% WR needs room but don't sacrifice 90%+ edge)
            # Rule: can close a tier STRICTLY LOWER than incoming tier, OR dust of any tier
            TIER_RANK = {'PURE': 4, 'NINETY_99': 3, 'EIGHTY_89': 2, 'SEVENTY_79': 1}
            incoming_rank = TIER_RANK.get(incoming_tier, 0)
            # Categorize candidates
            # dust_cands: list of (abs_pnl_usd, k, notional, pnl, tier) — can always close
            # profit_by_tier: dict of tier -> list of (pnl_usd, k, notional) — tier-ranked sacrifice
            dust_cands = []
            profit_by_tier = {'SEVENTY_79': [], 'EIGHTY_89': [], 'NINETY_99': [], 'PURE': []}
            for k, lp in live_positions.items():
                if k == coin: continue
                sz = lp.get('size', 0); entry = lp.get('entry', 0)
                if sz == 0 or not entry: continue
                pos_tier = percoin_configs.get_tier(k) or 'NONE'
                pos_pnl = lp.get('pnl', 0)  # USD, from HL state (more reliable than get_mid which 429s)
                notional = abs(sz) * entry
                # DUST: |pnl| ≤ $0.10 — always close regardless of tier
                if abs(pos_pnl) <= DUST_USD:
                    dust_cands.append((abs(pos_pnl), k, notional, pos_pnl, pos_tier))
                # PROFIT ≥ $0.10: tier-ranked
                elif pos_pnl > DUST_USD and pos_tier in profit_by_tier:
                    profit_by_tier[pos_tier].append((pos_pnl, k, notional))
            
            closed_one = False
            # Phase 1: sweep ALL dust (no edge sacrificed)
            for _, k, notional, pos_pnl, ptier in sorted(dust_cands):
                try:
                    pnl = close(k); state['positions'].pop(k, None)
                    log(f"DUST-CLOSE {k} ({ptier}) pnl=${pos_pnl:+.3f} (for {incoming_tier} {coin} {sig}, freed ${notional:.0f})")
                    if pnl is not None: state['last_pnl_close'] = pnl
                    closed_one = True
                except Exception as e:
                    log(f"dust-close err {k}: {e}")
            # Check if room now
            total_locked = get_total_margin()
            # Phase 2: tier-ranked profit close (only if STILL tight)
            if (total_locked + proposed)/equity > MAX_TOTAL_RISK:
                # Close in order: lowest-tier first, within tier smallest profit first (cheapest to give up)
                # Only close tiers STRICTLY LOWER than incoming
                close_order = ['SEVENTY_79', 'EIGHTY_89', 'NINETY_99']  # PURE never closed for margin
                for ptier in close_order:
                    if TIER_RANK[ptier] >= incoming_rank: break  # can't sacrifice equal or higher tier
                    if (total_locked + proposed)/equity <= MAX_TOTAL_RISK: break
                    cands = sorted(profit_by_tier[ptier])  # ascending: smallest profit first (cheapest sacrifice)
                    for pos_pnl, k, notional in cands:
                        try:
                            pnl = close(k); state['positions'].pop(k, None)
                            log(f"MARGIN-CLOSE {k} ({ptier}) +${pos_pnl:.3f} (for {incoming_tier} {coin} {sig}, freed ${notional:.0f})")
                            if pnl is not None: state['last_pnl_close'] = pnl
                            closed_one = True
                            total_locked = get_total_margin()
                            if (total_locked + proposed)/equity <= MAX_TOTAL_RISK: break
                        except Exception as e:
                            log(f"margin-close err {k}: {e}")
            # Final check
            if (total_locked + proposed)/equity > MAX_TOTAL_RISK:
                log(f"{coin} {sig} SKIP (margin still {total_locked:.0f}+{proposed:.0f} > {MAX_TOTAL_RISK*100:.0f}% after close attempt)")
                return
        else:
            log(f"{coin} {sig} SKIP (margin {total_locked:.0f}+{proposed:.0f} > {MAX_TOTAL_RISK*100:.0f}%)")
            return

    # Per-ticker gate check — uses candles already fetched above (no extra API call)
    # Elite coins: STRICT gate (return on fail). Non-elite: SOFT (conf penalty, no reject).
    _ticker_gate_penalty = 0
    is_non_elite = bool(percoin_configs.ELITE_MODE and not percoin_configs.is_elite(coin))
    try:
        px_for_gate = get_mid(coin) or 0
        passed, gate_reasons = apply_ticker_gate(coin, sig, px_for_gate, candles, return_reasons=True)
        if not passed:
            if is_non_elite:
                _ticker_gate_penalty = 10 * len(gate_reasons)  # -10 per failed sub-gate
                log(f"{coin} {sig} SOFT-GATED(non-elite): {','.join(gate_reasons)} → -{_ticker_gate_penalty} conf")
            else:
                log(f"{coin} {sig} GATED(elite-strict): {','.join(gate_reasons)}")
                return
    except Exception as e:
        log(f"{coin} gate check err: {e}")

    # Signal persistence: DISABLED temporarily (blocking all live signals, OOS +15% but requires market movement)
    # if not signal_persistence.check(coin, sig, bar_ts): return

    # Confidence scoring: 0-100 → sizing multiplier (regime-conditional floor)
    # OOS: every score tier profitable, use as SIZING not filter. Every signal trades.
    try:
        btc_state = btc_correlation.get_state()
        btc_d = btc_state.get('btc_dir', 0)
        conf_score, conf_breakdown = confidence.score(candles, [], coin, sig, btc_d)
        # Non-elite penalty: -15 to confidence + soft ticker-gate penalty (10 per failed sub-gate)
        if is_non_elite:
            _orig_conf = conf_score
            _total_penalty = 15 + _ticker_gate_penalty
            conf_score = max(0, conf_score - _total_penalty)
            log(f"{coin} {sig} non-elite conf {_orig_conf}→{conf_score} (-15 base, -{_ticker_gate_penalty} gate)")

        # ─── LEVER 1 PATCH 1: MULTI-ENGINE CONFLUENCE BOOST (2026-04-25) ───
        # Apply AFTER scoring, BEFORE floor check. Re-scan engines on the
        # finalized sig+bar_ts (signal may have switched from PIVOT to BB/IB
        # during cascade). True confluence = independent agreement.
        _conf_engines = set()
        try:
            if sig and bar_ts:
                _conf_engines.add(signal_engine or 'PIVOT')
                try:
                    _alt_pv, _alt_ts = signal(candles, 0, 0, coin=coin)
                    if _alt_pv == sig and _alt_ts and abs(_alt_ts - bar_ts) <= 15*60*1000:
                        _conf_engines.add('PIVOT')
                except Exception: pass
                try:
                    _bb_sig, _bb_ts = bb_signal(candles, coin=coin, last_buy_ts=0, last_sell_ts=0)
                    if _bb_sig == sig and _bb_ts and abs(_bb_ts - bar_ts) <= 15*60*1000:
                        _conf_engines.add('BB_REJ')
                except Exception: pass
                try:
                    _ib_sig, _ib_ts = ib_signal(candles, coin=coin, last_buy_ts=0, last_sell_ts=0)
                    if _ib_sig == sig and _ib_ts and abs(_ib_ts - bar_ts) <= 15*60*1000:
                        _conf_engines.add('INSIDE_BAR')
                except Exception: pass
        except Exception: pass
        _conf_count = len(_conf_engines)
        _multi_boost = 0
        if _conf_count >= 3:
            _multi_boost = 8
        elif _conf_count >= 2:
            _multi_boost = 5
        if _multi_boost > 0:
            _pre = conf_score
            conf_score += _multi_boost
            log(f"{coin} {sig} CONFLUENCE +{_multi_boost} ({_conf_count} engines: {sorted(_conf_engines)}) conf {_pre}→{conf_score}")

        # Pass current regime so floor adapts: 30 in trending, 15 in chop
        import regime_detector as _regdet
        cur_regime = _regdet.get_regime()

        # ─── LEVER 1 PATCH 2: SELECTIVE CHOP SOFTENING (2026-04-25) ───
        # Don't crush strong signals in chop. Lift conf+2 if already strong (≥12),
        # additional -2 only if weak. Final: floor still applies.
        if cur_regime == 'chop' or cur_regime is None:
            if conf_score >= 12:
                _pre = conf_score
                conf_score += 2
                log(f"{coin} {sig} CHOP-LIFT (strong): conf {_pre}→{conf_score}")
            elif conf_score < 10:
                # Already at/below floor — no further punish needed; floor handles it.
                pass

        size_mult = confidence.size_multiplier(conf_score, cur_regime)
        if size_mult <= 0.0:
            log(f"{coin} {sig} SKIP: conf={conf_score} below conviction floor (regime={cur_regime}) {conf_breakdown}")
            _shadow_record_rejection(coin, sig, 'conf_below_floor',
                                     {'conf': conf_score, 'regime': cur_regime})
            return
        # Adaptive risk: per-coin × per-hour × per-side rolling WR multipliers
        adapt = adaptive_mult(coin, sig, state)
        risk_mult = risk_mult * size_mult * adapt

        # PATH DEPENDENCY: live streak-based sizing.
        # Consec-losses → reduce size. 7+ losses → pause entries.
        if _PD_OK and _path_dep is not None:
            try:
                _pd_mult, _pd_flags = _path_dep.get_size_multiplier()
                if _pd_flags.get('paused'):
                    log(f"{coin} {sig} SKIP: path_dep entry pause "
                        f"({_pd_flags.get('pause_remaining_sec')}s remaining)")
                    return
                if _pd_mult != 1.0:
                    risk_mult = risk_mult * _pd_mult
                    log(f"{coin} path_dep: size_mult={_pd_mult} "
                        f"consec_losses={_pd_flags.get('consec_losses')}")
            except Exception:
                pass  # fail-open

        # ENSEMBLE VOTE: top-K=3 agreement boosts or reduces size.
        # unanimous → 1.3x | majority → 1.0x | minority → 0.5x
        if _EV_OK and _ensemble is not None:
            try:
                import regime_configs as _rc
                _cur_regime = None
                try:
                    import regime_detector as _rd4
                    _cur_regime = _rd4.get_regime()
                except Exception: pass
                if _cur_regime:
                    _ens_list = _rc.get_ensemble(coin, _cur_regime)
                    if _ens_list and len(_ens_list) >= 2:
                        _vote = _ensemble.vote(coin, candles, sig, _ens_list)
                        _em = _vote.get('size_multiplier', 1.0)
                        if _em != 1.0:
                            risk_mult = risk_mult * _em
                            log(f"{coin} ensemble: {_vote.get('decision')} "
                                f"({_vote.get('confirming_votes')}/{_vote.get('total_votes')}) "
                                f"size_mult={_em}")
            except Exception:
                pass

        # TF ISOLATION: HTF alignment sizing. 4h bias modifies 15m entry size.
        # Aligned → boost 1.5x. Neutral → 1.0x. Opposing → 0.5x or block.
        # Does NOT force exits; advisory for entry sizing only.
        try:
            import tf_isolation as _tfi
            import urllib.request as _ureq
            # Pull 4h candles from HL on demand (cached in module below)
            _c_4h = None
            try:
                _cache_key = f'htf_4h_{coin}'
                _cached = _CANDLE_CACHE.get(_cache_key)
                if _cached and time.time() - _cached['ts'] < 600:  # 10min cache
                    _c_4h = _cached['data']
                else:
                    _hl_throttle()
                    _c_4h = okx_fetch.fetch_klines(coin, '4h', 30)
                    _CANDLE_CACHE[_cache_key] = {'data': _c_4h, 'ts': time.time()}
            except Exception:
                pass
            if _c_4h and len(_c_4h) >= 30:
                # Convert HL candle dict to list format expected by derive_htf_bias
                _bars_list = [[b['t'], b['o'], b['h'], b['l'], b['c'], b['v']] for b in _c_4h]
                _htf = _tfi.derive_htf_bias(_bars_list)
                _mult, _action = _tfi.compute_alignment_multiplier(sig, _htf['bias'], _htf['strength'])
                # ─── 2026-04-25: HTF opposing → soft penalty, not hard block ───
                # Hard block was killing 4/14 signals. Replaced with -2 conf
                # penalty applied to risk_mult. Strong signals still pass with
                # reduced size; weak ones already filtered by floor upstream.
                # Per spec: "softens HTF veto without removing it."
                if _action == 'block':
                    risk_mult = risk_mult * 0.5  # half size on opposing HTF
                    log(f"{coin} {sig} HTF SOFT PENALTY: 4h bias={_htf['bias']} strength={_htf['strength']:.2f} opposing — size×0.5 (not blocked)")
                    _shadow_record_rejection(coin, sig, 'htf_opposing_softened',
                                             {'htf_bias': _htf['bias'], 'strength': _htf['strength']})
                    # NO RETURN — let signal proceed at half size
                if _mult != 1.0:
                    risk_mult = risk_mult * _mult
                    log(f"{coin} HTF {_action}: 4h bias={_htf['bias']} strength={_htf['strength']:.2f} mult={_mult:.2f}")
        except Exception as _tfe:
            pass  # non-fatal: fall back to neutral sizing

        # EXPERIMENTAL PROMOTION: apply size multiplier if this signal is
        # flagged for experimental promotion. Halves position size.
        if coin in _EXPERIMENT_PENDING:
            _exp_tag = _EXPERIMENT_PENDING.get(coin, {})
            _exp_size_mult = _exp_tag.get('size_mult', 0.5)
            risk_mult = risk_mult * _exp_size_mult
            log(f"{coin} EXPERIMENTAL sizing: mult={_exp_size_mult} · final risk_mult={risk_mult:.2f}")

        log(f"{coin} CONF={conf_score} conf_mult={size_mult} adapt={adapt:.2f} final_mult={risk_mult:.2f} regime={cur_regime} {conf_breakdown}")
    except Exception as e:
        log(f"{coin} conf err: {e}")
        conf_score = 0

    log_signal(coin, "SIGNAL", sig); log(f"{coin} SIGNAL: {sig} engine={signal_engine} risk={int(risk_pct*100)}% mult={risk_mult:.2f} conf={conf_score}")

    # FUNDING SIGNAL (SILENT): evaluate, log would-fire state. Activation pending
    # once ensemble + shadow data confirms funding signal has orthogonal edge.
    if _FS_OK and _funding_sig is not None:
        try:
            _fund_side, _fund_reason = _funding_sig.check_signal(coin, candles)
            if _fund_side:
                log(f"{coin} [funding_sig WOULD_FIRE]: {_fund_side} ({_fund_reason})")
        except Exception:
            pass

    # SHADOW THRESHOLDS: evaluate relaxed variants silently. No live impact.
    if _SH_OK and _shadow is not None:
        try:
            _cfg = None
            try:
                import percoin_configs as _pcc2
                _cfg = _pcc2.get_config(coin)
            except Exception: pass
            _rh = (_cfg or {}).get('RH') or 70
            _rl = (_cfg or {}).get('RL') or 30
            _shadow.evaluate_shadow(
                coin=coin,
                bars=candles,
                production_rsi_hi=_rh,
                production_rsi_lo=_rl,
                production_pivot_lb=5,
                engine=signal_engine,
                actual_fired=True,
                actual_side=sig,
            )
        except Exception:
            pass

    # RED TEAM: regime staleness gate. If BTC 1h data is stale, the regime
    # classifier is running on cached/wrong data. Abort rather than trade on lies.
    if _RT_OK and _red_team is not None:
        try:
            if not _red_team.regime_staleness_ok():
                log(f"{coin} {sig} SKIP: BTC 1h regime data stale (red_team gate)")
                return
        except Exception:
            pass  # fail-open

    # Parallel telemetry for future mutual information analysis.
    # Records which engine fired + confluence state; non-blocking.
    if _SL_OK and _signal_logger is not None:
        try:
            _cur_regime = None
            try:
                import regime_detector as _rd
                _cur_regime = _rd.get_regime()
            except Exception: pass
            _signal_logger.log_state(
                coin=coin,
                regime=_cur_regime,
                engines_fired={signal_engine: True},  # only the firing engine known here
                confluence_state={
                    'conf_score': conf_score,
                    'risk_pct': risk_pct,
                    'risk_mult': risk_mult,
                },
                actual_fired=True,
                price=float(price) if 'price' in dir() else None,
                bar_ts=int(time.time()),
                side_if_fired=sig,
                conf_score=conf_score,
            )
        except Exception:
            pass  # never block signal path

    # Convexity telemetry — silent. Records payoff asymmetry score at signal fire.
    # NO live sizing impact until explicitly activated post-100-close analysis.
    if _CX_OK and _convex is not None:
        try:
            _cfg = None
            try:
                import percoin_configs as _pcc
                _cfg = _pcc.get_config(coin)
            except Exception: pass
            _tp = (_cfg or {}).get('TP') or 0.05
            _sl = (_cfg or {}).get('SL') or 0.025
            _wlb = (_cfg or {}).get('wilson_lb')
            _convex.log_signal_score(
                coin=coin,
                side=sig,
                engine=signal_engine,
                tp_pct=_tp,
                sl_pct=_sl,
                wilson_lb=_wlb,
                bar_ts=int(time.time()),
                actual_size=risk_pct,
            )
        except Exception:
            pass  # never block signal path

    # Optimal inaction telemetry — silent. Logs abstain score per signal.
    # NO gating; evaluates post-hoc at 100 closes.
    if _IA_OK and _inaction is not None:
        try:
            _cur_regime = None
            try:
                import regime_detector as _rd
                _cur_regime = _rd.get_regime()
            except Exception: pass
            _inaction.log_signal_abstain(
                coin=coin,
                side=sig,
                engine=signal_engine,
                regime=_cur_regime,
                bar_ts=int(time.time()),
            )
        except Exception:
            pass

    # Reflexivity telemetry — crowding + move position + echo-of-echo scoring.
    # No filtering applied until 75-close activation decision.
    if _RX_OK and _reflex is not None:
        try:
            _px = None
            try: _px = float(price) if 'price' in dir() else None
            except Exception: pass
            _reflex.log_signal_score(
                coin=coin,
                side=sig,
                current_price=_px,
                engine=signal_engine,
                bar_ts=int(time.time()),
            )
        except Exception:
            pass

    # REGIME-SIDE BLOCKER (global, pre-gate): fails CLOSED for data-confirmed
    # regime-mismatched trades. Cannot be overridden by KB/LLM gate.
    _rb, _rb_reason = regime_blocks_side(coin, sig)
    if _rb:
        log(f"{coin} {sig} REGIME-BLOCK: {_rb_reason}")
        return

    # MTF CONFLUENCE — 15m signal must align with 1h + 4h bias.
    # 4h = dominant trend (regime confirmation at coin level, not just BTC).
    # 1h = thesis (where the actual swing structure lives).
    # 15m = trigger (we're here because a 15m signal fired).
    #
    # 2026-04-25: hard MTF-BLOCK → conviction-gated soft penalty.
    # Floor: 20→15 after observing PUMP signal distribution clusters at 15-24
    # in chop. A conf=15+ signal that reaches MTF check has already passed
    # conviction/WR/R:R/HTF/chop filters — it's heavily screened, not weak.
    # New behavior:
    #   - conf >= 15: soft penalty size×0.3, signal proceeds
    #   - conf <  15: still hard block (junk stays out)
    # Per spec: aligning floor with actual signal distribution, not weakening.
    if _MTF_OK and _mtf is not None:
        try:
            _ok, _detail, _partial_mult = _mtf.aligned(coin, sig)
            if not _ok:
                _conf_for_mtf = locals().get('conf_score', 0)
                if _conf_for_mtf >= 15:
                    risk_mult = risk_mult * 0.3
                    log(f"{coin} {sig} MTF-PENALTY ×0.3 (conviction bypass conf={_conf_for_mtf}): {_detail}")
                    # NO RETURN — let conviction-gated signal proceed at reduced size
                else:
                    log(f"{coin} {sig} MTF-BLOCK: {_detail} (conf={_conf_for_mtf} < 15 floor)")
                    return
            else:
                log(f"{coin} {sig} MTF-OK: {_detail}")
            # Apply partial-opposition downsize BEFORE conviction boost
            if _partial_mult < 1.0:
                _old_pm = risk_mult
                risk_mult = risk_mult * _partial_mult
                log(f"{coin} {sig} MTF-PARTIAL-DOWNSIZE ×{_partial_mult} (risk_mult {_old_pm:.2f} → {risk_mult:.2f})")
            # CONVICTION SIZING — scale risk when HTFs strongly confirm.
            # Multiplier 1.0-MTF_SIZE_MAX based on combined 1h+4h distance
            # from EMA20 in favorable direction. Applied on top of existing
            # risk_mult. Final risk_mult hard-capped at RISK_MULT_CEIL.
            _mtf_mult, _mtf_mdet = _mtf.conviction_mult(coin, sig, max_mult=MTF_SIZE_MAX)
            if _mtf_mult > 1.0:
                _old_mult = risk_mult
                risk_mult = min(RISK_MULT_CEIL, risk_mult * _mtf_mult)
                log(f"{coin} {sig} MTF-CONVICTION ×{_mtf_mult}: {_mtf_mdet} (risk_mult {_old_mult:.2f} → {risk_mult:.2f})")
        except Exception as _me:
            log(f"{coin} MTF err (fail-open): {_me}")

    # NOTE: clear_path_mult REMOVED 2026-04-22. Wall presence detection is
    # symmetric-risk — walls often get pulled right as price approaches, which
    # is where explosive squeezes originate. Sizing up on "no wall in path"
    # rewards setups that might be about to squeeze AGAINST us. The real alpha
    # from orderbook is change-detection (wall pulled, wall grew, multi-venue
    # agreement), which is a different module. Revisit when we have that.

    # R:R FLOOR — enforce minimum profit-to-risk ratio at entry time.
    # 2026-04-25: per-coin min_rr override added.
    # Each coin's percoin_configs entry can specify its own 'min_rr' (defaults
    # to global MIN_RR=1.2). Grid sweep validated each coin's TP/SL combo as
    # profitable AT that specific R:R after fees+slippage — global rule should
    # not override per-coin empirical reality. Falls through to global only
    # when a coin doesn't specify its own min_rr.
    if MIN_RR > 0:
        try:
            _cfg = percoin_configs.get_config(coin) if percoin_configs.ELITE_MODE else None
            _sl_cfg = (_cfg or {}).get('SL')
            _tp_cfg = (_cfg or {}).get('TP')
            _coin_min_rr = (_cfg or {}).get('min_rr', MIN_RR)
            if _sl_cfg and _tp_cfg and _sl_cfg > 0:
                _rr = _tp_cfg / _sl_cfg
                if _rr < _coin_min_rr:
                    log(f"{coin} {sig} R:R REJECT: TP={_tp_cfg*100:.2f}% / SL={_sl_cfg*100:.2f}% = {_rr:.2f} < {_coin_min_rr} (per-coin)")
                    return
        except Exception as _rre:
            log(f"{coin} R:R check err (fail-open): {_rre}")

    # ─────────────────────────────────────────────────────
    # ENTRY LLM GATE — final semantic pre-trade check.
    # Reads tuned params + active vetos + KB entries → ALLOW | SIZE_DOWN | BLOCK.
    # Fail-open on any error (returns ALLOW with 1.0 mult) so trading never halts.
    # Set env POSTMORTEM_ENTRY_GATE=0 to disable.
    # ─────────────────────────────────────────────────────
    if _POSTMORTEM_OK and _postmortem is not None:
        try:
            cur_px = get_mid(coin) or 0
            session_hour = time.gmtime(time.time()).tm_hour
            session_name = ('asian' if 0 <= session_hour < 8
                            else 'london' if 8 <= session_hour < 13
                            else 'ny' if 13 <= session_hour < 21
                            else 'overnight')
            try:
                funding_bps = get_funding_rate(coin) * 10000.0  # fraction → bps
            except Exception:
                funding_bps = None
            try:
                _btc_dir = btc_correlation.get_state().get('btc_dir', 0)
            except Exception:
                _btc_dir = 0
            signal_ctx = {
                'engine': signal_engine,
                'conf_score': conf_score,
                'conf_breakdown': conf_breakdown if 'conf_breakdown' in dir() else None,
                'price': cur_px,
                'session': session_name,
                'funding_rate_bps': funding_bps,
                'btc_dir': _btc_dir,
                'equity': equity,
                'open_positions': len(live_positions),
                'regime_state': None,  # optional; left null for now
            }
            verdict = _postmortem.evaluate_entry(coin, sig, signal_ctx)
            dec = verdict.get('decision', 'ALLOW')
            sm = float(verdict.get('size_mult', 1.0))
            reason = verdict.get('reason', '')
            if dec == 'BLOCK':
                log(f"{coin} {sig} GATE BLOCK: {reason}")
                return
            if dec == 'SIZE_DOWN' and sm < 1.0:
                old_mult = risk_mult
                risk_mult = risk_mult * sm
                log(f"{coin} {sig} GATE SIZE_DOWN ×{sm:.2f}: {reason} (mult {old_mult:.2f} → {risk_mult:.2f})")
            else:
                log(f"{coin} {sig} GATE ALLOW: {reason}" if reason else f"{coin} {sig} GATE ALLOW")
        except Exception as e:
            log(f"{coin} gate err (continuing): {e}")

    now = time.time()

    # SIGNAL-REVERSAL PROFIT-FLOOR GUARD
    # 2026-04-22: Analysis of last 30 closes showed avg win ~$0.09-$0.36 on
    # $200 notional positions (0.05-0.15% move) — far below the 6-8% TP target.
    # Root cause: when a weak counter-signal fires on an existing position,
    # the bot closes it immediately (regardless of PnL) and reverses. Most
    # reversals happen while the position is in tiny profit, killing winners
    # before TP can fire. Trades that survive signal churn return to proper
    # 1-2% wins (MANTA +$1.04, PAXG +$1.83, REZ +$0.74); trades that get
    # flipped return $0.05-$0.28.
    #
    # Guard logic: reversal is ALLOWED only if at least one of:
    #   (a) Position is at a loss — reversal confirms we were wrong
    #   (b) Position has reached ≥50% of TP target — peak-taking is valid
    #   (c) Position is older than MIN_HOLD_SEC (15 min) — signal churn
    #       protection; give setups time to play out before flipping
    # Otherwise: ignore the opposite signal. Let SL/TP or max-hold handle it.
    MIN_HOLD_BEFORE_REVERSE_SEC = 900  # 15 min
    MIN_FAV_FRAC_FOR_REVERSE = 0.50    # need 50% of TP to bail
    def _allow_reversal(live_pos, pos_state):
        """Return (allow: bool, reason: str)."""
        if not live_pos or not pos_state: return True, "no_state"
        pnl = live_pos.get('pnl') or live_pos.get('upnl') or 0.0
        # (a) in loss
        if pnl < 0: return True, f"loss(${pnl:.2f})"
        # (b) fav >= 50% of TP target
        entry = pos_state.get('entry', 0)
        tp_pct = pos_state.get('tp_pct')
        if entry and tp_pct:
            cur_px = get_mid(live_pos.get('coin', '') if hasattr(live_pos,'get') else '')
            # fallback: derive from pnl + notional
            notional = abs(live_pos.get('size', 0)) * entry
            if notional > 0:
                fav_frac = pnl / notional  # fraction of notional gained
                if fav_frac >= tp_pct * MIN_FAV_FRAC_FOR_REVERSE:
                    return True, f"fav={fav_frac*100:.2f}%>=50%TP({tp_pct*100:.1f}%)"
        # (c) REMOVED 2026-04-22. Age > 15min used to allow reversal by itself.
        # But a 16-minute-old position with +$0.30 profit would still get flipped
        # on a weak counter-signal, reproducing the exact leak the guard was
        # supposed to fix. Reversal now strictly requires loss OR 50%+ of TP.
        # Old native TP / SL handle the time-based exit via normal TP-LOCK.
        return False, f"pnl=${pnl:.2f} profit floor not met"

    # ─── SURVIVAL GUARDS: entry-time gates (2026-04-25) ───
    # Run only when there's a valid sig AND no existing position to manage.
    # Reversals (sig opposing existing pos) skip these gates — they go through
    # _allow_reversal logic below and get handled via the existing queue.
    if sig and not live:
        # Pull regime + WR snapshot up front for unified reject log
        try:
            import regime_detector as _rd
            _regime = _rd.get_regime() or 'unknown'
        except Exception:
            _regime = 'unknown'
        _wr_rec = _COIN_WR_BOOTSTRAP.get(coin.upper(), {})
        _coin_wr = _wr_rec.get('wr', 0.0)
        _wr_n = _wr_rec.get('n', 0)
        _bypass = _deadlock_active()
        if _bypass:
            log(f"DEADLOCK {coin} {sig} | {int((time.time()-_LAST_OPEN_TS)/60)}min since last open → bypass WR+regime")

        # Guard 2: regime direction filter (softened — block only if conf<65)
        if not _bypass:
            rb_block, rb_reason = _regime_dir_blocks_entry(sig)
            if rb_block:
                # Only block weak counter-trend (conf<65 if known); strong setups pass
                _conf_pre = locals().get('conf_score', None)
                if _conf_pre is None or _conf_pre < 65:
                    log(f"REJECT {coin} | regime={_regime} sig={sig} wr={_coin_wr:.2f} trades={_wr_n} reason=regime/{rb_reason}")
                    return
                else:
                    log(f"ALLOW {coin} | regime={_regime} sig={sig} conf={_conf_pre} note=counter_trend_high_conf")
        # Guard 3: per-coin WR filter (three-tier with time-decay forgiveness)
        # ─── LEVER 1 PATCH 3: HIGH-CONF BYPASS ON DUPLICATE PENALTY ───
        # If conf already passed at high threshold (conf≥65), don't double-penalize
        # for low coin WR — conviction has already been validated by score stack.
        # 2026-04-25: returns (blocked, size_mult, reason). Tiers:
        #   - Tier 1: WR<20% → blocked (true loser)
        #   - Tier 2: 20-35% → allowed at size×0.3 (marginal, prove yourself)
        #   - Tier 3: ≥35%  → allowed at full size
        if not _bypass:
            _conf_for_wr = locals().get('conf_score', None)
            wr_block, wr_size_mult, wr_reason = _coin_wr_blocks_entry(coin)
            if wr_block:
                if _conf_for_wr is not None and _conf_for_wr >= 65:
                    log(f"ALLOW {coin} | regime={_regime} sig={sig} conf={_conf_for_wr} wr={_coin_wr:.2f} note=high_conf_bypass_wr_filter")
                else:
                    log(f"REJECT {coin} | regime={_regime} sig={sig} wr={_coin_wr:.2f} trades={_wr_n} reason=coin_wr/{wr_reason}")
                    return
            elif wr_size_mult < 1.0:
                # Soft-allow: marginal coin, apply size penalty
                _old_rm = risk_mult
                risk_mult = risk_mult * wr_size_mult
                log(f"ALLOW {coin} | regime={_regime} sig={sig} wr={_coin_wr:.2f} trades={_wr_n} note=coin_wr/{wr_reason} risk_mult {_old_rm:.2f}→{risk_mult:.2f}")
            elif wr_reason and 'unproven' in wr_reason:
                # Allowed at full size but log for visibility (non-blocking)
                log(f"ALLOW {coin} | regime={_regime} sig={sig} wr={_coin_wr:.2f} trades={_wr_n} note=unproven/{wr_reason}")
    # ─────────────────────────────────────────────────────

    if sig == 'SELL':
        state['cooldowns'][coin+'_sell'] = bar_ts
        if live and live['size']>0:
            prev_pos = dict(state.get('positions', {}).get(coin, {}))
            # Guard: don't flip a winning long on a weak SELL signal
            allow, reason = _allow_reversal({**live, 'coin':coin}, prev_pos)
            if not allow:
                log(f"{coin} SELL reversal SKIPPED: long position held ({reason})")
                return
            # CONTRACT: signal reversal is ADVISORY. Queue the desire; do NOT
            # close the existing position. TP/SL on exchange will resolve it.
            # After TP/SL fills, next cycle picks up the queue and opens new side.
            if _EC_OK and _contract is not None:
                _pos_tf = prev_pos.get('tf', '15m')
                _ok = _contract.queue_reversal(coin, 'SELL', 'signal_reversal',
                                               position_tf=_pos_tf, incoming_tf='15m')
                if _ok:
                    log(f"{coin} SELL reversal QUEUED (contract, pos_tf={_pos_tf}): {reason}")
                else:
                    log(f"{coin} SELL reversal BLOCKED by TF isolation (pos_tf={_pos_tf}): {reason}")
                return
            # If contract module unavailable, fall back to old behavior
            prev_pos['exit_reason'] = 'signal_reversal'
            log(f"{coin} SELL reversal ALLOWED (no contract): {reason}")
            pnl_pct = close(coin)
            if pnl_pct is not None:
                record_close(prev_pos, coin, pnl_pct, state)
                if pnl_pct < 0: state['consec_losses'] += 1; risk_ladder.record_trade(False)
                else: state['consec_losses'] = 0; risk_ladder.record_trade(True)
                state['last_pnl_close'] = pnl_pct
        if not live or live['size']>0:
            px = get_mid(coin)
            if px:
                # Tier-priority bump: if margin might reject, close lower-tier positions first
                try_tier_bump(coin, state, live_positions)
                # CRITICAL: compute sz ONCE. calc_size reads live news/whale/CVD/OI state
                # and is not deterministic across calls — calling it twice produces
                # DIFFERENT sizes, causing SL/TP to be placed with wrong quantity and
                # rejected as reduce_only violations (bug fix 2026-04-22: 5 of 6 new
                # shorts opened with TP but no SL because re-computed sz differed).
                sz = calc_size(equity, px, risk_pct, risk_mult, coin=coin, side=sig)
                # STEP 2: generate trade identity before placing
                _trade_id = _ledger.new_trade_id() if _LEDGER_OK and _ledger else None
                _cloid = (f"{_trade_id[:8]}{coin[:4]}{'S'}"[:16]) if _trade_id else None
                # 2026-04-25: route entry through _dispatch_entry. When
                # USE_ATOMIC_EXEC=1, this submits entry+SL+TP via bulk_orders
                # (one atomic call) and atomic_used=True; we then skip
                # enforce_position_protection. When flag=0 (default),
                # dispatcher calls legacy place() and atomic_used=False;
                # enforce runs normally.
                _dr = _dispatch_entry(coin, False, sz, cloid=_cloid, trade_id=_trade_id)
                fill_px = _dr['fill_px']
                _atomic_used = _dr['atomic_used']
                _atomic_sl_pct = _dr.get('sl_pct')
                _atomic_tp_pct = _dr.get('tp_pct')
                if fill_px:
                    # STEP 2: ENTRY event to ledger — binds identity before any further writes
                    if _LEDGER_OK and _ledger is not None and _trade_id:
                        try:
                            _ledger.append_entry(
                                coin=coin, side='SELL', entry_price=fill_px,
                                engine=signal_engine, source='precog_signal',
                                sl_pct=None, tp_pct=None,  # populated below after EP
                                cloid=_cloid, trade_id=_trade_id,
                                regime=_current_regime(),
                            )
                        except Exception as _le:
                            log(f"[ledger] append_entry err {coin}: {_le}")
                    if _INV_OK and _invariants is not None:
                        try: _invariants.record_action(coin, 'entry', size_before=0, size_after=sz, origin='precog_15m_sell')
                        except Exception: pass
                    # ENFORCE PROTECTION: skip when atomic already placed bracket.
                    # Atomic path: SL+TP submitted in same bulk_orders as entry —
                    # there's no race window. Legacy: place SL/TP via enforce.
                    if _atomic_used:
                        _sl_pct_used = _atomic_sl_pct
                        _tp_pct_used = _atomic_tp_pct
                    else:
                        _ep_result = enforce_position_protection(coin, False, fill_px, origin='precog_15m_sell')
                        _sl_pct_used = _ep_result.get('sl_pct')
                        _tp_pct_used = _ep_result.get('tp_pct')
                        if _INV_OK and _invariants is not None:
                            try:
                                _invariants.record_action(coin, 'sl_place', size_after=_ep_result.get('actual_size') or sz, origin='precog_15m_sell', detail={'sl_pct': _sl_pct_used, 'ep_replaced': _ep_result.get('replaced')})
                                _invariants.record_action(coin, 'tp_place', size_after=_ep_result.get('actual_size') or sz, origin='precog_15m_sell', detail={'tp_pct': _tp_pct_used, 'ep_replaced': _ep_result.get('replaced')})
                            except Exception: pass
                    # LEDGER: record actual sl/tp/edge now that protection is placed
                    if _LEDGER_OK and _ledger is not None and _trade_id:
                        try:
                            _edge = _gates.compute_expected_edge(_tp_pct_used, _sl_pct_used) \
                                if (_GATES_OK and _tp_pct_used and _sl_pct_used) else None
                            _ledger.update_entry_fields(_trade_id, sl_pct=_sl_pct_used,
                                tp_pct=_tp_pct_used, expected_edge_at_entry=_edge,
                                realized_slippage_pct=_dr.get('realized_slippage_pct'))
                        except Exception as _le:
                            log(f"[ledger] update_entry_fields err {coin}: {_le}")
                    # CONTRACT: both TP and SL must be on exchange. If either
                    # is None, emergency close immediately (naked position).
                    if _EC_OK and _contract is not None:
                        try:
                            ok = _contract.ensure_tp_sl_placed(
                                coin, _tp_pct_used, _sl_pct_used, close)
                            if not ok:
                                log(f"{coin} CONTRACT: position closed due to TP/SL failure")
                                state['positions'].pop(coin, None)
                                return
                        except Exception as _ce:
                            log(f"{coin} contract enforcement err: {_ce}")
                    log_trade('HL', coin, 'SELL', fill_px, 0, 'precog_signal')
                    # sigs and other context for post-mortem agents
                    try:
                        _sigs_for_pm = list((percoin_configs.get_config(coin) or {}).get('sigs', [])) if percoin_configs.ELITE_MODE else None
                    except Exception:
                        _sigs_for_pm = None
                    state['positions'][coin] = {'side':'S', 'opened_at':now, 'entry':fill_px,
                                                'stage':'initial', 'peak':fill_px,
                                                'engine':signal_engine, 'conf':conf_score,
                                                'utc_h': time.gmtime(now).tm_hour,
                                                'sl_pct': _sl_pct_used,
                                                'tp_pct': _tp_pct_used,
                                                'sigs': _sigs_for_pm,
                                                'size': sz,
                                                'tf': '15m',
                                                'trade_id': _trade_id,
                                                'cloid': _cloid}
                    # Register experimental promotion if pending
                    if coin in _EXPERIMENT_PENDING and _PROMO_OK and _promo is not None:
                        _exp_tag = _EXPERIMENT_PENDING.pop(coin)
                        state['positions'][coin]['experimental'] = True
                        state['positions'][coin]['exp_tag'] = _exp_tag
                        try:
                            _promo.record_promotion(coin, 'SELL', _exp_tag)
                        except Exception as _pe:
                            print(f'[precog] promo record err: {_pe}', flush=True)
                    # Deadlock valve tracker — successful entry resets timer
                    _LAST_OPEN_TS = time.time()
    else:
        state['cooldowns'][coin+'_buy'] = bar_ts
        if live and live['size']<0:
            prev_pos = dict(state.get('positions', {}).get(coin, {}))
            # Guard: don't flip a winning short on a weak BUY signal
            allow, reason = _allow_reversal({**live, 'coin':coin}, prev_pos)
            if not allow:
                log(f"{coin} BUY reversal SKIPPED: short position held ({reason})")
                return
            # CONTRACT: queue reversal; do NOT close existing position
            if _EC_OK and _contract is not None:
                _pos_tf = prev_pos.get('tf', '15m')
                _ok = _contract.queue_reversal(coin, 'BUY', 'signal_reversal',
                                               position_tf=_pos_tf, incoming_tf='15m')
                if _ok:
                    log(f"{coin} BUY reversal QUEUED (contract, pos_tf={_pos_tf}): {reason}")
                else:
                    log(f"{coin} BUY reversal BLOCKED by TF isolation (pos_tf={_pos_tf}): {reason}")
                return
            prev_pos['exit_reason'] = 'signal_reversal'
            log(f"{coin} BUY reversal ALLOWED (no contract): {reason}")
            pnl_pct = close(coin)
            if pnl_pct is not None:
                record_close(prev_pos, coin, pnl_pct, state)
                if pnl_pct < 0: state['consec_losses'] += 1; risk_ladder.record_trade(False)
                else: state['consec_losses'] = 0; risk_ladder.record_trade(True)
                state['last_pnl_close'] = pnl_pct
        if not live or live['size']<0:
            px = get_mid(coin)
            if px:
                # Tier-priority bump: if margin might reject, close lower-tier positions first
                try_tier_bump(coin, state, live_positions)
                # CRITICAL: compute sz ONCE — calc_size is not deterministic (see short path above)
                sz = calc_size(equity, px, risk_pct, risk_mult, coin=coin, side=sig)
                # STEP 2: generate trade identity before placing
                _trade_id = _ledger.new_trade_id() if _LEDGER_OK and _ledger else None
                _cloid = (f"{_trade_id[:8]}{coin[:4]}{'L'}"[:16]) if _trade_id else None
                # 2026-04-25: route entry through _dispatch_entry. See SELL site for rationale.
                _dr = _dispatch_entry(coin, True, sz, cloid=_cloid, trade_id=_trade_id)
                fill_px = _dr['fill_px']
                _atomic_used = _dr['atomic_used']
                _atomic_sl_pct = _dr.get('sl_pct')
                _atomic_tp_pct = _dr.get('tp_pct')
                if fill_px:
                    # STEP 2: ENTRY event to ledger
                    if _LEDGER_OK and _ledger is not None and _trade_id:
                        try:
                            _ledger.append_entry(
                                coin=coin, side='BUY', entry_price=fill_px,
                                engine=signal_engine, source='precog_signal',
                                sl_pct=None, tp_pct=None,
                                cloid=_cloid, trade_id=_trade_id,
                                regime=_current_regime(),
                            )
                        except Exception as _le:
                            log(f"[ledger] append_entry err {coin}: {_le}")
                    if _INV_OK and _invariants is not None:
                        try: _invariants.record_action(coin, 'entry', size_before=0, size_after=sz, origin='precog_15m_buy')
                        except Exception: pass
                    # ENFORCE PROTECTION: skip when atomic placed bracket atomically.
                    if _atomic_used:
                        _sl_pct_used = _atomic_sl_pct
                        _tp_pct_used = _atomic_tp_pct
                    else:
                        _ep_result = enforce_position_protection(coin, True, fill_px, origin='precog_15m_buy')
                        _sl_pct_used = _ep_result.get('sl_pct')
                        _tp_pct_used = _ep_result.get('tp_pct')
                        if _INV_OK and _invariants is not None:
                            try:
                                _invariants.record_action(coin, 'sl_place', size_after=_ep_result.get('actual_size') or sz, origin='precog_15m_buy', detail={'sl_pct': _sl_pct_used, 'ep_replaced': _ep_result.get('replaced')})
                                _invariants.record_action(coin, 'tp_place', size_after=_ep_result.get('actual_size') or sz, origin='precog_15m_buy', detail={'tp_pct': _tp_pct_used, 'ep_replaced': _ep_result.get('replaced')})
                            except Exception: pass
                    # LEDGER: record actual sl/tp/edge now that protection is placed
                    if _LEDGER_OK and _ledger is not None and _trade_id:
                        try:
                            _edge = _gates.compute_expected_edge(_tp_pct_used, _sl_pct_used) \
                                if (_GATES_OK and _tp_pct_used and _sl_pct_used) else None
                            _ledger.update_entry_fields(_trade_id, sl_pct=_sl_pct_used,
                                tp_pct=_tp_pct_used, expected_edge_at_entry=_edge,
                                realized_slippage_pct=_dr.get('realized_slippage_pct'))
                        except Exception as _le:
                            log(f"[ledger] update_entry_fields err {coin}: {_le}")
                    # CONTRACT: enforce TP/SL presence post-entry
                    if _EC_OK and _contract is not None:
                        try:
                            ok = _contract.ensure_tp_sl_placed(
                                coin, _tp_pct_used, _sl_pct_used, close)
                            if not ok:
                                log(f"{coin} CONTRACT: position closed due to TP/SL failure")
                                state['positions'].pop(coin, None)
                                return
                        except Exception as _ce:
                            log(f"{coin} contract enforcement err: {_ce}")
                    log_trade('HL', coin, 'BUY', fill_px, 0, 'precog_signal')
                    try:
                        _sigs_for_pm = list((percoin_configs.get_config(coin) or {}).get('sigs', [])) if percoin_configs.ELITE_MODE else None
                    except Exception:
                        _sigs_for_pm = None
                    state['positions'][coin] = {'side':'L', 'opened_at':now, 'entry':fill_px,
                                                'stage':'initial', 'peak':fill_px,
                                                'engine':signal_engine, 'conf':conf_score,
                                                'utc_h': time.gmtime(now).tm_hour,
                                                'sl_pct': _sl_pct_used,
                                                'tp_pct': _tp_pct_used,
                                                'sigs': _sigs_for_pm,
                                                'size': sz,
                                                'tf': '15m',
                                                'trade_id': _trade_id,
                                                'cloid': _cloid}
                    # Register experimental promotion if pending
                    if coin in _EXPERIMENT_PENDING and _PROMO_OK and _promo is not None:
                        _exp_tag = _EXPERIMENT_PENDING.pop(coin)
                        state['positions'][coin]['experimental'] = True
                        state['positions'][coin]['exp_tag'] = _exp_tag
                        try:
                            _promo.record_promotion(coin, 'BUY', _exp_tag)
                        except Exception as _pe:
                            print(f'[precog] promo record err: {_pe}', flush=True)
                    # Deadlock valve tracker — successful entry resets timer
                    _LAST_OPEN_TS = time.time()

# ═══════════════════════════════════════════════════════

# MAIN LOOP
# ═══════════════════════════════════════════════════════
state = {'consec_losses': 0, 'cooldowns': {}, 'coin_hist': {}, 'coin_kill': {}}

def main():
    global state, _LAST_OPEN_TS
    log(f"PreCog v8.28 | {WALLET} | risk={INITIAL_RISK_PCT} trail={TRAIL_PCT} V3={V3_HTF}/{V3_EMA}")

    # SURVIVAL GUARDS bootstrap: load per-coin WR from trades.csv
    try:
        _bootstrap_coin_wr_from_ledger()
    except Exception as _bs_e:
        log(f"survival bootstrap err: {_bs_e}")

    # INVARIANT DEADMAN: scans every 30s for naked positions, recreates missing
    # TP/SL, emergency-closes if SL missing > 60s.
    if _INV_OK and _invariants is not None:
        try:
            def _open_orders_live():
                """Pull current open trigger orders on HL."""
                try:
                    return _cached_frontend_orders() or []
                except Exception:
                    return []
            _invariants.start_deadman_daemon(
                live_positions_fn=get_all_positions_live,
                get_open_orders_fn=_open_orders_live,
                place_tp_fn=place_native_tp,
                place_sl_fn=place_native_sl,
                close_fn=close,
                enforce_fn=enforce_position_protection,  # PHASE 1: daemon repairs via enforce
            )
        except Exception as e:
            log(f"deadman daemon start err: {e}")

    try: bybit_ws.start()
    except Exception as e: log(f"bybit_ws err: {e}")

    # STEP 1 LIFECYCLE: start exchange snapshot daemon (read-only)
    if _SNAPSHOT_OK and _snapshot is not None:
        try:
            def _user_state_wrapper(w):
                # 2026-04-25: throttle alongside candle fetches to avoid
                # exchange_snapshot flooding HL during snapshot build window.
                try: _hl_throttle()
                except Exception: pass
                return info.user_state(w)
            def _user_fills_wrapper(w, start_ms, end_ms):
                try:
                    try: _hl_throttle()
                    except Exception: pass
                    import urllib.request, json as _json
                    body = _json.dumps({'type':'userFillsByTime','user':w,
                                        'startTime':start_ms,'endTime':end_ms}).encode()
                    req = urllib.request.Request('https://api.hyperliquid.xyz/info',
                        data=body, headers={'Content-Type':'application/json'})
                    with urllib.request.urlopen(req, timeout=5) as r:
                        return _json.loads(r.read())
                except Exception: return []
            _snapshot.start(
                user_state_fn=_user_state_wrapper,
                user_fills_fn=_user_fills_wrapper,
                wallet=WALLET,
                refresh_interval_sec=10,
            )
        except Exception as e: log(f"snapshot start err: {e}")

    # STEP 3: start lifecycle reconciler (observe mode until RECONCILER_AUTHORITATIVE=1)
    if _RECONCILER_OK and _reconciler is not None and _LEDGER_OK and _INTENTS_OK:
        try:
            def _execute_close_on_exchange(coin):
                """RAW exchange close via _close_direct.
                Returns fill_px (real exchange fill) or None (failure).

                2026-04-25 (final): exit price comes ONLY from the actual
                exchange fill stored in _LAST_CLOSE_FILL. NEVER from get_mid
                or any inferred/snapshot/LKG source. If _close_direct succeeded
                but no fill was stashed within 3s, we treat it as a half-fail
                and return None — better to retry next cycle than write a fake
                exit price into the ledger.

                CRITICAL: returning a non-None value when _close_direct failed caused the
                HMSTR-12-closes-in-4-min loop (ledger marked closed, exchange still open,
                re-adoption next cycle).
                """
                try:
                    result = _close_direct(coin)
                    if result is None:
                        log(f"[reconciler] _close_direct returned None for {coin} — "
                            f"will NOT write ledger close (reconciler retry next cycle)")
                        return None
                    # _close_direct succeeded. Read REAL fill from stash.
                    fill = _LAST_CLOSE_FILL.get(coin)
                    if not fill:
                        log(f"[reconciler] {coin} no stashed fill — refusing inferred price, retry next cycle")
                        return None
                    if (time.time() - fill.get('ts', 0)) > 5:
                        log(f"[reconciler] {coin} stashed fill stale ({time.time()-fill['ts']:.1f}s) — retry")
                        return None
                    fill_px = fill.get('fill_px') or 0
                    if fill_px <= 0:
                        log(f"[reconciler] {coin} stashed fill_px invalid ({fill_px}) — retry")
                        return None
                    return float(fill_px)
                except Exception as e:
                    log(f"[reconciler] execute_close err {coin}: {e}")
                    return None
            _reconciler.start(deps={
                'ledger': _ledger,
                'snapshot': _snapshot,
                'intent_queue': _intents,
                'close_trade_fn': close_trade,
                'execute_close_fn': _execute_close_on_exchange,
                'log_fn': log,
                'state': state,
            }, interval_sec=15)
        except Exception as e: log(f"reconciler start err: {e}")
    try: orderbook_ws.start()
    except Exception as e: log(f"orderbook_ws err: {e}")
    try: liquidation_ws.start()
    except Exception as e: log(f"liq_ws err: {e}")
    try: whale_filter.start()
    except Exception as e: log(f"whale_filter err: {e}")
    try: cvd_ws.start()
    except Exception as e: log(f"cvd_ws err: {e}")
    try: oi_tracker.start()
    except Exception as e: log(f"oi_tracker err: {e}")
    try: threading.Timer(60.0, funding_arb.refresh).start()
    except Exception as e: log(f"funding_arb err: {e}")
    # Funding refresh deferred — first tick runs it after 30s delay
    threading.Timer(30.0, lambda: funding_filter.refresh_all(COINS)).start()
    try: news_filter.start()
    except Exception as e: log(f"news err: {e}")
    try: leverage_map.refresh(info)
    except Exception as e: log(f"lev refresh err: {e}")
    log(f"Universe ({len(COINS)}): {COINS}")
    log(f"Chase-gate ({len(CHASE_GATE_COINS)}): {sorted(CHASE_GATE_COINS)}")
    log(f"Risk: {int(INITIAL_RISK_PCT*100)}% → {int(SCALED_RISK_PCT*100)}% at ${SCALE_DOWN_AT}")
    log(f"Caps: max_pos={MAX_POSITIONS} side={MAX_SAME_SIDE} margin={int(MAX_TOTAL_RISK*100)}%")
    log(f"Safety: max_hold={MAX_HOLD_SEC/3600:.0f}h | CB={CB_CONSEC_LOSSES} losses→{CB_PAUSE_SEC/60:.0f}min pause")
    log(f"Funding cut ratio: {FUNDING_CUT_RATIO*100:.0f}%")
    log(f"Grid: {GRID}")
    log(f"Derived: pivot_lb={SP['pivot_lb']} rsi_lo={BP['rsi_lo']} rsi_hi={SP['rsi_hi']} cd={SP['cd']}")

    while True:
        try:
            # FIX #8: Kill switch check first
            if kill_switch_active():
                log("KILL SWITCH DETECTED — flattening all positions and exiting")
                flatten_all('KILL')
                log("Kill complete. Remove /var/data/KILL to restart.")
                while kill_switch_active():
                    time.sleep(30)
                log("Kill switch cleared — resuming")

            state = load_state()
            equity = get_balance()
            # ACCOUNT DRAWDOWN BREAKER — flatten if equity drops 15% from session high
            session_hwm = state.get('session_hwm', equity)
            if equity > session_hwm:
                state['session_hwm'] = equity
                session_hwm = equity
            # Monitor health check (non-blocking)
            try:
                import monitor
                live_pos = get_all_positions_live() or {}
                monitor.check_health(equity, session_hwm, live_pos)
            except Exception: pass
            dd = (session_hwm - equity) / session_hwm if session_hwm > 0 else 0
            if dd >= 0.15:
                log(f"!!! ACCOUNT DRAWDOWN {dd*100:.1f}% (hwm=${session_hwm:.2f} now=${equity:.2f}) — FLATTENING ALL")
                flatten_all('DRAWDOWN')
                state['cb_pause_until'] = time.time() + CB_PAUSE_SEC
                state['session_hwm'] = equity  # reset hwm after flatten
                save_state(state)
                time.sleep(30)
                continue
            now = time.time()

            # FIX #5: Circuit breaker check
            if now < state.get('cb_pause_until', 0):
                remaining = (state['cb_pause_until'] - now) / 60
                log(f"--- CIRCUIT BREAKER active: {remaining:.0f}min remaining (consec losses: {state['consec_losses']}) ---")
                time.sleep(LOOP_SEC)
                continue

            if state.get('consec_losses', 0) >= CB_CONSEC_LOSSES:
                log(f"!!! CIRCUIT BREAKER TRIPPED: {state['consec_losses']} consecutive losses. Pausing {CB_PAUSE_SEC/60:.0f}min !!!")
                state['cb_pause_until'] = now + CB_PAUSE_SEC
                state['consec_losses'] = 0  # reset after pause starts
                save_state(state)
                time.sleep(LOOP_SEC)
                continue

            # FIX #4: Reconcile state with HL reality
            live_positions = get_all_positions_live()
            # Drop phantoms (state has it, HL doesn't)
            for k in list(state['positions'].keys()):
                if state['positions'][k] and k not in live_positions:
                    log(f"RECONCILE: phantom {k} cleared (may be liquidation or native SL)")
                    # STEP 2: also close in ledger if trade_id known (reconciler_missing reason)
                    _phantom_tid = state['positions'][k].get('trade_id')
                    if _phantom_tid and _LEDGER_OK and _ledger is not None:
                        try:
                            if not _ledger.is_closed(_phantom_tid):
                                _phantom_funding = _funding_for_close(_ledger.get_by_trade_id(_phantom_tid))
                                _ledger.append_close(
                                    trade_id=_phantom_tid,
                                    exit_price=None,
                                    pnl=None,
                                    close_reason='reconcile_phantom_clear',
                                    source='reconcile',
                                    funding_paid_pct=_phantom_funding,
                                )
                                log(f"[ledger] phantom {k} trade_id={_phantom_tid[:8]} marked closed")
                        except Exception as _le:
                            log(f"[ledger] phantom close err {k}: {_le}")
                    state['positions'].pop(k)
            # Track live-only positions (HL has it, state doesn't)
            for k in live_positions:
                if k not in state['positions']:
                    side = 'L' if live_positions[k]['size']>0 else 'S'
                    entry_px = live_positions[k]['entry']
                    # STEP 4 FIX: orphan adoption must check ledger first.
                    # Before Step 4: every adoption created a new trade_id, causing
                    # duplicate ledger entries across restarts (18 ledger opens vs 9 exch).
                    # Fix: reuse existing open trade_id from ledger if present.
                    _adopt_trade_id = None
                    if _LEDGER_OK and _ledger is not None:
                        try:
                            # Check if ledger already has an open trade for this coin
                            _existing_tid = _ledger.latest_open_trade_id_for_coin(k)
                            if _existing_tid:
                                _adopt_trade_id = _existing_tid
                                log(f"RECONCILE LEDGER: reusing existing open trade_id={_existing_tid[:8]} for {k}")
                            else:
                                _adopt_trade_id = _ledger.new_trade_id()
                                _ledger.append_entry(
                                    coin=k,
                                    side='BUY' if side == 'L' else 'SELL',
                                    entry_price=entry_px,
                                    engine='RECONCILED',
                                    source='reconcile_adopt',
                                    cloid=None,
                                    trade_id=_adopt_trade_id,
                                    regime=_current_regime(),
                                )
                                log(f"RECONCILE LEDGER: adopted NEW {k} as trade_id={_adopt_trade_id[:8]}")
                        except Exception as _le:
                            log(f"[ledger] reconcile adopt err {k}: {_le}")
                    else:
                        log(f"ORPHAN POSITION: {k} on exchange — ledger unavailable, cannot bind identity")
                    # opened_at = now (not now-3600). Previous value guaranteed
                    # dust-sweep would kill adopted positions on the same tick
                    # because age would immediately exceed DUST_MIN_AGE_SEC (30min).
                    # Post-deploy positions get a fresh 30min grace window.
                    state['positions'][k] = {'side':side, 'opened_at':now, 'entry':entry_px,
                                             'stage':'initial', 'peak':entry_px,
                                             'engine':'RECONCILED', 'source':'reconcile',
                                             'trade_id': _adopt_trade_id}
                    log(f"RECONCILE: adopting existing {k} {side} (fresh 30min grace window)")
                    if _INV_OK and _invariants is not None:
                        try: _invariants.record_action(k, 'reconcile_adopt', size_after=abs(live_positions[k]['size']), origin='reconcile_loop')
                        except Exception: pass
                    # ENFORCE PROTECTION: adopted positions may be naked.
                    # Fire protection enforcement; will no-op if TP+SL already sized correctly.
                    try:
                        _is_long_adopt = (side == 'L')
                        _ep_res = enforce_position_protection(k, _is_long_adopt, entry_px, origin='reconcile_adopt')
                        if _ep_res.get('replaced'):
                            log(f"RECONCILE {k}: protection enforced (was naked or mis-sized)")
                    except Exception as _ee:
                        log(f"RECONCILE {k}: enforce_protection err: {_ee}")

            # ─── SURVIVAL GUARD 1: PROFIT LOCK (2026-04-25) ───
            # Per-tick check on every open position. RAW price move:
            #   raw >= 2% in favor → emit FORCE_CLOSE intent (lock profit)
            #   raw >= 1% in favor → move SL to breakeven if not already
            # Tier-agnostic. Uses intent queue, not direct exchange calls.
            try:
                if PROFIT_LOCK_ENABLED:
                    for _pl_coin, _pl_lp in list(live_positions.items()):
                        try:
                            _pl_mark = get_mid(_pl_coin)
                            if not _pl_mark:
                                continue
                            # MFE/MAE tracking — high/low water of raw move during hold.
                            # Updates state['positions'][coin] which the close path
                            # reads to populate trade_ledger CLOSE row.
                            try:
                                _mm_pos = state.get('positions', {}).get(_pl_coin, {})
                                _mm_entry = float(_mm_pos.get('entry') or _pl_lp.get('entry') or 0)
                                _mm_side = _mm_pos.get('side') or ('L' if float(_pl_lp.get('size', 0)) > 0 else 'S')
                                if _mm_entry > 0:
                                    _mm_raw = ((_pl_mark - _mm_entry) / _mm_entry) if _mm_side in ('L', 'BUY') else ((_mm_entry - _pl_mark) / _mm_entry)
                                    if 'mfe_pct' not in _mm_pos:
                                        _mm_pos['mfe_pct'] = 0.0
                                    if 'mae_pct' not in _mm_pos:
                                        _mm_pos['mae_pct'] = 0.0
                                    if _mm_raw > _mm_pos['mfe_pct']:
                                        _mm_pos['mfe_pct'] = _mm_raw
                                    if _mm_raw < _mm_pos['mae_pct']:
                                        _mm_pos['mae_pct'] = _mm_raw
                            except Exception:
                                pass
                            _pl_action = _profit_lock_check(_pl_coin, _pl_lp, _pl_mark)
                            if _pl_action == 'force_close':
                                # Emit intent — reconciler resolves
                                if _INTENTS_OK and _intents is not None:
                                    _pl_tid = None
                                    if _LEDGER_OK and _ledger is not None:
                                        try:
                                            _pl_tid = _ledger.latest_open_trade_id_for_coin(_pl_coin)
                                        except Exception: pass
                                    _intents.emit('FORCE_CLOSE', _pl_coin,
                                                  trade_id=_pl_tid,
                                                  reason='profit_lock_2pct')
                                    log(f"{_pl_coin} PROFIT_LOCK fired: raw move ≥{PROFIT_LOCK_CLOSE_PCT*100:.1f}% — FORCE_CLOSE emitted")
                            elif _pl_action == 'move_be':
                                # Move SL to breakeven if not already there
                                _pl_state = state.get('positions', {}).get(_pl_coin, {})
                                if not _pl_state.get('sl_at_be'):
                                    try:
                                        _pl_entry = float(_pl_lp.get('entry') or 0)
                                        _pl_size = abs(_pl_lp.get('size', 0))
                                        _pl_is_long = _pl_lp.get('size', 0) > 0
                                        if _pl_entry > 0 and _pl_size > 0:
                                            modify_sl_to_breakeven(_pl_coin, _pl_entry,
                                                                   _pl_size, _pl_is_long,
                                                                   buffer_pct=0.002)
                                            _pl_state['sl_at_be'] = True
                                            state.setdefault('positions', {})[_pl_coin] = _pl_state
                                            log(f"{_pl_coin} PROFIT_LOCK BE: raw move ≥{PROFIT_LOCK_BE_PCT*100:.1f}% — SL moved to entry")
                                    except Exception as _be_e:
                                        log(f"{_pl_coin} profit_lock BE err: {_be_e}")

                            # ─── TRAIL SL LADDER ──────────────────────
                            # 2026-04-26: data showed avg trade reached MFE
                            # ~1-2% then retraced. With BE-lock-only, those
                            # round-tripped to entry + 0.2% buffer (~$0.02).
                            # Trail ladder locks more profit per rung as MFE
                            # grows. Each rung is permanent; SL never moves
                            # back. /analyze TP backtest can be re-run after
                            # 24h to see if MFE distribution shifts up.
                            try:
                                _pl_state = state.get('positions', {}).get(_pl_coin, {})
                                _trail_level = int(_pl_state.get('sl_trail_level', 0))
                                _cur_mfe = (_mm_pos.get('mfe_pct') or 0) if _mm_pos else 0
                                for _ti, (_t_mfe, _t_lock) in enumerate(TRAIL_LADDER):
                                    if _ti < _trail_level:
                                        continue
                                    if _cur_mfe >= _t_mfe:
                                        _pl_entry = float(_pl_lp.get('entry') or 0)
                                        _pl_size = abs(_pl_lp.get('size', 0))
                                        _pl_is_long = _pl_lp.get('size', 0) > 0
                                        if _pl_entry > 0 and _pl_size > 0:
                                            modify_sl_to_breakeven(_pl_coin, _pl_entry,
                                                                   _pl_size, _pl_is_long,
                                                                   buffer_pct=_t_lock)
                                            _pl_state['sl_trail_level'] = _ti + 1
                                            state.setdefault('positions', {})[_pl_coin] = _pl_state
                                            log(f"{_pl_coin} TRAIL_SL rung {_ti+1}/{len(TRAIL_LADDER)}: "
                                                f"MFE {_cur_mfe*100:.2f}% ≥ {_t_mfe*100:.1f}% — "
                                                f"SL → entry+{_t_lock*100:.1f}%")
                                        break  # one rung per tick
                            except Exception as _tr_e:
                                log(f"{_pl_coin} trail_sl err: {_tr_e}")
                        except Exception as _pl_inner:
                            log(f"profit_lock {_pl_coin} err: {_pl_inner}")
            except Exception as _pl_outer:
                log(f"profit_lock loop err: {_pl_outer}")
            # ──────────────────────────────────────────────────

            # PROFIT MANAGEMENT: check every open 15m position for TP1 partial
            # exit or TP extension. Runs each tick. Uses exchange-side order
            # modifications (not polling-close). TF-scoped to 15m positions only.
            try:
                run_profit_management(state, live_positions)
            except Exception as _pme:
                log(f"profit_mgmt loop err: {_pme}")

            # DUST-SWEEP — DISABLED 2026-04-22.
            # POSTMORTEM AUDIT of 51 trades: 45 dust_sweep exits, ZERO TP hits, 1 SL hit.
            # Distribution: 100% of dust exits within ±1.0% of entry, 78% within ±0.3%.
            # Signal edge is ~0.6-1% but dust_sweep at ±0.13% of notional ($0.10 floor on
            # $70 positions) snipped every winner at the noise level. Even FOGO (id=51),
            # the first trade that passed ALL new filters with correctly-attached native
            # SL+TP, dust-swept at -0.148% in 31 min — never tested either native order.
            #
            # Now that place_native_sl/tp reliably attach to every position (b8f5b9a5),
            # dust_sweep has no value. Native SL/TP handles exits. Winners can reach
            # their 0.6-1.2% TP, losers bounded by 5% SL. Real expectancy finally visible.
            #
            # Env override: DUST_SWEEP_ENABLED=1 re-enables the legacy behavior.
            if os.environ.get('DUST_SWEEP_ENABLED', '0') == '1':
                DUST_THRESHOLD_FIXED = 0.10
                DUST_THRESHOLD_PCT = 0.001
                DUST_MIN_AGE_SEC = 1800
                now_ts = time.time()
                swept = 0
                for k in list(live_positions.keys()):
                    try:
                        lp = live_positions[k]
                        sz = lp.get('size', 0)
                        entry = lp.get('entry', 0)
                        if sz == 0 or not entry: continue
                        pos_tier = percoin_configs.get_tier(k) if percoin_configs.ELITE_MODE else None
                        if pos_tier == 'PURE': continue
                        pos_state = state.get('positions', {}).get(k, {})
                        opened_at = pos_state.get('opened_at', now_ts)
                        age_sec = now_ts - opened_at
                        if age_sec < DUST_MIN_AGE_SEC: continue
                        unrealized_usd = lp.get('pnl', 0)
                        notional = abs(sz) * entry
                        dust_threshold = max(DUST_THRESHOLD_FIXED, notional * DUST_THRESHOLD_PCT)
                        if abs(unrealized_usd) <= dust_threshold:
                            try:
                                pnl = close(k)
                                log(f"DUST-SWEEP {k} ({pos_tier or 'NONE'}) pnl=${unrealized_usd:+.3f} threshold=${dust_threshold:.3f} age={age_sec/60:.0f}min notional=${notional:.0f} (freeing margin)")
                                if pnl is not None:
                                    try:
                                        pos_for_pm = dict(pos_state)
                                        pos_for_pm['exit_reason'] = 'dust_sweep'
                                        pos_for_pm['dust_age_sec'] = age_sec
                                        record_close(pos_for_pm, k, pnl, state)
                                    except Exception as _e:
                                        log(f"dust-sweep postmortem hook err {k}: {_e}")
                                state['positions'].pop(k, None)
                                if pnl is not None:
                                    state['last_pnl_close'] = pnl
                                    if pnl > 0: state['consec_losses'] = 0
                                swept += 1
                            except Exception as e:
                                log(f"dust-sweep err {k}: {e}")
                    except Exception as e:
                        log(f"dust-sweep scan err {k}: {e}")
                if swept: log(f"DUST-SWEEP: closed {swept} stale positions (|PnL|<=max($0.10,0.1%%notional), age>={DUST_MIN_AGE_SEC/60:.0f}min)")

            # Wall-as-TP check — ADVISORY under contract. Disabled when contract active.
            # Exchange TP/SL is primary; wall exits bypass the contract hierarchy.
            _wall_exit_enabled = not (_EC_OK and _contract is not None)
            if _wall_exit_enabled:
                for k, lp in live_positions.items():
                    try:
                        side_long = lp['size']>0
                        wall_side = 'ask' if side_long else 'bid'
                        wall = orderbook_ws.get_nearest_wall(k, wall_side)
                        if not wall: continue
                        cp = get_mid(k)
                        if not cp: continue
                        # LONG reaches ask wall (resistance) OR SHORT reaches bid wall (support)
                        if side_long and cp >= wall['price'] * 1.002:  # 0.2% past wall, not just touching
                            log(f"WALL-TP {k} LONG reached ask wall ${wall['usd']/1000:.0f}k @ {wall['price']}")
                            close(k)
                        elif not side_long and cp <= wall['price'] * 0.998:  # 0.2% past wall
                            log(f"WALL-TP {k} SHORT reached bid wall ${wall['usd']/1000:.0f}k @ {wall['price']}")
                            close(k)
                    except Exception as e:
                        pass

            # Wall-break auto-exit — ADVISORY under contract
            if _wall_exit_enabled:
                wall_ents = state.get('wall_entries', {})
                for wcoin, wdata in list(wall_ents.items()):
                    if wcoin not in live_positions:
                        wall_ents.pop(wcoin); continue
                    try:
                        cp = get_mid(wcoin)
                        if wall_bounce.wall_broken(wcoin, wdata['side'], wdata['wall_price'], cp):
                            log(f"WALL-BROKEN {wcoin} {wdata['side']} — exiting")
                            close(wcoin)
                            wall_ents.pop(wcoin)
                    except Exception as e:
                        log(f"wall-break check err {wcoin}: {e}")

            # Profit-lock @ 3.0%/2.0% (user override — OOS -25% vs no_plock, but best plock config)
            for k, lp in live_positions.items():
                try:
                    side = 'BUY' if lp['size']>0 else 'SELL'
                    entry = lp['entry']
                    cur_px = get_mid(k) or entry
                    cur_sl = state.get('sl_overrides', {}).get(k)
                    new_sl = profit_lock.compute_new_sl(entry, cur_px, side, cur_sl)
                    if new_sl is not None and not state.get('scaled_out', {}).get(k):
                        try:
                            half_sz = round_size(k, abs(lp['size']) / 2)
                            if half_sz > 0:
                                side_long = lp['size']>0
                                exchange.order(k, not side_long, half_sz,
                                               cur_px * (1.005 if not side_long else 0.995),
                                               {'limit':{'tif':'Ioc'}}, reduce_only=True)
                                state.setdefault('scaled_out', {})[k] = True
                                log(f"SCALE-OUT 50% {k} {side} @ {cur_px:.6f}")
                        except Exception as e:
                            log(f"scale-out err {k}: {e}")
                        state.setdefault('sl_overrides', {})[k] = new_sl
                        log(f"PROFIT-LOCK {k} {side}: SL→{new_sl:.6f}")
                except Exception as e:
                    log(f"profit-lock err {k}: {e}")

            # Spoof scan per open position + near-wall coins
            for k in list(live_positions.keys()):
                try: spoof_detection.scan_walls(k, get_mid(k))
                except Exception: pass

            # Hourly funding refresh (both funding_filter and funding_arb)
            fund_age = time.time() - getattr(main, '_funding_ts', 0)
            if fund_age > 3600:
                try: funding_arb.refresh()
                except Exception: pass
                try: funding_filter.refresh_all(COINS); main._funding_ts = time.time()
                except Exception as e: log(f"funding refresh err: {e}")

            # BTC vol throttle
            risk_mult = 1.0
            # BTC vol throttle — cached, fetch only every 15 min
            btc_vol_age = now - getattr(main, '_btc_vol_ts', 0)
            if btc_vol_age > 900:  # 15 min
                try:
                    btc_c = fetch('BTC')
                    if len(btc_c) >= 12:
                        recent = btc_c[-12:]
                        hi = max(c[2] for c in recent); lo = min(c[3] for c in recent)
                        main._btc_vol = (hi-lo)/lo
                    main._btc_vol_ts = now
                except Exception as e:
                    log(f"btc vol err: {e}")
            btc_range = getattr(main, '_btc_vol', 0)
            if btc_range > BTC_VOL_THRESHOLD:
                risk_mult = 0.5
                log(f"BTC vol {btc_range*100:.1f}% — risk halved")

            cur_risk = current_risk_pct(equity)
            log(f"--- tick eq=${equity:.2f} risk={cur_risk*100:.2f}% mult={risk_mult} pos={len(live_positions)} cL={state['consec_losses']} ---")

            # ─── SNAPSHOT BUILD (single fan-in per tick) ───
            # Replaces the previous 78× per-coin REST cascade with one controlled
            # rebuild per tick cycle. Each signal engine reads from snapshot via
            # get_candles() — zero network calls in hot path. TTL ~60s matches
            # tick cadence; per-coin failures fall back to last-known-good.
            if _SNAPSHOT_OK and _candle_snap is not None:
                try:
                    def _snap_fetch(c, tf, nb):
                        # 2026-04-25: auto-quarantine unknown coins. KeyError
                        # from HL SDK = coin not in exchange universe; no point
                        # retrying every tick. Skip until process restart.
                        if c in _UNKNOWN_COINS:
                            return []
                        # Per-coin 429 cooldown. If this coin 429'd recently,
                        # skip it — retrying inside the same window just adds
                        # CloudFront pressure and gets us blacklisted harder.
                        cold_until = _CANDLE_COLD.get(c, 0)
                        if time.time() < cold_until:
                            return []
                        end = int(time.time() * 1000)
                        # tf to seconds: 15m=900, 1h=3600, 4h=14400
                        tf_sec = {'15m': 900, '1h': 3600, '4h': 14400}.get(tf, 900)
                        start_ms = end - nb * tf_sec * 1000
                        try:
                            d = okx_fetch.fetch_klines(c, tf, nb)
                            if not d:
                                # OKX-side absence (HL-only coin or delisted) is
                                # treated like KeyError from HL SDK — quarantine
                                # so we stop trying this coin every tick.
                                _UNKNOWN_COINS.add(c)
                                log(f"[snapshot] AUTO-QUARANTINE {c}: not on OKX (returns empty)")
                                return []
                        except KeyError as _ke:
                            _UNKNOWN_COINS.add(c)
                            log(f"[snapshot] AUTO-QUARANTINE {c}: not in OKX universe (KeyError {_ke})")
                            return []
                        except Exception as _e:
                            es = str(_e)
                            if '429' in es:
                                # OKX shouldn't 429 us, but keep the cooldown
                                # mechanism in place defensively.
                                _CANDLE_COLD[c] = time.time() + 90
                            raise   # let candle_snapshot.build_snapshot count + log
                        return [(int(b['t']), float(b['o']), float(b['h']),
                                 float(b['l']), float(b['c']), float(b['v'])) for b in d]
                    _candle_snap.build_snapshot(COINS, '15m', _snap_fetch,
                                                 throttle_fn=_hl_throttle,
                                                 n_bars=100, log_fn=log)
                except Exception as _se:
                    log(f"[snapshot] build err (fail-soft): {_se}")

            # Publish cached state for /dash
            try:
                pos_list = []
                for k, v in live_positions.items():
                    side_long = v['size'] > 0
                    entry = v['entry']
                    # TP target: nearest wall (if any) or trail-projected target
                    tp_target = None
                    try:
                        wall = orderbook_ws.get_nearest_wall(k, 'ask' if side_long else 'bid')
                        if wall: tp_target = wall['price']
                    except Exception: pass
                    if not tp_target:
                        # Fallback: entry * (1 + 3*trail) as rough target
                        tp_target = entry * (1.024 if side_long else 0.976)
                    pos_list.append({
                        'coin': k,
                        'side': 'L' if side_long else 'S',
                        'size': abs(v['size']),
                        'entry': entry,
                        'upnl': v.get('upnl', v.get('pnl', 0)),
                        'lev': v.get('lev', 10),
                        'tp': tp_target,
                        'mark': v.get('mark', 0),
                    })
                main._cached_account = {'equity': equity, 'ts': time.time(), 'positions': pos_list}
            except Exception as e: log(f"cache err: {e}")

            # WEBHOOK QUEUE — process DynaPro signals first (higher priority)
            wh_count = 0
            while not WEBHOOK_QUEUE.empty() and wh_count < 10:
                try:
                    sig = WEBHOOK_QUEUE.get_nowait()
                    coin = sig['coin']; action = sig['action']
                    live = live_positions.get(coin)
                    risk_pct = current_risk_pct(equity)

                    if action in ('exit_buy', 'exit_sell'):
                        # Explicit close command → AUTHORIZED (kill_switch_manual)
                        if live:
                            pnl_pct = close(coin)
                            if pnl_pct is not None:
                                if pnl_pct < 0: state['consec_losses'] += 1; update_coin_wr(coin, False, state); risk_ladder.record_trade(False)
                                else: state['consec_losses'] = 0; update_coin_wr(coin, True, state); risk_ladder.record_trade(True)
                            state['positions'].pop(coin, None)
                            log(f"WEBHOOK CLOSE {coin} ({action}) pnl={pnl_pct} [authorized: explicit]")
                    elif action in ('buy', 'sell'):
                        # Opposite-direction webhook on open position
                        if live:
                            is_opposite = (action == 'buy' and live['size'] < 0) or (action == 'sell' and live['size'] > 0)
                            if is_opposite:
                                # CONTRACT: queue reversal, do NOT close existing
                                if _EC_OK and _contract is not None:
                                    _contract.queue_reversal(coin, action.upper(), 'signal_reversal')
                                    log(f"WEBHOOK {coin} {action} reversal QUEUED (contract)")
                                    wh_count += 1; continue
                                close(coin)
                                state['positions'].pop(coin, None)
                            elif (action == 'buy' and live['size'] > 0) or (action == 'sell' and live['size'] < 0):
                                log(f"WEBHOOK {coin} {action} — already positioned, skip")
                                wh_count += 1; continue
                        if len(live_positions) < MAX_POSITIONS:
                            # BYBIT WS lead price for entry trigger (fallback to HL mid)
                            by_px, by_age = bybit_ws.get(coin)
                            hl_px = get_mid(coin)
                            px = by_px if (by_px and by_age is not None and by_age < 3000) else hl_px
                            if px:
                                is_buy = (action == 'buy')
                                side_str = 'BUY' if is_buy else 'SELL'
                                # GATE 1 — webhook must clear same ticker/trend filter as internal signal
                                candles_for_gate = fetch(coin)
                                if not apply_ticker_gate(coin, side_str, px, candles_for_gate):
                                    log(f"WEBHOOK {coin} {side_str} GATED (trend/ticker filter)")
                                    wh_count += 1; continue

                                # GATE 2 — CONVICTION FLOOR. Webhook signals must pass the same
                                # conf=30 floor as internal signals. Without this, TradingView
                                # alerts bypass the sizing filter and trade at default 1.0x
                                # even at near-noise levels. Score the candles, apply floor.
                                wh_size_mult = 1.0
                                wh_conf = 0
                                wh_conf_brk = None
                                try:
                                    _btc_d = btc_correlation.get_state().get('btc_dir', 0)
                                    # Use "BUY"/"SELL" as engine name when scoring (confidence.score
                                    # needs a sig string). It maps to direction-aware scoring.
                                    wh_conf, wh_conf_brk = confidence.score(
                                        candles_for_gate, [], coin, side_str, _btc_d)
                                    import regime_detector as _regdet2
                                    _cur_reg = _regdet2.get_regime()
                                    wh_size_mult = confidence.size_multiplier(wh_conf, _cur_reg)
                                    if wh_size_mult <= 0.0:
                                        log(f"WEBHOOK {coin} {side_str} SKIP: conf={wh_conf} below conviction floor (regime={_cur_reg}) {wh_conf_brk}")
                                        wh_count += 1; continue
                                except Exception as _ce:
                                    log(f"WEBHOOK {coin} conf err: {_ce} — allowing at 1.0x")

                                # GATE 3 — ENTRY LLM GATE. Check KB + vetos + regime before placing.
                                # Fail-open: any error returns ALLOW at 1.0x.
                                # REGIME-SIDE BLOCKER runs FIRST and cannot be overridden.
                                _wrb, _wrb_reason = regime_blocks_side(coin, side_str)
                                if _wrb:
                                    log(f"WEBHOOK {coin} {side_str} REGIME-BLOCK: {_wrb_reason}")
                                    wh_count += 1; continue
                                # MTF CONFLUENCE (1h + 4h alignment).
                                # 2026-04-25: hard MTF-BLOCK → conviction-gated soft penalty.
                                # conf>=15 = soft penalty ×0.3, conf<15 = still block.
                                _wh_mtf_mult = 1.0
                                _wh_partial = 1.0
                                if _MTF_OK and _mtf is not None:
                                    try:
                                        _wmo, _wmd, _wpm = _mtf.aligned(coin, side_str)
                                        if not _wmo:
                                            if wh_conf >= 15:
                                                _wh_partial = _wh_partial * 0.3
                                                log(f"WEBHOOK {coin} {side_str} MTF-PENALTY ×0.3 (conviction bypass conf={wh_conf}): {_wmd}")
                                            else:
                                                log(f"WEBHOOK {coin} {side_str} MTF-BLOCK: {_wmd} (conf={wh_conf} < 15 floor)")
                                                wh_count += 1; continue
                                        else:
                                            _wh_partial = _wpm
                                            if _wpm < 1.0:
                                                log(f"WEBHOOK {coin} {side_str} MTF-PARTIAL-DOWNSIZE ×{_wpm}: {_wmd}")
                                        _wh_mtf_mult, _wh_mtf_det = _mtf.conviction_mult(coin, side_str, max_mult=MTF_SIZE_MAX)
                                        if _wh_mtf_mult > 1.0:
                                            log(f"WEBHOOK {coin} {side_str} MTF-CONVICTION ×{_wh_mtf_mult}: {_wh_mtf_det}")
                                    except Exception as _wme:
                                        log(f"WEBHOOK {coin} MTF err (fail-open): {_wme}")
                                # R:R FLOOR for webhook entries too.
                                if MIN_RR > 0:
                                    try:
                                        _wcfg = percoin_configs.get_config(coin) if percoin_configs.ELITE_MODE else None
                                        _wsl = (_wcfg or {}).get('SL')
                                        _wtp = (_wcfg or {}).get('TP')
                                        _wmin_rr = (_wcfg or {}).get('min_rr', MIN_RR)
                                        if _wsl and _wtp and _wsl > 0:
                                            _wrr = _wtp / _wsl
                                            if _wrr < _wmin_rr:
                                                log(f"WEBHOOK {coin} {side_str} R:R REJECT: {_wrr:.2f} < {_wmin_rr} (per-coin)")
                                                wh_count += 1; continue
                                    except Exception: pass
                                wh_gate_mult = 1.0
                                if _POSTMORTEM_OK and _postmortem is not None:
                                    try:
                                        _session_h = time.gmtime(time.time()).tm_hour
                                        _session = ('asian' if 0 <= _session_h < 8
                                                    else 'london' if 8 <= _session_h < 13
                                                    else 'ny' if 13 <= _session_h < 21
                                                    else 'overnight')
                                        try:
                                            _funding_bps = get_funding_rate(coin) * 10000.0
                                        except Exception:
                                            _funding_bps = None
                                        _signal_ctx = {
                                            'engine': 'WEBHOOK',
                                            'conf_score': wh_conf,
                                            'conf_breakdown': wh_conf_brk,
                                            'price': px,
                                            'session': _session,
                                            'funding_rate_bps': _funding_bps,
                                            'btc_dir': _btc_d if '_btc_d' in dir() else 0,
                                            'equity': equity,
                                            'open_positions': len(live_positions),
                                        }
                                        _v = _postmortem.evaluate_entry(coin, side_str, _signal_ctx)
                                        _dec = _v.get('decision', 'ALLOW')
                                        wh_gate_mult = float(_v.get('size_mult', 1.0))
                                        if _dec == 'BLOCK':
                                            log(f"WEBHOOK {coin} {side_str} GATE BLOCK: {_v.get('reason','')}")
                                            wh_count += 1; continue
                                        if _dec == 'SIZE_DOWN':
                                            log(f"WEBHOOK {coin} {side_str} GATE SIZE_DOWN ×{wh_gate_mult}: {_v.get('reason','')}")
                                    except Exception as _ge:
                                        log(f"WEBHOOK {coin} gate err: {_ge} — allowing at 1.0x")

                                # Apply both conviction and gate multipliers to size calc
                                wh_risk_mult = min(RISK_MULT_CEIL, risk_mult * wh_size_mult * wh_gate_mult * _wh_mtf_mult * _wh_partial)
                                sz = calc_size(equity, px, risk_pct, wh_risk_mult, coin=coin, side=side_str)
                                # STEP 2: generate trade identity before placing
                                _wh_trade_id = _ledger.new_trade_id() if _LEDGER_OK and _ledger else None
                                _wh_cloid = (f"{_wh_trade_id[:8]}{coin[:4]}{'L' if is_buy else 'S'}"[:16]) if _wh_trade_id else None
                                # 2026-04-25: route entry through _dispatch_entry. See process() entry sites.
                                _wh_dr = _dispatch_entry(coin, is_buy, sz, cloid=_wh_cloid, trade_id=_wh_trade_id)
                                fill = _wh_dr['fill_px']
                                _wh_atomic_used = _wh_dr['atomic_used']
                                if fill:
                                    # STEP 2: ENTRY event to ledger
                                    if _LEDGER_OK and _ledger is not None and _wh_trade_id:
                                        try:
                                            _ledger.append_entry(
                                                coin=coin, side=side_str, entry_price=fill,
                                                engine='WEBHOOK', source='webhook',
                                                sl_pct=None, tp_pct=None,
                                                cloid=_wh_cloid, trade_id=_wh_trade_id,
                                                regime=_current_regime(),
                                            )
                                        except Exception as _le:
                                            log(f"[ledger] webhook append_entry err {coin}: {_le}")
                                    # ENFORCE PROTECTION: skip when atomic placed bracket atomically.
                                    if _wh_atomic_used:
                                        _sl_pct_used = _wh_dr.get('sl_pct')
                                        _tp_pct_used = _wh_dr.get('tp_pct')
                                    else:
                                        _ep_res = enforce_position_protection(coin, is_buy, fill, origin='webhook')
                                        _sl_pct_used = _ep_res.get('sl_pct')
                                        _tp_pct_used = _ep_res.get('tp_pct')
                                    # LEDGER: record actual sl/tp/edge now that protection is placed
                                    if _LEDGER_OK and _ledger is not None and _wh_trade_id:
                                        try:
                                            _edge = _gates.compute_expected_edge(_tp_pct_used, _sl_pct_used) \
                                                if (_GATES_OK and _tp_pct_used and _sl_pct_used) else None
                                            _ledger.update_entry_fields(_wh_trade_id, sl_pct=_sl_pct_used,
                                                tp_pct=_tp_pct_used, expected_edge_at_entry=_edge,
                                                realized_slippage_pct=_wh_dr.get('realized_slippage_pct'))
                                        except Exception as _le:
                                            log(f"[ledger] webhook update_entry_fields err {coin}: {_le}")
                                    try:
                                        _sigs_for_pm = list((percoin_configs.get_config(coin) or {}).get('sigs', [])) if percoin_configs.ELITE_MODE else None
                                    except Exception:
                                        _sigs_for_pm = None
                                    state['positions'][coin] = {
                                        'side': 'L' if is_buy else 'S',
                                        'opened_at': time.time(),
                                        'entry': fill,
                                        'stage': 'initial', 'peak': fill,
                                        'source': 'dynapro',
                                        'engine': 'WEBHOOK',
                                        'sl_pct': _sl_pct_used,
                                        'tp_pct': _tp_pct_used,
                                        'sigs': _sigs_for_pm,
                                        'size': sz,
                                        'utc_h': time.gmtime(time.time()).tm_hour,
                                        'conf': wh_conf,
                                        'conf_mult': wh_size_mult,
                                        'gate_mult': wh_gate_mult,
                                        'trade_id': _wh_trade_id,
                                        'cloid': _wh_cloid,
                                    }
                                    # Deadlock valve tracker — webhook entry counts
                                    _LAST_OPEN_TS = time.time()
                                    log(f"WEBHOOK OPEN {coin} {side_str} @ {fill} (px_src={'bybit_ws' if px==by_px else 'hl_mid'}, age={by_age}ms)")
                                    log_trade('HL', coin, side_str, fill, 0, 'webhook')
                    wh_count += 1
                except Exception as e:
                    log(f"webhook process err: {e}"); break

            # PRECOG scan — parallel 8 workers (Bybit WS candles = no rate limit)
            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor(max_workers=8) as pool:
                futs = {pool.submit(process, c, state, equity, live_positions, risk_mult): c for c in COINS}
                for f in as_completed(futs):
                    try: f.result()
                    except Exception as e: log(f"err {futs[f]}: {e}")

            save_state(state)

            # Shadow-trade resolution: advance pending rejected-trade records
            # using DETERMINISTIC candle-based resolution (matches live spec).
            # Uses 1m HL candles since rejection timestamp. Conservative SL-first
            # on same-candle TP+SL hits. Friction applied: 0.07% fee + 0.16% slip.
            if _SR_OK and _shadow_rej is not None:
                try:
                    def _shadow_candle_fetcher(coin, since_ts_ms):
                        """Fetch 1m candles from HL between since_ts_ms and now.
                        Returns [] on any error (resolve_pending handles None)."""
                        try:
                            now_ms = int(time.time() * 1000)
                            if now_ms - since_ts_ms < 60_000:
                                return []  # less than 1 candle available
                            # Approximate bar count from time range; OKX doesn't
                            # accept arbitrary start/end so we ask for enough
                            # 1m bars to cover and let downstream filter by ts.
                            n_bars_needed = max(1, int((now_ms - since_ts_ms) / 60_000) + 5)
                            cs = okx_fetch.fetch_klines(coin, '1m', min(n_bars_needed, 300))
                            return cs or []
                        except Exception:
                            return []
                    _shadow_rej.resolve_pending(_shadow_candle_fetcher)
                except Exception as _sre:
                    log(f"shadow resolve err: {_sre}")

            log(f"--- tick complete ---")
        except Exception as e:
            log(f"tick err: {e}\n{traceback.format_exc()}")
        time.sleep(LOOP_SEC)


@app.route('/tuner/update', methods=['POST'])
def tuner_update():
    try:
        import json
        data = flask_request.get_json(force=True, silent=True) or {}
        # Store to web disk
        try:
            os.makedirs('/var/data', exist_ok=True)
            with open('/var/data/tuner_results.json','w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            with open('/tmp/tuner_results.json','w') as f:
                json.dump(data, f, indent=2)
        # Also log summary to buffer
        top = data.get('top',[])
        if top:
            t0 = top[0]
            log(f"TUNER {data.get('phase','?')} {data.get('completed','?')}/{data.get('total','?')} | top: n={t0.get('n')} WR={t0.get('wr',0):.1f}% pnl={t0.get('pnl',0):+.1f}%")
        return jsonify({'ok':True})
    except Exception as e:
        return jsonify({'error':str(e)}), 500

@app.route('/tuner/status', methods=['GET'])
def tuner_status():
    try:
        import json
        for p in ['/var/data/tuner_results.json','/tmp/tuner_results.json']:
            if os.path.exists(p):
                d=json.load(open(p))
                return jsonify({'phase':d.get('phase'),'completed':d.get('completed'),
                                'total':d.get('total'),'elapsed_sec':d.get('elapsed_sec'),
                                'top3':d.get('top',[])[:3]})
        return jsonify({'status':'no_results_yet'})
    except Exception as e:
        return jsonify({'error':str(e)})

@app.route('/tuner/top', methods=['GET'])
def tuner_top():
    try:
        import json
        for p in ['/var/data/tuner_results.json','/tmp/tuner_results.json']:
            if os.path.exists(p):
                return jsonify(json.load(open(p)))
        return jsonify({'status':'no_results_yet'})
    except Exception as e:
        return jsonify({'error':str(e)})

@app.route('/tuner/log', methods=['GET'])
def tuner_log():
    try:
        for p in ['/var/data/tuner.log','/tmp/tuner.log']:
            if os.path.exists(p):
                with open(p) as f:
                    lines=f.readlines()[-200:]
                return jsonify({'log': ''.join(lines)})
        return jsonify({'status':'no_log'})
    except Exception as e:
        return jsonify({'error':str(e)})


@app.route('/lifecycle', methods=['GET'])
def lifecycle_status():
    """Step 4 — full lifecycle observability with circuit breaker + multi-tier drift."""
    ledger_stats = _ledger.stats() if _LEDGER_OK and _ledger else {'err': 'ledger_unavailable'}
    snap_stats = _snapshot.status() if _SNAPSHOT_OK and _snapshot else {'err': 'snapshot_unavailable'}
    iq_stats = _intents.status() if _INTENTS_OK and _intents else {'err': 'intents_unavailable'}
    rc_stats = _reconciler.status() if _RECONCILER_OK and _reconciler else {'err': 'reconciler_unavailable'}

    drift_pct = None
    drift_status = 'unknown'
    exch_open_count = None
    if _LEDGER_OK and _SNAPSHOT_OK:
        try:
            snap = _snapshot.get()
            exch_open_count = len(snap['positions'])
            if not snap['stale']:
                ledger_open = ledger_stats.get('open_trades_count', 0)
                denom = max(exch_open_count, 1)
                drift_pct = abs(exch_open_count - ledger_open) / denom
                drift_status = ('healthy' if drift_pct < 0.01
                                else 'warn' if drift_pct < 0.03
                                else 'degraded' if drift_pct < 0.05
                                else 'unsafe')
        except Exception as _e:
            drift_status = f'err: {_e}'

    halt = _reconciler.is_halted() if _RECONCILER_OK and _reconciler else False
    authoritative_env = os.environ.get('RECONCILER_AUTHORITATIVE', '0') == '1'
    authoritative_actual = rc_stats.get('authoritative', False)
    cb_tripped = rc_stats.get('circuit_breaker_tripped', False)

    return jsonify({
        'step': 4,
        # Spec §8 top-level keys
        'drift_pct': round(drift_pct, 4) if drift_pct is not None else None,
        'drift_status': drift_status,
        'trading_halted': halt,
        'intent_backlog': iq_stats.get('queue_depth', 0) if isinstance(iq_stats, dict) else 0,
        'snapshot_stale': snap_stats.get('stale', True) if isinstance(snap_stats, dict) else True,
        'reconciler_lag_s': rc_stats.get('reconciler_lag_s', 0),
        # Extended detail
        'mode': 'authoritative' if authoritative_actual else 'observe',
        'halt_flag': halt,
        'circuit_breaker_tripped': cb_tripped,
        'emergency_flatten_authorized': rc_stats.get('emergency_flatten_authorized', False),
        'stall_emergency': rc_stats.get('stall_emergency', False),
        'entry_limiter': rc_stats.get('entry_limiter', 'full'),
        'pause_new_intents': rc_stats.get('pause_new_intents', False),
        'ledger': ledger_stats,
        'snapshot': snap_stats,
        'intents': iq_stats,
        'reconciler': rc_stats,
        'drift': {
            'pct': round(drift_pct, 4) if drift_pct is not None else None,
            'status': drift_status,
            'tier': rc_stats.get('drift_tier', 'unknown'),
            'ledger_open_count': ledger_stats.get('open_trades_count'),
            'exchange_open_count': exch_open_count,
        },
        'note': (
            f"AUTHORITATIVE env=1 but CB tripped — forced observe. Reset via /lifecycle/emergency?action=clear_breaker"
            if authoritative_env and cb_tripped else
            'AUTHORITATIVE: reconciler is sole writer. Legacy close() emits FORCE_CLOSE intent.'
            if authoritative_actual else
            'OBSERVE: reconciler logs would-close decisions but does not execute. Legacy close() still active.'
        ),
    })


@app.route('/lifecycle/heartbeat', methods=['GET'])
def lifecycle_heartbeat():
    """Lightweight endpoint for external monitors. Returns quickly.
    Status is 'ok' only when: reconciler alive, not stalled, CB not tripped, drift <5%.
    """
    if not (_RECONCILER_OK and _reconciler and _LEDGER_OK and _SNAPSHOT_OK):
        return jsonify({'status': 'degraded', 'reason': 'modules_unavailable'}), 503

    try:
        rc = _reconciler.status()
        daemon_alive = rc.get('daemon_alive', False)
        stalled = rc.get('stall_emergency', False)
        cb_tripped = rc.get('circuit_breaker_tripped', False)
        drift = rc.get('last_drift_pct')

        issues = []
        if not daemon_alive: issues.append('daemon_dead')
        if stalled: issues.append('reconciler_stalled')
        if cb_tripped: issues.append('circuit_breaker_tripped')
        if drift is not None and drift >= 0.05: issues.append(f'drift_{drift*100:.0f}pct')

        if issues:
            return jsonify({'status': 'degraded', 'issues': issues}), 503
        return jsonify({
            'status': 'ok',
            'cycles': rc.get('cycles_total'),
            'mode': rc.get('mode'),
            'drift_pct': drift,
        }), 200
    except Exception as e:
        return jsonify({'status': 'error', 'err': str(e)}), 500


@app.route('/lifecycle/emergency', methods=['POST', 'GET'])
def lifecycle_emergency():
    """Admin controls. Accepts ?action=<name> and optional ?token=<WEBHOOK_SECRET>.

    Actions:
      clear_halt       — force-clear drift halt flag
      clear_breaker    — reset circuit breaker
      clear_emergency  — clear emergency flatten authorization
      clear_ring       — empty recent-closed ring buffer
      clear_all        — all of the above
      flatten_all      — emergency flatten (requires emergency_flatten_authorized=true OR explicit confirm=1)
    """
    from flask import request
    # Simple auth — require matching WEBHOOK_SECRET to avoid accidental hits
    token = request.args.get('token') or (request.json or {}).get('token') if request.is_json else request.args.get('token')
    if token != WEBHOOK_SECRET:
        return jsonify({'err': 'unauthorized'}), 401

    action = request.args.get('action') or (request.json or {}).get('action') if request.is_json else request.args.get('action')
    if not action:
        return jsonify({'err': 'action required', 'valid_actions': [
            'clear_halt', 'clear_breaker', 'clear_emergency', 'clear_ring', 'clear_all',
            'flatten_all', 'close_coin']}), 400

    if action == 'close_coin':
        coin = request.args.get('coin')
        if not coin:
            return jsonify({'err': 'close_coin requires ?coin=<SYMBOL>'}), 400
        coin = coin.upper()
        # Route through the same pipeline a legitimate close() would take.
        # CRITICAL: use ledger as trade_id source of truth, not state (state can
        # hold a stale trade_id where the ledger trade is already closed).
        try:
            tid = None
            if _LEDGER_OK and _ledger:
                tid = _ledger.latest_open_trade_id_for_coin(coin)
            if not tid:
                return jsonify({'err': f'no OPEN ledger trade for {coin}'}), 404
            if _INTENTS_OK and _intents is not None:
                _intents.emit('FORCE_CLOSE', coin, trade_id=tid, reason='admin_close')
                log(f"[admin] FORCE_CLOSE emitted for {coin} trade_id={tid[:8]}")
                return jsonify({'action': 'close_coin', 'coin': coin,
                                'trade_id': tid, 'ok': True,
                                'note': 'intent emitted; reconciler will execute within 15s'})
            else:
                return jsonify({'err': 'intents module unavailable'}), 503
        except Exception as e:
            return jsonify({'err': str(e)}), 500

    if action == 'flatten_all':
        confirm = request.args.get('confirm') == '1'
        if not confirm:
            rc = _reconciler.status() if _RECONCILER_OK and _reconciler else {}
            if not rc.get('emergency_flatten_authorized'):
                return jsonify({'err': 'flatten_all requires emergency_flatten_authorized=true OR ?confirm=1'}), 400
        try:
            flatten_all('EMERGENCY_API')
            return jsonify({'action': action, 'ok': True})
        except Exception as e:
            return jsonify({'action': action, 'ok': False, 'err': str(e)}), 500

    if not _RECONCILER_OK or _reconciler is None:
        return jsonify({'err': 'reconciler_unavailable'}), 503

    try:
        result = _reconciler.emergency_reset(action)
        return jsonify(result)
    except Exception as e:
        return jsonify({'err': str(e)}), 500


@app.route('/lifecycle/cleanup', methods=['POST', 'GET'])
def lifecycle_cleanup():
    """One-time ledger cleanup. Two passes:
      1. Dedupe duplicate open trade_ids per coin (keep earliest, close rest)
      2. Close ledger-open trades whose coin is NOT on exchange (flushes Step 2 bug residue)

    Requires token.
    """
    from flask import request
    token = request.args.get('token')
    if token != WEBHOOK_SECRET:
        return jsonify({'err': 'unauthorized'}), 401
    if not (_LEDGER_OK and _ledger):
        return jsonify({'err': 'ledger_unavailable'}), 503
    try:
        dedup = _ledger.dedupe_open_trades()

        # Get current exchange coins from snapshot
        missing = {'closed_missing': 0, 'details': []}
        if _SNAPSHOT_OK and _snapshot:
            snap = _snapshot.get()
            if not snap.get('stale'):
                exch_coins = list(snap.get('positions', {}).keys())
                missing = _ledger.close_missing_on_exchange(exch_coins)
            else:
                missing['err'] = 'snapshot_stale — skipped missing-close pass'

        log(f"[lifecycle/cleanup] dedupe coins={dedup['coins_affected']} dupes={dedup['dupes_closed']} "
            f"missing_closed={missing.get('closed_missing', 0)}")
        return jsonify({'dedupe': dedup, 'missing_closes': missing})
    except Exception as e:
        return jsonify({'err': str(e)}), 500


@app.route('/dash', methods=['GET'])
def dash_json():
    # Use cached account state from main tick to avoid HL 429 on dash hits
    cached = getattr(main, '_cached_account', {})
    eq = cached.get('equity', 0)
    positions = cached.get('positions', [])
    if not cached or time.time() - cached.get('ts', 0) > 30:
        try:
            cs = info.user_state(WALLET)
            eq = float(cs.get('marginSummary',{}).get('accountValue',0))
            positions = []
            for p in cs.get('assetPositions',[]):
                pp=p['position']; sz=float(pp['szi'])
                positions.append({'coin':pp['coin'],'side':'L' if sz>0 else 'S','size':abs(sz),
                                  'entry':float(pp['entryPx']),'upnl':float(pp['unrealizedPnl']),
                                  'lev':int(pp['leverage']['value'])})
        except Exception as e:
            pass
    # Enrich with TP/SL prices from bot's tracked state. HL's user_state
    # doesn't return TP/SL — those are separate trigger orders. We compute
    # the price from entry * (1 ± tp_pct) using the bot's own tracker.
    # Sources checked, in order: precog state['positions'][coin], then
    # confluence_worker open_positions[coin]. Either has tp_pct/sl_pct.
    try:
        cw_open = {}
        try:
            import confluence_worker as _cw_mod
            cw_open = dict(_cw_mod._state.get('open_positions', {}))
        except Exception:
            cw_open = {}
        for pos_rec in positions:
            coin = pos_rec.get('coin')
            entry_px = pos_rec.get('entry')
            is_long = pos_rec.get('side') == 'L'
            if not coin or not entry_px:
                continue
            tracked = state.get('positions', {}).get(coin, {})
            tp_pct = tracked.get('tp_pct')
            sl_pct = tracked.get('sl_pct')
            engine = tracked.get('engine')
            if not tp_pct:
                cf = cw_open.get(coin, {})
                tp_pct = cf.get('tp_pct')
                sl_pct = sl_pct or cf.get('sl_pct')
                if cf.get('systems'):
                    engine = engine or ('CONFLUENCE_' + '+'.join(cf.get('systems') or []))
            if tp_pct:
                pos_rec['tp_pct'] = tp_pct
                pos_rec['tp'] = entry_px * (1 + tp_pct) if is_long else entry_px * (1 - tp_pct)
            if sl_pct:
                pos_rec['sl_pct'] = sl_pct
                pos_rec['sl'] = entry_px * (1 - sl_pct) if is_long else entry_px * (1 + sl_pct)
            if engine:
                pos_rec['engine'] = engine
    except Exception:
        pass
    try: news = news_filter.get_state()
    except Exception: news = {}
    try: ladder = risk_ladder.get_state()
    except Exception: ladder = {}
    try: ob_stat = orderbook_ws.status()
    except Exception: ob_stat = {}
    try: lev_cache = leverage_map.get_cache()
    except Exception: lev_cache = {}
    try: liq_stat = liquidation_ws.status()
    except Exception: liq_stat = {}
    try: wall_entries = state.get('wall_entries', {})
    except Exception: wall_entries = {}

    coin_hist = state.get('coin_hist', {})
    coin_kill = state.get('coin_kill', {})
    coin_wr = {}
    for coin, h in coin_hist.items():
        if len(h) >= 5: coin_wr[coin] = round(sum(h)/len(h)*100, 1)
    killed = {c:v.get('until',0) for c,v in coin_kill.items() if time.time() < v.get('until',0)}
    return jsonify({
        'equity': eq, 'version': 'v8.28',
        'positions': positions, 'n_positions': len(positions),
        'universe_size': len(COINS),
        'news': news, 'risk_ladder': ladder,
        'orderbook': ob_stat, 'leverage_cache_size': len(lev_cache),
        'liquidation': liq_stat, 'wall_entries': len(wall_entries),
        'btc_corr': btc_correlation.get_state(),
        'funding_cached': len(funding_filter._CACHE) if hasattr(funding_filter, '_CACHE') else 0,
        'spoof': spoof_detection.status(),
        'session': {'name': session_scaler.session_name(), 'mult': session_scaler.get_mult()},
        'whale': whale_filter.status(),
        'cvd': cvd_ws.status(),
        'oi': oi_tracker.status(),
        'funding_arb': funding_arb.status(),
        'coin_wr': coin_wr, 'killed_coins': killed,
        'consec_losses': state.get('consec_losses', 0),
    })

@app.route('/dash/html', methods=['GET'])
def dash_html():
    return """<!DOCTYPE html><html><head><title>PreCog Live</title>
<style>body{font-family:monospace;background:#0b0b0b;color:#ccc;padding:20px;max-width:1400px;margin:auto}
h2{color:#0f0;border-bottom:1px solid #333;padding-bottom:4px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px}
.card{background:#111;padding:12px;border:1px solid #222;border-radius:4px}
.kv{display:flex;justify-content:space-between;padding:2px 0}
.k{color:#888} .v{color:#fff}
.pos{background:#0a1a0a}.neg{background:#1a0a0a}
table{width:100%;border-collapse:collapse}
td,th{padding:4px 8px;text-align:left;border-bottom:1px solid #222}
.red{color:#f55}.green{color:#5f5}.yellow{color:#ff5}
</style></head><body>
<h2>PreCog Live Dashboard</h2>
<div id="root">loading...</div>
<script>
async function refresh(){
  const r = await fetch('/dash'); const d = await r.json();
  const fmt = (n,d=2) => Number(n).toFixed(d);
  const news = d.news || {};
  const rl = d.risk_ladder || {};
  const ob = d.orderbook || {};
  const pos_rows = (d.positions||[]).map(p=>`<tr><td>${p.coin}</td><td class="${p.side=='L'?'green':'red'}">${p.side}</td><td>${p.size}</td><td>${fmt(p.entry,4)}</td><td class="${p.upnl>=0?'green':'red'}">${fmt(p.upnl,2)}</td><td>${p.lev}x</td></tr>`).join('');
  const wr_rows = Object.entries(d.coin_wr||{}).sort((a,b)=>b[1]-a[1]).slice(0,30).map(([c,w])=>`<tr><td>${c}</td><td class="${w>=60?'green':w>=45?'yellow':'red'}">${w}%</td></tr>`).join('');
  const killed = Object.keys(d.killed_coins||{});
  const news_list = (news.last_events||[]).slice(0,8).map(e=>`<div class="kv"><span class="k">[${e.src}]</span><span class="v">${e.title} (${e.mag}/${e.dir>0?'↑':e.dir<0?'↓':'?'})</span></div>`).join('');
  document.getElementById('root').innerHTML = `
  <div class="grid">
    <div class="card"><h3>Account</h3>
      <div class="kv"><span class="k">Equity</span><span class="v">$${fmt(d.equity)}</span></div>
      <div class="kv"><span class="k">Positions</span><span class="v">${d.n_positions}/${30}</span></div>
      <div class="kv"><span class="k">Universe</span><span class="v">${d.universe_size} coins</span></div>
      <div class="kv"><span class="k">Consec losses</span><span class="v">${d.consec_losses}</span></div>
    </div>
    <div class="card"><h3>Risk Ladder</h3>
      <div class="kv"><span class="k">Tier</span><span class="v">${rl.tier||0}</span></div>
      <div class="kv"><span class="k">Risk</span><span class="v">${fmt((rl.risk||0)*100,2)}%</span></div>
      <div class="kv"><span class="k">Trades logged</span><span class="v">${rl.trades_logged||0}</span></div>
      <div class="kv"><span class="k">WR (100)</span><span class="v">${fmt((rl.rolling_wr_100||0)*100,1)}%</span></div>
      <div class="kv"><span class="k">WR (50)</span><span class="v">${fmt((rl.rolling_wr_50||0)*100,1)}%</span></div>
    </div>
    <div class="card"><h3>News / Regime</h3>
      <div class="kv"><span class="k">Blackout</span><span class="v ${news.blackout?'red':'green'}">${news.blackout?'YES':'clear'}</span></div>
      <div class="kv"><span class="k">Risk mult</span><span class="v">${news.risk_mult||1}x</span></div>
      <div class="kv"><span class="k">Direction bias</span><span class="v">${news.direction_bias||0}</span></div>
    </div>
    <div class="card"><h3>Orderbook WS</h3>
      <div class="kv"><span class="k">Feeds</span><span class="v">${ob.depth_feeds||0}</span></div>
      <div class="kv"><span class="k">Verified walls</span><span class="v">${ob.tracked_walls||0}</span></div>
      <div class="kv"><span class="k">Coins w/ walls</span><span class="v">${ob.verified_coins||0}</span></div>
    </div>
  </div>
  <h2>Open Positions</h2>
  <table><tr><th>Coin</th><th>Side</th><th>Size</th><th>Entry</th><th>uPnL</th><th>Lev</th></tr>${pos_rows||'<tr><td colspan=6>none</td></tr>'}</table>
  <h2>Per-Coin WR (top 30)</h2>
  <table><tr><th>Coin</th><th>WR</th></tr>${wr_rows}</table>
  ${killed.length?`<h2>Killed coins (12h)</h2><div>${killed.join(', ')}</div>`:''}
  <h2>Recent news (${news.last_events?.length||0})</h2>${news_list}`;
}
refresh(); setInterval(refresh, 10000);
</script></body></html>"""

if __name__ == '__main__':
    # Run precog signal loop in background thread
    t = threading.Thread(target=main, daemon=True)
    t.start()

    # 2026-04-25: atomic_reconciler daemon — wakes every 1s to check for
    # PROVISIONAL ledger rows (atomic entries pending size confirmation).
    # When actual fill differs from intent_size by >0.5%, cancels old SL/TP
    # and places new at correct size. Only meaningful when USE_ATOMIC_EXEC=1;
    # legacy entries are CONFIRMED at creation by enforce_protection's
    # synchronous size verification.
    try:
        import atomic_reconciler
        atomic_reconciler.init(
            cancel_order_fn=lambda c, oid: exchange.cancel(c, oid),
            place_sl_fn=place_native_sl,
            place_tp_fn=place_native_tp,
            emergency_close_fn=lambda c, r: close(c, reason=f'reconciler:{r}'),
            log_fn=log,
        )
        atomic_reconciler.start()
        log("[atomic_reconciler] daemon started")
    except Exception as _re:
        log(f"atomic_reconciler init failed (non-fatal): {_re}")

    # SYSTEM B — Confluence engine worker (optional, gated by env var)
    try:
        import sys as _sys
        import confluence_worker as _cw
        _cw.start(_sys.modules[__name__])
    except Exception as _e:
        log(f"confluence worker init failed (non-fatal): {_e}")

    # Run latency arbitrage module in background thread
    # LA KILLED — was burning 60 API calls/sec with 0 trades, causing 429s
    # Run Flask webhook server in main thread (Render expects port 10000)
    port = int(os.environ.get('PORT', 10000))
    _mt4_load()  # restore MT4 queue from disk across deploys
    log(f"Webhook server starting on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False)
