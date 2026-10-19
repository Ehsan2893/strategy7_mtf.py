# Strategy 7 (multi-timeframe): Supertrend + ADX + EMA200 TREND on 1h, pullback + RSI CONFIRMATION on 15m,
# ENTRY on 5m. Top-5 coins, risk:reward 1:2 / 1:3 / 1:4, last 200 trades per coin.
# SELF-CONTAINED. Data: OKX public 5m candles (15m and 1h are built from them). NO real money.
#   python strategy7_mtf.py 365 200      (days of 5m history, trades per coin)
import requests, time, sys, csv
from datetime import datetime, timezone

OKX = "https://www.okx.com/api/v5"
COINS = ["BTC", "ETH", "BNB", "XRP", "SOL"]
RRS = [2.0, 3.0, 4.0]
RISK = 0.005        # 0.5% of capital risked per trade (equity curve in the report)

CFG = dict(
    st_n=10, st_mult=3.0,          # Supertrend (1h)
    ema_slow=200, adx_n=14, adx_min=25.0,    # 1h trend filter
    ema_fast=20, rsi_n=14, atr_n=14,         # 15m pullback / confirmation
    long_rsi=(40.0, 55.0), short_rsi=(45.0, 60.0),
    zone_atr=0.5,       # "near EMA20" = within this many ATRs (15m) of EMA20
    ext_atr=1.0,        # earlier move away from EMA20 (ATRs) that makes the return a pullback
    ext_bars=10,        # ...seen within the last N 15m bars
    stop_bars=12,       # stop beyond the extreme of the last N 5m candles (1 hour)
    atr_buf=0.25,       # extra stop margin in 15m ATRs
    warm_days=15,       # first days ignored (1h EMA200 needs history)
    cap=288,            # max 5m candles in a trade (24h)
    fee_pct=0.05,       # taker fee per side
    slip_pct=0.02,      # slippage per side
)


def get_json(url, params=None, tries=3):
    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=25,
                             headers={"User-Agent": "paperbot"})
            if r.status_code == 200:
                return r.json()
            print("HTTP", r.status_code, url[-30:])
            if r.status_code in (429, 403):
                time.sleep(5)
        except Exception as e:
            print("net error:", str(e)[:80])
            time.sleep(3)
    return None


def parse(c):
    return dict(ts=int(c[0]) // 1000, o=float(c[1]), h=float(c[2]),
                l=float(c[3]), c=float(c[4]), v=float(c[5]))


def atr(cs, n):
    out = [None] * len(cs)
    tr = [None] + [max(cs[i]["h"] - cs[i]["l"], abs(cs[i]["h"] - cs[i - 1]["c"]),
                       abs(cs[i]["l"] - cs[i - 1]["c"])) for i in range(1, len(cs))]
    if len(cs) <= n:
        return out
    a = sum(tr[1:n + 1]) / n
    out[n] = a
    for i in range(n + 1, len(cs)):
        a = (a * (n - 1) + tr[i]) / n
        out[i] = a
    return out


def simulate(cs, e, side, sl, rr):
    entry = cs[e]["o"]
    risk = abs(entry - sl)
    if risk <= 0 or (side == 1 and sl >= entry) or (side == -1 and sl <= entry):
        return None
    tp = entry + side * rr * risk
    last = min(e + CFG["cap"], len(cs) - 1)
    ex, reason, xi = None, None, None
    for k in range(e, last + 1):
        c = cs[k]
        if k > e:
            if (side == 1 and c["o"] <= sl) or (side == -1 and c["o"] >= sl):
                ex, reason, xi = c["o"], "SL", k
                break
            if (side == 1 and c["o"] >= tp) or (side == -1 and c["o"] <= tp):
                ex, reason, xi = c["o"], "TP", k
                break
        hit_sl = c["l"] <= sl if side == 1 else c["h"] >= sl
        hit_tp = c["h"] >= tp if side == 1 else c["l"] <= tp
        if hit_sl:
            ex, reason, xi = sl, "SL", k
            break
        if hit_tp:
            ex, reason, xi = tp, "TP", k
            break
        if k == e + CFG["cap"]:
            ex, reason, xi = c["c"], "TIME", k
            break
    if ex is None:
        return {"open": True}
    cost = 2 * (CFG["fee_pct"] + CFG["slip_pct"])
    gross = side * (ex / entry - 1) * 100
    risk_pct = risk / entry * 100
    return dict(entry=entry, sl=sl, tp=tp, exit=ex, reason=reason, exit_idx=xi,
                risk_pct=risk_pct, gross_pct=gross, net_pct=gross - cost,
                net_R=(gross - cost) / risk_pct)


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M")


