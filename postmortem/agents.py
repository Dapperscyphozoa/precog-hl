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
_BASE_SYSTEM = '''You are a forensic component analyst for a crypto perps trading system.
You analyze one specific signal component per trade close and decide:
1. Did this component behave correctly for this trade?
2. If not, what single-parameter change would most likely have prevented the bad behavior?

Output ONLY valid JSON in this exact shape:
{
  "verdict": "passed" | "failed" | "irrelevant",
  "confidence": 0.0,
  "reasoning": "concise explanation",
  "proposed_delta": [{"component": "...", "param": "...", "new_value": 0.0}],
  "proposed_veto": null
}

Rules:
- If your component did not apply to this trade, return "irrelevant" with empty delta and no veto.
- If your component behaved correctly even though the trade lost, return "passed" — not every
  loss is a component failure. Losses can come from other components.
- Propose at most ONE delta per run. Never propose deltas for components outside your responsibility.
- If the same pattern has failed repeatedly (3+ times with same root cause), propose a veto
  instead of a delta, with expires_in_sec = 43200 (12h).
- Be conservative. Do not propose changes if reasoning is weak.
- Never include markdown fences. Pure JSON only.'''


def _build_prompt(component_name, trade, context, param_bounds):
    return f'''Component under analysis: {component_name}

TRADE:
{json.dumps(trade, indent=2, default=str)}

COMPONENT CONTEXT AT ENTRY:
{json.dumps(context, indent=2, default=str)}

CURRENT PARAMETERS AND BOUNDS FOR YOUR COMPONENT:
{json.dumps(param_bounds, indent=2, default=str)}

Analyze this trade from the perspective of the {component_name} component only.
Output the JSON verdict as specified.'''


def _run_agent(component_name, trade, context):
    """Generic agent runner. Returns parsed verdict dict or None on failure."""
    try:
        param_bounds = {p: bounds.get_bounds(component_name, p)
                        for p in bounds.params_for(component_name)}
        prompt = _build_prompt(component_name, trade, context, param_bounds)
        text = _call_claude(_BASE_SYSTEM, prompt)
        verdict = _parse_verdict(text)
        if verdict:
            verdict['_raw'] = text[:500]
        return verdict
    except Exception as e:
        return {
            'verdict': 'irrelevant',
            'confidence': 0.0,
            'reasoning': f'agent error: {type(e).__name__}: {str(e)[:200]}',
            'proposed_delta': [],
            'proposed_veto': None,
            '_error': traceback.format_exc()[:1000],
        }


# ─────────────────────────────────────────────────────
# COMPONENT AGENTS
# ─────────────────────────────────────────────────────
# Each function accepts (trade, context) and returns a verdict dict.
# `trade` = {coin, side, pnl_pct, entry_px, exit_px, entry_ts, exit_ts,
#            duration_s, engine, exit_reason, is_win}
# `context` = per-component snapshot (candles, indicators, features at entry)

def agent_rsi(trade, context):
    return _run_agent('rsi', trade, context)

def agent_pivot(trade, context):
    return _run_agent('pivot', trade, context)

def agent_cooldown(trade, context):
    return _run_agent('cooldown', trade, context)

def agent_bollinger(trade, context):
    return _run_agent('bollinger', trade, context)

def agent_adx(trade, context):
    return _run_agent('adx', trade, context)

def agent_ema(trade, context):
    return _run_agent('ema', trade, context)

def agent_ob(trade, context):
    return _run_agent('ob', trade, context)

def agent_wall(trade, context):
    return _run_agent('wall', trade, context)

def agent_cvd(trade, context):
    return _run_agent('cvd', trade, context)

def agent_fvg(trade, context):
    return _run_agent('fvg', trade, context)

def agent_fib(trade, context):
    return _run_agent('fib', trade, context)

def agent_sr(trade, context):
    return _run_agent('sr', trade, context)

def agent_structure(trade, context):
    return _run_agent('structure', trade, context)

def agent_funding(trade, context):
    return _run_agent('funding', trade, context)

def agent_session(trade, context):
    return _run_agent('session', trade, context)

def agent_oi(trade, context):
    return _run_agent('oi', trade, context)

def agent_whale(trade, context):
    return _run_agent('whale', trade, context)

def agent_liq(trade, context):
    return _run_agent('liq', trade, context)

def agent_regime(trade, context):
    return _run_agent('regime', trade, context)

def agent_sl(trade, context):
    return _run_agent('sl', trade, context)

def agent_tp(trade, context):
    return _run_agent('tp', trade, context)


# Registry — every agent defined above plus its component key
AGENTS = {
    'rsi':       agent_rsi,
    'pivot':     agent_pivot,
    'cooldown':  agent_cooldown,
    'bollinger': agent_bollinger,
    'adx':       agent_adx,
    'ema':       agent_ema,
    'ob':        agent_ob,
    'wall':      agent_wall,
    'cvd':       agent_cvd,
    'fvg':       agent_fvg,
    'fib':       agent_fib,
    'sr':        agent_sr,
    'structure': agent_structure,
    'funding':   agent_funding,
    'session':   agent_session,
    'oi':        agent_oi,
    'whale':     agent_whale,
    'liq':       agent_liq,
    'regime':    agent_regime,
    'sl':        agent_sl,
    'tp':        agent_tp,
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
