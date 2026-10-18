"""
选股引擎 — 漏斗式5层过滤

条件1: 主板 (600/601/603/605/000/001/002)
条件2: 换手率 < 10% (2025-07回测优化，原8%)
条件3: 全天最低价 > MA5
条件4: 最高价(前复权)创近5年新高
条件5: 筹码峰不在当前价位（顶部非最高筹码）

用法:
  python screener.py                    # 全市场扫描
  python screener.py --pool             # 只扫股票池
  python screener.py --test             # 小样本测试(10支)
"""
import sys
import time
import json
import requests
import re
from datetime import datetime, timedelta
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed


from .db import get_db, init_db
from . import tencent as tc
from . import tushare_api as ts


# ════════════════════════════════════════════════════
#  工具函数
# ════════════════════════════════════════════════════
def is_main_board(code):
    """判断是否主板"""
    prefixes = ("600", "601", "603", "605", "000", "001", "002")
    return code.startswith(prefixes)


def get_all_stocks():
    """获取全A股主板列表，优先从DB缓存，其次Tushare"""
    conn = get_db()
    rows = conn.execute("SELECT ts_code, symbol, name, market FROM stock_basic").fetchall()
    conn.close()

    if rows:
        return [(r["market"], r["symbol"], r["name"]) for r in rows
                if is_main_board(r["symbol"])]

    # DB为空 → 从Tushare拉
    print("[Tushare] 拉取全A股列表...")
    stocks = ts.fetch_stock_basic()
    if not stocks:
        print("  ⚠️ Tushare限频，使用默认股票池")
        from .config import DEFAULT_POOL
        return DEFAULT_POOL

    # 写入DB缓存
    conn = get_db()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for s in stocks:
        market = "sh" if s["ts_code"].endswith(".SH") else "sz"
        conn.execute("""
            INSERT OR REPLACE INTO stock_basic
            (ts_code, symbol, name, market, area, industry, list_date, updated)
            VALUES (?,?,?,?,?,?,?,?)
        """, (s.get("ts_code"), s.get("symbol"), s.get("name"),
              market, s.get("area"), s.get("industry"),
              s.get("list_date"), now))
    conn.commit()
    conn.close()

    return [(("sh" if s["ts_code"].endswith(".SH") else "sz"), s["symbol"], s["name"])
            for s in stocks if is_main_board(s["symbol"])]


def get_all_tracked():
    """获取所有曾入选股票（全历史去重用）"""
    conn = get_db()
    rows = conn.execute("SELECT ts_code, screen_date, entry_close FROM screen_track").fetchall()
    conn.close()
    return {r["ts_code"]: {"screen_date": r["screen_date"], "entry_close": r["entry_close"]} for r in rows}


def get_yesterday_screen(screen_date):
    """获取前一天选股结果"""
    conn = get_db()
    # 找最近一次选股日（不一定是自然昨天）
    row = conn.execute("""
        SELECT DISTINCT screen_date FROM screen_result
        WHERE screen_date < ? ORDER BY screen_date DESC LIMIT 1
    """, (screen_date,)).fetchone()
    if not row:
        conn.close()
        return set()
    prev_date = row["screen_date"]
    rows = conn.execute("SELECT ts_code FROM screen_result WHERE screen_date = ?",
                        (prev_date,)).fetchall()
    conn.close()
    return {r["ts_code"] for r in rows}


