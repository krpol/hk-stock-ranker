"""
基准指数采集（默认恒生指数 HSI）

策略：使用 yfinance（^HSI）拉历史月度收盘价，按 (period, close) 缓存到本地。
也兼容用户自带 CSV：data/csv/hk_benchmark_hsi.csv 列 [date, close]。
"""
import logging
import os
from typing import Optional

import pandas as pd

from config.settings import CSV_DIR

logger = logging.getLogger(__name__)

BENCHMARK_CSV = os.path.join(CSV_DIR, "hk_benchmark_hsi.csv")


def fetch_hsi_yfinance(start: str = "2020-01-01") -> pd.DataFrame:
    """
    从 yfinance 拉 ^HSI 月度收盘价。
    Returns DataFrame[date, close]
    """
    import yfinance as yf
    ticker = yf.Ticker("^HSI")
    hist = ticker.history(start=start, interval="1mo", auto_adjust=False)
    if hist.empty:
        return pd.DataFrame(columns=["date", "close"])
    hist = hist.reset_index()
    out = pd.DataFrame({
        "date": pd.to_datetime(hist["Date"]).dt.tz_localize(None),
        "close": hist["Close"].astype(float),
    })
    return out


def save_hsi(df: pd.DataFrame):
    os.makedirs(CSV_DIR, exist_ok=True)
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    df.to_csv(BENCHMARK_CSV, index=False, encoding="utf-8-sig")
    logger.info(f"基准指数已保存: {BENCHMARK_CSV} ({len(df)} 行)")


def load_hsi() -> pd.DataFrame:
    if not os.path.exists(BENCHMARK_CSV):
        return pd.DataFrame()
    df = pd.read_csv(BENCHMARK_CSV)
    df["date"] = pd.to_datetime(df["date"], format="mixed", errors="coerce")
    df = df.dropna(subset=["date"])
    return df


def get_hsi_monthly_returns(start_period: str = None,
                              end_period: str = None) -> pd.Series:
    """
    返回 HSI 月度收益 Series，index = 'YYYY-MM'。
    自动从本地 CSV 加载，缺则尝试拉一次（失败返回空 Series）。
    """
    df = load_hsi()
    if df.empty:
        try:
            logger.info("本地无 HSI 缓存，尝试从 yfinance 下载...")
            df = fetch_hsi_yfinance(start="2018-01-01")
            if not df.empty:
                save_hsi(df)
        except Exception as e:
            logger.warning(f"HSI 采集失败: {e}")
            return pd.Series(dtype=float)

    if df.empty:
        return pd.Series(dtype=float)

    df = df.sort_values("date")
    df["period"] = df["date"].dt.strftime("%Y-%m")
    # 取每月最后一条
    last = df.groupby("period").tail(1).set_index("period")["close"]
    rets = last.pct_change().dropna()
    if start_period:
        rets = rets[rets.index >= start_period]
    if end_period:
        rets = rets[rets.index <= end_period]
    return rets


def update_benchmark(start: str = "2018-01-01"):
    """主入口：拉 HSI 并保存到本地"""
    logger.info("===== 更新基准指数 HSI =====")
    df = fetch_hsi_yfinance(start=start)
    if df.empty:
        logger.error("HSI 采集为空，请检查 yfinance 网络")
        return
    save_hsi(df)
    logger.info(f"HSI 已更新：{df['date'].min()} ~ {df['date'].max()}, {len(df)} 行")
