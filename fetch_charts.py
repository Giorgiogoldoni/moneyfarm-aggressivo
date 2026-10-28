#!/usr/bin/env python3
"""fetch_charts.py — moneyfarm-aggressivo
Scarica da Yahoo i prezzi (2 anni) dei 10 ETF in config.json e genera
data/charts/{TICKER}.json nel formato standard scannerv3.
Indicatori e motore segnali (BUY1/BUY2/BUY3/EXIT1/EXIT2) IDENTICI a core/generate_charts.py.
Fonte dati propria del repo: nessuna dipendenza da core o scannerv2. Segnali solo informativi.
"""
import json, math, os, time
import yfinance as yf

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHARTS_DIR = os.path.join(BASE_DIR, "data", "charts")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
SLEEP_BETWEEN_TICKERS = 0.5

#  per restare coerenti con lo standard grafico condiviso tra i repo.
# ═══════════════════════════════════════════════════════

def calc_kama(close, n=10, fast=2, slow=30):
    fast_sc = 2 / (fast + 1)
    slow_sc = 2 / (slow + 1)
    kama = [None] * len(close)
    if len(close) <= n:
        return kama
    kama[n] = close[n]
    for i in range(n + 1, len(close)):
        direction = abs(close[i] - close[i - n])
        volatility = sum(abs(close[j] - close[j - 1]) for j in range(i - n + 1, i + 1))
        er = direction / volatility if volatility != 0 else 0
        sc = (er * (fast_sc - slow_sc) + slow_sc) ** 2
        kama[i] = kama[i - 1] + sc * (close[i] - kama[i - 1])
    return kama


def calc_sar_array(high, low, af0=0.02, af_max=0.20):
    n = len(high)
    sar_arr = [None] * n
    bull_arr = [None] * n
    if n < 5:
        return sar_arr, bull_arr
    sar = low[0]
    ep = high[0]
    af = af0
    bull = True
    sar_arr[0] = round(sar, 4)
    bull_arr[0] = bull
    for i in range(1, n):
        if bull:
            new_sar = sar + af * (ep - sar)
            new_sar = min(new_sar, low[max(0, i - 1)], low[max(0, i - 2)])
            if low[i] < new_sar:
                bull = False
                new_sar = ep
                ep = low[i]
                af = af0
            else:
                if high[i] > ep:
                    ep = high[i]
                    af = min(af + af0, af_max)
        else:
            new_sar = sar + af * (ep - sar)
            new_sar = max(new_sar, high[max(0, i - 1)], high[max(0, i - 2)])
            if high[i] > new_sar:
                bull = True
                new_sar = ep
                ep = high[i]
                af = af0
            else:
                if low[i] < ep:
                    ep = low[i]
                    af = min(af + af0, af_max)
        sar = new_sar
        sar_arr[i] = round(sar, 4)
        bull_arr[i] = bull
    return sar_arr, bull_arr


def calc_ao_array(high, low):
    mid = [(h + l) / 2 for h, l in zip(high, low)]
    result = [None] * len(mid)
    for i in range(33, len(mid)):
        sma5 = sum(mid[i - 4:i + 1]) / 5
        sma34 = sum(mid[i - 33:i + 1]) / 34
        result[i] = round(sma5 - sma34, 4)
    return result


def calc_rsi_array(close, n=14):
    result = [None] * len(close)
    if len(close) < n + 2:
        return result
    for i in range(n, len(close)):
        gains = 0.0
        losses = 0.0
        for j in range(i - n + 1, i + 1):
            d = close[j] - close[j - 1]
            if d > 0:
                gains += d
            else:
                losses += -d
        ag = gains / n
        al = losses / n
        result[i] = round(100 - 100 / (1 + ag / al), 2) if al > 0 else 100.0
    return result


def calc_trend_array(close, kama, n=20):
    """Etichetta di trend SOLO INFORMATIVA (non modifica i segnali).
    R = KAMA in calo su n barre e prezzo sotto KAMA; U = KAMA in salita e prezzo sopra; L = altrimenti."""
    result = [None] * len(close)
    for i in range(n, len(close)):
        k, k0 = kama[i], kama[i - n]
        if k is None or k0 is None:
            continue
        result[i] = "R" if (k < k0 and close[i] < k) else "U" if (k > k0 and close[i] > k) else "L"
    return result


def calc_er_array(close, n=10):
    result = [0] * len(close)
    for i in range(n, len(close)):
        direction = abs(close[i] - close[i - n])
        volatility = sum(abs(close[j] - close[j - 1]) for j in range(i - n + 1, i + 1))
        result[i] = round(direction / volatility, 4) if volatility != 0 else 0
    return result