def ema_list(vals, n):
    out = [None] * len(vals)
    if len(vals) < n:
        return out
    e = sum(vals[:n]) / n
    out[n - 1] = e
    k = 2.0 / (n + 1)
    for i in range(n, len(vals)):
        e = vals[i] * k + e * (1 - k)
        out[i] = e
    return out


def fetch_bars(inst, bar, days, per_day):
    need = days * per_day
    out, after = [], None
    while len(out) < need:
        p = {"instId": inst, "bar": bar, "limit": 100}
        if after:
            p["after"] = after
        d = (get_json(OKX + "/market/history-candles", p) or {}).get("data") or []
        time.sleep(0.15)
        if not d:
            break
        out.extend(parse(c) for c in d if len(c) >= 9 and c[8] == "1")
        after = d[-1][0]
    seen, rows = set(), []
    for c in sorted(out, key=lambda x: x["ts"]):
        if c["ts"] not in seen:
            seen.add(c["ts"])
            rows.append(c)
    return rows[-need:]


def rsi_list(closes, n):
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    g = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    l = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    ag, al = sum(g[:n]) / n, sum(l[:n]) / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n, len(g)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + l[i]) / n
        out[i + 1] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def adx_list(cs, n):
    out = [None] * len(cs)
    if len(cs) < 2 * n + 1:
        return out
    pdm, mdm, tr = [0.0], [0.0], [0.0]
    for i in range(1, len(cs)):
        up, dn = cs[i]["h"] - cs[i - 1]["h"], cs[i - 1]["l"] - cs[i]["l"]
        pdm.append(up if (up > dn and up > 0) else 0.0)
        mdm.append(dn if (dn > up and dn > 0) else 0.0)
        tr.append(max(cs[i]["h"] - cs[i]["l"], abs(cs[i]["h"] - cs[i - 1]["c"]),
                      abs(cs[i]["l"] - cs[i - 1]["c"])))
    sp, sm, st = sum(pdm[1:n + 1]), sum(mdm[1:n + 1]), sum(tr[1:n + 1])
    dx = [None] * len(cs)
    for i in range(n, len(cs)):
        if i > n:
            sp = sp - sp / n + pdm[i]
            sm = sm - sm / n + mdm[i]
            st = st - st / n + tr[i]
        if st == 0:
            dx[i] = 0.0
            continue
        pdi, mdi = 100 * sp / st, 100 * sm / st
        dx[i] = 0.0 if pdi + mdi == 0 else 100 * abs(pdi - mdi) / (pdi + mdi)
    first = [v for v in dx[n:2 * n]]
    a = sum(first) / n
    out[2 * n - 1] = a
    for i in range(2 * n, len(cs)):
        a = (a * (n - 1) + dx[i]) / n
        out[i] = a
    return out


def supertrend(cs, n, mult):
    a = atr(cs, n)
    d, fu, fl = [0] * len(cs), [None] * len(cs), [None] * len(cs)
    for i in range(len(cs)):
        if a[i] is None:
            continue
        hl2 = (cs[i]["h"] + cs[i]["l"]) / 2
        ub, lb = hl2 + mult * a[i], hl2 - mult * a[i]
        if fu[i - 1] is None:
            fu[i], fl[i] = ub, lb
            d[i] = 1 if cs[i]["c"] > hl2 else -1
            continue
        fu[i] = ub if (ub < fu[i - 1] or cs[i - 1]["c"] > fu[i - 1]) else fu[i - 1]
        fl[i] = lb if (lb > fl[i - 1] or cs[i - 1]["c"] < fl[i - 1]) else fl[i - 1]
        if cs[i]["c"] > fu[i - 1]:
            d[i] = 1
        elif cs[i]["c"] < fl[i - 1]:
            d[i] = -1
        else:
            d[i] = d[i - 1]
    return d