# ════════════════════════════════════════════════════
#  漏斗层1: 主板 + 换手率 < 10%（批量实时快照）
#  2025-07回测优化: 8%→10%，放宽后年化+8.7% > +5.8%
# ════════════════════════════════════════════════════
def filter_turnover(stocks, max_turnover=10.0):
    """批量拉取实时快照，过滤主板+换手率"""
    from .config import REALTIME_BATCH_SIZE
    survivors = []
    total = len(stocks)

    for i in range(0, total, REALTIME_BATCH_SIZE):
        batch = stocks[i:i + REALTIME_BATCH_SIZE]
        codes = ",".join(f"{m}{c}" for m, c, _ in batch)
        try:
            resp = requests.get(f"https://qt.gtimg.cn/q={codes}",
                                headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            for line in resp.text.strip().split(";"):
                line = line.strip()
                if not line:
                    continue
                m = re.search(r'v_(\w+)="(.+)"', line)
                if not m:
                    continue
                f = m.group(2).split("~")
                if len(f) < 49:
                    continue
                code = f[2]
                name = f[1]

                # 主板过滤
                if not is_main_board(code):
                    continue

                turnover = _safe_float(f[38])
                # 换手率过滤
                if turnover >= max_turnover:
                    continue

                # 排除ST/退市
                if "ST" in name or "退" in name:
                    continue

                # 排除停牌（成交量为0）
                vol = _safe_float(f[6])
                if vol == 0:
                    continue

                market = "sh" if code.startswith("6") else "sz"
                survivors.append({
                    "market": market,
                    "code": code,
                    "name": name,
                    "turnover": turnover,
                    "close_qfq": _safe_float(f[3]),
                    "close_raw": _safe_float(f[3]),  # 实时价≈不复权
                    "pct_chg": _safe_float(f[32]),
                    "high_today": _safe_float(f[33]),
                    "low_today": _safe_float(f[34]),
                    "pre_close": _safe_float(f[4]),
                    "vol": _safe_float(f[6]),
                })
        except Exception as e:
            print(f"  批次 {i//REALTIME_BATCH_SIZE} 失败: {e}")

        if (i // REALTIME_BATCH_SIZE) % 10 == 0:
            print(f"  换手率过滤: {min(i+REALTIME_BATCH_SIZE, total)}/{total} → {len(survivors)} 支存活")

    return survivors


# ════════════════════════════════════════════════════
#  漏斗层2: 全天最低价 > MA5（Tushare批量取代腾讯逐只）
# ════════════════════════════════════════════════════
def get_recent_5_dates(date_str):
    """获取最近5个交易日"""
    from datetime import datetime, timedelta
    dates = []
    d = datetime.strptime(date_str, "%Y%m%d")
    while len(dates) < 5:
        d -= timedelta(days=1)
        if d.weekday() < 5:  # 跳过周末
            dates.append(d.strftime("%Y%m%d"))
    return dates


def fetch_batch_ma5(screen_date):
    """用Tushare trade_date批量查询5天行情，跳过无数据日期"""
    from . import tushare_api as tushare
    import time
    from datetime import datetime, timedelta

    # 从screen_date往前逐个交易日查，直到拿到5个有效日
    all_closes = {}
    d = datetime.strptime(screen_date, "%Y%m%d")
    found = 0
    max_attempts = 15  # 最多查15个日期

    while found < 5 and max_attempts > 0:
        d -= timedelta(days=1)
        max_attempts -= 1
        if d.weekday() >= 5:  # 周末跳过
            continue

        dt_str = d.strftime("%Y%m%d")
        data = tushare._post("daily", {"trade_date": dt_str},
                             "ts_code,trade_date,close,pre_close")
        if not data or not data.get("items"):
            print(f"  📅 {dt_str}: 0 支 (跳过)", flush=True)
            continue

        n = len(data["items"])
        for item in data["items"]:
            rec = dict(zip(data["fields"], item))
            code = rec["ts_code"]
            if code not in all_closes:
                all_closes[code] = []
            try:
                all_closes[code].append(float(rec.get("close", 0)))
            except (ValueError, TypeError):
                pass
        found += 1
        print(f"  📅 {dt_str}: {n} 支 (第{found}/5)", flush=True)
        time.sleep(3)

    # 只保留有5天数据的股票
    ma5_map = {}
    for code, closes in all_closes.items():
        if len(closes) >= 5:
            ma5_map[code] = sum(closes) / len(closes)
    print(f"  完成: {len(ma5_map)} 支有完整MA5数据", flush=True)
    return ma5_map


def filter_ma5(stocks, screen_date=None):
    """用Tushare批量MA5数据过滤"""
    if screen_date is None:
        from datetime import datetime
        screen_date = datetime.now().strftime("%Y%m%d")

    print("  从Tushare批量查询5日行情...", flush=True)
    ma5_map = fetch_batch_ma5(screen_date)

    survivors = []
    total = len(stocks)
    for i, s in enumerate(stocks):
        ts_code = f"{s['code']}.{'SH' if s['market']=='sh' else 'SZ'}"
        ma5 = ma5_map.get(ts_code)
        if ma5 is None:
            continue
        s["ma5"] = ma5
        # 条件: 最低价 > MA5
        if s["low_today"] <= ma5:
            continue
        survivors.append(s)

        if (i + 1) % 200 == 0:
            print(f"  MA5过滤: {i+1}/{total} → {len(survivors)} 支存活", flush=True)

    return survivors


# ════════════════════════════════════════════════════
#  漏斗层3+4: 创近5年新高 + 筹码峰非顶部（并发+重试）
# ════════════════════════════════════════════════════
def filter_high_and_chips(stocks, years=5):
    """串行+重试检查新高+筹码"""
    survivors = []
    total = len(stocks)
    for i, s in enumerate(stocks):
        # 带重试拉取5年日K
        target_count = 250 * years
        bars = None
        for attempt in range(3):
            bars = tc.fetch_daily_kline(s["market"], s["code"], count=target_count, fq="qfq")
            if bars and len(bars) >= 60:
                break
            time.sleep(1)

        if not bars or len(bars) < 60:
            if (i + 1) % 50 == 0:
                print(f"  新高+筹码: {i+1}/{total} → {len(survivors)} 支存活", flush=True)
            continue

        today = bars[-1]
        highs = [b["high"] for b in bars[:-1]]

        # 条件4: 创近5年新高
        max_hist = max(highs) if highs else 0
        if today["high"] < max_hist:
            if (i + 1) % 50 == 0:
                print(f"  新高+筹码: {i+1}/{total} → {len(survivors)} 支存活", flush=True)
            continue
        s["high_5y"] = today["high"]

        # 条件5: 筹码峰非顶部
        lookback = min(120, len(bars))
        recent = bars[-lookback:]
        price_vol = defaultdict(float)
        for b in recent:
            avg_p = (b["high"] + b["low"] + b["close"]) / 3
            bucket = round(avg_p * 2) / 2
            price_vol[bucket] += b["vol"]

        peak_price = max(price_vol, key=price_vol.get)
        s["chip_peak"] = peak_price

        # 筹码集中度
        sorted_p = sorted(price_vol.keys())
        total_vol = sum(price_vol.values())
        s["chip_conc"] = 0.0

        # 筹码峰不在当前价的95%以内
        if peak_price >= s["close_qfq"] * 0.95:
            if (i + 1) % 50 == 0:
                print(f"  新高+筹码: {i+1}/{total} → {len(survivors)} 支存活", flush=True)
            continue

        survivors.append(s)
        if (i + 1) % 50 == 0:
            print(f"  新高+筹码: {i+1}/{total} → {len(survivors)} 支存活", flush=True)

        time.sleep(0.3)  # 防限频

    return survivors


# ════════════════════════════════════════════════════
#  主入口
# ════════════════════════════════════════════════════
def run_screen(stocks=None, screen_date=None):
    """执行完整选股流程"""
    if screen_date is None:
        screen_date = datetime.now().strftime("%Y%m%d")

    if stocks is None:
        stocks = get_all_stocks()

    print(f"\n{'='*60}")
    print(f"📊 每日选股引擎启动 — {screen_date}")
    print(f"{'='*60}")

    # 大盘 regime 判断
    try:
        from .indicators import get_market_regime
        regime = get_market_regime()
        regime_emoji = {"bull": "🟢", "bear": "🔴", "warning": "🟡", "neutral": "⚪", "deep_bear": "💀"}.get(regime["regime"], "⚪")
        print(f"\n{regime_emoji} 大盘状态: {regime['description']}")
        if regime["regime"] in ("bear", "deep_bear"):
            print(f"  ⚠️ 空头格局! sim_trader将限制开仓(仓位系数{regime['position_pct']:.0%})")
        if regime["regime"] == "deep_bear":
            print(f"  💀 深熊防御模式! 系统将加速清理存量仓位")
        print()
    except Exception as e:
        print(f"  (regime获取失败: {e})\n")

    print(f"候选股票池: {len(stocks)} 支\n")

    t0 = time.time()

    # 漏斗层1: 换手率
    print("【层1】主板 + 换手率 < 10%")
    s1 = filter_turnover(stocks)
    print(f"  → {len(s1)} 支通过\n")

    # 漏斗层2: MA5
    print("【层2】全天最低价 > MA5")
    s2 = filter_ma5(s1)
    print(f"  → {len(s2)} 支通过\n")

    # 漏斗层3+4: 新高 + 筹码
    print("【层3】创近5年新高 + 筹码峰非顶部")
    s3 = filter_high_and_chips(s2)
    print(f"  → {len(s3)} 支通过\n")

    elapsed = time.time() - t0

    # 去重: 与前一天对比
    prev_codes = get_yesterday_screen(screen_date)
    for s in s3:
        ts_code = f"{s['code']}.{'SH' if s['market']=='sh' else 'SZ'}"
        s["ts_code"] = ts_code
        s["is_new"] = 0 if ts_code in prev_codes else 1

    # 写入DB
    _save_results(s3, screen_date)

    # 输出结果
    _print_results(s3, screen_date, elapsed)

    return s3


def _save_results(stocks, screen_date):
    conn = get_db()
    for s in stocks:
        conn.execute("""
            INSERT OR REPLACE INTO screen_result
            (screen_date, ts_code, name, close_qfq, close_raw, pct_chg,
             turnover, ma5, high_5y, chip_peak, chip_conc, is_new)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (screen_date, s["ts_code"], s["name"],
              s.get("close_qfq"), s.get("close_raw"), s.get("pct_chg"),
              s.get("turnover"), s.get("ma5"), s.get("high_5y"),
              s.get("chip_peak"), s.get("chip_conc"), s.get("is_new", 1)))

    # 写入选股跟踪表（所有入选股票都更新，ts_code 不重复）
    # 新增 → INSERT（记录首次入选价 entry_close）；
    # 续选 → 只更新 last_update，绝不覆盖 entry_close（否则 T+N 收益基准被刷新，
    #        跟踪失去意义）。screen_date 也保留为首次入选日。
    tracked = get_all_tracked()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for s in stocks:
        if s["ts_code"] not in tracked:
            conn.execute("""
                INSERT OR IGNORE INTO screen_track
                (ts_code, screen_date, entry_close, last_update)
                VALUES (?,?,?,?)
            """, (s["ts_code"], screen_date, s.get("close_qfq"), now_str))
        else:
            conn.execute("""
                UPDATE screen_track
                SET last_update = ?
                WHERE ts_code = ?
            """, (now_str, s["ts_code"]))

    conn.commit()
    conn.close()

    # 立即刷新 T+N 数据
    try:
        from .tracker import update_all
        update_all()
    except Exception as e:
        print(f"  ⚠️ update_all 失败: {e}")


def _print_results(stocks, screen_date, elapsed):
    new_count = sum(1 for s in stocks if s.get("is_new", 1) == 1)
    print(f"\n{'='*60}")
    print(f"✅ 选股完成! 耗时 {elapsed:.0f}s")
    print(f"{'='*60}")
    print(f"  符合条件: {len(stocks)} 支 (新增: {new_count}, 续选: {len(stocks)-new_count})")

    if stocks:
        print(f"\n{'代码':>10}  {'名称':>8}  {'收盘':>7}  {'涨跌%':>6}  {'换手%':>5}  {'MA5':>7}  {'5年高':>7}  {'筹码峰':>7}  {'集中度':>6}  新增")
        print("-" * 90)
        for s in sorted(stocks, key=lambda x: x.get("pct_chg", 0), reverse=True):
            tag = "🆕" if s.get("is_new") else "  "
            print(f"  {s['ts_code']:>10}  {s['name']:>8}  {s.get('close_qfq',0):>7.2f}  {s.get('pct_chg',0):>+6.2f}  {s.get('turnover',0):>5.1f}  {s.get('ma5',0):>7.2f}  {s.get('high_5y',0):>7.2f}  {s.get('chip_peak',0):>7.1f}  {s.get('chip_conc',0):>6.2f}  {tag}")


def _safe_float(val):
    try:
        if val == "" or val is None:
            return 0.0
        return float(val)
    except (ValueError, TypeError):
        return 0.0


if __name__ == "__main__":
    init_db()

    if "--test" in sys.argv:
        # 小样本测试
        from .config import DEFAULT_POOL
        stocks = DEFAULT_POOL
        run_screen(stocks)
    elif "--pool" in sys.argv:
        from .config import load_pool
        stocks = load_pool()
        run_screen(stocks)
    else:
        run_screen()
