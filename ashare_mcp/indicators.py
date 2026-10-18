"""
量化指标模块 — 多因子Regime系统、ATR动态止损(Regime自适应)、行业分散

被 sim_trader.py 和 screener.py 共用。

Regime v2: 4因子加权评分系统（多指数趋势 + 动量加速 + 均线斜率 + 量价配合）
"""
import requests
import re
from datetime import datetime


# ── 腾讯指数日K ──
def fetch_index_daily(code="sh000001", count=120):
    """获取指数日K（不复权，指数无除权）"""
    try:
        resp = requests.get(
            "https://ifzq.gtimg.cn/appstock/app/fqkline/get",
            params={"param": f"{code},day,,,{count},"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10
        )
        data = resp.json().get("data", {})
        sd = data.get(code, {})
        raw = sd.get("day", []) or sd.get("qfqday", [])
        bars = []
        for b in raw:
            if len(b) < 6:
                continue
            try:
                bars.append({
                    "trade_date": b[0],
                    "open": float(b[1]), "close": float(b[2]),
                    "high": float(b[3]), "low": float(b[4]),
                    "vol": float(b[5])
                })
            except (ValueError, TypeError):
                continue
        return bars
    except Exception as e:
        print(f"  [regime] 获取指数日K失败: {e}")
        return []


# ── 移动平均 ──
def calc_ma(values, period):
    """简单移动平均"""
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


# ── ATR (Average True Range) ──
def calc_atr(bars, period=14):
    """
    计算ATR(14)。bars需要含 high/low/close 字段的dict列表。
    返回 None 如果数据不足。
    """
    if len(bars) < period + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        h = bars[i]["high"]
        l = bars[i]["low"]
        prev_c = bars[i - 1]["close"]
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)
    if len(trs) < period:
        return None
    # Wilder's smoothing
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


# ════════════════════════════════════════════════════════════════
#  Regime v2: 多因子加权评分系统
# ════════════════════════════════════════════════════════════════

# 三指数定义
REGIME_INDICES = [
    ("sh000001", "上证指数"),
    ("sh000852", "中证1000"),
    ("sz399006", "创业板指"),
]

# Regime → 参数映射（仓位 + 止损系数）
REGIME_PARAMS = {
    "bull":      {"position_pct": 1.0, "atr_mult": 2.5, "floor_pct": -10, "breakeven_thresh": 15, "label": "📈牛市"},
    "neutral":   {"position_pct": 0.7, "atr_mult": 2.0, "floor_pct": -8,  "breakeven_thresh": 10, "label": "➡️震荡"},
    "warning":   {"position_pct": 0.5, "atr_mult": 1.5, "floor_pct": -5,  "breakeven_thresh": 7,  "label": "⚠️警示"},
    "bear":      {"position_pct": 0.3, "atr_mult": 1.0, "floor_pct": -3,  "breakeven_thresh": 5,  "label": "🐻熊市"},
    "deep_bear": {"position_pct": 0.0, "atr_mult": 0.5, "floor_pct": -2,  "breakeven_thresh": 3,  "label": "💀深熊"},
}

# 评分 → Regime 映射
SCORE_THRESHOLDS = [
    (0.5,  "bull"),
    (0.2,  "neutral"),
    (-0.2, "warning"),
    (-0.5, "bear"),
    (-9.9, "deep_bear"),  # 兜底
]


def _score_index_trend(bars):
    """
    单指数趋势评分。返回 -1.0 ~ +1.0。

    判定: 价格 vs MA60 + MA20 vs MA60
    - 价格>MA60 且 MA20>MA60 → +1.0 (多头确认)
    - 价格>MA60 但 MA20<MA60 → +0.3 (反弹但趋势未确认)
    - 价格<MA60 但 MA20>MA60 → -0.3 (回落但趋势未坏)
    - 价格<MA60 且 MA20<MA60 → -1.0 (空头确认)
    """
    closes = [b["close"] for b in bars]
    if len(closes) < 60:
        return 0.0
    current = closes[-1]
    ma20 = calc_ma(closes, 20)
    ma60 = calc_ma(closes, 60)
    if not ma20 or not ma60:
        return 0.0

    if current > ma60 and ma20 > ma60:
        return 1.0
    elif current > ma60 and ma20 <= ma60:
        return 0.3
    elif current <= ma60 and ma20 > ma60:
        return -0.3
    else:
        return -1.0