def resample(cs, minutes):
    """Complete higher-timeframe bars built from 5m candles."""
    per, nb = minutes * 60, minutes // 5
    buckets = {}
    for c in cs:
        buckets.setdefault(c["ts"] // per * per, []).append(c)
    bars = []
    for st in sorted(buckets):
        g = buckets[st]
        if len(g) == nb:
            bars.append(dict(ts=st, end=st + per, o=g[0]["o"], h=max(x["h"] for x in g),
                             l=min(x["l"] for x in g), c=g[-1]["c"]))
    return bars


def map_closed(cs, bars):
    """For every 5m candle: index of the last higher-timeframe bar already CLOSED (no look-ahead)."""
    out, k = [-1] * len(cs), -1
    for i, c in enumerate(cs):
        known = c["ts"] + 300
        while k + 1 < len(bars) and bars[k + 1]["end"] <= known:
            k += 1
        out[i] = k
    return out


def trend_1h(b1h):
    cl = [b["c"] for b in b1h]
    e200 = ema_list(cl, CFG["ema_slow"])
    st = supertrend(b1h, CFG["st_n"], CFG["st_mult"])
    adx = adx_list(b1h, CFG["adx_n"])
    out = [0] * len(b1h)
    for k in range(len(b1h)):
        if e200[k] is None or adx[k] is None or st[k] == 0 or adx[k] <= CFG["adx_min"]:
            continue
        if cl[k] > e200[k] and st[k] == 1:
            out[k] = 1
        elif cl[k] < e200[k] and st[k] == -1:
            out[k] = -1
    return out


def setups_15m(b15):
    cl = [b["c"] for b in b15]
    e20, a, rs = ema_list(cl, CFG["ema_fast"]), atr(b15, CFG["atr_n"]), rsi_list(cl, CFG["rsi_n"])
    z, ex, eb = CFG["zone_atr"], CFG["ext_atr"], CFG["ext_bars"]
    out = [0] * len(b15)
    for k in range(max(CFG["ema_fast"], eb + 2), len(b15)):
        if None in (e20[k], a[k], rs[k], rs[k - 1]):
            continue
        ok = lambda j: e20[j] is not None and a[j] is not None
        if (cl[k] > e20[k] - z * a[k]
                and CFG["long_rsi"][0] <= rs[k - 1] <= CFG["long_rsi"][1] and rs[k] > rs[k - 1]
                and any(b15[j]["l"] <= e20[j] + z * a[j] for j in (k - 1, k) if ok(j))
                and any(cl[j] > e20[j] + ex * a[j] for j in range(k - eb, k) if ok(j))):
            out[k] = 1
        elif (cl[k] < e20[k] + z * a[k]
                and CFG["short_rsi"][0] <= rs[k - 1] <= CFG["short_rsi"][1] and rs[k] < rs[k - 1]
                and any(b15[j]["h"] >= e20[j] - z * a[j] for j in (k - 1, k) if ok(j))
                and any(cl[j] < e20[j] - ex * a[j] for j in range(k - eb, k) if ok(j))):
            out[k] = -1
    return out, a


def find_signals(cs):
    b1h, b15 = resample(cs, 60), resample(cs, 15)
    tr1 = trend_1h(b1h)
    set15, a15 = setups_15m(b15)
    m1, m15 = map_closed(cs, b1h), map_closed(cs, b15)
    sb, buf = CFG["stop_bars"], CFG["atr_buf"]
    used, sigs = set(), []
    for i in range(max(sb, 1), len(cs)):
        k1, k15 = m1[i], m15[i]
        if k1 < 0 or k15 < 1 or a15[k15] is None:
            continue
        tr = tr1[k1]
        if tr == 0:
            continue
        kk = None
        for k in (k15, k15 - 1):          # the setup comes from the last two closed 15m bars
            if set15[k] == tr and k not in used:
                kk = k
                break
        if kk is None:
            continue
        c, p = cs[i], cs[i - 1]
        if tr == 1 and c["c"] > c["o"] and c["c"] > p["h"]:
            used.add(kk)
            sigs.append(dict(i=i, side=1, sl=min(x["l"] for x in cs[i - sb + 1:i + 1]) - buf * a15[k15]))
        elif tr == -1 and c["c"] < c["o"] and c["c"] < p["l"]:
            used.add(kk)
            sigs.append(dict(i=i, side=-1, sl=max(x["h"] for x in cs[i - sb + 1:i + 1]) + buf * a15[k15]))
    return sigs


def coin_trades(cs, coin, rr):
    t_start = cs[0]["ts"] + CFG["warm_days"] * 86400
    trades, busy = [], -1
    for sg in find_signals(cs):
        e = sg["i"] + 1
        if e >= len(cs) or e <= busy or cs[sg["i"]]["ts"] < t_start:
            continue
        t = simulate(cs, e, sg["side"], sg["sl"], rr)
        if t is None or t.get("open"):
            continue
        busy = t["exit_idx"]
        t.update(coin=coin, side=sg["side"], entry_ts=cs[e]["ts"], exit_ts=cs[t["exit_idx"]]["ts"])
        trades.append(t)
    return trades


def summ(ts, rr):
    n = len(ts)
    if n == 0:
        return None
    wins = [t for t in ts if t["net_pct"] > 0]
    gp = sum(t["net_pct"] for t in wins)
    gl = -sum(t["net_pct"] for t in ts if t["net_pct"] <= 0)
    stop = sum(t["risk_pct"] for t in ts) / n
    cost = 2 * (CFG["fee_pct"] + CFG["slip_pct"])
    return dict(n=n, win=100.0 * len(wins) / n, avg=sum(t["net_pct"] for t in ts) / n,
                pf=gp / gl if gl else 99.0, stop=stop, be=100.0 * (1 + cost / stop) / (rr + 1),
                er=sum(t["net_R"] for t in ts) / n)


def risk_equity(ts):
    eq, peak, mdd = 1.0, 1.0, 0.0
    for t in sorted(ts, key=lambda x: x["exit_ts"]):
        eq *= 1 + RISK * t["net_R"]
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak * 100)
    return (eq - 1) * 100, mdd


