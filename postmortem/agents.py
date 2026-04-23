"""Forensic agents. One per signal component.

Each agent:
  1. Receives the closed trade + reconstructed component state at entry
  2. Determines if its component fired correctly
  3. Proposes a surgical parameter delta (or vetoes the component for this coin)
  4. Returns {verdict, confidence, reasoning, proposed_delta}

Agents run in parallel via runner.py. Claude API calls use haiku for speed
(cheap, 100+ per day is fine). The final synthesis agent uses Sonnet.

Env vars:
    ANTHROPIC_API_KEY     (required, tuner no-ops silently if missing)
    POSTMORTEM_MODEL      (default: claude-haiku-4-5)
    POSTMORTEM_SYNTH_MODEL (default: claude-sonnet-4-5)
    POSTMORTEM_DRY_RUN    (if '1', agents run but no deltas applied)
"""
import os
import json
import time
import traceback

try:
    from anthropic import Anthropic
except ImportError:
    Anthropic = None

from . import bounds

_CLIENT = None
_MODEL = os.environ.get('POSTMORTEM_MODEL', 'claude-haiku-4-5')
_SYNTH_MODEL = os.environ.get('POSTMORTEM_SYNTH_MODEL', 'claude-sonnet-4-5')
DRY_RUN = os.environ.get('POSTMORTEM_DRY_RUN', '0') == '1'


def _client():
    global _CLIENT
    if _CLIENT is None:
        if Anthropic is None:
            raise RuntimeError('anthropic SDK not installed')
        api_key = os.environ.get('ANTHROPIC_API_KEY')
        if not api_key:
            raise RuntimeError('ANTHROPIC_API_KEY not set')
        _CLIENT = Anthropic(api_key=api_key)
    return _CLIENT


def _call_claude(system_prompt, user_prompt, model=None, max_tokens=800):
    """Low-level Claude call returning text. Raises on failure."""
    client = _client()
    resp = client.messages.create(
        model=model or _MODEL,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{'role': 'user', 'content': user_prompt}],
    )
    # Concatenate any text blocks
    text = ''
    for block in resp.content:
        if getattr(block, 'type', None) == 'text':
            text += block.text
    return text.strip()


def _parse_verdict(text):
    """Extract JSON verdict from agent output. Tolerant to preamble/fences."""
    try:
        # strip code fences
        t = text.replace('```json', '').replace('```', '').strip()
        # find first {
        i = t.find('{')
        j = t.rfind('}')
        if i < 0 or j <= i:
            return None
        return json.loads(t[i:j+1])
    except Exception:
        return None


# ─────────────────────────────────────────────────────
# AGENT CONTRACT
# ─────────────────────────────────────────────────────
# Each agent returns:
# {
#   'verdict':         'passed' | 'failed' | 'irrelevant',
#   'confidence':      0.0..1.0,
#   'reasoning':       '<short explanation>',
#   'proposed_delta':  [{'component','param','new_value'}, ...]    # may be []
#   'proposed_veto':   {'component': '<name>', 'expires_in_sec': <int|null>} or null
# }
#
# An agent outputs 'irrelevant' when its component did not apply to this
# trade (e.g. FVG agent on a coin with no FVG in lookback). The tuner
# skips irrelevant findings entirely.

# Shared prompt template; each agent gets a specialized block of instructions


_SHARED_SCHEMA = '''Output ONLY valid JSON. No markdown fences. Exact shape:
{
  "verdict": "passed" | "failed" | "irrelevant",
  "confidence": 0.0,
  "reasoning": "<=300 chars, plain english",
  "proposed_delta": [{"component": "...", "param": "...", "new_value": 0.0}],
  "proposed_veto": null
}

UNIT CONVENTIONS:
- pnl_pct is ALREADY in percent. -0.158 means -0.158%, not -15.8%. Do NOT multiply by 100.
- Always use pnl_display (e.g. "-0.158%") when referring to trade outcome.
- sl_pct and tp_pct are decimals (0.02 = 2%, 0.08 = 8%).

Rules:
- If none of your components applied, return "irrelevant" with empty delta.
- If all components behaved correctly even on a loss, return "passed" — losses can come from elsewhere.
- You may propose MULTIPLE deltas in one call if multiple of your components warrant tuning.
- Be conservative. No proposal > reasoning is weak.'''


