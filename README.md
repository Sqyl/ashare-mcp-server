# ashare-mcp-server

**A 股选股 MCP Server** — 把一套 5 层漏斗选股引擎封装为 [MCP](https://modelcontextprotocol.io) 工具，供 Claude / Cursor / 任意 LLM Agent 直接调用。

**A-share stock screener exposed as MCP tools** — a 5-layer funnel screening engine your LLM agent can call directly.

```
数据源(腾讯行情/Tushare) → 5层漏斗选股引擎 → SQLite → MCP Server ──→ 你的 Agent
                                                          ↑ stdio
                                              Claude / Cursor / DSH / 自研Agent
```

## 工具 / Tools

| 工具 | 说明 | 耗时 |
|---|---|---|
| `run_screen(mode)` | 5 层漏斗选股：主板过滤 → 换手率<10% → 全天最低价>MA5 → 创5年新高 → 筹码峰非顶部。`full`=全市场 / `pool`=自选池 / `test`=10 支小样本 | full 2–5 min，test ~30s |
| `latest_results()` | 查询最近一次选股结果（读 SQLite，秒回，演示首选） | <1s |
| `quote(ts_code)` | 单只实时报价，如 `600519.SH` | ~1s |

选股结果入库去重（`is_new` 标记新增/续选），天然支持 Agent 做"今天有哪些新入选"这类增量分析。

## 快速开始 / Quick Start

```bash
# 安装
pip install git+https://github.com/Sqyl/ashare-mcp-server.git

# 环境变量（全市场扫描需要 Tushare 免费token；test/pool 模式和实时报价不需要）
export TUSHARE_TOKEN=xxx        # https://tushare.pro 免费注册
# export ASHARE_DB=/path/to/ashare.db   # 可选，默认 ./data/ashare.db
```

**Claude Code / Cursor / 任意 MCP client 配置：**

```json
{
  "mcpServers": {
    "ashare-screener": {
      "command": "ashare-mcp",
      "env": { "TUSHARE_TOKEN": "xxx" }
    }
  }
}
```

配好后对你的 Agent 说：

> "先 run_screen test 跑个小样本，然后告诉我今天有哪些入选" 
> "查一下 600519.SH 现在什么价"

## 选股逻辑（漏斗 5 层）

| 层 | 条件 | 数据源 |
|---|---|---|
| 1 | 主板（60x/000/001/002），换手率 < 10%，剔除 ST/停牌 | 腾讯实时快照（批量） |
| 2 | 全天最低价 > MA5 | Tushare 按交易日批量 |
| 3 | 前复权最高价创近 5 年新高 | 腾讯日 K（带重试） |
| 4 | 筹码峰（120 日量价分布）不在当前价位顶部 | 腾讯日 K 计算 |
| 5 | 与前一日结果去重，标记 🆕 新入选 | SQLite |

> 策略本身来自个人回测迭代（如换手率阈值 8%→10% 的调整依据），**仅供学习研究，不构成投资建议**。

## 工程细节

- **限频对策**：腾讯实时接口按 50 支/批分组；日 K 拉取带 3 次重试 + 0.3s 间隔；Tushare 按交易日批量查询取代逐只查询
- **字段陷阱**：腾讯实时接口指数与个股字段数不同（~45 vs ~60 字段），解析按 `len(f)` 分流处理
- **前复权口径**：新高判断统一用腾讯前复权日 K，避免除权假突破
- **并发安全**：SQLite WAL 模式，选股写入与实时采集互不阻塞

## License

MIT
