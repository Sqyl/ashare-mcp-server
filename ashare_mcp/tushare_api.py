"""
Tushare 数据源
  - 日K线 / 周/月K线
  - 每日指标 (PE/PB/换手率/量比/市值)
  - 全A股列表
  - 复权因子
"""
import requests
import time
from .config import TUSHARE_TOKEN, TUSHARE_API, REQUEST_TIMEOUT, RETRY_COUNT, RETRY_DELAY


def _post(api_name, params, fields=""):
    """调用 Tushare REST API，带重试"""
    payload = {"api_name": api_name, "token": TUSHARE_TOKEN, "params": params}
    if fields:
        payload["fields"] = fields
    for attempt in range(RETRY_COUNT + 1):
        try:
            resp = requests.post(TUSHARE_API, json=payload, timeout=REQUEST_TIMEOUT)
            data = resp.json()
            if data.get("code") == 0:
                return data.get("data", {})
            else:
                code = data.get("code", "?")
                msg = data.get("msg", "")
                if attempt < RETRY_COUNT:
                    time.sleep(RETRY_DELAY)
                    continue
                print(f"  [Tushare] {api_name} 失败: code={code} msg={msg}")
                return None
        except Exception as e:
            if attempt < RETRY_COUNT:
                time.sleep(RETRY_DELAY)
                continue
            print(f"  [Tushare] {api_name} 异常: {e}")
            return None
    return None


def _rows_to_dicts(data):
    """Tushare 返回格式 → list[dict]"""
    if not data or "fields" not in data:
        return []
    fields = data["fields"]
    return [dict(zip(fields, item)) for item in data.get("items", [])]


# ════════════════════════════════════════════════════
#  全A股列表
# ════════════════════════════════════════════════════
def fetch_stock_basic(list_status="L"):
    data = _post("stock_basic", {"list_status": list_status},
                 "ts_code,symbol,name,area,industry,list_date")
    return _rows_to_dicts(data)


# ════════════════════════════════════════════════════
#  日K线
# ════════════════════════════════════════════════════
def fetch_daily(ts_code, start_date="", end_date=""):
    """日K线 (不复权)"""
    params = {"ts_code": ts_code}
    if start_date: params["start_date"] = start_date
    if end_date:   params["end_date"] = end_date
    data = _post("daily", params,
                 "ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount")
    return _rows_to_dicts(data)


# ════════════════════════════════════════════════════
#  每日指标
# ════════════════════════════════════════════════════
def fetch_daily_basic(ts_code, start_date="", end_date=""):
    """PE/PB/换手率/量比/市值等"""
    params = {"ts_code": ts_code}
    if start_date: params["start_date"] = start_date
    if end_date:   params["end_date"] = end_date
    data = _post("daily_basic", params,
                 "ts_code,trade_date,close,turnover_rate,turnover_rate_f,"
                 "volume_ratio,pe,pe_ttm,pb,ps,ps_ttm,dv_ratio,total_mv,circ_mv")
    return _rows_to_dicts(data)


# ════════════════════════════════════════════════════
#  复权因子
# ════════════════════════════════════════════════════
def fetch_adj_factor(ts_code, start_date="", end_date=""):
    params = {"ts_code": ts_code}
    if start_date: params["start_date"] = start_date
    if end_date:   params["end_date"] = end_date
    data = _post("adj_factor", params, "ts_code,trade_date,adj_factor")
    return _rows_to_dicts(data)
