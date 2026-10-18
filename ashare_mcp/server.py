"""ashare-mcp-server — A 股选股 MCP Server

3 个工具：
  - run_screen      全市场/股票池 5 层漏斗选股（写库）
  - latest_results  查询最近一次选股结果（读库秒回）
  - quote           单只股票实时报价

stdio transport，可挂到任意 MCP client（Claude / Cursor / DSH / 自研 Agent）。
"""
# MCP Python SDK 2.x 起 FastMCP 改名为 MCPServer；兼容 1.x 与 2.x
try:
    from mcp.server.mcpserver import MCPServer as FastMCP  # SDK 2.x
except ImportError:
    from mcp.server.fastmcp import FastMCP  # SDK 1.x

from . import screener
from .db import get_db, init_db
from . import tencent as tc

mcp = FastMCP("ashare-screener")

# 首次启动自动建表
try:
    init_db()
except Exception as e:
    print(f"[DB] 初始化跳过: {e}")


@mcp.tool()
def run_screen(mode: str = "full") -> str:
    """跑完整5层漏斗选股(主板/换手/MA5/5年新高/筹码峰)。

    mode: full=全市场(2-5分钟, 需 TUSHARE_TOKEN) | pool=自选池 | test=小样本(10支)
    """
    if mode == "pool":
        from .config import load_pool
        stocks = load_pool()
    elif mode == "test":
        from .config import DEFAULT_POOL
        stocks = DEFAULT_POOL
    else:
        stocks = None
    rows = screener.run_screen(stocks=stocks)
    return "\n".join(
        f"{'🆕' if s.get('is_new') else '　'} {s['ts_code']} {s['name']} "
        f"涨{s.get('pct_chg')}% 换手{s.get('turnover')}%" for s in rows) or "今日无入选"


@mcp.tool()
def latest_results() -> str:
    """查最近一次选股结果(读DB秒回，演示首选)。空库时友好提示。"""
    conn = get_db()
    try:
        d = conn.execute("SELECT MAX(screen_date) FROM screen_result").fetchone()[0]
        if not d:
            return "库中暂无选股结果——请先调用 run_screen(mode='test') 跑一次小样本。"
        rows = conn.execute(
            "SELECT ts_code,name,pct_chg,turnover,is_new FROM screen_result WHERE screen_date=?",
            (d,)).fetchall()
    finally:
        conn.close()
    head = f"📅 {d} 共{len(rows)}支入选"
    return head + "\n" + "\n".join(
        f"{'🆕' if r['is_new'] else '　'} {r['ts_code']} {r['name']} "
        f"涨{r['pct_chg']}% 换手{r['turnover']}%" for r in rows)


@mcp.tool()
def quote(ts_code: str) -> str:
    """实时报价。ts_code 格式: 600519.SH / 000858.SZ"""
    code, suf = ts_code.split(".")
    r = tc.fetch_realtime([("sh" if suf.upper() == "SH" else "sz", code, "")])[0]
    if not r or not r.get("price"):
        return f"{ts_code} 未取到行情（检查代码/交易时间）"
    return f"{ts_code} 现价 {r['price']} 涨跌 {r['pct_chg']}% ({r['trade_time']})"


def main():
    mcp.run()  # stdio


if __name__ == "__main__":
    main()
