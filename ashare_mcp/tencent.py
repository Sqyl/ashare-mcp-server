"""
腾讯行情数据源
  - 实时快照 (qt.gtimg.cn)         → 五档 + PE/PB/市值
  - 分钟K线 (ifzq.gtimg.cn/mkline)
  - 日K线   (web.ifzq.gtimg.cn)
"""
import re
import requests
from .config import TENCENT_QUOTE, TENCENT_MKLINE, TENCENT_DKLINE, REQUEST_TIMEOUT


def _ts_code(market, code):
    """sh 600519 → 600519.SH"""
    suffix = {"sh": ".SH", "sz": ".SZ"}.get(market, "." + market.upper())
    return f"{code}{suffix}"


def _tencent_code(market, code):
    """sh 600519 → sh600519"""
    return f"{market}{code}"


# ════════════════════════════════════════════════════
#  实时快照（批量）
# ════════════════════════════════════════════════════
def fetch_realtime(pool):
    """
    批量拉取实时行情快照
    pool: [(market, code, name), ...]
    return: [{ts_code, trade_time, price, pct_chg, ...}, ...]
    """
    results = []
    # 分批，每批 REALTIME_BATCH_SIZE
    from .config import REALTIME_BATCH_SIZE
    for i in range(0, len(pool), REALTIME_BATCH_SIZE):
        batch = pool[i:i + REALTIME_BATCH_SIZE]
        codes = ",".join(_tencent_code(m, c) for m, c, _ in batch)
        url = TENCENT_QUOTE.format(codes=codes)
        try:
            resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"},
                                timeout=REQUEST_TIMEOUT)
            results.extend(_parse_realtime(resp.text, batch))
        except Exception as e:
            print(f"  [腾讯] 批次 {i//REALTIME_BATCH_SIZE} 失败: {e}")
    return results


def _parse_realtime(text, batch):
    """解析腾讯实时行情文本"""
    snapshots = []
    for line in text.strip().split(";"):
        line = line.strip()
        if not line:
            continue
        m = re.search(r'v_(\w+)="(.+)"', line)
        if not m:
            continue
        f = m.group(2).split("~")
        if len(f) < 49:   # 字段不足，跳过
            continue

        try:
            snap = {
                "ts_code": _ts_code_from_tencent(m.group(1)),
                "trade_time": _parse_tencent_time(f[30]) if f[30] else "",
                "price":     _f(f[3]),
                "pct_chg":   _f(f[32]),
                "change":    _f(f[31]),
                "open":      _f(f[5]),
                "high":      _f(f[33]),
                "low":       _f(f[34]),
                "pre_close": _f(f[4]),
                "vol":       _f(f[6]),       # 成交量(手)
                "amount":    _f(f[37]) * 1e4, # 万 → 元
                "turnover":  _f(f[38]),
                "pe":        _f(f[39]),
                "pb":        _f(f[46] if len(f) > 46 else "0"),
                "total_mv":  _f(f[44] if len(f) > 44 else "0") * 1e8,  # 亿 → 元
                "circ_mv":   _f(f[45] if len(f) > 45 else "0") * 1e8,
                # 五档买盘
                "bid1_p": _f(f[17]), "bid1_v": _f(f[18]),
                "bid2_p": _f(f[15]), "bid2_v": _f(f[16]),
                "bid3_p": _f(f[13]), "bid3_v": _f(f[14]),
                "bid4_p": _f(f[11]), "bid4_v": _f(f[12]),
                "bid5_p": _f(f[9]),  "bid5_v": _f(f[10]),
                # 五档卖盘
                "ask1_p": _f(f[19]), "ask1_v": _f(f[20]),
                "ask2_p": _f(f[21]), "ask2_v": _f(f[22]),
                "ask3_p": _f(f[23]), "ask3_v": _f(f[24]),
                "ask4_p": _f(f[25]), "ask4_v": _f(f[26]),
                "ask5_p": _f(f[27]), "ask5_v": _f(f[28]),
            }
            snapshots.append(snap)
        except (IndexError, ValueError) as e:
            print(f"  [腾讯] 解析失败 {m.group(1)}: {e}")
    return snapshots