def _score_momentum(bars):
    """
    动量/加速度评分。返回 -1.0 ~ +1.0。

    20日涨跌幅: >+3%偏多, <-3%偏空
    5日涨跌幅（加速度）: 急跌<-3%直接拉警报，急涨>+3%加分
    加权: 20日×0.6 + 5日×0.4
    """
    closes = [b["close"] for b in bars]
    if len(closes) < 21:
        return 0.0

    pct_20d = (closes[-1] / closes[-21] - 1) * 100
    pct_5d = (closes[-1] / closes[-6] - 1) * 100

    # 20日 → score: ±3%对应±0.5, ±6%对应±1.0
    s_20d = max(-1.0, min(1.0, pct_20d / 6.0))

    # 5日加速度 → 更敏感
    s_5d = max(-1.0, min(1.0, pct_5d / 6.0))

    return s_20d * 0.6 + s_5d * 0.4


def _score_ma_slope(bars):
    """
    MA60斜率评分。返回 -1.0 ~ +1.0。

    计算 MA60 的5日变化率:
    > +0.3% → 上升 (+1.0)
    ±0.3% → 横盘 (0)
    < -0.3% → 下降 (-1.0)
    """
    closes = [b["close"] for b in bars]
    if len(closes) < 65:
        return 0.0

    ma60_now = calc_ma(closes[-60:], 60)
    ma60_5d_ago = calc_ma(closes[:-5], 60)
    if not ma60_now or not ma60_5d_ago or ma60_5d_ago <= 0:
        return 0.0

    slope = (ma60_now / ma60_5d_ago - 1) * 100
    # ±0.3% → ±0.5, ±0.6% → ±1.0
    return max(-1.0, min(1.0, slope / 0.6))


def _score_volume_price(bars):
    """
    量价配合评分。返回 -1.0 ~ +1.0。

    基于上证（主指数）的近5日 vs 近20日量价关系:
    - 放量上涨: +0.8 (资金入场)
    - 缩量上涨: +0.3 (上涨乏力)
    - 放量下跌: -1.0 (恐慌抛售，最危险)
    - 缩量下跌: -0.4 (缺乏承接但非恐慌)
    """
    if len(bars) < 21:
        return 0.0

    # 近5日 vs 近20日
    vols = [b["vol"] for b in bars]
    closes = [b["close"] for b in bars]

    vol_ma5 = sum(vols[-5:]) / 5
    vol_ma20 = sum(vols[-20:]) / 20
    vol_ratio = vol_ma5 / vol_ma20 if vol_ma20 > 0 else 1.0

    pct_5d = (closes[-1] / closes[-6] - 1) * 100

    is_volume_up = vol_ratio > 1.15   # 放量
    is_volume_down = vol_ratio < 0.85  # 缩量
    is_price_up = pct_5d > 0.5
    is_price_down = pct_5d < -0.5

    if is_price_up and is_volume_up:
        return 0.8    # 放量上涨
    elif is_price_up and is_volume_down:
        return 0.3    # 缩量上涨
    elif is_price_down and is_volume_up:
        return -1.0   # 放量下跌（恐慌）
    elif is_price_down and is_volume_down:
        return -0.4   # 缩量下跌
    else:
        return 0.0    # 碰撞


