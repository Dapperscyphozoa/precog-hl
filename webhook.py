#!/usr/bin/env python3
"""PreCog Webhook — DynaPro signal receiver + HL executor
Runs as separate Render web service (free tier).
Receives TradingView alerts, executes on HL directly.
Runs alongside precog.py background worker — both trade same MAIN wallet.
"""
import os, json, time, math
from datetime import datetime
from flask import Flask, request as req, jsonify
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants
from eth_account import Account

WALLET   = os.environ.get('HYPERLIQUID_ACCOUNT','')
PRIV_KEY = os.environ.get('HL_PRIVATE_KEY','')
SECRET   = os.environ.get('WEBHOOK_SECRET','precog_dynapro_2026')

LEV = 10
RISK_PCT = 0.05
TP_PCT = 0.008  # +0.8% TP
MAX_POSITIONS = 20

info = Info(constants.MAINNET_API_URL, skip_ws=True)
account = Account.from_key(PRIV_KEY) if PRIV_KEY else None
exchange = Exchange(account, constants.MAINNET_API_URL, account_address=WALLET) if account else None

app = Flask(__name__)
trades_log = []  # in-memory trade log

def log(m): print(f"[{datetime.utcnow().isoformat()}] {m}", flush=True)

# HL meta cache for price/size rounding
_META = None
def _sz_dec(coin):
    global _META
    if _META is None:
        try: _META = {u['name']:int(u.get('szDecimals',0)) for u in info.meta()['universe']}
        except: _META = {}
    return _META.get(coin, 2)

def round_price(coin, px):
    szD = _sz_dec(coin)
    max_dec = max(0, 6 - szD)
    if px > 0:
        sig = 10 ** (5 - int(math.floor(math.log10(abs(px)))) - 1)
        px = round(px * sig) / sig
    return round(px, max_dec)

def round_size(coin, sz):
    return round(sz, _sz_dec(coin))

def tv_to_hl(ticker):
    t = ticker.upper().replace('USDT.P','').replace('.P','').replace('USDT','').replace('USD','').replace('PERP','')
    remap = {'BONK':'kBONK','PEPE':'kPEPE','SHIB':'kSHIB','MATIC':'POL',
             '1000BONK':'kBONK','1000PEPE':'kPEPE','1000SHIB':'kSHIB'}
    return remap.get(t, t)

def get_balance():
    try: return float(info.user_state(WALLET)['marginSummary']['accountValue'])
    except: return 0

def get_mid(coin):
    try: return float(info.all_mids()[coin])
    except: return None

def get_positions():
    out = {}
    try:
        for p in info.user_state(WALLET).get('assetPositions',[]):
            pos = p['position']
            sz = float(pos.get('szi',0))
            if sz != 0:
                out[pos['coin']] = {'size':sz,'entry':float(pos['entryPx']),'pnl':float(pos['unrealizedPnl'])}
    except: pass
    return out

def set_lev(coin):
    try: exchange.update_leverage(LEV, coin, is_cross=False)
    except: pass

def place_order(coin, is_buy, size):
    px = get_mid(coin)
    if not px: return None
    set_lev(coin)
    size = round_size(coin, size)
    if size <= 0: return None
    # Maker attempt
    maker_px = round_price(coin, px * (0.9998 if is_buy else 1.0002))
    try:
        r = exchange.order(coin, is_buy, size, maker_px, {'limit':{'tif':'Alo'}}, reduce_only=False)
        st = r.get('response',{}).get('data',{}).get('statuses',[{}])[0] if r else {}
        if 'filled' in st: return px
        if 'resting' in st:
            oid = st['resting'].get('oid')
            for _ in range(15):
                time.sleep(1)
                pos = get_positions()
                if coin in pos: return px
            try: exchange.cancel(coin, oid)
            except: pass
    except Exception as e:
        log(f"maker err {coin}: {e}")
    # Taker fallback
    px = get_mid(coin) or px
    slip = round_price(coin, px * (1.005 if is_buy else 0.995))
    try:
        r = exchange.order(coin, is_buy, size, slip, {'limit':{'tif':'Ioc'}}, reduce_only=False)
        st = r.get('response',{}).get('data',{}).get('statuses',[{}])[0] if r else {}
        if 'error' in st: log(f"taker rejected {coin}: {st}"); return None
        return px
    except Exception as e:
        log(f"taker err {coin}: {e}"); return None

