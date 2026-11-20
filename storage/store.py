"""
数据存储层：CSV 原始数据 + Excel 排序结果
"""
import os
import logging
import re
from datetime import datetime

import pandas as pd

from config.settings import CSV_DIR, EXCEL_DIR

logger = logging.getLogger(__name__)

# ---- 文件路径约定 ----
STOCK_LIST_CSV = os.path.join(CSV_DIR, "hk_stock_list.csv")
STOCK_POOL_CSV = os.path.join(CSV_DIR, "hk_stock_pool.csv")
POOL_SNAPSHOT_DIR = os.path.join(CSV_DIR, "pools")
POOL_SNAPSHOT_CSV = os.path.join(POOL_SNAPSHOT_DIR, "hk_stock_pool_{date}.csv")
MONTHLY_PRICE_CSV = os.path.join(CSV_DIR, "hk_monthly_price.csv")
FINANCIAL_CSV = os.path.join(CSV_DIR, "hk_financial.csv")
INDUSTRY_CSV = os.path.join(CSV_DIR, "hk_industry.csv")
RANKING_CSV = os.path.join(CSV_DIR, "hk_ranking_{period}.csv")
RANKING_EXCEL = os.path.join(EXCEL_DIR, "hk_ranking_{period}.xlsx")


def _ensure_dirs():
    os.makedirs(CSV_DIR, exist_ok=True)
    os.makedirs(EXCEL_DIR, exist_ok=True)
    os.makedirs(POOL_SNAPSHOT_DIR, exist_ok=True)


# ============================================================
# 股票列表
# ============================================================

def save_stock_list(df: pd.DataFrame):
    _ensure_dirs()
    df.to_csv(STOCK_LIST_CSV, index=False, encoding="utf-8-sig")
    logger.info(f"股票列表已保存: {STOCK_LIST_CSV} ({len(df)} 只)")


def load_stock_list() -> pd.DataFrame:
    if not os.path.exists(STOCK_LIST_CSV):
        return pd.DataFrame()
    return pd.read_csv(STOCK_LIST_CSV, dtype={"code": str})


# ============================================================
# 股票池（年度更新：满足市值+分红条件的稳定列表）
# ============================================================

def save_stock_pool(df: pd.DataFrame):
    """
    保存年度股票池，自动记录构建时间。

    股票池文件比 stock_list 更稳定，每年重建一次，
    月度更新时只对池内股票采集数据。
    """
    _ensure_dirs()
    df = df.copy()
    df["code"] = df["code"].astype(str).str.zfill(5)
    df["pool_build_date"] = datetime.now().strftime("%Y-%m-%d")
    df.to_csv(STOCK_POOL_CSV, index=False, encoding="utf-8-sig")
    logger.info(f"股票池已保存: {STOCK_POOL_CSV} ({len(df)} 只)")


def load_stock_pool() -> pd.DataFrame:
    """加载股票池，返回空 DataFrame 如果文件不存在"""
    if not os.path.exists(STOCK_POOL_CSV):
        return pd.DataFrame()
    return pd.read_csv(STOCK_POOL_CSV, dtype={"code": str})


def get_stock_pool_age_days() -> int:
    """
    返回股票池距今的天数，若不存在则返回 -1
    """
    if not os.path.exists(STOCK_POOL_CSV):
        return -1
    df = pd.read_csv(STOCK_POOL_CSV, dtype={"code": str})
    if "pool_build_date" not in df.columns or df.empty:
        return -1
    try:
        build_date = pd.to_datetime(df["pool_build_date"].iloc[0])
        return (pd.Timestamp.now() - build_date).days
    except Exception:
        return -1


# ============================================================
# 股票池快照（年度独立池）
# ============================================================