def calc_baffetti_array(high, low):
    """Barre consecutive con mid-price in salita."""
    mid = [(h + l) / 2 for h, l in zip(high, low)]
    result = [0] * len(mid)
    streak = 0
    for i in range(1, len(mid)):
        streak = streak + 1 if mid[i] > mid[i - 1] else 0
        result[i] = streak
    return result


def calc_mm_align_array(close):
    n = len(close)
    result = [False] * n
    cum = [0.0] * (n + 1)
    for i in range(n):
        cum[i + 1] = cum[i] + close[i]

    def avg(i, w):
        return (cum[i + 1] - cum[i + 1 - w]) / w if i + 1 >= w else None

    for i in range(n):
        mm20, mm50, mm100 = avg(i, 20), avg(i, 50), avg(i, 100)
        if mm20 is not None and mm50 is not None and mm100 is not None:
            result[i] = close[i] > mm20 > mm50 > mm100
    return result


def calc_cross_days_array(close, kama):
    n = len(close)
    result = [999] * n
    last_flip = None
    prev_above = None
    for i in range(n):
        if kama[i] is None:
            continue
        above = close[i] > kama[i]
        if prev_above is None:
            prev_above = above
            last_flip = i
            result[i] = 0
            continue
        if above != prev_above:
            last_flip = i
            prev_above = above
        result[i] = i - last_flip
    return result


def calc_ao_improving_array(ao):
    n = len(ao)
    result = [False] * n
    for i in range(1, n):
        if ao[i] is not None and ao[i - 1] is not None and ao[i] > ao[i - 1]:
            result[i] = True
    return result


def calc_segnale_array(close, kama, er_arr, baff_arr, ao_imp_arr, sar_bull_arr, cross_arr, mm_arr, rsi_arr):
    """Motore di segnale nativo del grafico (BUY1/BUY2/BUY3/EXIT1/EXIT2/MEAN REV/WATCH)."""
    n = len(close)
    result = [None] * n
    for i in range(n):
        if kama[i] is None or sar_bull_arr[i] is None:
            continue
        lk = kama[i]
        lc = close[i]
        above_kama = lc > lk if lk else False
        sar_bull = sar_bull_arr[i]
        cross = cross_arr[i]
        ao_imp = ao_imp_arr[i]
        baff = baff_arr[i]
        er = er_arr[i]
        mm_align = mm_arr[i]
        rsi = rsi_arr[i] if rsi_arr[i] is not None else 50
        if sar_bull and cross <= 3 and ao_imp:
            result[i] = "BUY1"
        elif above_kama and baff >= 2:
            result[i] = "BUY2"
        elif above_kama and er >= 0.50 and baff >= 3 and mm_align:
            result[i] = "BUY3"
        elif not above_kama and not sar_bull:
            result[i] = "EXIT2"
        elif not sar_bull:
            result[i] = "EXIT1"
        else:
            near_kama = abs(lc - lk) / lk < 0.03 if lk and lk > 0 else False
            if er < 0.30 and rsi < 30 and ao_imp and (near_kama or not above_kama):
                result[i] = "MEAN REV"
            else:
                result[i] = "WATCH"
    return result


# ═══════════════════════════════════════════════════════
#  RENKO — brick adattivo su ATR(14)
# ═══════════════════════════════════════════════════════

def calc_atr(high, low, close, n=14):
    trs = []
    for i in range(len(close)):
        if i == 0:
            trs.append(high[i] - low[i])
        else:
            trs.append(max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1])))
    if len(trs) < n + 1:
        return None
    atr = sum(trs[1:n + 1]) / n
    for i in range(n + 1, len(trs)):
        atr = (atr * (n - 1) + trs[i]) / n
    return atr


def calc_renko(dates, close, brick_size):
    if not brick_size or brick_size <= 0 or len(close) < 2:
        return []
    bricks = []
    level = close[0]
    direction = 0
    for i in range(1, len(close)):
        price = close[i]
        d = dates[i] if i < len(dates) else None
        while True:
            if direction >= 0 and price >= level + brick_size:
                new_level = level + brick_size
                bricks.append({"o": round(level, 4), "c": round(new_level, 4), "dir": 1, "d": d})
                level = new_level
                direction = 1
                continue
            if direction <= 0 and price <= level - brick_size:
                new_level = level - brick_size
                bricks.append({"o": round(level, 4), "c": round(new_level, 4), "dir": -1, "d": d})
                level = new_level
                direction = -1
                continue
            if direction == 1 and price <= level - 2 * brick_size:
                new_level = level - brick_size
                bricks.append({"o": round(level, 4), "c": round(new_level, 4), "dir": -1, "d": d})
                level = new_level
                direction = -1
                continue
            if direction == -1 and price >= level + 2 * brick_size:
                new_level = level + brick_size
                bricks.append({"o": round(level, 4), "c": round(new_level, 4), "dir": 1, "d": d})
                level = new_level
                direction = 1
                continue
            break
    return bricks[-200:]