def close_position(coin):
    pos = get_positions().get(coin)
    if not pos: return None
    is_buy = pos['size'] < 0
    size = round_size(coin, abs(pos['size']))
    px = get_mid(coin)
    if not px: return None
    slip = round_price(coin, px * (1.005 if is_buy else 0.995))
    try:
        exchange.order(coin, is_buy, size, slip, {'limit':{'tif':'Ioc'}}, reduce_only=True)
        pnl = pos['pnl']
        log(f"CLOSED {coin} pnl=${pnl:+.3f}")
        return pnl
    except Exception as e:
        log(f"close err {coin}: {e}"); return None

# ═══════════════════════════════════════════════════════
# FLASK ROUTES
# ═══════════════════════════════════════════════════════
@app.route('/health', methods=['GET'])
def health():
    eq = get_balance()
    pos = get_positions()
    return jsonify({'status':'ok','equity':eq,'positions':len(pos),
                    'trades':len(trades_log),'version':'webhook-v1'})

@app.route('/webhook', methods=['POST'])
@app.route('/signal', methods=['POST'])
def webhook():
    """DynaPro signal from TradingView.
    JSON: {"ticker":"BTCUSD","action":"buy|sell|exit_buy|exit_sell","price":12345}
    Text: "buy BTCUSD 12345"
    """
    try:
        data = req.get_json(force=True, silent=True)
        if not data:
            text = req.get_data(as_text=True).strip()
            parts = text.split()
            if len(parts) >= 2:
                data = {'action':parts[0].lower(),'ticker':parts[1]}
                if len(parts) >= 3:
                    try: data['price'] = float(parts[2])
                    except: pass
    except:
        return jsonify({'error':'bad payload'}), 400

    if not data or 'ticker' not in data or 'action' not in data:
        return jsonify({'error':'need ticker + action'}), 400

    coin = tv_to_hl(data['ticker'])
    action = data['action'].lower().replace(' ','_')
    price = data.get('price', 0)

    if action not in ('buy','sell','exit_buy','exit_sell'):
        return jsonify({'error':f'unknown action: {action}'}), 400

    log(f"WEBHOOK IN: {action} {coin} @ {price}")
    result = {'coin':coin,'action':action,'price':price}

    try:
        positions = get_positions()
        equity = get_balance()

        if action in ('exit_buy','exit_sell'):
            if coin in positions:
                pnl = close_position(coin)
                result['executed'] = 'closed'
                result['pnl'] = pnl
            else:
                result['executed'] = 'no_position'

        elif action in ('buy','sell'):
            is_buy = (action == 'buy')
            existing = positions.get(coin)
            # Close opposite if exists
            if existing:
                is_opposite = (is_buy and existing['size']<0) or (not is_buy and existing['size']>0)
                if is_opposite:
                    close_position(coin)
                else:
                    result['executed'] = 'already_positioned'
                    trades_log.append(result)
                    return jsonify(result), 200

            # Open new position
            if len(positions) < MAX_POSITIONS:
                px = get_mid(coin)
                if px:
                    size = equity * RISK_PCT * LEV / px
                    fill = place_order(coin, is_buy, size)
                    if fill:
                        result['executed'] = 'opened'
                        result['fill'] = fill
                    else:
                        result['executed'] = 'order_failed'
                else:
                    result['executed'] = 'no_price'
            else:
                result['executed'] = 'max_positions'

    except Exception as e:
        log(f"webhook exec err: {e}")
        result['error'] = str(e)

    trades_log.append(result)
    if len(trades_log) > 500: trades_log.pop(0)
    log(f"WEBHOOK OUT: {result}")
    return jsonify(result), 200

@app.route('/trades', methods=['GET'])
def get_trades():
    return jsonify(trades_log[-50:])

@app.route('/positions', methods=['GET'])
def get_pos():
    return jsonify(get_positions())

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    log(f"PreCog Webhook v1 | wallet={WALLET} | port={port}")
    log(f"Endpoint: POST /webhook or /signal")
    log(f"Health:   GET /health")
    log(f"Trades:   GET /trades")
    log(f"Positions: GET /positions")
    app.run(host='0.0.0.0', port=port, debug=False)