def get_market_regime(verbose=False):
    """
    多因子加权评分 → Regime判定。

    4因子:
    1. 多指数趋势 (40%): 上证+中证1000+创业板 各自MA20/MA60投票
    2. 短期动量 (30%): 20日+5日涨跌幅加速度
    3. MA60斜率 (15%): 均线方向
    4. 量价配合 (15%): 放量/缩量 × 上涨/下跌

    评分范围: -1.0 ~ +1.0
    ≥+0.5   → bull      满仓，止损放宽
    +0.2~+0.5 → neutral  70%仓
    -0.2~+0.2 → warning  50%仓
    -0.5~-0.2 → bear     30%仓，不开新仓，止损收紧
    <-0.5   → deep_bear  清仓防御

    Returns:
        dict: regime, score, position_pct, factors, description, can_open_new
    """
    # ── 拉取三指数日K ──
    all_indices = {}
    for code, name in REGIME_INDICES:
        bars = fetch_index_daily(code, count=120)
        all_indices[code] = {"name": name, "bars": bars}

    # 数据不足检查
    sh_bars = all_indices["sh000001"]["bars"]
    if len(sh_bars) < 65:
        return {
            "regime": "unknown",
            "score": 0,
            "position_pct": 0.5,
            "description": "数据不足，默认50%仓位",
            "can_open_new": False,
            "factors": {},
        }

    # ── 因子1: 多指数趋势 (权重0.40) ──
    trend_scores = {}
    for code, info in all_indices.items():
        bars = info["bars"]
        if len(bars) >= 60:
            trend_scores[info["name"]] = _score_index_trend(bars)
        else:
            trend_scores[info["name"]] = 0.0

    f1_score = sum(trend_scores.values()) / len(trend_scores) if trend_scores else 0.0

    # ── 因子2: 动量/加速度 (权重0.30) ──
    # 用上证做基准
    f2_score = _score_momentum(sh_bars)

    # ── 因子3: MA60斜率 (权重0.15) ──
    f3_score = _score_ma_slope(sh_bars)

    # ── 因子4: 量价配合 (权重0.15) ──
    f4_score = _score_volume_price(sh_bars)

    # ── 加权总分 ──
    total_score = f1_score * 0.40 + f2_score * 0.30 + f3_score * 0.15 + f4_score * 0.15
    total_score = max(-1.0, min(1.0, total_score))

    # ── 评分 → Regime ──
    regime = "neutral"
    for threshold, label in SCORE_THRESHOLDS:
        if total_score >= threshold:
            regime = label
            break

    p = REGIME_PARAMS[regime]
    position_pct = p["position_pct"]

    # 描述
    trend_str = " ".join(f"{n}:{s:+.1f}" for n, s in trend_scores.items())
    sh_close = sh_bars[-1]["close"]
    sh_ma60 = calc_ma([b["close"] for b in sh_bars], 60)

    desc_parts = [
        f"{p['label']} 评分{total_score:+.2f}",
        f"趋势[{trend_str}]",
        f"动量{f2_score:+.2f}",
        f"斜率{f3_score:+.2f}",
        f"量价{f4_score:+.2f}",
        f"仓位{int(position_pct*100)}%",
    ]
    if regime in ("bear", "deep_bear"):
        desc_parts.append("不开新仓+收紧止损")
    if regime == "deep_bear":
        desc_parts.append("清仓防御")

    description = " | ".join(desc_parts)

    result = {
        "regime": regime,
        "score": round(total_score, 3),
        "position_pct": position_pct,
        "description": description,
        "can_open_new": regime in ("bull", "neutral"),
        "factors": {
            "trend": {k: round(v, 2) for k, v in trend_scores.items()},
            "trend_combined": round(f1_score, 3),
            "momentum": round(f2_score, 3),
            "ma_slope": round(f3_score, 3),
            "volume_price": round(f4_score, 3),
        },
        "index_price": round(sh_close, 2),
        "ma60": round(sh_ma60, 2) if sh_ma60 else None,
    }

    if verbose:
        print(f"\n  Regime分解:")
        print(f"    因子1 趋势(40%): {f1_score:+.3f} ← {trend_str}")
        print(f"    因子2 动量(30%): {f2_score:+.3f}")
        print(f"    因子3 斜率(15%): {f3_score:+.3f}")
        print(f"    因子4 量价(15%): {f4_score:+.3f}")
        print(f"    → 总分: {total_score:+.3f} → {regime}")

    return result


# ── 行业映射 ──
def get_industry_map(ts_codes):
    """从DB批量获取股票行业"""
    try:
        from .db import get_db
        conn = get_db()
        placeholders = ",".join("?" * len(ts_codes))
        rows = conn.execute(
            f"SELECT ts_code, industry FROM stock_basic WHERE ts_code IN ({placeholders})",
            ts_codes
        ).fetchall()
        conn.close()
        return {r["ts_code"]: (r["industry"] or "未知") for r in rows}
    except Exception:
        return {}


