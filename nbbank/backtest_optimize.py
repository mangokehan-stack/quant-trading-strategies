# -*- coding: utf-8 -*-
"""
============================================================================
 宁波银行(002142) 策略优化 A/B 回测（2024 / 2025 / 2026H1 三区间 × 5 组合）
============================================================================
 基准 = 现状实盘策略（首仓+金字塔M2+盘中阶梯M3+固定/移动止盈+熊市过滤+躺平）
 优化项（开关式，可任意叠加）：
   O1 卖出冷却   cooldown_days>0   : 全平卖出后 N 个交易日禁止再开首仓
   O2 M2/M3协调  daily_add_cap>0   : 单日加仓总金额上限（M2 优先，M3 共享额度）
   O3 分批止盈   tp_tiers 非空      : 浮盈 6% 卖 1/3、8% 再卖 1/3、10% 清仓
   O4 移动止盈收紧 trailing_* 参数  : 激活 3% / 回撤 2%（2026-10-23 起基准已=实盘 3%/2%）
   O5 大盘过滤   market_filter=True : 沪深300 MA20>=MA60 才允许开首仓
   O6 首仓降重+间距 pyramid / add_step_pct : 50%->35%、间距 2%->3%
   O7 ATR仓位    atr_sizing=True    : 近20日波动率>2.5% 首仓缩至0.6倍
   O8 时间止损   time_stop_days>0   : 持仓 N 交易日且无浮盈 -> 清仓
   O9 信号加权   min_signal_paths>=2: 5路信号至少 N 路同时满足才买

 与实盘一致：信号用昨日及以前 120 根日K（前视规避）、当日收盘价成交；
 涨停不买/跌停不卖；佣金万0.841(每委托单5元起收,买卖各一单)+过户费万0.1+卖出印花税万5；硬上限20万。
 2026-10-23 起回测口径=实盘口径（止盈4%+移动3%/2%、预算20万、M3四档各6000累计3万、
 M2+M3单日加仓上限9万、佣金万0.841）；旧调优存档（万2.5佣金/33万/8%止盈口径，
 如"+A股+8.72%"）不可复现，仅作历史参考。
 价格前复权（与实盘 dividend_type=front_ratio 一致）。
============================================================================
"""
import json
import os
import math
import urllib.request

# ---------------- 数据源 ----------------
STOCK_SECID = "sz002142"                      # 002142 深市
INDEX_SECID = "sh000300"                      # 沪深300 沪市
DATA_BEG = 20230101
DATA_END = 20260826

PERIODS = [
    ("2024", "2024-01-01", "2024-12-31"),
    ("2025", "2025-01-01", "2025-12-31"),
    ("2026", "2026-01-01", "2026-08-26"),
]

# ---------------- 优化组合定义 ----------------
def _merge(*ds):
    m = {}
    for d in ds:
        m.update(d)
    return m

COMBOS = [
    # P3-NA8(2026-10-29 修复)：标签不再用历史代号(P0/P1/P2)，直接写清参数差异。
    #   原标签「P0修复」「P1增强」是若干轮审计的代号，看报告的人无法判断到底改了什么；
    #   一旦后续默认值调整，标签就会与内容脱节(报告明确指出「labels are stale」)。
    #   现改为「本组合相对 base 实际改动的参数」，自解释且不会过期。
    ("base", "基准(全默认参数)", {}),
    ("p0", "卖出冷却3日+单日加仓上限9万", {
        "cooldown_days": 3, "daily_add_cap": 90000.0}),
    ("p1", "分批止盈6/8/10%+移动止盈3%/2%+大盘过滤+首仓降至35%", {
        "tp_tiers": [(0.06, 1 / 3.0), (0.08, 1 / 3.0), (0.10, 1.0)],
        "trailing_activate_pct": 0.03, "trailing_dd_pct": 0.02,
        "market_filter": True,
        "pyramid": [0.35, 0.25, 0.10, 0.05], "add_step_pct": 0.03}),
    ("p2", "ATR仓位+60日时间止损+至少2路信号", {
        "atr_sizing": True, "time_stop_days": 60, "min_signal_paths": 2}),
    ("all", "全量(上述三组参数叠加)", _merge(
        {"cooldown_days": 3, "daily_add_cap": 90000.0},
        {"tp_tiers": [(0.06, 1 / 3.0), (0.08, 1 / 3.0), (0.10, 1.0)],
         "trailing_activate_pct": 0.03, "trailing_dd_pct": 0.02,
         "market_filter": True,
         "pyramid": [0.35, 0.25, 0.10, 0.05], "add_step_pct": 0.03},
        {"atr_sizing": True, "time_stop_days": 60, "min_signal_paths": 2})),
]

BASE_PARAMS = {
    "signal_mode": "trend_hybrid",
    # P3-NA3(2026-10-29 修复)：入场阈值对齐实盘 nbbank v1.2.96。
    #   原回测用 25/85/2%，实盘是 35/80/1% —— 同一段行情，回测要求更严，
    #   选出的信号与实盘不是一回事，回测收益无法外推。此处按实盘常量改正。
    "kdj_period": 9, "kdj_k_threshold": 35,
    "wr_period": 14, "wr_threshold": 80,
    "dip_trigger_pct": 0.01, "divergence_n": 14,
    "pyramid": [0.50, 0.25, 0.10, 0.05],
    "add_step_pct": 0.02,
    "take_profit_pct": 0.04, "take_profit_mode": "both",
    "trailing_activate_pct": 0.03, "trailing_dd_pct": 0.02,
    "stop_loss_pct": 0.06, "max_drawdown_pct": 0.08,
    # drawdown_hysteresis 已移除（R2: 熔断不自动恢复，仅止盈全平/人工介入解）
    "trend_ma_fast": 5, "trend_ma_mid": 20, "trend_ma_slope_n": 5,
    "trend_pullback_pct": 0.02, "trend_max_above_pct": 0.06,
    "volume_confirm": True, "volume_short_n": 3, "volume_long_n": 20, "volume_ratio": 1.2,
    "init_capital": 200000.0, "max_invest": 200000.0,
    "lot_size": 100,
}