def line(label, ts, rr):
    s = summ(ts, rr)
    if s is None:
        return "%s n=0" % label
    return "%s n=%-4d win=%2.0f%% be=%2.0f%% PF=%.2f avg=%+.3f%% E=%+.2fR stop=%.2f%%" % (
        label, s["n"], s["win"], s["be"], s["pf"], s["avg"], s["er"], s["stop"])


def pf_of(ts, rr):
    s = summ(ts, rr)
    return "n=0" if s is None else "PF=%.2f(n=%d)" % (s["pf"], s["n"])


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 365
    last_n = int(sys.argv[2]) if len(sys.argv) > 2 else 200
    cost = 2 * (CFG["fee_pct"] + CFG["slip_pct"])
    print("Strategy 7 MTF (1h trend, 15m confirmation, 5m entry) | cost %.2f%%/trade | last %d trades per coin | be = break-even win rate" % (cost, last_n))
    data = {}
    for coin in COINS:
        cs = fetch_bars(coin + "-USDT-SWAP", "5m", days, 288)
        print("%s: %d candles (%s -> %s)" % (coin, len(cs), iso(cs[0]["ts"]) if cs else "-", iso(cs[-1]["ts"]) if cs else "-"))
        if len(cs) >= 5000:
            data[coin] = cs
    rows, saved = [], []
    for rr in RRS:
        used = []
        print("=== RR 1:%d ===" % rr)
        for coin, cs in data.items():
            tr = coin_trades(cs, coin, rr)
            use = tr[-last_n:]
            used.extend(use)
            print("  " + line("%-4s" % coin, use, rr) + ("   (only %d available)" % len(tr) if len(tr) < last_n else ""))
        used.sort(key=lambda t: t["entry_ts"])
        half = len(used) // 2
        print("  " + line("ALL ", used, rr))
        print("  LONG %s | SHORT %s | FIRST half %s | SECOND half %s" % (
            pf_of([t for t in used if t["side"] > 0], rr), pf_of([t for t in used if t["side"] < 0], rr),
            pf_of(used[:half], rr), pf_of(used[half:], rr)))
        ret, dd = risk_equity(used)
        print("  risk 0.5%%/trade: return %+.1f%% (max drawdown %.1f%%)" % (ret, dd))
        s = summ(used, rr)
        if s:
            rows.append([rr, s["n"], round(s["win"], 1), round(s["be"], 1), round(s["pf"], 2),
                         round(s["avg"], 3), round(s["er"], 3), round(ret, 1), round(dd, 1)])
        saved.extend([(rr, t) for t in used])
    with open("strategy7_mtf_summary.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["rr", "trades", "win_pct", "breakeven_win_pct", "pf", "avg_net_pct",
                    "exp_R", "return_risk0.5_pct", "max_dd_pct"])
        w.writerows(rows)
    with open("strategy7_mtf_trades.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["rr", "coin", "side", "entry_time", "exit_time", "entry", "sl", "tp",
                    "exit", "reason", "stop_pct", "net_pct", "net_R"])
        for rr, t in saved:
            w.writerow([rr, t["coin"], "LONG" if t["side"] > 0 else "SHORT", iso(t["entry_ts"]),
                        iso(t["exit_ts"]), t["entry"], round(t["sl"], 6), round(t["tp"], 6), t["exit"],
                        t["reason"], round(t["risk_pct"], 3), round(t["net_pct"], 3), round(t["net_R"], 3)])


if __name__ == "__main__":
    main()
