"""
B20: Web 层动量因子计算（mom1/mom3/mom6/mom9）+ 横截面 zscore + 加权复合。

仅在 web 层使用，不落 ranking csv。
"""
import os
import pandas as pd
import numpy as np

_PRICE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "csv", "hk_monthly_price.csv",
)
_cache = {"mtime": 0, "df": None}


def _load_monthly_price() -> pd.DataFrame:
    """加载月度价格，带 mtime 缓存。返回排序后 [code, date, close]。

    B24：优先用 close_qfq（前复权）计动量；fallback 到 close 以兼容旧文件。
    """
    if not os.path.exists(_PRICE_FILE):
        return pd.DataFrame(columns=["code", "date", "close"])
    mtime = os.path.getmtime(_PRICE_FILE)
    if mtime == _cache["mtime"] and _cache["df"] is not None:
        return _cache["df"]
    # 先探查列
    head = pd.read_csv(_PRICE_FILE, nrows=0)
    if "close_qfq" in head.columns:
        df = pd.read_csv(_PRICE_FILE, dtype={"code": str}, usecols=["date", "code", "close_qfq"])
        df = df.rename(columns={"close_qfq": "close"})
    else:
        df = pd.read_csv(_PRICE_FILE, dtype={"code": str}, usecols=["date", "code", "close"])
    df["code"] = df["code"].str.zfill(5)
    df["date"] = pd.to_datetime(df["date"], format="mixed", errors="coerce")
    df = df.dropna(subset=["date", "close"]).sort_values(["code", "date"])
    _cache["mtime"] = mtime
    _cache["df"] = df
    return df


# 各动量的回看月数（latest 与 latest-N 比）
LOOKBACKS = {"mom1": 1, "mom3": 3, "mom6": 6, "mom9": 9}


def compute_momentum(codes: list) -> pd.DataFrame:
    """
    对给定股票列表，计算 mom1/mom3/mom6/mom9。

    公式：mom_N = close[-1] / close[-1-N] - 1   （用每只股票自身可用的最新月份）

    Returns:
        DataFrame: [code, mom1, mom3, mom6, mom9]，对应 code 缺数据时填 NaN
    """
    if not codes:
        return pd.DataFrame(columns=["code"] + list(LOOKBACKS.keys()))

    codes_set = {c.zfill(5) for c in codes}
    df = _load_monthly_price()
    if df.empty:
        return pd.DataFrame({"code": list(codes_set), **{k: np.nan for k in LOOKBACKS}})

    df = df[df["code"].isin(codes_set)]

    rows = []
    for code, g in df.groupby("code"):
        closes = g["close"].values
        n = len(closes)
        latest = closes[-1] if n >= 1 else np.nan
        out = {"code": code}
        for k, lb in LOOKBACKS.items():
            # 需要 n >= lb + 1 才能算 latest / close[-1-lb]
            if n >= lb + 1 and closes[-1 - lb] and closes[-1 - lb] > 0:
                out[k] = latest / closes[-1 - lb] - 1
            else:
                out[k] = np.nan
        rows.append(out)

    result = pd.DataFrame(rows)
    # 补齐没有任何价格记录的 code
    missing = codes_set - set(result["code"])
    if missing:
        miss_df = pd.DataFrame(
            [{"code": c, **{k: np.nan for k in LOOKBACKS}} for c in missing]
        )
        result = pd.concat([result, miss_df], ignore_index=True)
    return result


def _zscore(s: pd.Series) -> pd.Series:
    """横截面 zscore，NaN 保持 NaN。"""
    sn = pd.to_numeric(s, errors="coerce")
    mu = sn.mean(skipna=True)
    sd = sn.std(skipna=True, ddof=0)
    if not sd or pd.isna(sd):
        return pd.Series([np.nan] * len(sn), index=s.index)
    return (sn - mu) / sd


# 复合权重
WEIGHTS = {"mom1": 1.0, "mom3": 2.0, "mom6": 1.0, "mom9": 1.0}


def compute_momentum_zscores(df_momentum: pd.DataFrame) -> pd.DataFrame:
    """
    输入 [code, mom1, mom3, mom6, mom9]，加列 [z_mom1, z_mom3, z_mom6, z_mom9, mom_factor]。

    mom_factor = sum(w_k * z_k) / sum(w_k for k where z_k notna)
    （有效权重重归一；4 个全 NaN 才返回 NaN）
    """
    out = df_momentum.copy()
    for k in LOOKBACKS:
        out[f"z_{k}"] = _zscore(out[k])

    z_cols = [f"z_{k}" for k in LOOKBACKS]
    w = np.array([WEIGHTS[k] for k in LOOKBACKS])
    Z = out[z_cols].to_numpy(dtype=float)  # shape (n, 4)
    mask = ~np.isnan(Z)
    weighted = np.where(mask, Z * w, 0.0).sum(axis=1)
    w_sum = (mask * w).sum(axis=1)
    factor = np.where(w_sum > 0, weighted / w_sum, np.nan)
    out["mom_factor"] = factor
    return out


def apply_momentum_filter(df: pd.DataFrame, full_df: pd.DataFrame | None = None) -> pd.DataFrame:
    """
    应用三条动量过滤：
      - mom_factor > 2     → 过滤
      - z_mom6 < -3        → 过滤
      - z_mom1 > 2.0       → 过滤
    NaN 不触发过滤。

    B24-followup: zscore 基于全池（full_df）计算，避免过滤叠加后池缩小导致 z 偏移。
    若未传入 full_df，退化为原逻辑（以 df 为池）。

    Returns: 过滤后 df（保持原列，不暴露 mom_* 给上层）
    """
    if df.empty or "code" not in df.columns:
        return df
    # 从全池算 zscore
    base = full_df if (full_df is not None and not full_df.empty and "code" in full_df.columns) else df
    base_codes = base["code"].astype(str).str.zfill(5).tolist()
    mom_all = compute_momentum(base_codes)
    mom_all = compute_momentum_zscores(mom_all)

    work = df.copy()
    work["_code5"] = work["code"].astype(str).str.zfill(5)
    merged = work.merge(
        mom_all[["code", "z_mom1", "z_mom6", "mom_factor"]].rename(columns={"code": "_code5"}),
        on="_code5", how="left",
    )

    mom_factor = pd.to_numeric(merged["mom_factor"], errors="coerce")
    z6 = pd.to_numeric(merged["z_mom6"], errors="coerce")
    z1 = pd.to_numeric(merged["z_mom1"], errors="coerce")
    # NaN 不触发过滤
    drop_factor = mom_factor > 2
    drop_z6 = z6 < -3
    drop_z1 = z1 > 2.0
    keep = ~(drop_factor.fillna(False) | drop_z6.fillna(False) | drop_z1.fillna(False))
    return merged[keep].drop(columns=["_code5", "z_mom1", "z_mom6", "mom_factor"])