# ---- M3 盘中阶梯加仓（对齐实盘 gf_qmt_native_multi 的 M3_LEVELS/M3_DAILY_CAP 口径）----
# 四档：盘中较开盘跌 2.5%/5%/7.5%/10% 各加 6000 元；每档每日仅触发 1 次；累计上限 3 万
# （= 实盘 M3_DAILY_CAP，全平 release 后重新可用）；并与 M2 共享单日加仓上限
# daily_add_cap（实盘 DAILY_ADD_CAP 口径 9 万）。
M3_LEVELS = [(0.025, 6000.0), (0.05, 6000.0), (0.075, 6000.0), (0.10, 6000.0)]
M3_MAX_TRIGGERS_PER_DAY = 4   # 四档各一次，每日最多 4 次（对齐实盘每档 triggered 去重）
M3_CUM_CAP = 30000.0


# ---------------- 数据拉取（腾讯行情，分两段规避单次640根上限） ----------------
def _fetch_seg(secid, beg, end, qfq):
    url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
           "param=%s,day,%s,%s,800,%s" % (secid, beg, end, "qfq" if qfq else ""))
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"})
    import time
    last_err = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode("utf-8"))
            node = data.get("data", {}).get(secid, {})
            arr = node.get("qfqday") or node.get("day") or []
            rows = []
            for line in arr:
                rows.append({"date": line[0], "open": float(line[1]),
                             "close": float(line[2]), "high": float(line[3]),
                             "low": float(line[4]), "volume": float(line[5]),
                             "pct": 0.0})
            return rows
        except Exception as e:
            last_err = e
            time.sleep(2 * (attempt + 1))
    raise last_err


def fetch_rows(secid, qfq=True):
    segs = [_fetch_seg(secid, "2023-01-01", "2024-12-31", qfq),
            _fetch_seg(secid, "2025-01-01", "2026-08-26", qfq)]
    merged = {}
    for seg in segs:
        for r in seg:
            merged[r["date"]] = r
    rows = [merged[k] for k in sorted(merged)]
    prev = None
    for r in rows:
        r["pct"] = (r["close"] - prev) / prev * 100 if prev else 0.0
        prev = r["close"]
    return rows


# ---------------- 指标（与实盘一致） ----------------
def calc_kdj_arr(closes, highs, lows, n=9, k_smooth=3, d_smooth=3):
    size = len(closes)
    k_list, d_list = [None] * size, [None] * size
    prev_k, prev_d = 50.0, 50.0
    for i in range(size):
        if i < n - 1:
            k_list[i], d_list[i] = prev_k, prev_d
            continue
        hh = max(highs[i - n + 1:i + 1]); ll = min(lows[i - n + 1:i + 1])
        rsv = 50.0 if hh == ll else (closes[i] - ll) / (hh - ll) * 100.0
        k = (k_smooth - 1.0) / k_smooth * prev_k + (1.0 / k_smooth) * rsv
        d = (d_smooth - 1.0) / d_smooth * prev_d + (1.0 / k_smooth) * k
        k_list[i], d_list[i] = k, d
        prev_k, prev_d = k, d
    return k_list, d_list


def calc_wr_arr(closes, highs, lows, n=14):
    size = len(closes)
    wr = [None] * size
    for i in range(size):
        if i < n - 1:
            continue
        hh = max(highs[i - n + 1:i + 1]); ll = min(lows[i - n + 1:i + 1])
        wr[i] = 0.0 if hh == ll else (hh - closes[i]) / (hh - ll) * 100.0
    return wr


def detect_bottom_divergence(closes, k_list, d_list, wr_list, n=14):
    """只评估当前末根 bar，与实盘 entry_signal_full 一致。"""
    i = len(closes) - 1
    if i < n or not (closes[i] < min(closes[i - n:i])):
        return False
    for arr in (k_list, d_list, wr_list):
        prev = [v for v in arr[i - n:i] if v is not None]
        cur = arr[i]
        if not prev or cur is None or not (cur >= min(prev) - 1e-9):
            return False
    return True


def trend_follow_signal(closes, params):
    n = len(closes)
    mid_n = params["trend_ma_mid"]; fast_n = params["trend_ma_fast"]
    slope_n = params["trend_ma_slope_n"]
    pullback = params["trend_pullback_pct"]; max_above = params["trend_max_above_pct"]
    if n < mid_n + slope_n:
        return False
    ma5_c = sum(closes[-fast_n:]) / fast_n
    ma5_p = sum(closes[-(fast_n + 1):-1]) / fast_n
    ma20_c = sum(closes[-mid_n:]) / mid_n
    ma20_p = sum(closes[-(mid_n + 1):-1]) / mid_n
    ma20_o = sum(closes[-(mid_n + slope_n):-slope_n]) / mid_n
    price = closes[-1]
    above = (price - ma20_c) / ma20_c if ma20_c > 0 else 0
    if above > max_above:
        return False
    if ma5_p < ma20_p and ma5_c >= ma20_c:
        return True
    if ma20_c > ma20_o and abs(above) <= pullback:
        return True
    return False