_ENTRY_SIGNAL_SYSTEM = f'''You are the Entry-Signal Specialist for a crypto perps trading system.
You own three components that gate trade entry:

  rsi:       buy_threshold (RL, default 25-45), sell_threshold (RH, default 55-90), period
  pivot:     lookback (default 5, range 2-20 bars)
  bollinger: period (default 20), std_mult (default 2.0), rsi_buffer (default 5)

These three components together determine WHETHER an entry fires. Your job:
1. Given this closed trade, did the entry conditions actually justify the trade?
2. If the trade lost AND your components share blame, propose parameter delta(s).
3. If the trade won or your components weren't the failure point, verdict = passed.

Typical tuning signals:
- Repeated losing entries at RSI borderline (e.g. RSI=74 for short with RH=75) → tighten RH
- Pivot lookback too short for regime → LB up (misses broader trend)
- Bollinger too wide (std_mult>2) → catches too many false reversals
- Bollinger rsi_buffer too small → signals fire without RSI confirmation

{_SHARED_SCHEMA}'''


_REGIME_CONTEXT_SYSTEM = f'''You are the Regime/Context Specialist for a crypto perps trading system.
You analyze whether the trade's direction was aligned with the market regime.
You DO NOT tune parameters. You propose VETOS when regime mismatch caused the loss.

Dimensions you evaluate:
  regime:  bull-calm / bear-calm / chop / squeeze / high-vol
  funding: positive / negative / flat (and magnitude bps)
  session: asian / london / ny (entries in thin liquidity = less reliable)
  macro:   BTC dominance, total mcap change, cross-venue funding divergence

Common veto-worthy patterns:
- Shorts in bull-calm with positive macro funding → regime-incompatible entry
- Longs in bear-calm with negative funding → regime-incompatible entry
- Asian session entries on low-liquidity alts that dust-swept → session filter needed

When proposing a veto, use component names that match the tuner's veto system
(e.g. "pivot", "bollinger") or "regime_filter" for cross-engine suppression.
Typical expires_in_sec: 43200 (12h) for one-off, 86400 (24h) for repeated pattern.

{_SHARED_SCHEMA}

Additional: "proposed_veto" field may be:
  null  OR  {{"component": "...", "expires_in_sec": 43200, "reason": "..."}}'''


_EXECUTION_SYSTEM = f'''You are the Execution Forensics Specialist for a crypto perps trading system.
You analyze OPERATIONAL issues — not signal quality, not regime fit, but how the trade
was actually executed and exited.

Dimensions you evaluate:
  exit_reason: dust_sweep, sl_hit, tp_hit, signal_reversal, trail_exit, unknown
  duration_s:  how long was the position held
  size:        position size (may be too small → dust threshold)
  leverage:    applied leverage
  entry_px:    absolute price (micro-cap vs large-cap behavior differs)

Tell-tale execution problems:
- dust_sweep after <10min: undersized position, never had chance to play out
- dust_sweep with ~0% pnl: operational close, no directional info
- exit_reason=unknown with non-zero pnl: missed exit tag (pipeline bug)
- Multiple dust_sweeps on same coin: sizing logic needs review

Your proposals should focus on VETOS for coins/regimes showing operational failure,
NOT param deltas (you don't own any wired params).

{_SHARED_SCHEMA}

Additional: "proposed_veto" field may be:
  null  OR  {{"component": "...", "expires_in_sec": 43200, "reason": "..."}}'''


_HISTORICAL_SYSTEM = f'''You are the Historical Pattern Specialist.
You have access to this coin's prior knowledge-base entries AND patterns from
structurally similar coins. You look for RECURRING failure modes.

Your job:
1. Does this trade match a known failure pattern from the KB?
2. Is this the Nth occurrence of the same mistake?
3. Should we escalate from "one-off observation" to "enforced veto"?

You are the memory-weighted voice. If the same pattern has failed 3+ times
(reinforced_count >= 3 on a matching KB entry), propose a HARD VETO even if
the other specialists only propose deltas.

{_SHARED_SCHEMA}

Additional: "proposed_veto" field may be:
  null  OR  {{"component": "...", "expires_in_sec": 86400, "reason": "..."}}'''


# ─────────────────────────────────────────────────────
# 5 SPECIALIST AGENTS
# ─────────────────────────────────────────────────────
# Each takes (trade, context), makes ONE Haiku call, returns a verdict dict
# that may contain MULTIPLE proposed deltas (for multiple components under
# that agent's responsibility).