def _snapshot_path(as_of_date) -> str:
    """根据基准日返回 snapshot 文件路径。as_of_date 可以是 datetime/date/str(YYYY-MM-DD)。"""
    if hasattr(as_of_date, "strftime"):
        date_str = as_of_date.strftime("%Y-%m-%d")
    else:
        date_str = str(as_of_date)
    return POOL_SNAPSHOT_CSV.format(date=date_str)


def save_stock_pool_snapshot(df: pd.DataFrame, as_of_date) -> str:
    """
    保存某基准日的股票池快照。
    
    Args:
        df: 股票池 DataFrame，必须含 code 列
        as_of_date: 基准日（datetime/date/YYYY-MM-DD）
    
    Returns:
        写入的文件路径
    """
    _ensure_dirs()
    if df is None or df.empty:
        logger.warning(f"股票池快照为空，跳过保存 (as_of_date={as_of_date})")
        return ""
    df = df.copy()
    df["code"] = df["code"].astype(str).str.zfill(5)
    if hasattr(as_of_date, "strftime"):
        df["pool_build_date"] = as_of_date.strftime("%Y-%m-%d")
    else:
        df["pool_build_date"] = str(as_of_date)
    path = _snapshot_path(as_of_date)
    df.to_csv(path, index=False, encoding="utf-8-sig")
    logger.info(f"股票池快照已保存: {path} ({len(df)} 只)")
    return path


def load_stock_pool_snapshot(as_of_date) -> pd.DataFrame:
    """加载某基准日的股票池快照；不存在则返回空 DataFrame。"""
    path = _snapshot_path(as_of_date)
    if not os.path.exists(path):
        return pd.DataFrame()
    return pd.read_csv(path, dtype={"code": str})


def list_pool_snapshots() -> list:
    """
    列出所有 snapshot 的基准日（升序）。
    
    Returns:
        list[str]，例如 ["2020-12-31", "2021-12-31", ...]
    """
    if not os.path.isdir(POOL_SNAPSHOT_DIR):
        return []
    import re
    pat = re.compile(r"^hk_stock_pool_(\d{4}-\d{2}-\d{2})\.csv$")
    dates = []
    for fn in os.listdir(POOL_SNAPSHOT_DIR):
        m = pat.match(fn)
        if m:
            dates.append(m.group(1))
    return sorted(dates)


# ============================================================
# 月度价格（追加模式，去重）
# ============================================================

def save_monthly_price(df: pd.DataFrame):
    """追加月度价格数据，按 (code, date) 去重。date 统一归一化到月末，避免
    东财(月末)/yfinance(月初) 口径混用导致同月两条记录。"""
    _ensure_dirs()
    df["code"] = df["code"].astype(str).str.zfill(5)
    df["date"] = (pd.to_datetime(df["date"]) + pd.offsets.MonthEnd(0)).dt.strftime("%Y-%m-%d")

    if os.path.exists(MONTHLY_PRICE_CSV):
        existing = pd.read_csv(MONTHLY_PRICE_CSV, dtype={"code": str})
        # 存量数据同样归一化到月末，再按 (code, date) 去重，顺带清理历史混用
        existing["date"] = (pd.to_datetime(existing["date"]) + pd.offsets.MonthEnd(0)).dt.strftime("%Y-%m-%d")
        combined = pd.concat([existing, df], ignore_index=True)
        combined = combined.drop_duplicates(subset=["code", "date"], keep="last")
    else:
        combined = df

    combined.to_csv(MONTHLY_PRICE_CSV, index=False, encoding="utf-8-sig")
    logger.info(f"月度价格已保存: {MONTHLY_PRICE_CSV} ({len(combined)} 行)")


