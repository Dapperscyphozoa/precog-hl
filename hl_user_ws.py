"""
hl_user_ws.py — Hyperliquid user-channel WebSocket subscriber.

Feeds position_ledger from HL's user-event WebSocket channels. This is the
"WS = truth" layer of the event-sourced execution model.

Channels subscribed:
  webData2     — snapshot of positions + open orders + balances. Sent on
                 connect and on changes. This is the AUTHORITATIVE size feed.
  userFills    — granular fill events (entry/exit). Used for VWAP entry px.
  orderUpdates — order state transitions (open / filled / canceled / rejected).
                 Used to track sl_oid / tp_oid / detect rejections.

Why not info.user_state() polling?
  Polling on a tick loop hits CloudFront rate limits and creates a window
  where post-fill state is unknown until next poll lands. WS gives us
  push-based truth with sub-second latency and zero REST cost.

Reconnect strategy:
  HL SDK's Info.subscribe() handles WS lifecycle internally. On reconnect
  we lose nothing: webData2 sends a full snapshot, which on_webdata2()
  reconciles authoritatively.

Failure mode:
  If WS goes silent > 30s, position_ledger.ws_is_fresh() returns False.
  Callers (e.g. is_live()) keep working — they read last-known state —
  but the snapshot reconciler will detect divergence and alert.
"""

import logging
import time
import threading

import position_ledger

log = logging.getLogger("hl_user_ws")


class HLUserWS:
    def __init__(self, info, wallet):
        self.info = info
        self.wallet = wallet
        self._subs_active = False
        self._lock = threading.Lock()
        self._sub_ids = []
        self._stats = {
            'webdata2_msgs':    0,
            'fills_msgs':       0,
            'order_msgs':       0,
            'subscribe_errors': 0,
        }

    # ─── Callbacks (run on SDK's WS thread) ───────────────────────────
    def _on_webdata2(self, msg):
        try:
            self._stats['webdata2_msgs'] += 1
            data = msg.get('data') or {}
            ch_state = data.get('clearinghouseState') or {}
            asset_positions = ch_state.get('assetPositions') or []
            # webData2 includes openOrders directly under data
            open_orders = data.get('openOrders') or []
            position_ledger.on_webdata2(asset_positions, open_orders)
            position_ledger.mark_ws_connected(True)
        except Exception as e:
            log.warning(f"webdata2 handler err: {e}")

    def _on_user_fills(self, msg):
        try:
            self._stats['fills_msgs'] += 1
            data = msg.get('data') or {}
            fills = data.get('fills') or []
            # Spec: each fill has coin, side ('B' or 'A'), sz, px, time, oid, cloid
            for f in fills:
                coin = f.get('coin')
                if not coin: continue
                side = f.get('side')
                if side not in ('B', 'A'): continue
                try:
                    sz   = float(f.get('sz') or 0)
                    px   = float(f.get('px') or 0)
                    ts_ms = int(f.get('time') or (time.time() * 1000))
                except (TypeError, ValueError):
                    continue
                if sz <= 0 or px <= 0: continue
                position_ledger.on_fill(
                    coin, side, sz, px, ts_ms,
                    oid=f.get('oid'), cloid=f.get('cloid'),
                )
            position_ledger.mark_ws_connected(True)
        except Exception as e:
            log.warning(f"userFills handler err: {e}")

    def _on_order_updates(self, msg):
        try:
            self._stats['order_msgs'] += 1
            data = msg.get('data') or msg.get('orders') or []
            # orderUpdates payload is typically a list of order updates
            updates = data if isinstance(data, list) else (data.get('orders') or [])
            for o in updates:
                coin = o.get('coin')
                if not coin: continue
                # Status can be one of: 'open', 'filled', 'canceled', 'rejected',
                # 'triggered', 'marginCanceled' depending on HL's exact payload.
                status = (o.get('status') or '').lower()
                position_ledger.on_order_update(
                    coin,
                    oid=o.get('oid'),
                    status=status,
                    cloid=o.get('cloid'),
                )
            position_ledger.mark_ws_connected(True)
        except Exception as e:
            log.warning(f"orderUpdates handler err: {e}")

    # ─── Lifecycle ────────────────────────────────────────────────────
    def start(self):
        """Subscribe to the three user channels. Idempotent."""
        with self._lock:
            if self._subs_active:
                return
            try:
                # webData2 — primary authoritative snapshot
                sid = self.info.subscribe(
                    {"type": "webData2", "user": self.wallet},
                    self._on_webdata2,
                )
                self._sub_ids.append(('webData2', sid))
                log.info(f"subscribed webData2 (user={self.wallet[:10]}…)")
            except Exception as e:
                self._stats['subscribe_errors'] += 1
                log.error(f"webData2 subscribe failed: {e}")
            try:
                sid = self.info.subscribe(
                    {"type": "userFills", "user": self.wallet},
                    self._on_user_fills,
                )
                self._sub_ids.append(('userFills', sid))
                log.info("subscribed userFills")
            except Exception as e:
                self._stats['subscribe_errors'] += 1
                log.error(f"userFills subscribe failed: {e}")
            try:
                sid = self.info.subscribe(
                    {"type": "orderUpdates", "user": self.wallet},
                    self._on_order_updates,
                )
                self._sub_ids.append(('orderUpdates', sid))
                log.info("subscribed orderUpdates")
            except Exception as e:
                self._stats['subscribe_errors'] += 1
                log.error(f"orderUpdates subscribe failed: {e}")
            self._subs_active = True
            position_ledger.mark_ws_connected(True)

    def status(self):
        with self._lock:
            return {
                'subs_active': self._subs_active,
                'channels':    [c for c, _ in self._sub_ids],
                **self._stats,
            }


_INSTANCE = None


def init(info, wallet):
    """Initialize singleton + start subscriptions. Idempotent."""
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = HLUserWS(info, wallet)
        _INSTANCE.start()
    return _INSTANCE


def status():
    return _INSTANCE.status() if _INSTANCE else {'subs_active': False}