def _run_specialist(system_prompt, components, trade, context):
    """Generic specialist runner. Bundles bounds for this agent's components."""
    try:
        param_bounds = {}
        for comp in components:
            for p in bounds.params_for(comp):
                key = f'{comp}.{p}'
                param_bounds[key] = bounds.get_bounds(comp, p)

        prompt = f'''TRADE:
{json.dumps(trade, indent=2, default=str)}

ENTRY CONTEXT (snapshot at entry):
{json.dumps(context, indent=2, default=str)}

YOUR COMPONENTS AND THEIR BOUNDS (default, min, max, max_step, min_samples):
{json.dumps(param_bounds, indent=2, default=str)}

Analyze this trade from your specialist perspective. Output the JSON verdict.'''
        text = _call_claude(system_prompt, prompt, max_tokens=1000)
        verdict = _parse_verdict(text)
        if verdict:
            verdict['_raw'] = text[:500]
        return verdict
    except Exception as e:
        return {
            'verdict': 'irrelevant',
            'confidence': 0.0,
            'reasoning': f'specialist error: {type(e).__name__}: {str(e)[:200]}',
            'proposed_delta': [],
            'proposed_veto': None,
            '_error': traceback.format_exc()[:1000],
        }


def agent_entry_signal(trade, context):
    """Covers: rsi (buy/sell thresh), pivot.lookback, bollinger (all 3 params)."""
    return _run_specialist(_ENTRY_SIGNAL_SYSTEM, ['rsi', 'pivot', 'bollinger'], trade, context)


def agent_regime_context(trade, context):
    """Covers: regime + funding + session + macro. Vetoes only, no param tuning."""
    return _run_specialist(_REGIME_CONTEXT_SYSTEM,
                           ['regime', 'funding', 'session'], trade, context)


def agent_execution(trade, context):
    """Covers: dust_sweep patterns, duration, size, exit_reason. Vetoes only."""
    # No direct component bounds — this agent is operational.
    try:
        prompt = f'''TRADE:
{json.dumps(trade, indent=2, default=str)}

ENTRY CONTEXT:
{json.dumps(context, indent=2, default=str)}

Analyze the OPERATIONAL execution of this trade (not signal quality, not regime fit).
Output the JSON verdict.'''
        text = _call_claude(_EXECUTION_SYSTEM, prompt, max_tokens=800)
        verdict = _parse_verdict(text)
        if verdict:
            verdict['_raw'] = text[:500]
        return verdict
    except Exception as e:
        return {
            'verdict': 'irrelevant', 'confidence': 0.0,
            'reasoning': f'execution agent error: {type(e).__name__}: {str(e)[:200]}',
            'proposed_delta': [], 'proposed_veto': None,
        }


def agent_historical(trade, context):
    """Covers: KB retrieval for this coin + similar patterns. Escalates repeat failures."""
    try:
        coin = trade.get('coin', '?')
        side = trade.get('side', '?')
        # Pull existing KB entries for this coin+side
        try:
            from . import kb
            kb_entries = kb.read_relevant(coin, side, max_entries=8)
            kb_block = kb.format_for_prompt(kb_entries, max_chars=1500) if kb_entries else '(no prior entries)'
        except Exception:
            kb_block = '(kb module unavailable)'

        prompt = f'''TRADE:
{json.dumps(trade, indent=2, default=str)}

ENTRY CONTEXT:
{json.dumps(context, indent=2, default=str)}

EXISTING KNOWLEDGE-BASE ENTRIES FOR {coin} {side}:
{kb_block}

Analyze whether this trade matches a recurring pattern. If reinforced_count on any
matching KB entry is >= 3, propose a HARD VETO (expires_in_sec 86400). Otherwise
verdict=passed unless you see a clear pattern match.

Output the JSON verdict.'''
        text = _call_claude(_HISTORICAL_SYSTEM, prompt, max_tokens=800)
        verdict = _parse_verdict(text)
        if verdict:
            verdict['_raw'] = text[:500]
        return verdict
    except Exception as e:
        return {
            'verdict': 'irrelevant', 'confidence': 0.0,
            'reasoning': f'historical agent error: {type(e).__name__}: {str(e)[:200]}',
            'proposed_delta': [], 'proposed_veto': None,
        }


