"""
组合构建：依据某期 ranking CSV 输出 TOP N 持仓 / 五分位组合。
"""
import os
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd

from storage import store as _store


def _select_pool_for_signal_period(signal_period: str) -> pd.DataFrame:
    """
    根据信号期所在年份，加载对应年度的股票池快照。
    
    映射规则：signal_period='2024-06' → 用 pool_2023-12-31（上年末）
    
    若找不到对应年度快照：
      1. 尝试找 <= 当前年份的最近一个 snapshot
      2. 若仍然没有 snapshot，fallback 到当前 hk_stock_pool.csv
      3. 都没有则返回空 DataFrame（调用方决定 fallback 策略）
    """
    try:
        year = int(signal_period.split("-")[0])
    except Exception:
        return _store.load_stock_pool()
    target = datetime(year - 1, 12, 31)
    pool = _store.load_stock_pool_snapshot(target)
    if not pool.empty:
        return pool
    # 退化：用 ≤ target 的最近一个 snapshot
    target_str = target.strftime("%Y-%m-%d")
    avail = _store.list_pool_snapshots()
    older = [d for d in avail if d <= target_str]
    if older:
        return _store.load_stock_pool_snapshot(older[-1])
    # 最终 fallback：当前 pool
    return _store.load_stock_pool()


def _filter_by_pool(df: pd.DataFrame, signal_period: str) -> pd.DataFrame:
    """
    将 ranking df 过滤为「信号期所属年度池」内的股票。
    若池为空（没有任何 snapshot 且没有当前 pool），原样返回。
    """
    if df is None or df.empty:
        return df
    pool = _select_pool_for_signal_period(signal_period)
    if pool is None or pool.empty:
        return df  # 没池可过滤，原样返回（避免退化为空）
    pool_codes = set(pool["code"].astype(str).str.zfill(5).tolist())
    df = df.copy()
    df["code"] = df["code"].astype(str).str.zfill(5)
    return df[df["code"].isin(pool_codes)].reset_index(drop=True)


def load_ranking_period(period: str, tag: str = None) -> pd.DataFrame:
    """加载某期排名。tag=None 读旧口径 `hk_ranking_{period}.csv`；
    tag="ann_sus" 读 `hk_ranking_ann_sus_{period}.csv`（干净口径）。"""
    if tag:
        path = os.path.join(_store.CSV_DIR, f"hk_ranking_{tag}_{period}.csv")
    else:
        path = _store.RANKING_CSV.format(period=period)
    if not os.path.exists(path):
        return pd.DataFrame()
    df = pd.read_csv(path, dtype={"code": str})
    df["code"] = df["code"].astype(str).str.zfill(5)
    # 兼容旧文件 growth_factor.1 重复列
    dup_cols = [c for c in df.columns if c.endswith(".1")]
    df = df.drop(columns=dup_cols, errors="ignore")
    return df


def _assign_weights(df: pd.DataFrame, scheme: str) -> pd.DataFrame:
    """
    按方案分配权重，结果列 weight 之和等于 1.0。
    - equal: 1/N
    - score: max(0, composite_score - min) / sum
        若所有分数相同/全负 → 退化为 equal
    - rank:  (1 / rank) / sum(1/rank)，rank 从 1 开始
    """
    df = df.copy()
    n = len(df)
    if n == 0:
        df["weight"] = []
        return df

    if scheme == "equal":
        df["weight"] = 1.0 / n
        return df

    if scheme == "score":
        s = df["composite_score"].astype(float)
        s_min = s.min()
        shifted = (s - s_min).clip(lower=0)
        total = shifted.sum()
        if total <= 0:
            df["weight"] = 1.0 / n
        else:
            df["weight"] = shifted / total
        return df

    if scheme == "rank":
        # 用 rank 列（1=最强）；缺失则用 reset_index+1
        if "rank" in df.columns and df["rank"].notna().all():
            r = df["rank"].astype(float)
        else:
            r = pd.Series(np.arange(1, n + 1), index=df.index, dtype=float)
        inv = 1.0 / r
        df["weight"] = inv / inv.sum()
        return df

    raise ValueError(f"unknown weight scheme: {scheme}")


def build_top_n(period: str, top_n: int = 20, weight_scheme: str = "equal",
                min_composite_score: Optional[float] = None,
                tag: str = None) -> pd.DataFrame:
    """
    按 composite_score 降序取 TOP N（已在 ranking 中按 rank 排好）。

    Args:
        tag: 排名标签（如 "ann_sus"），默认 None 读旧口径。

    Returns:
        DataFrame[code, name, composite_score, rank, weight]
    """
    df = load_ranking_period(period, tag=tag)
    if df.empty:
        return pd.DataFrame(columns=["code", "name", "composite_score", "rank", "weight"])

    # 按 signal_period 所属年份的池过滤（消除幸存者偏差）
    df = _filter_by_pool(df, period)
    if df.empty:
        return pd.DataFrame(columns=["code", "name", "composite_score", "rank", "weight"])

    # 过滤极低分（可选）
    if min_composite_score is not None:
        df = df[df["composite_score"].fillna(-np.inf) >= min_composite_score]

    df = df.sort_values("rank").head(top_n).reset_index(drop=True)
    df = _assign_weights(df, weight_scheme)
    return df[["code", "name", "composite_score", "rank", "weight"]]


def build_quintiles(period: str, n_quantiles: int = 5,
                     weight_scheme: str = "equal",
                     tag: str = None) -> dict:
    """
    按 composite_score 五分位（默认）。返回 dict[label -> DataFrame]。
    Q1 = 最高分组，Q5 = 最低分组（与学界惯例对齐）。

    Args:
        tag: 排名标签（如 "ann_sus"），默认 None 读旧口径。
    """
    df = load_ranking_period(period, tag=tag)
    if df.empty or len(df) < n_quantiles:
        return {}

    # 按 signal_period 所属年份的池过滤（消除幸存者偏差）
    df = _filter_by_pool(df, period)
    if df.empty or len(df) < n_quantiles:
        return {}

    # 按 composite_score 降序，分位号 1=最高
    sorted_df = df.sort_values("composite_score", ascending=False).reset_index(drop=True)
    n = len(sorted_df)
    bin_size = n // n_quantiles
    groups: dict[str, pd.DataFrame] = {}
    for i in range(n_quantiles):
        start = i * bin_size
        end = (i + 1) * bin_size if i < n_quantiles - 1 else n
        chunk = sorted_df.iloc[start:end].copy().reset_index(drop=True)
        chunk = _assign_weights(chunk, weight_scheme)
        label = f"Q{i+1}"
        groups[label] = chunk[["code", "name", "composite_score", "rank", "weight"]]
    return groups
