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
    # 最小流动性门槛（港元）：
    # 由于新版 AKShare 不再提供市值字段，此值对应「当日成交额」过滤。
    # 成交额为 0 的停牌股会被直接剔除；非停牌股中成交额代表当日流动性。
    # 当日成交额 1 万 ≈ 月度成交额约 22 万（约 22 个交易日），
    # 用于过滤完全无交易的边缘票（停牌股已单独过滤，此门槛极低）。
    "min_market_cap": 10_000,
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
    "max_debt_to_equity": 1.00,
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