def load_monthly_price() -> pd.DataFrame:
    if not os.path.exists(MONTHLY_PRICE_CSV):
        return pd.DataFrame()
    df = pd.read_csv(MONTHLY_PRICE_CSV, dtype={"code": str})
    # 兼容历史脏数据：日期可能含时区或不一致格式
    df["date"] = pd.to_datetime(df["date"], format="mixed", utc=True, errors="coerce").dt.tz_localize(None)
    df = df.dropna(subset=["date"])
    # B24: 兼容旧 schema —— 若无 close_qfq 列，回退为 close（避免下游 KeyError）
    if "close_qfq" not in df.columns:
        logger.warning("hk_monthly_price.csv 缺少 close_qfq 列，回退为 close。请运行 scripts/migrate_to_dual_price.py 升级数据")
        df["close_qfq"] = df["close"]
    return df


def load_latest_close_map() -> dict:
    """
    B51: 返回 {code: 最新收盘价(close, 不复权)}。

    取自月度价格表每只股票【最新日期】的 close 列。close 语义即
    「原始（不复权）收盘价，用于 ranking 展示价」，与 current_price 完全一致。
    该表已持久化（成功采集即缓存），是 current_price 最可靠且最新鲜的来源，
    避免依赖东财实时接口（不稳定）或股票池旧 price 快照（可能滞后数月）。

    Returns:
        dict[str, float]，例如 {"06110": 1.12, "00700": 388.0}
    """
    df = load_monthly_price()
    if df.empty or "close" not in df.columns:
        return {}
    df = df.sort_values("date")
    latest = df.groupby("code").tail(1)
    # 仅保留有效 close（排除 NaN/<=0），code 统一 zfill(5)
    latest = latest[latest["close"].notna() & (latest["close"] > 0)]
    latest = latest.copy()
    latest["code"] = latest["code"].astype(str).str.zfill(5)
    return dict(zip(latest["code"], latest["close"]))


# ============================================================
# 财务数据（全量覆盖，每次更新时重写）
# ============================================================

def save_financial_data(df: pd.DataFrame):
    _ensure_dirs()
    df = df.copy()
    df["code"] = df["code"].astype(str).str.zfill(5)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if "updated_at" not in df.columns:
        df["updated_at"] = now_str
    else:
        # 仅对缺失的行打时间戳（保留增量场景下复用记录的旧时间）
        df["updated_at"] = df["updated_at"].fillna(now_str)
        df.loc[df["updated_at"].astype(str).str.strip() == "", "updated_at"] = now_str
    df.to_csv(FINANCIAL_CSV, index=False, encoding="utf-8-sig")
    logger.info(f"财务数据已保存: {FINANCIAL_CSV} ({len(df)} 只)")


def load_financial_data() -> pd.DataFrame:
    if not os.path.exists(FINANCIAL_CSV):
        return pd.DataFrame()
    return pd.read_csv(FINANCIAL_CSV, dtype={"code": str})


# ============================================================
# 行业归属（独立缓存：相对静态、单独刷新）
# ============================================================