def calc_sar_streak_array(sarBull_arr):
    n = len(sarBull_arr)
    streak = [0] * n
    for i in range(1, n):
        if sarBull_arr[i] is None or sarBull_arr[i - 1] is None:
            continue
        streak[i] = streak[i - 1] + 1 if sarBull_arr[i] == sarBull_arr[i - 1] else 0
    return streak


def sanitize_nan(obj):
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {k: sanitize_nan(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_nan(v) for v in obj]
    return obj


def fmt(arr):
    return [round(v, 4) if isinstance(v, (int, float)) else v for v in arr]


# ═══════════════════════════════════════════════════════
#  TRADE SIMULATION — "Posizioni Aperte"
#  Porting adattato da chart/fetch.py (simulate_trades/perf_stats),
#  riscritto sul vocabolario nativo del motore core:
#  entrata: BUY1/BUY2/BUY3 — uscita: EXIT1/EXIT2.
#  WATCH e MEAN REV non aprono né chiudono (il concetto di
#  mean-reversion è già implicito nelle condizioni di BUY, come da
#  scelta esplicita — nessun motore MR separato).
# ═══════════════════════════════════════════════════════

ENTRY_SIGNALS = ("BUY1", "BUY2", "BUY3")
EXIT_SIGNALS = ("EXIT1", "EXIT2")

# Note a template (v1) — associate al codice segnale di entrata/uscita.
# Non leggono i valori esatti degli indicatori in quel giorno (versione
# "narrativa" dinamica, più costosa, valutabile in seguito): sono frasi
# fisse ma correttamente informative sul perché del segnale.
ENTRY_NOTES = {
    "BUY1": "Entrata: inversione SAR rialzista con momentum in miglioramento (AO) e incrocio KAMA recente.",
    "BUY2": "Entrata: prezzo sopra KAMA con pattern Baffetti forte.",
    "BUY3": "Entrata: prezzo sopra KAMA, Efficiency Ratio elevato e medie allineate (trend maturo).",
}
EXIT_NOTES = {
    "EXIT1": "Uscita: inversione SAR ribassista (perdita di trend).",
    "EXIT2": "Uscita: prezzo sotto KAMA e SAR ribassista (trend invertito).",
    "OPEN": "Posizione ancora aperta — nessun segnale di uscita ricevuto.",
}


def simulate_trades(dates, closes, segnale_arr):
    """Deriva i trade storici (chiusi + eventuale aperto) dalla sequenza segnali.
    Ogni trade: data/prezzo entrata e uscita, %var, giorni, segnali, nota."""
    trades = []
    in_trade = False
    ent_i = -1
    ent_sig = None
    n = len(closes)
    for i in range(n):
        sig = segnale_arr[i]
        if not in_trade:
            if sig in ENTRY_SIGNALS:
                in_trade = True
                ent_i = i
                ent_sig = sig
        else:
            is_last = (i == n - 1)
            if sig in EXIT_SIGNALS or is_last:
                exit_sig = sig if sig in EXIT_SIGNALS else "OPEN"
                is_open = exit_sig == "OPEN"
                pnl = round((closes[i] - closes[ent_i]) / closes[ent_i] * 100, 2) if closes[ent_i] else 0
                trades.append({
                    "dataEntrata": dates[ent_i], "dataUscita": None if is_open else dates[i],
                    "prezzoEntrata": closes[ent_i], "prezzoUscita": None if is_open else closes[i],
                    "entSig": ent_sig, "exitSig": exit_sig,
                    "pnlPct": pnl, "giorni": i - ent_i, "isOpen": is_open,
                    "note": ENTRY_NOTES.get(ent_sig, "") + (" " + EXIT_NOTES.get(exit_sig, "") if exit_sig in EXIT_NOTES else ""),
                })
                if not is_open:
                    in_trade = False
    return trades


def perf_stats(trades):
    closed = [t for t in trades if not t["isOpen"]]
    if not closed:
        return {"trades": len(trades), "closed": 0, "wins": 0, "wr": 0, "totalPnl": 0, "best": 0, "worst": 0, "avg": 0, "avgWin": 0, "avgLoss": 0, "dd": 0}
    wins = [t for t in closed if t["pnlPct"] > 0]
    losses = [t for t in closed if t["pnlPct"] <= 0]
    pnls = [t["pnlPct"] for t in closed]
    win_pnls = [t["pnlPct"] for t in wins]
    loss_pnls = [t["pnlPct"] for t in losses]
    total_pnl = sum(pnls)
    peak = eq = dd = 0
    for p in pnls:
        eq += p
        if eq > peak:
            peak = eq
        if peak - eq > dd:
            dd = peak - eq
    return {
        "trades": len(trades), "closed": len(closed), "wins": len(wins),
        "wr": round(len(wins) / len(closed) * 100, 1) if closed else 0,
        "totalPnl": round(total_pnl, 2),
        "best": round(max(pnls), 2), "worst": round(min(pnls), 2),
        "avg": round(total_pnl / len(closed), 2) if closed else 0,
        "avgWin": round(sum(win_pnls) / len(win_pnls), 2) if win_pnls else 0,
        "avgLoss": round(sum(loss_pnls) / len(loss_pnls), 2) if loss_pnls else 0,
        "dd": round(dd, 2),
    }


# ═══════════════════════════════════════════════════════
#  PROCESS TICKER
# ═══════════════════════════════════════════════════════

def process_ticker(info):
    symbol = info["yahoo"]
    try:
        hist = yf.Ticker(symbol).history(period="2y", interval="1d", timeout=20)
        if hist.empty or len(hist) < 60:
            return None
        opens = [round(float(x), 4) for x in hist["Open"].values]
        highs = [round(float(x), 4) for x in hist["High"].values]
        lows = [round(float(x), 4) for x in hist["Low"].values]
        closes = [round(float(x), 4) for x in hist["Close"].values]
        vols = [int(x) for x in hist["Volume"].values]
        dates = [ts.strftime("%Y-%m-%d") for ts in hist.index]
        ts_d = [int(ts.timestamp()) for ts in hist.index]
        d_bars = [[ts_d[i], opens[i], highs[i], lows[i], closes[i], vols[i]] for i in range(len(closes))]

        kama = calc_kama(closes)
        sar, sar_bull = calc_sar_array(highs, lows)
        ao = calc_ao_array(highs, lows)
        rsi = calc_rsi_array(closes)
        er = calc_er_array(closes)
        baff = calc_baffetti_array(highs, lows)
        mm = calc_mm_align_array(closes)
        cross = calc_cross_days_array(closes, kama)
        ao_imp = calc_ao_improving_array(ao)
        seg = calc_segnale_array(closes, kama, er, baff, ao_imp, sar_bull, cross, mm, rsi)
        atr = calc_atr(highs, lows, closes, 14)
        trades = simulate_trades(dates, closes, seg)
        perf = perf_stats(trades)
        result = {
            "ticker": info["ticker"], "yahoo": symbol, "name": info.get("name"),
            "asset_class": "Azionario", "isin": info.get("isin"),
            "d": d_bars, "kama_d": fmt(kama), "sar_d": fmt(sar), "sarBull_d": sar_bull,
            "ao_d": fmt(ao), "rsi_d": fmt(rsi), "baff_d": baff, "segnale_d": seg,
            "er_d": fmt(er), "trend_d": calc_trend_array(closes, kama),
            "atr": round(atr, 4) if atr else None,
            "trades": trades[-30:], "perf": perf,
            "open_trade": next((t for t in trades if t["isOpen"]), None),
        }
        return sanitize_nan(result)
    except Exception as e:
        print(f"  ERR {symbol}: {e}")
        return None


def main():
    cfg = json.load(open(CONFIG_PATH, encoding="utf-8"))
    os.makedirs(CHARTS_DIR, exist_ok=True)
    index, falliti = [], []
    for pos in cfg["posizioni"]:
        r = process_ticker(pos)
        if r is None:
            falliti.append(pos["yahoo"])
            print(f"KO  {pos['yahoo']}")
            continue
        fname = pos["yahoo"].replace(".", "_") + ".json"
        with open(os.path.join(CHARTS_DIR, fname), "w", encoding="utf-8") as f:
            json.dump(r, f, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        last = r["segnale_d"][-1] if r["segnale_d"] else None
        index.append({"yahoo": pos["yahoo"], "segnale": last, "prezzo": r["d"][-1][4],
                      "ultimo_dato": r["d"][-1][0], "isOpen": bool(r["open_trade"])})
        print(f"OK  {pos['yahoo']}  {last}")
        time.sleep(SLEEP_BETWEEN_TICKERS)
    with open(os.path.join(CHARTS_DIR, "index.json"), "w", encoding="utf-8") as f:
        json.dump({"generato": time.strftime("%Y-%m-%d %H:%M:%S"), "falliti": falliti, "ticker": index},
                  f, ensure_ascii=False, allow_nan=False)
    if falliti:
        print("ATTENZIONE ticker senza dati:", falliti)


if __name__ == "__main__":
    main()
