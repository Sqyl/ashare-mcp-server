"""
数据库初始化与操作
"""
import sqlite3
from datetime import datetime
from .config import DB_PATH


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # 并发读写不阻塞
    conn.execute("PRAGMA synchronous=NORMAL")  # 速度与安全平衡
    return conn


def init_db():
    """创建所有表"""
    conn = get_db()
    c = conn.cursor()

    # ── 股票池元数据 ──
    c.execute("""
    CREATE TABLE IF NOT EXISTS stock_basic (
        ts_code   TEXT PRIMARY KEY,   -- 600519.SH
        symbol    TEXT,               -- 600519
        name      TEXT,               -- 贵州茅台
        market    TEXT,               -- sh / sz
        area      TEXT,
        industry  TEXT,
        list_date TEXT,
        updated   TEXT
    )""")

    # ── 盘中实时快照（每分钟一条） ──
    c.execute("""
    CREATE TABLE IF NOT EXISTS minute_snapshot (
        ts_code     TEXT,
        trade_time  TEXT,    -- 2025-06-22 14:30:00
        price       REAL,    -- 最新价
        pct_chg     REAL,    -- 涨跌幅%
        change       REAL,   -- 涨跌额
        open        REAL,
        high        REAL,
        low         REAL,
        pre_close   REAL,
        vol         REAL,    -- 成交量(手)
        amount      REAL,    -- 成交额(元)
        turnover    REAL,    -- 换手率%
        pe          REAL,
        pb          REAL,
        total_mv    REAL,    -- 总市值(元)
        circ_mv     REAL,    -- 流通市值(元)
        -- 五档买卖盘
        bid1_p REAL, bid1_v REAL,
        bid2_p REAL, bid2_v REAL,
        bid3_p REAL, bid3_v REAL,
        bid4_p REAL, bid4_v REAL,
        bid5_p REAL, bid5_v REAL,
        ask1_p REAL, ask1_v REAL,
        ask2_p REAL, ask2_v REAL,
        ask3_p REAL, ask3_v REAL,
        ask4_p REAL, ask4_v REAL,
        ask5_p REAL, ask5_v REAL,
        PRIMARY KEY (ts_code, trade_time)
    )""")

    # ── 分钟K线 ──
    c.execute("""
    CREATE TABLE IF NOT EXISTS minute_kline (
        ts_code     TEXT,
        trade_time  TEXT,    -- 2025-06-22 14:30:00
        open        REAL,
        close       REAL,
        high        REAL,
        low         REAL,
        vol         REAL,    -- 成交量(手)
        amount      REAL,    -- 成交额(元)
        PRIMARY KEY (ts_code, trade_time)
    )""")

    # ── 日K线 ──
    c.execute("""
    CREATE TABLE IF NOT EXISTS daily_kline (
        ts_code     TEXT,
        trade_date  TEXT,    -- 20250622
        open        REAL,
        close       REAL,
        high        REAL,
        low         REAL,
        pre_close   REAL,
        change      REAL,
        pct_chg     REAL,
        vol         REAL,
        amount      REAL,
        PRIMARY KEY (ts_code, trade_date)
    )""")

    # ── 每日指标（PE/PB/换手率/量比等） ──
    c.execute("""
    CREATE TABLE IF NOT EXISTS daily_basic (
        ts_code         TEXT,
        trade_date      TEXT,
        close           REAL,
        turnover_rate   REAL,
        turnover_rate_f REAL,    -- 自由流通换手率
        volume_ratio    REAL,    -- 量比
        pe              REAL,
        pe_ttm          REAL,
        pb              REAL,
        ps              REAL,
        ps_ttm          REAL,
        dv_ratio        REAL,    -- 股息率
        total_mv        REAL,    -- 总市值
        circ_mv         REAL,    -- 流通市值
        PRIMARY KEY (ts_code, trade_date)
    )""")

    # ── 采集日志 ──
    c.execute("""
    CREATE TABLE IF NOT EXISTS collect_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        task_type   TEXT,
        ts_code     TEXT,
        status      TEXT,
        message     TEXT,
        created_at  TEXT DEFAULT (datetime('now','localtime'))
    )""")

    # ── 每日选股结果 ──
    c.execute("""
    CREATE TABLE IF NOT EXISTS screen_result (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        screen_date     TEXT,
        ts_code         TEXT,
        name            TEXT,
        close_qfq       REAL,
        close_raw       REAL,
        pct_chg         REAL,
        turnover        REAL,
        ma5             REAL,
        high_5y         REAL,
        chip_peak       REAL,
        chip_conc       REAL,
        is_new          INTEGER DEFAULT 1,
        UNIQUE(screen_date, ts_code)
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_screen_date ON screen_result(screen_date)")

    # ── 选股跟踪（入选日起的T+N收益） ──
    c.execute("""
    CREATE TABLE IF NOT EXISTS screen_track (
        ts_code         TEXT,
        screen_date     TEXT,       -- 入选日期
        entry_close     REAL,       -- 入选日收盘(前复权)
        next_ret        REAL,       -- 次日收益%
        next_red        INTEGER,    -- 1=红盘 0=绿盘
        next_limit      INTEGER,    -- 1=涨停
        ret_t1          REAL,
        ret_t3          REAL,
        ret_t5          REAL,
        ret_t10         REAL,
        ret_t20         REAL,
        ret_t60         REAL,
        max_ret         REAL,       -- 区间最大收益%
        min_ret         REAL,       -- 区间最大回撤%
        last_update     TEXT,
        PRIMARY KEY (ts_code, screen_date)
    )""")

    conn.commit()
    conn.close()
    print(f"[DB] 初始化完成: {DB_PATH}")
    return True


if __name__ == "__main__":
    init_db()