# ── 动态止损计算（含大盘Regime自适应）──
def calc_dynamic_stop(avg_cost, cur_price, daily_bars, max_price=None, regime=None):
    """
    计算动态止损价。结合ATR + 大盘Regime + 移动止损逻辑。

    🔑 核心规则：牛市放宽止损（让利润跑），熊市缩紧止损（保本金）。

    Regime 系数表:
    ┌──────────┬─────────┬────────┬───────────────┐
    │ Regime   │ ATR倍数 │ 地板线 │ 保本触发浮盈  │
    ├──────────┼─────────┼────────┼───────────────┤
    │ bull     │  2.5×   │  -10%  │  ≥15%         │
    │ neutral  │  2.0×   │   -8%  │  ≥10%         │
    │ warning  │  1.5×   │   -5%  │  ≥7%          │
    │ bear     │  1.0×   │   -3%  │  ≥5%          │
    │ deep_bear│  0.5×   │   -2%  │  ≥3%          │
    └──────────┴─────────┴────────┴───────────────┘

    Args:
        avg_cost: 持仓成本价
        cur_price: 当前价
        daily_bars: 近30日前复权日K (dict列表, 含high/low/close)
        max_price: 持仓期间最高价（用于 trailing stop）
        regime: 大盘regime字符串或dict。None则自动获取。

    Returns:
        dict: stop_price, stop_type, stop_reason, atr, regime, atr_multiplier
    """
    ret = (cur_price / avg_cost - 1) * 100 if avg_cost > 0 else 0

    # ── Regime 参数 ──
    if not regime:
        regime_data = get_market_regime()
        regime = regime_data["regime"]
    elif isinstance(regime, dict):
        regime = regime["regime"]

    p = REGIME_PARAMS.get(regime, REGIME_PARAMS["neutral"])

    # 地板线（按regime动态调整）
    floor_stop = avg_cost * (1 + p["floor_pct"] / 100)

    if not daily_bars or len(daily_bars) < 15:
        fallback_pct = p["floor_pct"] / 2
        return {
            "stop_price": round(avg_cost * (1 + fallback_pct / 100), 2),
            "stop_type": "fixed_fallback",
            "stop_reason": f"固定止损{fallback_pct:.0f}%(数据不足, {p['label']})",
            "atr": None,
            "regime": regime,
            "atr_multiplier": p["atr_mult"],
        }

    atr = calc_atr(daily_bars, 14)
    closes = [b["close"] for b in daily_bars]
    ma5 = calc_ma(closes, 5)

    if not atr or atr <= 0:
        return {
            "stop_price": round(avg_cost * (1 + p["floor_pct"] / 100), 2),
            "stop_type": "fixed_fallback",
            "stop_reason": f"固定止损{p['floor_pct']:.0f}%(ATR计算异常, {p['label']})",
            "atr": None,
            "regime": regime,
            "atr_multiplier": p["atr_mult"],
        }

    # 基础ATR止损线（regime自适应倍数）
    atr_stop = avg_cost - p["atr_mult"] * atr
    base_stop = max(atr_stop, floor_stop)
    atr_pct = (base_stop / avg_cost - 1) * 100

    # 移动止损阶梯（regime自适应保本触发点）
    be_thresh = p["breakeven_thresh"]

    if ret >= 20 and ma5 and ma5 > 0:
        stop_price = ma5
        stop_type = "trailing_ma5"
        stop_reason = f"移动止损(MA5={ma5:.2f}), 浮盈{ret:.1f}%, 锁定利润 [{p['label']}]"
    elif ret >= be_thresh:
        stop_price = max(base_stop, avg_cost)
        stop_type = "breakeven"
        stop_reason = f"保本止损(成本{avg_cost:.2f}), 浮盈{ret:.1f}%, ≥{be_thresh}%提前保护 [{p['label']}]"
    else:
        stop_price = base_stop
        stop_type = "atr"
        stop_reason = f"ATR止损({atr_pct:+.1f}%, ATR={atr:.2f}, {p['atr_mult']}σ) [{p['label']}]"

    return {
        "stop_price": round(stop_price, 2),
        "stop_type": stop_type,
        "stop_reason": stop_reason,
        "atr": round(atr, 2),
        "ma5": round(ma5, 2) if ma5 else None,
        "regime": regime,
        "atr_multiplier": p["atr_mult"],
    }


if __name__ == "__main__":
    print("=" * 60)
    print("📊 indicators.py v2 功能测试")
    print("=" * 60)

    # 1. Market regime
    print("\n── 大盘 Regime v2 (多因子) ──")
    regime = get_market_regime(verbose=True)
    print(f"\n  结果:")
    for k, v in regime.items():
        if k == "factors":
            print(f"    factors:")
            for fk, fv in v.items():
                print(f"      {fk}: {fv}")
        else:
            print(f"    {k}: {v}")

    # 2. ATR + 止损测试
    print("\n── ATR + 止损测试 (长江证券 sz000783) ──")
    from . import tencent as tc
    bars = tc.fetch_daily_kline("sz", "000783", count=30, fq="qfq")
    print(f"  获取 {len(bars)} 根日K")
    if bars:
        atr = calc_atr(bars, 14)
        print(f"  ATR(14) = {atr:.3f}" if atr else "  ATR计算失败")
        cost = 9.62
        cur = bars[-1]["close"]
        stop = calc_dynamic_stop(cost, cur, bars, regime=regime["regime"])
        print(f"  成本{cost} → 现价{cur:.2f} → 止损{stop['stop_price']} ({stop['stop_reason']})")

    print("\n✅ 测试完成")