def save_industry_data(df: pd.DataFrame):
    """保存行业数据：列 [code, industry, updated_at]"""
    _ensure_dirs()
    df = df.copy()
    df["code"] = df["code"].astype(str).str.zfill(5)
    if "updated_at" not in df.columns:
        df["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    df.to_csv(INDUSTRY_CSV, index=False, encoding="utf-8-sig")
    logger.info(f"行业数据已保存: {INDUSTRY_CSV} ({len(df)} 只)")


def load_industry_data() -> pd.DataFrame:
    """加载行业数据；不存在返回空 DataFrame。"""
    if not os.path.exists(INDUSTRY_CSV):
        return pd.DataFrame(columns=["code", "industry", "updated_at"])
    return pd.read_csv(INDUSTRY_CSV, dtype={"code": str})


def upsert_industry_data(updates: dict):
    """
    增量合并行业数据。
    Args:
        updates: {code: industry_or_None}；None 不会覆盖已有行业。
    """
    existing = load_industry_data()
    if existing.empty:
        existing = pd.DataFrame(columns=["code", "industry", "updated_at"])
    existing["code"] = existing["code"].astype(str).str.zfill(5)
    existing_map = dict(zip(existing["code"], existing["industry"]))

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    new_rows = []
    for raw_code, ind in updates.items():
        code = str(raw_code).zfill(5)
        if ind is None or (isinstance(ind, float) and pd.isna(ind)):
            # 不覆盖已有
            if code in existing_map and pd.notna(existing_map[code]) and str(existing_map[code]).strip():
                continue
            ind_save = None
        else:
            ind_save = str(ind).strip() or None
        existing_map[code] = ind_save
        new_rows.append({"code": code, "industry": ind_save, "updated_at": now_str})

    if not new_rows:
        return existing

    new_df = pd.DataFrame(new_rows)
    # 去掉旧行后追加新行
    existing = existing[~existing["code"].isin(new_df["code"])]
    merged = pd.concat([existing, new_df], ignore_index=True)
    save_industry_data(merged)
    return merged


# ============================================================
# 排序结果（CSV + Excel）
# ============================================================

def save_ranking(df: pd.DataFrame, period: str):
    """
    保存排序结果到 CSV 和 Excel。

    Args:
        df: 排序好的 DataFrame
        period: 时间标签，如 "2024-04"
    """
    _ensure_dirs()
    csv_path = RANKING_CSV.format(period=period)
    xlsx_path = RANKING_EXCEL.format(period=period)

    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    logger.info(f"排序结果 CSV 已保存: {csv_path}")

    # Excel：多一个 Sheet 存元数据
    with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="排名", index=False)
        # 写入更新时间等元数据
        meta = pd.DataFrame([{
            "更新时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "数据期间": period,
            "股票数量": len(df),
            "回报率公式": "(TTM分红 + 每股回购) / 当前收盘价 - 年化稀释率（TTM分红已含预告分红）",
            "TTM分红定义": "最近一个完整财报年度的分红总和（含已公告未除息的预告分红）",
            "上年TTM分红": "上一完整财年分红总和（同比对比）",
            "最近期分红": "最近一期财报（任意期，可能是中报）的分红",
            "同期上年分红": "最近一期同期上一年的分红（环比对比）",
        }])
        meta.to_excel(writer, sheet_name="说明", index=False)

    logger.info(f"排序结果 Excel 已保存: {xlsx_path}")


def load_ranking(period: str) -> pd.DataFrame:
    csv_path = RANKING_CSV.format(period=period)
    if not os.path.exists(csv_path):
        return pd.DataFrame()
    return pd.read_csv(csv_path, dtype={"code": str})


def list_ranking_periods(tag: str = None) -> list:
    """列出已保存的排序期间。

    Args:
        tag: 排名标签（如 "pit" / "ann" / "sus" / "ann_sus"）。默认 None 只匹配
             旧口径 `hk_ranking_{YYYY-MM}.csv`；传入 tag 匹配 `hk_ranking_{tag}_{YYYY-MM}.csv`。
             用正则校验 period 为 YYYY-MM，避免把带标签的文件名误解析成期间。
    """
    if not os.path.exists(CSV_DIR):
        return []
    prefix = f"hk_ranking_{tag}_" if tag else "hk_ranking_"
    periods = []
    for f in os.listdir(CSV_DIR):
        if not f.startswith(prefix) or not f.endswith(".csv") or f.endswith("_zh.csv"):
            continue
        period = f[len(prefix):-4]
        if re.fullmatch(r"\d{4}-\d{2}", period):
            periods.append(period)
    return sorted(periods, reverse=True)


def prune_old_data(keep_years: int = 5):
    """删除超过 keep_years 年的月度价格数据"""
    df = load_monthly_price()
    if df.empty:
        return
    cutoff = pd.Timestamp.now() - pd.DateOffset(years=keep_years)
    df = df[df["date"] >= cutoff]
    df.to_csv(MONTHLY_PRICE_CSV, index=False, encoding="utf-8-sig")
    logger.info(f"已裁剪历史价格数据，保留 {keep_years} 年")
