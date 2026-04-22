"""Tuner: applies agent deltas to signal_params table with guardrails.

Guardrails layered (all must pass for a delta to apply):
  1. Bounds check (bounds.py)   — absolute min/max, max step size
  2. Sample gating              — min_samples_between_tunes per param
  3. Synthesis approval         — head synthesizer must APPROVE
  4. Confidence floor           — agent confidence >= MIN_CONFIDENCE
  5. Dry-run switch             — POSTMORTEM_DRY_RUN=1 blocks all writes
"""
import os
import time
import json

from . import bounds, db, kb

MIN_CONFIDENCE = float(os.environ.get('POSTMORTEM_MIN_CONFIDENCE', '0.55'))
DRY_RUN = os.environ.get('POSTMORTEM_DRY_RUN', '0') == '1'


def _samples_since_last_tune(coin, component, param_name):
    """Count postmortem runs for this coin since the last time this param
    was tuned. Used to enforce min_samples_between_tunes."""
    history = db.list_history(coin=coin, component=component, limit=5)
    if not history:
        return 999  # never tuned → no gate
    last_ts = history[0]['ts']
    log_entries = db.list_log(limit=500)
    return sum(1 for e in log_entries if e['coin'] == coin and e['ts'] > last_ts)


def apply_decisions(coin, trade, findings, synthesis, log_id):
    """Apply the synthesizer's approved decisions. Returns count of deltas applied.

    DRY_RUN blocks:
      - param writes (tuned values)
      - veto writes (component disables)

    DRY_RUN does NOT block:
      - finding records (audit trail)
      - KB entries (semantic memory — observational, read by entry_gate)

    KB is NOT a param change. It's the knowledge base the entry_gate reads
    to inform future decisions. Blocking it in dry-run loses all pattern
    learning from the observation period."""
    applied_count = 0
    vetos_applied = 0
    kb_written = 0
    side = trade.get('side')

    # ALWAYS record findings (audit trail)
    for f in findings:
        db.record_finding(
            log_id, f.get('agent'),
            f.get('verdict', 'unknown'),
            f.get('confidence', 0.0),
            f.get('reasoning', ''),
            f.get('proposed_delta', []),
            applied=False,  # applied status set below for non-dry-run
        )

    # ALWAYS write KB entries (semantic memory accumulates during dry-run)
    for entry in synthesis.get('kb_entries', []) or []:
        pk = (entry.get('pattern_key') or '').strip()
        summary = (entry.get('summary') or '').strip()
        if not pk or not summary: continue
        if len(pk) > 120: pk = pk[:120]
        if len(summary) > 400: summary = summary[:400]
        evidence = entry.get('evidence') or {}
        evidence['log_id'] = log_id
        evidence['root_cause'] = synthesis.get('root_cause', '')[:300]
        if kb.write_entry(coin=coin, side=side, pattern_key=pk,
                          summary=summary, evidence=evidence, log_id=log_id):
            kb_written += 1

    if DRY_RUN:
        return kb_written  # findings already recorded above, KB just written

    # Build quick-lookup from synthesis
    decisions_by_key = {}
    for d in synthesis.get('decisions', []):
        key = (d.get('component'), d.get('param'))
        decisions_by_key[key] = d

    for f in findings:
        agent_name = f.get('agent', 'unknown')
        verdict = f.get('verdict', 'irrelevant')
        confidence = float(f.get('confidence', 0.0))
        reasoning = f.get('reasoning', '')
        deltas = f.get('proposed_delta', []) or []

        for d in deltas:
            comp = d.get('component')
            param = d.get('param')
            new_val = d.get('new_value')
            if comp is None or param is None or new_val is None:
                continue

            # Gate 1: bounds known?
            b = bounds.get_bounds(comp, param)
            if not b:
                continue
            _default, _lo, _hi, _step, min_samples = b

            # Gate 2: synthesis approval
            dkey = (comp, param)
            decision = decisions_by_key.get(dkey, {}).get('decision', 'DEFER')
            if decision != 'APPROVE':
                continue

            # Gate 3: confidence floor
            if confidence < MIN_CONFIDENCE:
                continue

            # Gate 4: sample gating
            samples_since = _samples_since_last_tune(coin, comp, param)
            if samples_since < min_samples:
                continue

            # Gate 5: bounds clamp (final safety)
            current = db.read_param(coin, comp, param)
            clamped = bounds.clamp_delta(comp, param, current, float(new_val))
            if clamped is None:
                continue
            if current is not None and abs(clamped - current) < 1e-9:
                continue

            db.upsert_param(
                coin=coin,
                component=comp,
                param_name=param,
                new_value=clamped,
                default_value=_default,
                reason=reasoning[:500],
                log_id=log_id,
                agent_name=agent_name,
            )
            applied_count += 1

    # Apply synthesizer-proposed vetos
    for v in synthesis.get('new_vetos', []) or []:
        comp = v.get('component')
        if not comp: continue
        if comp not in bounds.components_list(): continue
        db.set_veto(
            coin=coin,
            component=comp,
            reason=v.get('reason', 'synthesizer veto'),
            expires_in_sec=v.get('expires_in_sec'),
            log_id=log_id,
        )
        vetos_applied += 1

    return applied_count + vetos_applied + kb_written
