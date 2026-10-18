"""
ashare-mcp-server — 全局配置

所有路径/密钥均可通过环境变量覆盖，仓库不含任何私有信息。
"""
import os

# ── 路径 ──
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# SQLite 数据库位置：默认包目录下 data/ashare.db，可用 ASHARE_DB 覆盖
_DB_DIR = os.environ.get("ASHARE_DB_DIR", os.path.join(BASE_DIR, "..", "data"))
os.makedirs(_DB_DIR, exist_ok=True)
DB_PATH = os.environ.get("ASHARE_DB", os.path.join(_DB_DIR, "ashare.db"))

# ── Tushare（全市场扫描需要；仅用股票池/实时报价可不配）──
TUSHARE_TOKEN = os.environ.get("TUSHARE_TOKEN", "")
TUSHARE_API = "https://api.tushare.pro"

# ── 腾讯行情（公开接口，无需鉴权）──
TENCENT_QUOTE = "https://qt.gtimg.cn/q={codes}"
TENCENT_MKLINE = "https://ifzq.gtimg.cn/appstock/app/kline/mkline?param={code},m{period},,{count}"
TENCENT_DKLINE = "https://ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{count},{fq}"

# ── 股票池 ──
# 默认测试池；生产环境从 stock_pool.txt 加载或 Tushare 自动筛选
DEFAULT_POOL = [
    ("sh", "600519", "贵州茅台"),
    ("sz", "000001", "平安银行"),
    ("sz", "300750", "宁德时代"),
    ("sh", "601318", "中国平安"),
    ("sz", "002594", "比亚迪"),
    ("sh", "688981", "中芯国际"),
    ("sz", "000858", "五粮液"),
    ("sh", "600036", "招商银行"),
    ("sz", "300059", "东方财富"),
    ("sh", "601012", "隆基绿能"),
]

POOL_FILE = os.path.join(BASE_DIR, "stock_pool.txt")


def load_pool():
    """从 stock_pool.txt 加载股票池，格式每行: sh 600519 贵州茅台"""
    if not os.path.exists(POOL_FILE):
        return DEFAULT_POOL
    pool = []
    with open(POOL_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 2)
            if len(parts) >= 2:
                market = parts[0]
                code = parts[1]
                name = parts[2] if len(parts) > 2 else ""
                pool.append((market, code, name))
    return pool if pool else DEFAULT_POOL


# ── 采集参数 ──
REALTIME_BATCH_SIZE = 50       # 腾讯每次最多 ~80 支，留余量
REALTIME_INTERVAL_SEC = 60     # 盘中采集间隔（秒）
MARKET_START = "09:15"         # 采集开始
MARKET_END = "15:05"           # 采集结束
STRATEGY_START = "14:30"       # 策略启动时间

# 网络参数
REQUEST_TIMEOUT = 10
RETRY_COUNT = 2
RETRY_DELAY = 1