# Registry — 4 specialists. SL/TP tuning removed — exits are human policy.
AGENTS = {
    'entry_signal':   agent_entry_signal,
    'regime_context': agent_regime_context,
    'execution':      agent_execution,
    'historical':     agent_historical,
}




# ─────────────────────────────────────────────────────
# SYNTHESIS AGENT (runs last, uses Sonnet)
# ─────────────────────────────────────────────────────
# Takes all component verdicts and writes a plain-english summary + final
# sanity check. Can veto any delta the tuner was about to apply if cross-
# component evidence is weak.

_SYNTH_SYSTEM = '''You are the head of a forensic tuning team. Individual component
analysts have each analyzed a closed trade from the perspective of their
single component. You see all their verdicts at once.

CRITICAL UNIT CONVENTIONS — read carefully:
- pnl_pct is ALREADY IN PERCENT. A value of -0.158 means -0.158% (less than
  one-fifth of one percent), NOT -15.8%. Do NOT multiply by 100.
- Always use the pnl_display field (e.g. "-0.158%", "+0.410%") when referring
  to trade outcome in your root_cause and KB summaries.
- sl_pct and tp_pct in the trade dict are DECIMAL FRACTIONS (0.02 = 2%, 0.08 = 8%).
- When quoting percent values in prose, render them as percent (e.g. "2%", "8%"),
  not as decimals.

DO NOT SPECULATE ABOUT CONFIG MISMATCH:
- You do not see the percoin config file. Only compare trade parameters to
  values you have direct evidence for. Never claim "config says X, trade says Y"
  unless the discrepancy is present in the findings you received.

Your job:
1. Write a concise root-cause summary (<=4 sentences) of why the trade lost or won.
2. For each proposed delta, decide: APPROVE, REJECT, or DEFER.
   - APPROVE: delta is supported by the findings and not contradicted by other agents.
   - REJECT: delta would move the system in a direction contradicted by other agents.
   - DEFER: insufficient evidence, wait for more samples.
3. Propose knowledge-base entries that capture transferable patterns. Each entry
   has a pattern_key (how the entry will be retrieved) and a summary (what to remember).

Pattern_key formats you may use:
   "{coin}:{side}"                           — coin+side baseline
   "{coin}:{side}:regime={regime}"           — regime-specific (squeeze/neg_funding/high_vol/...)
   "{coin}:{side}:session={session}"         — session-specific (asian/london/ny/...)
   "{coin}:{side}:engine={engine}"           — engine-specific (PIVOT/BB_REJ/...)
   "{coin}:{side}:funding={pos|neg|flat}"    — funding-state specific

Summary should be <=200 chars, plain english, actionable. Example:
   "LIT shorts during negative funding + BTC up-trend failed 3/3. OB was stale. Avoid
    unless fresh OB within 10 bars."

Output JSON only. No markdown fences. Exact shape:
{
  "root_cause": "string",
  "decisions": [{"component": "...", "param": "...", "decision": "APPROVE|REJECT|DEFER",
                 "note": "..."}],
  "new_vetos": [{"component": "...", "expires_in_sec": 43200, "reason": "..."}],
  "kb_entries": [{"pattern_key": "...", "summary": "...",
                  "evidence": {"key": "value"}}]
}'''


def synthesize(trade, findings):
    try:
        prompt = f'''TRADE:
{json.dumps(trade, indent=2, default=str)}

AGENT FINDINGS:
{json.dumps(findings, indent=2, default=str)}

Synthesize, decide tunings, and extract KB entries for future reference.'''
        text = _call_claude(_SYNTH_SYSTEM, prompt, model=_SYNTH_MODEL, max_tokens=2000)
        parsed = _parse_verdict(text)
        if parsed:
            # Normalize fields so callers never KeyError
            parsed.setdefault('root_cause', '')
            parsed.setdefault('decisions', [])
            parsed.setdefault('new_vetos', [])
            parsed.setdefault('kb_entries', [])
            return parsed
        return {'root_cause': 'synthesis parse failed', 'decisions': [],
                'new_vetos': [], 'kb_entries': []}
    except Exception as e:
        return {'root_cause': f'synthesis error: {type(e).__name__}: {e}',
                'decisions': [], 'new_vetos': [], 'kb_entries': []}
