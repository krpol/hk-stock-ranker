# ============================================================
# 全局配置
# ============================================================
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 数据存储目录
DATA_DIR = os.path.join(BASE_DIR, "data")
CSV_DIR = os.path.join(DATA_DIR, "csv")
EXCEL_DIR = os.path.join(DATA_DIR, "excel")

# 历史数据保留年数
HISTORY_YEARS = 5

# 港股股票池过滤条件（可按需调整）
HK_STOCK_FILTERS = {
    # 最小流动性门槛（港元，实际对应「当日成交额」amount）：
    # 新版 AKShare 的 market_cap 字段已不可靠（实测与 amount 完全一致，存的是成交额），
    # 故统一用成交额做流动性过滤。Step 4（2026-11-20）：阈值从 1 万港元（形同虚设，
    # 只剔除停牌票）提高到 100 万港元，剔除微盘股——成交额 <100 万无法容纳调仓资金、
    # 买卖价差大、易被操纵、幸存者偏差严重。对应月度成交额约 2200 万港元。
    "min_market_cap": 1_000_000,
    # 最大市值限制（None 表示不限）
    "max_market_cap": None,
    # 要求 N 年内有分红记录（None 表示不限）
    "require_dividend_years": 3,
    # 最大有息负债率（None 表示不限）；超过此阈值的股票将被过滤
    # 有息负债率 = 有息负债 / 总资产 = (短期借款 + 长期借款) / 总资产
    # B21: 已从 ranker 阶段迁移到 web 层 checkbox（fnd），此处置 None 关闭硬过滤
    "max_net_debt_ratio": None,
    # B3: 最大资产负债率（None 表示不限）；超过此阈值的股票将被过滤
    # 资产负债率 = 总负债 / 总资产
    "max_total_liability_ratio": 0.80,
    # B19: 最大有息负债/净资产（None 表示不限）；超过此阈值的股票将被过滤
    # 有息负债/净资产 = (短期借款 + 长期借款) / 股东权益
    # B32-UI: 已禁用，与 web 层 fndz (zscore) 过滤重复
    "max_debt_to_equity": None,
}

# B9: 流动性窗口复核
# rebuild-pool 时，对"单日快照成交额 < min_market_cap"的票，
# 再用近 N 个交易日的 max(close*volume) 复核；只要窗口内任一天满足门槛即入池。
# 解决仙股/冷门股因 rebuild 当天恰好停牌或零成交而被永久踢出的问题（badcase B9：01245 NIRAKU）。
# 设为 0 则禁用复核（退回到旧行为：只看单日快照）。
LIQUIDITY_WINDOW_DAYS = 5

# yfinance 超时与重试
YFINANCE_TIMEOUT = 30
YFINANCE_MAX_RETRIES = 3

# akshare 请求间隔（秒），避免被封
AKSHARE_SLEEP = 0.5

# Web 服务端口
WEB_PORT = 5001
WEB_DEBUG = True

# 调度：每月几号更新（1~28）
MONTHLY_UPDATE_DAY = 5

# 股票池重建提醒阈值（天）：超过此天数会发出过期警告
# 默认 365 天（约 1 年），表示股票池应每年重建一次
POOL_REBUILD_INTERVAL_DAYS = 365

# ============================================================
# 增量更新策略
# ============================================================
# 财务数据 TTL（天）：updated_at 超过此天数会强制刷新
FINANCIAL_REFRESH_TTL_DAYS = 30
# 财年到期判定（天）：财年结束 N 天后视为应已发布新年报，需刷新
FORCE_REFRESH_AFTER_FY_DAYS = 120
# 财年到期触发的最低 age 阈值（避免刚刷过又刷）
FY_DUE_MIN_AGE_DAYS = 7
# 股票池轻量复检间隔（天）
POOL_LIGHT_CHECK_DAYS = 90
# 月度价格历史回填月数（用于回测）
HISTORY_BACKFILL_MONTHS = 60

# ============================================================
# B45: 财务数据 stale 扫描（--update / --rebuild-pool 尾部自动运行）
# 关键字段中 NaN 项数 >= threshold 视为 stale，自动触发补采
# ============================================================
STALE_FINANCIAL_SCAN = {
    "enabled": True,       # 是否在 --update / --rebuild-pool 尾部执行扫描
    "auto_refresh": True,  # 扫到 stale 是否自动补采
    "threshold": 3,        # NaN 项数阈值
    "workers": 4,
}

# ============================================================
# 量化回测配置
# ============================================================
# 单边交易成本（基点 bps，1 bps = 0.01%）；港股印花税+佣金估算
BACKTEST_TRANSACTION_COST_BPS = 15
# 默认主策略 TOP N
BACKTEST_DEFAULT_TOP_N = 20
# 默认权重方案（equal / score / rank）
BACKTEST_DEFAULT_WEIGHT = "equal"
# 无风险利率（年化），用于夏普比率
BACKTEST_RISK_FREE_RATE = 0.03
# 五分位组数（默认 5）
BACKTEST_QUANTILE_COUNT = 5

# ============================================================
# 网络代理配置
# ============================================================
# 统一出口代理：规避「直连 / WorkBuddy 沙箱代理出口 IP 被东财、yfinance 限流」的问题。
# 默认自动探测本机代理端口（127.0.0.1:6864，如 Atlas/Clash 等）；可用环境变量
# HK_RANKER_PROXY 显式覆盖；探测失败则不设代理（直连）。
# 所有采集请求（akshare / yfinance / curl）统一读此环境变量，一处设置全局生效。
NETWORK_PROXY = os.environ.get("HK_RANKER_PROXY", "").strip()
_NETWORK_PROXY_DEFAULT_HOST = "127.0.0.1"
_NETWORK_PROXY_DEFAULT_PORT = 6864


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def apply_network_proxy() -> str:
    """设置统一出口代理，返回生效的代理 URL（空串表示直连）。"""
    proxy = NETWORK_PROXY
    if not proxy and _port_open(_NETWORK_PROXY_DEFAULT_HOST, _NETWORK_PROXY_DEFAULT_PORT):
        proxy = f"http://{_NETWORK_PROXY_DEFAULT_HOST}:{_NETWORK_PROXY_DEFAULT_PORT}"
    if proxy:
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            os.environ[key] = proxy
    return proxy


# 模块加载即应用代理：main.py 与 scripts/*.py 只要 import config.settings 即生效
apply_network_proxy()