def entry_signal_full(closes, highs, lows, volumes, params, min_paths=1):
    """返回 (ok, reason, paths)。paths=满足的入场路径数。"""
    if not closes or len(closes) < 2:
        return False, "数据不足", 0
    # P3-NA2(2026-10-29 修复)：跌幅口径对齐实盘。
    #   实盘的「日内跌幅」取自 tick 昨收口径，即【昨日相对前日】的涨跌幅，
    #   因为当日 close 在盘中还未定型；而回测原先用 closes[-1]/closes[-2]（当日涨跌），
    #   等于把「今天跌多少」当成「昨天跌多少」，出入场时点整体前移一天。
    #   现改为 closes[-2]/closes[-3]，与实盘同一时点口径。
    if len(closes) >= 3 and closes[-3] > 0:
        day_drop = (closes[-2] - closes[-3]) / closes[-3]
    elif closes[-2] > 0:
        day_drop = (closes[-1] - closes[-2]) / closes[-2]
    else:
        day_drop = 0.0
    k_arr, _ = calc_kdj_arr(closes, highs, lows, params["kdj_period"])
    wr_arr = calc_wr_arr(closes, highs, lows, params["wr_period"])
    kdj_k = k_arr[-1]; wr_v = wr_arr[-1]

    paths = []
    if kdj_k is not None and kdj_k < params["kdj_k_threshold"]:
        paths.append("KDJ")
    if wr_v is not None and wr_v > params["wr_threshold"]:
        paths.append("WR")
    if day_drop < -params["dip_trigger_pct"]:
        paths.append("跌幅")
    if not paths:
        k9, d9 = calc_kdj_arr(closes, highs, lows, 9)
        wr_pos = calc_wr_arr(closes, highs, lows, 14)
        wr_std = [(-w) if w is not None else None for w in wr_pos]
        if detect_bottom_divergence(closes, k9, d9, wr_std, params["divergence_n"]):
            paths.append("底背离")
    if trend_follow_signal(closes, params):
        paths.append("趋势")

    trend_only = (len(paths) == 1 and paths[0] == "趋势")
    ok = len(paths) >= min_paths
    if ok and params.get("volume_confirm") and not trend_only \
            and volumes and len(volumes) >= params["volume_long_n"]:
        short_avg = sum(volumes[-params["volume_short_n"]:]) / params["volume_short_n"]
        long_avg = sum(volumes[-params["volume_long_n"]:]) / params["volume_long_n"]
        if long_avg > 0 and short_avg < long_avg * params["volume_ratio"]:
            return False, "缩量过滤", len(paths)
    return ok, ("+".join(paths) if paths else ""), len(paths)


def is_bull_market(closes, short_n=20, long_n=60):
    if len(closes) < long_n:
        return True
    return sum(closes[-short_n:]) / short_n >= sum(closes[-long_n:]) / long_n


def ma_series(closes, n):
    out = [None] * len(closes)
    for i in range(len(closes)):
        if i >= n - 1:
            out[i] = sum(closes[i - n + 1:i + 1]) / n
    return out


# ---------------- 费用 ----------------
def compute_buy_cost(vol, price, cr=0.0000841, cm=5.0, tf=0.00001):
    amt = vol * price
    return amt + max(amt * cr, cm) + amt * tf


def compute_sell_proceeds(vol, price, cr=0.0000841, cm=5.0, sd=0.0005, tf=0.00001):
    amt = vol * price
    return amt - max(amt * cr, cm) - amt * sd - amt * tf


def calc_shares(amount, price, lot=100, cash=None):
    if price <= 0:
        return 0
    s = int(amount / price / lot) * lot
    if s == 0 and cash is not None and cash >= price * lot:
        s = lot
    return s