def _ts_code_from_tencent(tencent_code):
    """sh600519 → 600519.SH"""
    market = tencent_code[:2]
    code = tencent_code[2:]
    return _ts_code(market, code)


def _parse_tencent_time(s):
    """20260622161423 → 2026-06-22 16:14:23"""
    if len(s) >= 14:
        return f"{s[:4]}-{s[4:6]}-{s[6:8]} {s[8:10]}:{s[10:12]}:{s[12:14]}"
    return s


# ════════════════════════════════════════════════════
#  分钟K线
# ════════════════════════════════════════════════════
def fetch_minute_kline(market, code, count=240, period=1):
    """
    拉取分钟K线
    period: 1/5/15/30/60
    return: [{ts_code, trade_time, open, close, high, low, vol, amount}]
    """
    tc = _tencent_code(market, code)
    url = TENCENT_MKLINE.format(code=tc, period=period, count=count)
    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"},
                            timeout=REQUEST_TIMEOUT)
        data = resp.json()
        ts_code = _ts_code(market, code)
        klines = data.get("data", {}).get(tc, {}).get(f"m{period}", [])
        results = []
        for k in klines:
            # 腾讯格式: ["202606221458", "1214.00", "1215.00", "1216.00", "1213.00", "120"]
            results.append({
                "ts_code": ts_code,
                "trade_time": _fmt_tencent_time(k[0]),
                "open":   _f(k[1]),
                "close":  _f(k[2]),
                "high":   _f(k[3]),
                "low":    _f(k[4]),
                "vol":    _f(k[5]),
                "amount": 0,  # 腾讯分钟K不返回成交额
            })
        return results
    except Exception as e:
        print(f"  [腾讯] 分钟K线 {code} 失败: {e}")
        return []


# ════════════════════════════════════════════════════
#  日K线
# ════════════════════════════════════════════════════
def fetch_daily_kline(market, code, count=250, fq="qfq"):
    """
    拉取日K线 (前复权)
    return: [{ts_code, trade_date, open, close, high, low, vol}]
    """
    tc = _tencent_code(market, code)
    url = TENCENT_DKLINE.format(code=tc, count=count, fq=fq)
    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"},
                            timeout=REQUEST_TIMEOUT)
        data = resp.json()
        ts_code = _ts_code(market, code)
        stock_data = data.get("data", {}).get(tc, {})

        # 找到 K线列表
        bars = []
        for key in [f"{fq}day", "day", f"{fq}week", "week"]:
            val = stock_data.get(key)
            if isinstance(val, dict) and val.get("data"):
                bars = val["data"]
                break
            elif isinstance(val, list) and val:
                bars = val
                break

        results = []
        for b in bars:
            # ["2026-06-12", "1271.18", "1291.91", "1295.00", "1265.01", "50495.00"]
            results.append({
                "ts_code": ts_code,
                "trade_date": b[0].replace("-", ""),
                "open":   _f(b[1]),
                "close":  _f(b[2]),
                "high":   _f(b[3]),
                "low":    _f(b[4]),
                "vol":    _f(b[5]),
                "amount": _f(b[6]) if len(b) > 6 else 0,
            })
        return results
    except Exception as e:
        print(f"  [腾讯] 日K线 {code} 失败: {e}")
        return []


def _fmt_tencent_time(s):
    """202606221458 → 2026-06-22 14:58:00"""
    s = str(s).strip()
    if len(s) == 12 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]} {s[8:10]}:{s[10:12]}:00"
    elif len(s) == 14 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]} {s[8:10]}:{s[10:12]}:{s[12:14]}"
    elif len(s) == 10 and "-" in s:
        return f"{s}:00"
    return s


def _f(val):
    """安全转float"""
    try:
        if val == "" or val is None:
            return 0.0
        return float(val)
    except (ValueError, TypeError):
        return 0.0