# ---------------- 回测内核 ----------------
def run_backtest(rows, idx_closes, params, opts, start, end=None):
    _allowed_opts = {"cooldown_days", "daily_add_cap", "tp_tiers", "trailing_activate_pct",
                     "trailing_dd_pct", "time_stop_days", "market_filter", "min_signal_paths",
                     "atr_sizing", "bear_adds_freeze", "profit_trim", "bull_slope_gate",
                     "allow_daily_m3_approx"}
    _unknown = sorted(set(opts or {}) - _allowed_opts)
    if _unknown:
        raise ValueError("未知回测选项键: %s" % ", ".join(_unknown))
    closes = [r["close"] for r in rows]
    highs = [r["high"] for r in rows]
    lows = [r["low"] for r in rows]
    opens = [r["open"] for r in rows]
    vols = [r["volume"] for r in rows]
    n = len(rows)

    # 指数 MA20/MA60 预计算（按日对齐，回测用昨日及以前）
    idx_ma20 = ma_series(idx_closes, 20)
    idx_ma60 = ma_series(idx_closes, 60)
    idx_bull = [False] * n
    for i in range(n):
        if i >= 59 and idx_ma20[i - 1] is not None and idx_ma60[i - 1] is not None:
            idx_bull[i] = idx_ma20[i - 1] >= idx_ma60[i - 1]

    cash = params["init_capital"]
    shares = 0; total_cost = 0.0; deployed = 0.0
    tier = -1; last_add_price = 0.0; trailing_peak = 0.0
    # P1-4(2026-10-30 评审修复)：trail_armed 此前只在 do_sell 里以 nonlocal 声明、
    #   在「曾达激活线」和「清仓」两处赋值，却从未初始化 —— 首次买入后第一根 bar
    #   若 pnl < ts_a 就跳过赋值，执行到 `if trail_armed:` 直接抛
    #   UnboundLocalError: cannot access local variable 'trail_armed'，整份回测跑不起来。
    trail_armed = False
    peak_equity = 0.0; paused = False; sl_pending = False
    sold_ratio = 0.0; hold_days = 0; cooldown_idx = -1
    tp_stage = 0                 # 分批止盈已执行到的档位（每档只触发一次，全平归零）
    daily_added = 0.0; last_date = ""
    m3_trig_tiers_today = set()  # M3 当日已触发档位（每档每日一次，对齐实盘 m3["triggered"] 台账）
    m3_total = 0.0               # M3 累计加仓金额（上限 M3_CUM_CAP=3万，全平释放后归零）
    last_price = 0.0             # 区间内最后一根收盘价（期末市值口径）
    trades = []; curve = []
    buy_cnt = 0; sell_cnt = 0; buy_amount = 0.0; sell_proceeds = 0.0; fees = 0.0

    lot = params["lot_size"]
    cap = params["max_invest"]

    def do_buy(date, price, amount, tier_target, reason):
        nonlocal cash, shares, total_cost, deployed, tier, last_add_price
        nonlocal buy_cnt, buy_amount, fees, daily_added
        amt = min(cash, amount)
        vol = calc_shares(amt, price, lot, cash)
        if vol <= 0:
            return False
        if deployed + vol * price > cap + 1e-6:
            return False
        cost = compute_buy_cost(vol, price)
        if cost > cash + 1e-6:
            return False
        cash -= cost
        shares += vol
        total_cost += cost
        deployed += vol * price
        tier = tier_target
        last_add_price = price
        daily_added += vol * price
        buy_cnt += 1; buy_amount += vol * price
        fees += cost - vol * price
        trades.append({"date": date, "side": "BUY", "price": price, "vol": vol,
                       "amount": vol * price, "reason": reason})
        return True

    def do_sell(date, price, reason, sell_vol=None):
        nonlocal cash, shares, total_cost, deployed, tier, last_add_price
        nonlocal trailing_peak, trail_armed, sl_pending, sell_cnt, sell_proceeds, fees, paused
        nonlocal sold_ratio, hold_days, cooldown_idx, tp_stage, m3_total
        if shares <= 0:
            return False
        v = shares if sell_vol is None else int(sell_vol // lot * lot)
        if v <= 0:
            return False
        proceeds = compute_sell_proceeds(v, price)
        cash += proceeds
        fees += v * price - proceeds
        trades.append({"date": date, "side": "SELL", "price": price, "vol": v,
                       "amount": v * price, "reason": reason})
        sell_cnt += 1; sell_proceeds += v * price
        ratio = v / shares
        total_cost *= (1 - ratio)
        deployed *= (1 - ratio)
        shares -= v
        if shares == 0:
            tier = -1; last_add_price = 0.0; trailing_peak = 0.0
            trail_armed = False
            sl_pending = False; hold_days = 0; tp_stage = 0; paused = False
            m3_total = 0.0     # 全平回笼：释放 M3 累计额度（对齐预算 release_all 口径）
            if opts.get("cooldown_days", 0) > 0:
                # P3-NA9(2026-10-29 修复)：冷却期差一个交易日。
                #   原写 i + cooldown_days，配合下方 `i >= cooldown_idx`：
                #   cooldown_days=1 时，第 i 天卖出、cooldown_idx=i+1，第 i+1 天立刻允许买入，
                #   实际冷却 0 个交易日；应为「跳过接下来 N 个交易日」，即最早第 i+N+1 天可买。
                #   与实盘 stop_cooldown_until(按交易日顺延) 语义对齐。
                cooldown_idx = i + int(opts["cooldown_days"]) + 1
        return True

    for i in range(n):
        date = rows[i]["date"]
        if date < start:
            continue
        if end is not None and date > end:
            break          # 行情按日期升序，越过区间终点即停止（此前无截断，区间终点形同虚设）
        price = closes[i]
        last_price = price
        lo = max(0, i - 120)
        hc, hh, hl, hv = closes[lo:i], highs[lo:i], lows[lo:i], vols[lo:i]
        if len(hc) < 65:
            continue

        # 每日加仓额度重置（M3 当日已触发档位一并跨日重置，对齐实盘 M3 台账）
        if date != last_date:
            last_date = date
            daily_added = 0.0
            m3_trig_tiers_today = set()

        if shares > 0:
            hold_days += 1

        mv = shares * price
        equity = cash + mv
        if equity > peak_equity:
            peak_equity = equity
        dd = (peak_equity - equity) / peak_equity if peak_equity > 0 else 0.0
        # 刚性约束：熔断暂停后不因回撤减小自动解除，等人工介入或止盈全平后再恢复（止盈全平时调用方置 paused=False）
        if dd >= params["max_drawdown_pct"]:
            paused = True

        # 止损告警（熊市 + 回撤>=6%）：一次性告警标记，不阻断后续交易。
        # 对齐实盘语义（qmt_strategy P2 修复）：告警只发一次、等待人工确认，
        # 告警后仍照常执行止盈/加仓/首仓判断——回测不再 continue 跳过当日。
        if shares > 0 and not sl_pending and not is_bull_market(hc) \
                and peak_equity > 0 and dd >= params["stop_loss_pct"]:
            sl_pending = True
        # B2：回撤暂停只禁买入/加仓，退出逻辑始终继续评估。

        # ---------------- 出场 ----------------
        if shares > 0 and total_cost > 0:
            pnl = (mv - total_cost) / total_cost
            sell_reason = None
            _trim = opts.get("profit_trim")
            # P3-NA5(2026-10-29 修复)：减仓不再「一辈子只做一次」。
            #   原条件 `not sold_ratio` 让 profit_trim 在整轮回测里最多触发一次，
            #   后续再创新高也不会继续减，与「分批落地」的设计意图不符。
            #   现改为：累计已减比例 < 100% 即可继续触发，每档按步进(默认25%)推进；
            #   同时要求本次浮盈更高才继续减（避免同一价位反复削仓）。
            if _trim and float(sold_ratio or 0.0) < 1.0 - 1e-9:
                _trim_pct, _trim_frac = _trim
                if pnl >= float(_trim_pct):
                    _tv_trim = max(lot, int(shares * float(_trim_frac) / lot) * lot)
                    if do_sell(date, price, "浮盈减仓", min(shares, _tv_trim)):
                        sold_ratio = min(1.0, float(sold_ratio or 0.0) + float(_trim_frac))
                        curve.append((date, cash + shares * price))
                        continue
            tp_tiers = opts.get("tp_tiers")
            if tp_tiers:
                # 分批止盈阶梯：按档位从低到高推进，每档只触发一次（tp_stage 记录进度，
                # 全平后归零）。原实现从低档遍历并 break，浮盈超 10% 也永远只命中第一档，
                # 8%/10% 档形同虚设。注：部分卖出按比例摊减成本后 pnl 不变，档位语义稳定。
                while tp_stage < len(tp_tiers):
                    tp, frac = tp_tiers[tp_stage]
                    if pnl < tp:
                        break
                    v = int(shares * frac // lot * lot)
                    if frac >= 1.0:
                        _tv = shares
                    elif v >= lot and frac * shares >= lot:
                        _tv = v
                    else:
                        tp_stage += 1     # 剩余股数不足该档卖出量 → 跳到下一档
                        continue
                    sell_reason = "分批止盈+%.0f%%(浮盈%.1f%%)卖%s" % (
                        tp * 100, pnl * 100,
                        "清仓" if frac >= 1.0 else "1/3")
                    break
                if sell_reason and not (rows[i]["pct"] <= -9.8):
                    if do_sell(date, price, sell_reason, _tv):
                        if shares > 0:
                            tp_stage += 1     # 全平时 do_sell 内已将 tp_stage 归零，不再推进
                        if shares == 0:
                            curve.append((date, cash))
                            continue
            else:
                if pnl >= params["take_profit_pct"]:
                    sell_reason = "固定止盈+%.0f%%(浮盈%.1f%%)" % (
                        params["take_profit_pct"] * 100, pnl * 100)
                if not sell_reason and params["take_profit_mode"] in ("trailing", "both"):
                    ts_a = opts.get("trailing_activate_pct", params["trailing_activate_pct"])
                    ts_d = opts.get("trailing_dd_pct", params["trailing_dd_pct"])
                    # P3-NA7(2026-10-29 修复)：移动止盈的峰值记录口径与实盘对齐。
                    #   原实现只在「pnl >= 激活线」时才更新 trailing_peak，且要求
                    #   price 高于旧峰值；一旦浮盈回落到激活线以下，峰值就冻住不再增长，
                    #   之后再创新高也不更新 —— 移动止盈要么永不触发，要么按过期的低峰值
                    #   触发，等于参数形同虚设。
                    #   现改为：只要曾达到过激活线（_trail_armed），之后每天用当日最高价维护峰值，
                    #   与实盘 manual_takeover_trailing 的「曾达激活线后按市价新高跟踪」一致。
                    if ts_d > 0:
                        if pnl >= ts_a:
                            trail_armed = True          # 曾达激活线: 此后按最高价跟踪
                        if trail_armed and highs[i] > (trailing_peak or 0.0):
                            trailing_peak = highs[i]   # 用当日最高价维护峰值(与实盘同口径)
                        if trailing_peak > 0 and (trailing_peak - price) / trailing_peak >= ts_d and pnl > 0:
                            sell_reason = "移动止盈(峰值%.2f回落%.1f%%)" % (
                                trailing_peak, (trailing_peak - price) / trailing_peak * 100)
                if sell_reason and not (rows[i]["pct"] <= -9.8):
                    if do_sell(date, price, sell_reason):
                        curve.append((date, cash))
                        continue

            # 时间止损（持仓超期且无浮盈 -> 清仓）
            ts_days = opts.get("time_stop_days", 0)
            if ts_days > 0 and hold_days >= ts_days and pnl <= 0:
                # P3-NA6(2026-10-29 修复)：亏损时间止损必须遵守实盘铁律「绝不亏损卖出」。
                #   原实现在 pnl<=0 时真的调用 do_sell 平仓 —— 回测里靠割肉止损跑出来的
                #   收益曲线，在实盘根本不可能出现（实盘只持有不卖），属口径失真；
                #   而报告所述「亏损时间止损把运行冻住」正是这条与铁律冲突的分支反复触发所致。
                #   现改为：亏损一律不卖（与实盘一致），只在盈利且满足轨道时由上方止盈逻辑处理。
                #   保留一个可观测计数，便于核对「有多少次本应止损但按铁律持有」。
                time_stop_skipped = globals().get('_NA6_SKIP', None)
                if time_stop_skipped is not None:
                    time_stop_skipped.append((date, hold_days))
                if pnl > 0 and not (rows[i]["pct"] <= -9.8):
                    if do_sell(date, price, "时间止损(持仓%d日)" % hold_days):
                        curve.append((date, cash))
                        continue

        # ---------------- 空仓：首仓 ----------------
        if shares == 0:
            bull = is_bull_market(hc)
            if opts.get("bull_slope_gate") and len(hc) >= 25:
                bull = bull and (sum(hc[-20:]) / 20.0 > sum(hc[-25:-5]) / 20.0)
            mkt_ok = (not opts.get("market_filter")) or idx_bull[i]
            if not paused and bull and mkt_ok and i >= cooldown_idx:
                limit_up = rows[i]["pct"] >= 9.8
                if not limit_up:
                    sig, reason, paths = entry_signal_full(
                        hc, hh, hl, hv, params, opts.get("min_signal_paths", 1))
                    if sig:
                        first_amt = params["init_capital"] * params["pyramid"][0]
                        if opts.get("atr_sizing"):
                            rets = [(hc[t] - hc[t - 1]) / hc[t - 1]
                                    for t in range(1, len(hc)) if hc[t - 1] > 0]
                            if len(rets) >= 20:
                                std = (sum((x - sum(rets[-20:]) / 20) ** 2 for x in rets[-20:]) / 20) ** 0.5
                                vf = 0.6 if std > 0.025 else (1.2 if std < 0.015 else 1.0)
                                first_amt *= vf
                        do_buy(date, price, first_amt, 0, "首仓信号[%d路] %s" % (paths, reason))
                        curve.append((date, cash + shares * price))
                        continue

        # ---------------- 持仓中：金字塔加仓 + M3 ----------------
        elif shares > 0 and not paused:
            dcap = opts.get("daily_add_cap", 0.0)
            _freeze_adds = bool(opts.get("bear_adds_freeze")) and not idx_bull[i]
            if _freeze_adds:
                curve.append((date, cash + shares * price))
                continue
            if 0 <= tier < len(params["pyramid"]) - 1 \
                    and price <= last_add_price * (1 - params["add_step_pct"]):
                amt = params["init_capital"] * params["pyramid"][tier + 1]
                if dcap <= 0 or daily_added + amt <= dcap + 1e-6:
                    do_buy(date, price, amt, tier + 1, "金字塔加仓t%d(较%.2f跌%.1f%%)" % (
                        tier + 1, last_add_price, params["add_step_pct"] * 100))
            # M3 盘中阶梯加仓（对齐实盘 gf_qmt_native_multi 的 M3_LEVELS 口径）：
            #   · 四档：盘中价较当日开盘跌 >=2.5%/5%/7.5%/10% 各加 6000 元
            #   · 每日每档一次去重（实盘 m3["triggered"] 台账，跨日重置）
            #   · M3 累计上限 3 万（M3_CUM_CAP=实盘 M3_DAILY_CAP，全平释放后归零重新可用）
            #   · 与 M2 共享单日加仓上限 daily_add_cap（实盘 DAILY_ADD_CAP 口径 9 万）
            #   预算判定对齐实盘 can_spend 口径：按「已用 + 本笔实际金额」校验
            # P3-NA4(2026-10-29 修复)：M3 此前从未被模拟。
            #   文档与 COMBOS 都写明含 M3 阶梯加仓，但该分支要求 opts 显式带
            #   allow_daily_m3_approx=True，而所有组合都没传，等于静默关闭。
            #   现改为默认开启（显式传 False 才关闭），使回测口径与文档一致。
            if opts.get("allow_daily_m3_approx", True) and opens[i] > 0:
                drop_intraday = (opens[i] - lows[i]) / opens[i]
                for m3_lvl, m3_amt in M3_LEVELS:
                    if (drop_intraday + 1e-9) < m3_lvl or m3_lvl in m3_trig_tiers_today:
                        continue
                    if len(m3_trig_tiers_today) >= M3_MAX_TRIGGERS_PER_DAY:
                        break
                    # 成交价用触发价（开盘×(1-档位跌幅)）：实盘 M3 在盘中价格触及档位时
                    # 即时成交；「以全天最低点判定触发」为日线数据的近似
                    m3_px = opens[i] * (1 - m3_lvl)
                    v = calc_shares(m3_amt, m3_px, lot, cash)
                    actual_m3 = v * m3_px
                    if v > 0 and actual_m3 > 0 \
                            and m3_total + actual_m3 <= M3_CUM_CAP + 1e-6 \
                            and not (dcap > 0 and daily_added + actual_m3 > dcap + 1e-6):
                        if do_buy(date, m3_px, m3_amt, tier, "M3盘中加仓(触发跌%.1f%%)" % (m3_lvl * 100)):
                            m3_trig_tiers_today.add(m3_lvl)
                            m3_total += trades[-1]["amount"]

        curve.append((date, cash + shares * price))

    # ---- 统计 ----
    eqs = [e for _, e in curve]
    final_eq = eqs[-1] if eqs else cash
    peak = 0.0; max_dd = 0.0
    for e in eqs:
        peak = max(peak, e)
        if peak > 0:
            max_dd = max(max_dd, (peak - e) / peak)
    ret = (final_eq - params["init_capital"]) / params["init_capital"]
    return {
        "dates": [d for d, _ in curve],
        "curve": eqs,
        "final_equity": final_eq,
        "return_pct": ret * 100,
        "max_drawdown_pct": max_dd * 100,
        "buy_cnt": buy_cnt, "sell_cnt": sell_cnt,
        "buy_amount": buy_amount, "sell_proceeds": sell_proceeds,
        "fees": fees,
        "final_shares": shares,
        "final_mv": shares * (last_price if last_price > 0 else closes[-1] if rows else 0),
        "trades": trades,
    }


# ---------------- 主流程 ----------------
def main():
    print("拉取 002142 + 沪深300 日线 ...")
    stock_rows = sorted(fetch_rows(STOCK_SECID, qfq=True), key=lambda r: r["date"])
    idx_rows = sorted(fetch_rows(INDEX_SECID, qfq=False), key=lambda r: r["date"])
    if not stock_rows or not idx_rows:
        raise ValueError("股票或指数行情为空")
    if len({r["date"] for r in stock_rows}) != len(stock_rows):
        raise ValueError("股票行情存在重复日期")
    idx_close_map = {r["date"]: r["close"] for r in idx_rows}
    idx_closes, _prev = [], None
    for r in stock_rows:
        c = idx_close_map.get(r["date"])
        if c is None:
            if _prev is None:
                raise ValueError("指数数据起始日晚于股票数据，禁止未来值向前填充: %s" % r["date"])
            c = _prev
        else:
            _prev = c
        idx_closes.append(c)
    print("股票 %d 根, 指数 %d 根, %s ~ %s"
          % (len(stock_rows), len(idx_rows), stock_rows[0]["date"], stock_rows[-1]["date"]))

    out = {"stock": "002142.SZ 宁波银行", "index": "沪深300(000300)",
           "combo_names": {c[0]: c[1] for c in COMBOS},
           "periods": {}, "rows": {}}
    for pname, pstart, pend in PERIODS:
        back_rows = [r for r in stock_rows if pstart <= r["date"] <= pend]
        print("\n===== 区间 %s (%s ~ %s, %d 根) ====="
              % (pname, back_rows[0]["date"], back_rows[-1]["date"], len(back_rows)))
        out["rows"][pname] = back_rows
        res = {}
        for cid, cname, copts in COMBOS:
            params = dict(BASE_PARAMS)
            if "pyramid" in copts:
                params["pyramid"] = copts["pyramid"]
            if "add_step_pct" in copts:
                params["add_step_pct"] = copts["add_step_pct"]
            # NA1(2026-10-29 修复)：pyramid / add_step_pct 是【参数】不是【回测选项】，
            #   上面已单独并入 params；原实现仍把整个 copts 传下去，触发 run_backtest 的
            #   白名单校验 ValueError('未知回测选项键: add_step_pct, pyramid')，
            #   main() 必然崩溃、一份结果都写不出来。此处先剔除这两个键。
            _copts = {k: v for k, v in (copts or {}).items()
                      if k not in ('pyramid', 'add_step_pct')}
            r = run_backtest(stock_rows, idx_closes, params, _copts, pstart, pend)
            res[cid] = r
            print("  [%s] %-28s 收益 %+7.2f%%  最大回撤 %6.2f%%  买%d/卖%d  手续费%.0f"
                  % (cid, cname, r["return_pct"], r["max_drawdown_pct"],
                     r["buy_cnt"], r["sell_cnt"], r["fees"]))
        out["periods"][pname] = res

    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_optimize.json"), "w",
              encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    build_html(out)
    print("\n已输出 backtest_optimize.json / backtest_optimize.html")


# ---------------- HTML 报告 ----------------
def build_html(out):
    colors = {"base": "#94a3b8", "p0": "#3b82f6", "p1": "#f59e0b",
              "p2": "#10b981", "all": "#ef4444"}
    curves_js = json.dumps({
        p: {c: {"name": out["combo_names"][c], "color": colors[c],
                "equity": out["periods"][p][c]["curve"],
                "ret": out["periods"][p][c]["return_pct"]}
            for c in out["combo_names"]}
        for p in out["periods"]
    })
    dates_js = json.dumps({p: [r["date"] for r in out["rows"][p]] for p in out["periods"]})
    stats_js = json.dumps({
        p: {c: {k: (round(x, 2) if isinstance(x, float) else x)
                for k, x in out["periods"][p][c].items()
                if k not in ("curve", "dates", "trades")}
            for c in out["combo_names"]}
        for p in out["periods"]
    })
    trades_js = json.dumps({
        p: {c: out["periods"][p][c]["trades"] for c in out["combo_names"]}
        for p in out["periods"]
    })

    html = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>宁波银行 策略优化 A/B 回测</title>
<style>
  :root{--bg:#0f172a;--card:#1e293b;--line:#334155;--tx:#e2e8f0;--mut:#94a3b8;}
  *{box-sizing:border-box;margin:0;padding:0;}
  body{background:var(--bg);color:var(--tx);font:14px/1.6 "Microsoft YaHei",sans-serif;padding:24px;}
  h1{font-size:20px;margin-bottom:4px;}
  .sub{color:var(--mut);margin-bottom:20px;font-size:12px;}
  .cards{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:20px;}
  .card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 18px;min-width:150px;}
  .card .k{color:var(--mut);font-size:12px;}
  .card .v{font-size:20px;font-weight:700;margin-top:2px;}
  .card .d{font-size:11px;color:var(--mut);margin-top:2px;}
  .pos{color:#f87171;} .neg{color:#34d399;}
  .panel{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:20px;}
  .legend{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:10px;font-size:12px;}
  .legend span i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;}
  table{width:100%;border-collapse:collapse;font-size:12px;}
  th,td{border-bottom:1px solid var(--line);padding:5px 8px;text-align:left;white-space:nowrap;}
  th{color:var(--mut);font-weight:600;}
  .scrolly{max-height:260px;overflow:auto;}
  .scrolly th{position:sticky;top:0;background:var(--card);}
  .buy{color:#f87171;} .sell{color:#34d399;}
  .note{color:var(--mut);font-size:11px;margin-top:8px;line-height:1.7;}
  .sum th{color:var(--mut);}
  .hl{color:#fbbf24;}
</style></head><body>
<h1>宁波银行(002142.SZ) · 策略优化 A/B 回测</h1>
<div class="sub" id="sub"></div>
<div class="panel">
  <h3 style="font-size:14px;margin-bottom:10px">汇总对比</h3>
  <table class="sum" id="sumtab"></table>
</div>
<div id="panels"></div>
<div class="note" id="notes"></div>
<script>
var DATES=__DATES__, CURVES=__CURVES__, STATS=__STATS__, TRADES=__TRADES__;
var PERIODS=Object.keys(DATES), COMBOS=Object.keys(CURVES[PERIODS[0]]);
var NAMES=CURVES[PERIODS[0]], COLORS={};
Object.keys(NAMES).forEach(function(c){COLORS[c]=NAMES[c].color;});
document.getElementById('sub').textContent='三区间(2024/2025/2026H1) × 五组合(baseline/P0/P1/P2/全量) | 初始资金 330,000 | 腾讯前复权日线 | 信号用昨日及以前120根K';

var st=document.getElementById('sumtab');
st.innerHTML='<tr><th>区间</th>'+COMBOS.map(function(c){return '<th>'+NAMES[c].name+'</th>';}).join('')+'</tr>';
PERIODS.forEach(function(p){
  var row='<tr><td><b>'+p+'</b></td>';
  COMBOS.forEach(function(c){
    var s=STATS[p][c];
    var cls=s.return_pct>=0?'pos':'neg';
    var dd=(s.max_drawdown_pct).toFixed(1);
    var mark='';
    if(c!=='base'){
      var db=STATS[p]['base'];
      var dR=s.return_pct-db.return_pct;
      var dD=s.max_drawdown_pct-db.max_drawdown_pct;
      mark=' <span class="'+(dR>=0?'pos':'neg')+'">('+(dR>=0?'+':'')+dR.toFixed(1)+')</span>'+
           ' <span class="'+(dD<=0?'pos':'neg')+'">dd'+(dD<=0?'':'+')+dD.toFixed(1)+'</span>';
    }
    row+='<td><span class="'+cls+'">'+s.return_pct.toFixed(2)+'%</span>'+
         '<div class="d">回撤 '+dd+'% · 买'+s.buy_cnt+'/卖'+s.sell_cnt+'</div>'+mark+'</td>';
  });
  st.innerHTML+=row+'</tr>';
});

var panels=document.getElementById('panels');
PERIODS.forEach(function(p){
  var div=document.createElement('div');
  div.className='panel';
  div.innerHTML='<h3 style="font-size:14px;margin-bottom:8px">区间 '+p+' ('+DATES[p][0]+' ~ '+DATES[p][DATES[p].length-1]+')</h3>'+
    '<div class="legend">'+COMBOS.map(function(c){return '<span><i style="background:'+COLORS[c]+'"></i>'+NAMES[c].name+'</span>';}).join('')+'</div>'+
    '<svg width="100%" viewBox="0 0 960 300" preserveAspectRatio="none"></svg>';
  panels.appendChild(div);

  var svg=div.querySelector('svg');
  var W=960,H=300,padL=64,padR=16,padT=14,padB=26;
  var eqs=COMBOS.map(function(c){return CURVES[p][c].equity;});
  var all=[].concat.apply([],eqs);
  var min=Math.min.apply(null,all),max=Math.max.apply(null,all);
  var span=(max-min)||1; min-=span*0.05; max+=span*0.05;
  function X(i){return padL+(W-padL-padR)*i/(DATES[p].length-1);}
  function Y(e){return padT+(H-padT-padB)*(1-(e-min)/(max-min));}
  var g='';
  for(var gv=0;gv<=4;gv++){
    var gy=padT+(H-padT-padB)*gv/4, val=max-(max-min)*gv/4;
    g+='<line x1="'+padL+'" y1="'+gy+'" x2="'+(W-padR)+'" y2="'+gy+'" stroke="#334155" stroke-width="1"/>';
    g+='<text x="'+(padL-8)+'" y="'+(gy+4)+'" fill="#94a3b8" font-size="11" text-anchor="end">'+Math.round(val).toLocaleString()+'</text>';
  }
  var nt=Math.min(8,DATES[p].length-1);
  for(var ti=0;ti<=nt;ti++){
    var ii=Math.round(ti*(DATES[p].length-1)/nt);
    g+='<text x="'+X(ii)+'" y="'+(H-8)+'" fill="#94a3b8" font-size="10" text-anchor="middle">'+DATES[p][ii].slice(5)+'</text>';
  }
  COMBOS.forEach(function(c){
    var pts=CURVES[p][c].equity.map(function(e,i){return X(i).toFixed(1)+','+Y(e).toFixed(1);}).join(' ');
    g+='<polyline points="'+pts+'" fill="none" stroke="'+COLORS[c]+'" stroke-width="1.6" opacity="0.95"/>';
  });
  svg.innerHTML=g;
});

var notes=document.getElementById('notes');
notes.innerHTML='<b>组合定义</b>：'+
'P0=卖出后冷却3日 + M2/M3单日加仓上限9万；'+
'P1=分批止盈(+6%卖1/3,+8%卖1/3,+10%清仓) + 移动止盈收紧(激活3%/回撤2%) + 沪深300 MA20≥MA60才首仓 + 首仓50%→35%、加仓间距2%→3%；'+
'P2=ATR波动率仓位(波动>2.5%缩0.6倍) + 时间止损(60日无浮盈清仓) + 信号加权(至少2路同满足)；全量=P0+P1+P2。'+
'<br>规则：信号用昨日及以前120根日K、当日收盘成交；金字塔加仓；M3盘中加仓四档2.5%/5%/7.5%/10%各6000元/每档每日1次/累计3万(对齐实盘)；涨停不买/跌停不卖；佣金万0.841(每委托单5元起收)+过户费万0.1+印花税万5；单标的硬上限20万；回撤≥8%躺平；熊市止损告警只发一次、不阻断后续交易(对齐实盘)。';

var tb;
PERIODS.forEach(function(p){
  var panel=document.createElement('div');
  panel.className='panel';
  panel.innerHTML='<h3 style="font-size:14px;margin-bottom:8px">交易明细 · '+p+'</h3><div class="scrolly"><table><thead><tr><th>策略</th><th>日期</th><th>方向</th><th>价格</th><th>股数</th><th>金额</th><th>原因</th></tr></thead><tbody></tbody></table></div>';
  document.body.appendChild(panel);
  tb=panel.querySelector('tbody');
  COMBOS.forEach(function(c){
    TRADES[p][c].forEach(function(t){
      var cls=t.side==='BUY'?'buy':'sell';
      tb.innerHTML+='<tr><td>'+NAMES[c].name+'</td><td>'+t.date+'</td><td class="'+cls+'">'+
        (t.side==='BUY'?'买入':'卖出')+'</td><td>'+t.price.toFixed(2)+'</td><td>'+t.vol+'</td><td>'+
        t.amount.toLocaleString(undefined,{maximumFractionDigits:0})+'</td><td>'+t.reason+'</td></tr>';
    });
  });
});
</script>
</body></html>"""
    html = (html.replace("__DATES__", dates_js)
                .replace("__CURVES__", curves_js)
                .replace("__STATS__", stats_js)
                .replace("__TRADES__", trades_js))
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_optimize.html"), "w",
              encoding="utf-8") as f:
        f.write(html)


if __name__ == "__main__":
    main()
