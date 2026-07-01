"""
Web 展示服务（Flask）
访问 http://localhost:5000 查看排名结果
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, render_template, request, send_file, jsonify
import pandas as pd
from typing import Optional

from storage.store import load_ranking, list_ranking_periods, EXCEL_DIR, RANKING_EXCEL
from engine.ranker import rename_for_display
from config.settings import WEB_PORT, WEB_DEBUG

app = Flask(__name__, template_folder="templates")

# ---- N 个月最低价缓存（按文件 mtime + months 自动失效） ----
_PRICE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "csv", "hk_monthly_price.csv")
_low_cache: dict = {}  # key=months -> {"mtime": float, "map": dict}
_latest_qfq_cache: dict = {"mtime": 0, "map": None}


def _load_low_map(months: int) -> dict:
    """按 code -> 近 N 个月最低【前复权】收盘价。带文件 mtime 缓存。
    B24: 价格比较统一用 close_qfq（跨除权点连续）。无 close_qfq 列时回退到 close。
    """
    if not os.path.exists(_PRICE_FILE):
        return {}
    mtime = os.path.getmtime(_PRICE_FILE)
    cached = _low_cache.get(months)
    if cached and cached["mtime"] == mtime and cached["map"]:
        return cached["map"]
    try:
        head = pd.read_csv(_PRICE_FILE, nrows=0)
        price_col = "close_qfq" if "close_qfq" in head.columns else "close"
        mp = pd.read_csv(_PRICE_FILE, dtype={"code": str}, usecols=["date", "code", price_col])
        mp["code"] = mp["code"].str.zfill(5)
        mp["date"] = pd.to_datetime(mp["date"], format="mixed", errors="coerce")
        mp = mp.dropna(subset=["date", price_col])
        mp = mp.sort_values(["code", "date"])
        mp = mp.groupby("code").tail(months)
        m = mp.groupby("code")[price_col].min().to_dict()
        _low_cache[months] = {"mtime": mtime, "map": m}
        return m
    except Exception:
        return {}


def _load_latest_qfq_map() -> dict:
    """code -> 最新月 close_qfq。用作 qfq 空间内的"当前价"，与 _load_low_map 同口径。"""
    if not os.path.exists(_PRICE_FILE):
        return {}
    mtime = os.path.getmtime(_PRICE_FILE)
    if _latest_qfq_cache["mtime"] == mtime and _latest_qfq_cache["map"] is not None:
        return _latest_qfq_cache["map"]
    try:
        head = pd.read_csv(_PRICE_FILE, nrows=0)
        price_col = "close_qfq" if "close_qfq" in head.columns else "close"
        mp = pd.read_csv(_PRICE_FILE, dtype={"code": str}, usecols=["date", "code", price_col])
        mp["code"] = mp["code"].str.zfill(5)
        mp["date"] = pd.to_datetime(mp["date"], format="mixed", errors="coerce")
        mp = mp.dropna(subset=["date", price_col])
        mp = mp.sort_values(["code", "date"])
        latest = mp.groupby("code").tail(1).set_index("code")[price_col].to_dict()
        _latest_qfq_cache["mtime"] = mtime
        _latest_qfq_cache["map"] = latest
        return latest
    except Exception:
        return {}


def _load_3y_low_map() -> dict:
    return _load_low_map(36)


# ---- 近 1 年换手率缓存（mtime + shares_outstanding mtime）----
_FIN_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "csv", "hk_financial.csv")
_turnover_cache: dict = {}

# B25: 现金流因子 — Cash Conversion / Dividend Cash Coverage
# 金融行业豁免：OCF 含存贷款变动，CC/DCC 无意义
_FIN_INDUSTRY_EXEMPT = {"银行", "保险", "其他金融", "投资公司"}
_cf_cache: dict = {"fmtime": 0, "df": None}


def _load_cashflow_fields() -> pd.DataFrame:
    """读 hk_financial.csv 取 code -> 3 期 TTM OCF + OpInc。
    B26: 升级为 3 期（t / t-1 / t-2），用于 3 年平均口径。
    带 mtime 缓存。
    """
    if not os.path.exists(_FIN_FILE):
        return pd.DataFrame(columns=["code"])
    fmtime = os.path.getmtime(_FIN_FILE)
    if _cf_cache["df"] is not None and _cf_cache["fmtime"] == fmtime:
        return _cf_cache["df"]
    try:
        # B29: cash_and_equivalents / interest_bearing_debt 列可能尚未回填，按文件实际列动态加载
        head = pd.read_csv(_FIN_FILE, nrows=0)
        wanted = [
            "code",
            "ttm_operating_cashflow", "prev_ttm_operating_cashflow", "ttm_operating_cashflow_t2",
            "ttm_operating_income", "prev_ttm_operating_income", "ttm_operating_income_t2",
            "two_years_ago_div_per_share",
            "cash_and_equivalents", "interest_bearing_debt",
            # B30: 用 financial.csv 的 fy_div / prev_year_div 覆盖 ranking.csv 的旧值
            # （ranking.csv 是 ranker 跑批的快照，不会自动跟随 financial.csv 修复）
            "fy_div_per_share", "prev_year_div_per_share",
            # B31: 剩余可用现金流偿债覆盖率所需的 FCF 组件
            "ocf_3y_avg", "capex_3y_avg", "dep_amortization_3y_avg",
            "current_lease_obligation_prev", "equity_latest",
            # B32: TTM 字段（突降监测）
            "capex_ttm", "da_ttm", "current_lease_obligation_ttm",
        ]
        usecols = [c for c in wanted if c in head.columns]
        df = pd.read_csv(_FIN_FILE, dtype={"code": str}, usecols=usecols)
        df["code"] = df["code"].str.zfill(5)
        _cf_cache["fmtime"] = fmtime
        _cf_cache["df"] = df
        return df
    except Exception:
        return pd.DataFrame(columns=["code"])


def _attach_cashflow_factors(df: pd.DataFrame) -> pd.DataFrame:
    """B25 + B26: 给 ranking df 附加 cash_conversion 和 dividend_coverage。
    口径：3 年平均（OCF/OpInc 用 3 期 nanmean；分红用当期 + 上年 + 两年前的 nanmean）
    降级：3 期中至少 2 期有数据才用 3y avg；否则降级到单年 TTM；都没有 → NaN
    """
    if df.empty or "code" not in df.columns:
        return df
    cf = _load_cashflow_fields()
    if cf.empty:
        df = df.copy()
        df["cash_conversion"] = pd.NA
        df["dividend_coverage"] = pd.NA
        return df
    out = df.copy()
    out["code"] = out["code"].astype(str).str.zfill(5)
    # B30: 用 financial.csv 的最新值覆盖 ranking.csv 的旧值（ranker 跑批快照可能过时）
    # B32: cash_and_equivalents / interest_bearing_debt 也用 financial.csv 的最新值
    overwrite_cols = [
        c for c in (
            "fy_div_per_share", "prev_year_div_per_share",
            "cash_and_equivalents", "interest_bearing_debt",
        )
        if c in cf.columns and c in out.columns
    ]
    if overwrite_cols:
        out = out.drop(columns=overwrite_cols)
    out = out.merge(cf, on="code", how="left")

    import numpy as np

    def _avg_with_fallback(s_t, s_t1, s_t2):
        """3 期 nanmean，至少 2 期有效；否则取 t；都无 → NaN"""
        arr = np.column_stack([
            pd.to_numeric(s_t, errors="coerce"),
            pd.to_numeric(s_t1, errors="coerce"),
            pd.to_numeric(s_t2, errors="coerce"),
        ])
        valid_count = np.sum(~np.isnan(arr), axis=1)
        avg = np.where(
            valid_count >= 2,
            np.nanmean(arr, axis=1),
            np.where(valid_count == 1, np.nan_to_num(arr[:, 0], nan=np.nan), np.nan),
        )
        # valid_count==1 但 t 不一定有，做更稳妥处理：用 nanmean
        avg = np.nanmean(arr, axis=1)
        avg[valid_count == 0] = np.nan
        return pd.Series(avg, index=s_t.index)

    ocf_avg = _avg_with_fallback(
        out["ttm_operating_cashflow"],
        out["prev_ttm_operating_cashflow"],
        out["ttm_operating_cashflow_t2"],
    )
    opinc_avg = _avg_with_fallback(
        out["ttm_operating_income"],
        out["prev_ttm_operating_income"],
        out["ttm_operating_income_t2"],
    )
    # CC = avg(OCF) / avg(OpInc)，OpInc<=0 → NaN
    out["cash_conversion"] = ocf_avg.where(opinc_avg > 0) / opinc_avg.where(opinc_avg > 0)

    # DCC: 分母用 3y avg 分红 × 当期 shares
    # B32: 分子从 3y OCF 替换为 real_fcf（更严格，标普 FCF/Div 口径）
    # 由于 real_fcf 在 B31 块算出，这里先取 div_avg；DCC 计算延后到 B31 块之后
    fy_div_t = pd.to_numeric(out.get("fy_div_per_share"), errors="coerce").fillna(0)
    fy_div_t1 = pd.to_numeric(out.get("prev_year_div_per_share"), errors="coerce").fillna(0)
    # two_years_ago_div_per_share 来自 hk_financial.csv（merge 进来）；缺失时退化为 0
    if "two_years_ago_div_per_share" in out.columns:
        fy_div_t2 = pd.to_numeric(out["two_years_ago_div_per_share"], errors="coerce").fillna(0)
    else:
        fy_div_t2 = pd.Series(0.0, index=out.index)
    div_avg = (fy_div_t + fy_div_t1 + fy_div_t2) / 3.0

    shs = pd.to_numeric(out.get("shares_outstanding"), errors="coerce").fillna(0)
    cash_div_avg = div_avg * shs
    # 暂占位，待 B31 块后用 real_fcf 重算
    out["_cash_div_avg"] = cash_div_avg

    # 保留 3y avg OCF/股 给 §11 sustainable_return_rate 使用
    out["_ocf_per_share_3y_avg"] = ocf_avg / shs.where(shs > 0)

    # sustainable_return_rate 计算延后到 real_fcf 算完之后

    # B31 + B32: 剩余可用现金流偿债覆盖率（含 TTM 突降监测）
    from engine.real_fcf import evaluate as _fcf_evaluate
    ocf_3y = pd.to_numeric(out.get("ocf_3y_avg"), errors="coerce")
    # B32: 注意改用 capex_ttm 替代 capex_3y_avg（更敏感）
    capex_used = pd.to_numeric(out.get("capex_ttm"), errors="coerce")
    # 缺失时 fallback 到 3y avg
    capex_3y_fallback = pd.to_numeric(out.get("capex_3y_avg"), errors="coerce")
    capex_used = capex_used.fillna(capex_3y_fallback)
    dep_3y = pd.to_numeric(out.get("dep_amortization_3y_avg"), errors="coerce")
    lease_p = pd.to_numeric(out.get("current_lease_obligation_prev"), errors="coerce")
    eq_lt = pd.to_numeric(out.get("equity_latest"), errors="coerce")
    ibd_s = pd.to_numeric(out.get("interest_bearing_debt"), errors="coerce")
    cash_s = pd.to_numeric(out.get("cash_and_equivalents"), errors="coerce")
    # TTM 突降字段
    ocf_ttm_s = pd.to_numeric(out.get("ttm_operating_cashflow"), errors="coerce")
    capex_ttm_s = pd.to_numeric(out.get("capex_ttm"), errors="coerce")
    da_ttm_s = pd.to_numeric(out.get("da_ttm"), errors="coerce")
    lease_ttm_s = pd.to_numeric(out.get("current_lease_obligation_ttm"), errors="coerce")

    # B31: 公司级 cash_fortress 标记（跨过滤器超级豁免）
    # B32-UI v2: 简化为净现金缓冲 (cash − ibd) > 市值，不再扣 3 年分红
    cp = pd.to_numeric(out.get("current_price"), errors="coerce")
    shs = pd.to_numeric(out.get("shares_outstanding"), errors="coerce")
    mcap = cp * shs
    net_cash = cash_s.fillna(0) - ibd_s.fillna(0)
    out["_cash_fortress"] = (
        cash_s.notna() & ibd_s.notna() & mcap.notna() & (mcap > 0)
        & (net_cash > mcap)
    ).fillna(False)

    real_fcf_vals = []
    cov_vals = []
    rating_vals = []
    real_fcf_ttm_vals = []
    drop_ratio_vals = []
    cf_flag = out["_cash_fortress"].tolist()
    for i, idx in enumerate(out.index):
        def _v(s):
            v = s.loc[idx] if idx in s.index else None
            return v if (v is not None and pd.notna(v)) else None
        r = _fcf_evaluate(
            ocf_3y=_v(ocf_3y),
            capex_3y=_v(capex_used),       # B32: 实际传 capex_ttm（fallback 3y_avg）
            dep_3y=_v(dep_3y),
            lease_principal=_v(lease_p),
            ibd=_v(ibd_s),
            cash=_v(cash_s),
            equity=_v(eq_lt),
            cash_fortress=bool(cf_flag[i]),
            ocf_ttm=_v(ocf_ttm_s),
            capex_ttm=_v(capex_ttm_s),
            da_ttm=_v(da_ttm_s),
            lease_ttm=_v(lease_ttm_s),
        )
        real_fcf_vals.append(r["real_fcf"])
        cov_vals.append(r["coverage_ratio"])
        rating_vals.append(r["fcf_rating"])
        real_fcf_ttm_vals.append(r["real_fcf_ttm"])
        drop_ratio_vals.append(r["drop_ratio"])
    out["real_fcf"] = real_fcf_vals
    out["coverage_ratio"] = cov_vals
    out["fcf_rating"] = rating_vals
    out["real_fcf_ttm"] = real_fcf_ttm_vals
    out["drop_ratio"] = drop_ratio_vals

    # B27 → B32-UI: sustainable_return_rate = (capped_fy_div + real_buyback) / current_price - dilution
    # 封顶：fy_div_per_share 不得超过 (3y real_fcf + 净现金 × 10%) / 股本
    #   含义：3 年真 FCF 是可持续来源；净现金按 10% 折现/年化补贴 = 用现金垫付 10 年的容忍度
    # 负值 → 0（直接砍光分红）；金融行业 / 3y OCF 缺失 → 回退到 return_rate
    ind = out.get("industry").astype(str) if "industry" in out.columns else pd.Series("", index=out.index)
    fin_exempt = ind.isin(_FIN_INDUSTRY_EXEMPT)
    rfcf_abs = pd.to_numeric(out["real_fcf"], errors="coerce")          # 绝对值（3y 平均）
    net_cash_abs = cash_s.fillna(0) - ibd_s.fillna(0)                    # 绝对值
    # 净现金 > 0 才生效（负净现金不再扣 cap，避免 real_fcf 与净负债双重惩罚）
    net_cash_pos = net_cash_abs.clip(lower=0)
    cap_total = rfcf_abs + net_cash_pos * 0.1                            # 可分配总额（绝对）
    cap_per_share = cap_total / shs.where(shs > 0)
    cap = cap_per_share.clip(lower=0)                                    # 负 → 0
    fy_div_ps = pd.to_numeric(out.get("fy_div_per_share"), errors="coerce").fillna(0)
    capped_fy_div = pd.concat([fy_div_ps, cap], axis=1).min(axis=1)
    rb = pd.to_numeric(out.get("real_buyback_per_share"), errors="coerce").fillna(0)
    cp_for_sust = pd.to_numeric(out.get("current_price"), errors="coerce")
    dil = pd.to_numeric(out.get("annual_dilution_rate"), errors="coerce").fillna(0)
    sust = (capped_fy_div + rb) / cp_for_sust.where(cp_for_sust > 0) * 100 - dil * 100
    # 豁免回退到 return_rate
    ret = pd.to_numeric(out.get("return_rate"), errors="coerce")
    sust = sust.where(rfcf_abs.notna() & ~fin_exempt, ret)
    out["sustainable_return_rate"] = sust.round(4)

    # B28: 让 sustainable_return_rate 紧跟 return_rate 显示
    if "return_rate" in out.columns:
        cols = list(out.columns)
        if "sustainable_return_rate" in cols:
            cols.remove("sustainable_return_rate")
            idx = cols.index("return_rate") + 1
            cols.insert(idx, "sustainable_return_rate")
            out = out[cols]

    # B32: DCC 用 real_fcf 替代 OCF（标普 FCF/Div 口径，更严格）
    real_fcf_series = pd.to_numeric(out["real_fcf"], errors="coerce")
    cash_div_avg = out["_cash_div_avg"]
    out["dividend_coverage"] = real_fcf_series.where(cash_div_avg > 0) / cash_div_avg.where(cash_div_avg > 0)
    out.drop(columns=["_cash_div_avg"], inplace=True, errors="ignore")
    # 单位：亿（用于展示）
    out["real_fcf_yi"] = pd.to_numeric(out["real_fcf"], errors="coerce") / 1e8

    # B32-UI v6: 综合得分三因子全部统一口径
    #   1) sustainable_return_zscore: 用 sustainable_return_rate（已含 real_fcf cap）替代 return_rate
    #   2) cash_buffer_zscore_v2: (cash − ibd) / mcap，去掉 3×div 扣减，与 cash_fortress 口径统一
    #   3) fcf_yield_zscore: real_fcf / mcap（FCF yield），替代原 fcf_div_zscore
    rfcf_for_z = pd.to_numeric(out["real_fcf"], errors="coerce")
    cp_z = pd.to_numeric(out.get("current_price"), errors="coerce")
    shs_z = pd.to_numeric(out.get("shares_outstanding"), errors="coerce").fillna(0)
    mcap_z = cp_z * shs_z

    # 计算 winsorized zscore
    def _wz(series):
        s = pd.to_numeric(series, errors="coerce")
        valid = s.dropna()
        if len(valid) < 10:
            return pd.Series([float("nan")] * len(s), index=s.index)
        lo, hi = valid.quantile(0.01), valid.quantile(0.99)
        sw = s.clip(lower=lo, upper=hi)
        mu = sw.mean(skipna=True)
        sd = sw.std(skipna=True, ddof=0)
        if not sd or sd < 1e-9:
            return pd.Series([float("nan")] * len(s), index=s.index)
        return ((sw - mu) / sd).round(4)

    # ---- (1) sustainable_return_zscore ----
    sust_rate = pd.to_numeric(out.get("sustainable_return_rate"), errors="coerce")
    sust_z = _wz(sust_rate)
    out["sustainable_return_zscore"] = sust_z

    # ---- (2) cash_buffer_zscore_v2 (无 3×div) ----
    cash_z = pd.to_numeric(out.get("cash_and_equivalents"), errors="coerce")
    ibd_z = pd.to_numeric(out.get("interest_bearing_debt"), errors="coerce")
    net_cash_z = cash_z.fillna(0) - ibd_z.fillna(0)
    cb_ratio_v2 = net_cash_z / mcap_z.where(mcap_z > 0)
    out["cash_buffer_ratio_v2"] = cb_ratio_v2.round(4)
    cbz_v2 = _wz(cb_ratio_v2)
    out["cash_buffer_zscore_v2"] = cbz_v2.fillna(0.0)

    # ---- (3) fcf_yield_zscore = real_fcf / mcap ----
    fcf_yield = rfcf_for_z / mcap_z.where(mcap_z > 0)
    out["fcf_yield"] = fcf_yield.round(4)
    fy_z = _wz(fcf_yield)
    # 现金堡垒豁免：给 z = 当前最大值（不被低 FCF 拖累）
    if "_cash_fortress" in out.columns:
        cf_mask = out["_cash_fortress"].fillna(False).astype(bool)
        max_z = fy_z.max(skipna=True)
        if pd.notna(max_z):
            fy_z = fy_z.where(~cf_mask, max_z)
    out["fcf_yield_zscore"] = fy_z.fillna(0.0)

    # 重算 composite_score
    # B36: 合并现金堡垒因子 = 0.5 × cash_buffer_v2 + 0.5 × fcf_yield（两项均以 mcap 为分母，同信号融合）
    #   sust_z × 1.0 + growth × 0.2 + cash_fortress × 0.2
    #   注：growth_factor 已在 ranker 内含 FCF/净资产、现金流动量两个子因子
    srz = pd.to_numeric(sust_z, errors="coerce").fillna(0.0)
    gz = pd.to_numeric(out.get("growth_factor"), errors="coerce").fillna(0.0)
    cbz2 = pd.to_numeric(out["cash_buffer_zscore_v2"], errors="coerce").fillna(0.0)
    fyz = pd.to_numeric(out["fcf_yield_zscore"], errors="coerce").fillna(0.0)
    cash_fortress_factor = (0.5 * cbz2 + 0.5 * fyz).round(4)
    out["cash_fortress_factor"] = cash_fortress_factor
    out["composite_score"] = (srz * 1.0 + gz * 0.2 + cash_fortress_factor * 0.2).round(4)


    # B32-UI v9: 保留 OCF_3y_avg 是否为正的布尔标记（drop 后 filter 仍可用）
    if "ocf_3y_avg" in out.columns:
        out["_ocf_3y_pos"] = pd.to_numeric(out["ocf_3y_avg"], errors="coerce").fillna(0) > 0
    else:
        out["_ocf_3y_pos"] = False
    # B32-UI v9: 保留 two_years_ago_div_per_share 数值（drop 后 fdcc 仍可用）
    if "two_years_ago_div_per_share" in out.columns:
        out["_two_years_ago_div_per_share"] = pd.to_numeric(out["two_years_ago_div_per_share"], errors="coerce")
    else:
        out["_two_years_ago_div_per_share"] = 0.0

    # 清理 merge 残留 + 内部辅助列
    out = out.drop(columns=[
        "ttm_operating_cashflow", "prev_ttm_operating_cashflow", "ttm_operating_cashflow_t2",
        "ttm_operating_income", "prev_ttm_operating_income", "ttm_operating_income_t2",
        "two_years_ago_div_per_share",
        "_ocf_per_share_3y_avg",
        # B31: 计算用的原始组件不展示
        "ocf_3y_avg", "capex_3y_avg", "dep_amortization_3y_avg",
        "current_lease_obligation_prev", "equity_latest",
        # B32: TTM 原始组件不展示
        "capex_ttm", "da_ttm", "current_lease_obligation_ttm",
    ], errors="ignore")
    return out



def _load_turnover_12m_map() -> dict:
    """返回 {code: turnover_12m} = 近 365 天 volume 合计 / shares_outstanding。
    缓存依赖价格表与财务表的 mtime。空值/0 返回不入表。
    """
    if not os.path.exists(_PRICE_FILE) or not os.path.exists(_FIN_FILE):
        return {}
    pmtime = os.path.getmtime(_PRICE_FILE)
    fmtime = os.path.getmtime(_FIN_FILE)
    cached = _turnover_cache.get("12m")
    if cached and cached["pmtime"] == pmtime and cached["fmtime"] == fmtime and cached["map"]:
        return cached["map"]
    try:
        mp = pd.read_csv(_PRICE_FILE, dtype={"code": str}, usecols=["date", "code", "volume"])
        mp["code"] = mp["code"].str.zfill(5)
        mp["date"] = pd.to_datetime(mp["date"], format="mixed", errors="coerce")
        mp = mp.dropna(subset=["date", "volume"])
        cutoff = mp["date"].max() - pd.Timedelta(days=365)
        mp = mp[mp["date"] >= cutoff]
        vol_sum = mp.groupby("code")["volume"].sum()

        fin = pd.read_csv(_FIN_FILE, dtype={"code": str}, usecols=["code", "shares_outstanding"])
        fin["code"] = fin["code"].str.zfill(5)
        fin = fin.dropna(subset=["shares_outstanding"])
        fin = fin[fin["shares_outstanding"] > 0]
        so = fin.set_index("code")["shares_outstanding"]

        df = pd.concat([vol_sum, so], axis=1, join="inner")
        df.columns = ["vol", "so"]
        turnover = (df["vol"] / df["so"]).to_dict()
        _turnover_cache["12m"] = {"pmtime": pmtime, "fmtime": fmtime, "map": turnover}
        return turnover
    except Exception:
        return {}


def _apply_filters(df: pd.DataFrame, filter_div_growth: bool, filter_return_growth: bool, filter_real_buyback: bool = False, filter_revenue: bool = False, filter_price_3y_low_3x: bool = False, filter_price_6m_low_18x: bool = False, filter_price_2m_low_13x: bool = False, filter_ocf: bool = False, filter_momentum: bool = False, filter_net_debt_hard: bool = False, filter_net_debt_z: bool = False, filter_growth_factor: bool = False, filter_roe_z: bool = False, filter_turnover_z: bool = False, filter_cash_conversion: bool = False, filter_dividend_coverage: bool = False, filter_real_fcf_coverage: bool = False, cash_fortress_exempt: bool = True, fcf_rating_keep: Optional[list] = None) -> pd.DataFrame:
    """根据筛选项过滤 DataFrame。
    - filter_div_growth: 剔除 TTM分红 < 上年TTM分红 且 最近期分红 < 同期上年分红
      （即 TTM 与最近期"两项都下降"才剔除；任一项不降则保留）
    - filter_return_growth: 剔除 TTM回报 < 上年TTM回报
    - filter_real_buyback: 仅保留 股本净变化 >= 0 （真实回购，排除被 SBC 稀释）
    - filter_revenue: 仅保留 TTM 营收同比 >= -10%（下降不超过 10%）
    - filter_price_3y_low_3x: 仅保留 当前股价 ≤ 3 年最低价 × 3（剔除追高）
    - filter_price_6m_low_18x: 仅保留 当前股价 ≤ 6 个月最低价 × 1.6
    - filter_price_2m_low_13x: 仅保留 当前股价 ≤ 2 个月最低价 × 1.25
    - filter_ocf: 仅保留 经营现金流 TTM 同比 >= -50%（减少不超过 50%）；空值被剔除
    - filter_momentum: 动量过滤。3 条任一命中即剔除：
        mom_factor > 2（加权复合过热） | z_mom6 < -3（6月深跌）| z_mom1 > 2.0（1月过热）
    - filter_net_debt_hard: 仅保留 debt_to_equity （有息负债/净资产）≤ 70%（硬阈值，NaN 保留）
    - filter_net_debt_z: 剔除 有息负债/净资产 (debt_to_equity) 横截面 zscore > 1.2（杠杆显著高于均值 1.2σ）
    - filter_growth_factor: 剔除 growth_factor < -0.35（成长因子加权 zscore 复合得分过低）
    - filter_roe_z: 剔除 roe 横截面 zscore < -1.8（ROE 显著低于均值 1.8σ）；NaN 保留
    - filter_turnover_z: 剔除 近 1 年换手率横截面 zscore > 3.6（过热投机盘）；NaN 保留
    """
    if df.empty:
        return df
    out = df.copy()
    # B31: 公司级 cash_fortress 已由 _attach_cashflow_factors 写入 _cash_fortress 列；
    # 这里取一次给后续 debt 过滤器共用做超级豁免
    cash_fortress_col = out.get("_cash_fortress")
    if cash_fortress_col is None:
        cash_fortress_col = pd.Series(False, index=out.index)
    cash_fortress_col = cash_fortress_col.fillna(False).astype(bool)
    # B32-UI: 用户可关闭 cash_fortress 豁免
    if not cash_fortress_exempt:
        cash_fortress_col = pd.Series(False, index=out.index)
    # B32-UI v8: 净现金为负 (cash − ibd < 0) 时启用严阈值；与 cash_fortress 互斥
    cb_v2 = pd.to_numeric(out.get("cash_buffer_ratio_v2"), errors="coerce")
    if cb_v2 is None:
        net_cash_neg_col = pd.Series(False, index=out.index)
    else:
        net_cash_neg_col = (cb_v2 < 0).fillna(False) & ~cash_fortress_col
    if filter_div_growth:
        a = out.get("fy_div_per_share")
        b = out.get("prev_year_div_per_share")
        c = out.get("latest_period_div_per_share")
        d = out.get("latest_period_yoy_div_per_share")
        # OR 逻辑：只要 TTM 或 最近期 任一不下降就保留；只有两项都下降才剔除
        ttm_ok = (a.fillna(0) >= b.fillna(0)) if (a is not None and b is not None) else None
        latest_ok = (c.fillna(0) >= d.fillna(0)) if (c is not None and d is not None) else None
        # B32-UI: 高股息率救生 - (2*fy_div - prev_year_div) / price >= 15% 仍保留
        # 公式含义：用"今年隐含分红 - 上年分红"近似前瞻派息能力，避免单期高基数误救
        cp = pd.to_numeric(out.get("current_price"), errors="coerce")
        div_yield_ok = None
        if a is not None and b is not None and cp is not None:
            an = pd.to_numeric(a, errors="coerce")
            bn = pd.to_numeric(b, errors="coerce")
            div_yield_ok = ((2 * an - bn) / cp.where(cp > 0)) >= 0.15
            div_yield_ok = div_yield_ok.fillna(False)
        if ttm_ok is not None and latest_ok is not None:
            keep = ttm_ok | latest_ok
            if div_yield_ok is not None:
                keep = keep | div_yield_ok
            out = out[keep]
        elif ttm_ok is not None:
            keep = ttm_ok | (div_yield_ok if div_yield_ok is not None else False)
            out = out[keep]
        elif latest_ok is not None:
            keep = latest_ok | (div_yield_ok if div_yield_ok is not None else False)
            out = out[keep]
    if filter_return_growth:
        e = out.get("ttm_return_rate")
        f = out.get("prev_ttm_return_rate")
        if e is not None and f is not None:
            keep = e.fillna(0) >= f.fillna(0)
            # B32-UI: 高回报率救生 - 最新回报/市值 >= 15% 仍保留 (return_rate 已是 % 单位)
            rr = pd.to_numeric(out.get("return_rate"), errors="coerce")
            if rr is not None:
                keep = keep | (rr >= 15).fillna(False)
            out = out[keep]
    if filter_real_buyback:
        # 只保留股本净变化 >= 0（未被稀释）的股票
        nsc = out.get("net_share_change_rate")
        if nsc is not None:
            out = out[nsc.fillna(0) >= 0]
    if filter_revenue:
        # TTM 营收同比 >= -0.10；现金堡垒豁免放宽到 -0.30；净现金为负收紧到 0
        ry = out.get("revenue_yoy")
        if ry is not None:
            ryn = pd.to_numeric(ry, errors="coerce")
            cf_col = cash_fortress_col.reindex(out.index).fillna(False)
            nn_col = net_cash_neg_col.reindex(out.index).fillna(False)
            threshold = pd.Series(-0.10, index=out.index)
            threshold[cf_col] = -0.30
            threshold[nn_col] = 0.0  # 净现金为负：营收不能下降
            out = out[ryn >= threshold]
    if filter_ocf:
        # 经营现金流 TTM 同比 >= -0.50；现金堡垒豁免放宽到 -0.80；净现金为负收紧到 -0.20；空值剔除
        cf = out.get("operating_cashflow_yoy")
        if cf is not None:
            cfn = pd.to_numeric(cf, errors="coerce")
            cf_col = cash_fortress_col.reindex(out.index).fillna(False)
            nn_col = net_cash_neg_col.reindex(out.index).fillna(False)
            threshold = pd.Series(-0.50, index=out.index)
            threshold[cf_col] = -0.80
            threshold[nn_col] = -0.20
            out = out[cfn.notna() & (cfn >= threshold)]
    if filter_net_debt_hard:
        # B24: 改用 debt_to_equity（有息负债/净资产）≤ 0.70；NaN 保留
        # B31: 现金堡垒豁免
        de = pd.to_numeric(out.get("debt_to_equity"), errors="coerce")
        if de is not None:
            drop_mask = (~cash_fortress_col.reindex(out.index).fillna(False)) & (de > 0.70).fillna(False)
            out = out[~drop_mask]
    if filter_net_debt_z:
        # B24: 改用 debt_to_equity（有息负债/净资产）横截面 zscore，剔除 z > 1.2；NaN 保留
        # B24-followup: mu/sd 基于全池 df计算（避免叠加池变小后标准偏差缩窄导致过滤过极）
        de_all = pd.to_numeric(df.get("debt_to_equity"), errors="coerce")
        de_out = pd.to_numeric(out.get("debt_to_equity"), errors="coerce")
        if de_all is not None and de_out is not None:
            mu = de_all.mean(skipna=True)
            sd = de_all.std(skipna=True, ddof=0)
            if sd and not pd.isna(sd):
                z = (de_out - mu) / sd
                out = out[~(z > 1.2).fillna(False)]
    if filter_growth_factor:
        # 剔除 growth_factor < -0.35；现金堡垒放宽到 -1.0；净现金为负收紧到 -0.10
        gf = pd.to_numeric(out.get("growth_factor"), errors="coerce")
        if gf is not None:
            cf_col = cash_fortress_col.reindex(out.index).fillna(False)
            nn_col = net_cash_neg_col.reindex(out.index).fillna(False)
            threshold = pd.Series(-0.35, index=out.index)
            threshold[cf_col] = -1.0
            threshold[nn_col] = -0.10
            out = out[~(gf < threshold).fillna(False)]
    if filter_roe_z:
        # B22: 横截面 zscore，剔除 z < -1.8；净现金为负收紧到 z < -1.0；NaN 保留
        # B24-followup: mu/sd 基于全池 df 计算
        roe_all = pd.to_numeric(df.get("roe"), errors="coerce")
        roe_out = pd.to_numeric(out.get("roe"), errors="coerce")
        if roe_all is not None and roe_out is not None:
            mu = roe_all.mean(skipna=True)
            sd = roe_all.std(skipna=True, ddof=0)
            if sd and not pd.isna(sd):
                z = (roe_out - mu) / sd
                nn_col = net_cash_neg_col.reindex(out.index).fillna(False)
                threshold = pd.Series(-1.8, index=out.index)
                threshold[nn_col] = -1.0
                out = out[~(z < threshold).fillna(False)]
    if filter_turnover_z:
        # B23: 近 1 年换手率 = volume_365d_sum / shares_outstanding
        # 横截面 zscore，剔除 z > 3.6（过热投机盘）；NaN 保留
        # B24-followup: mu/sd 基于全池 df 计算
        tmap = _load_turnover_12m_map()
        if "code" in out.columns and "code" in df.columns and tmap:
            codes_all = df["code"].astype(str).str.zfill(5)
            t_all = pd.to_numeric(codes_all.map(tmap), errors="coerce")
            codes_out = out["code"].astype(str).str.zfill(5)
            t_out = pd.to_numeric(codes_out.map(tmap), errors="coerce")
            mu = t_all.mean(skipna=True)
            sd = t_all.std(skipna=True, ddof=0)
            if sd and not pd.isna(sd):
                z = (t_out - mu) / sd
                out = out[~(z > 3.6).fillna(False)]
    if filter_cash_conversion:
        # B25: Cash Conversion = OCF / OpInc ≥ 0.5
        # 金融行业（OCF 含存贷款变动）豁免：industry 在 _FIN_INDUSTRY_EXEMPT 中视为 NaN 保留
        # B32-UI v9: 现金堡垒豁免需同时 OCF_3y_avg > 0
        cc = pd.to_numeric(out.get("cash_conversion"), errors="coerce")
        if cc is not None:
            ind = out.get("industry").astype(str) if "industry" in out.columns else None
            exempt = ind.isin(_FIN_INDUSTRY_EXEMPT) if ind is not None else pd.Series(False, index=out.index)
            cash_fortress = cash_fortress_col.reindex(out.index).fillna(False)
            ocf_3y_pos = out["_ocf_3y_pos"].astype(bool).reindex(out.index).fillna(False) if "_ocf_3y_pos" in out.columns else pd.Series(False, index=out.index)
            cf_exempt = cash_fortress & ocf_3y_pos
            # 剔除：非金融 + 非(堡垒且OCF>0) + 非NaN + cc<0.5
            drop_mask = (~exempt) & (~cf_exempt) & cc.notna() & (cc < 0.5)
            out = out[~drop_mask]
    if filter_dividend_coverage:
        # B25 → B32-UI v9: 分红现金覆盖过滤
        # 砍条件（同时满足才砍）：
        #   ① real_fcf / market_cap ≤ 8%（FCF 收益率不高）
        #   ② 3y avg real_fcf / 3y avg 现金分红 < 1.0（长周期口径不足）
        #   ③ real_fcf_ttm / 当年现金分红 < 1.0（当年 TTM 口径不足）
        # 豁免：非派息股 / 金融行业 / 可分配覆盖（3y avg real_fcf + 0.1×净现金 > 3y avg 分红总额）
        ind = out.get("industry").astype(str) if "industry" in out.columns else None
        exempt = ind.isin(_FIN_INDUSTRY_EXEMPT) if ind is not None else pd.Series(False, index=out.index)
        rfcf = pd.to_numeric(out.get("real_fcf"), errors="coerce")
        rfcf_ttm = pd.to_numeric(out.get("real_fcf_ttm"), errors="coerce")
        cp = pd.to_numeric(out.get("current_price"), errors="coerce")
        shs = pd.to_numeric(out.get("shares_outstanding"), errors="coerce")
        fyd_ps = pd.to_numeric(out.get("fy_div_per_share"), errors="coerce")
        mcap = cp * shs
        annual_div_total = fyd_ps * shs
        # ①
        cond_low_yield = (rfcf / mcap.where(mcap > 0)).fillna(-9.99) <= 0.08
        # ② 3y avg DCC < 1（dividend_coverage 已是 3y 口径）
        dcc_3y = pd.to_numeric(out.get("dividend_coverage"), errors="coerce")
        cond_3y_under = dcc_3y.notna() & (dcc_3y < 1.0)
        # ③ TTM coverage < 1
        ttm_cover = rfcf_ttm / annual_div_total.where(annual_div_total > 0)
        cond_ttm_under = ttm_cover.notna() & (ttm_cover < 1.0)
        # 豁免：现金堡垒 且 可分配总额（3y real_fcf + 0.1×净现金）盖得住 3y 平均分红 且 real_fcf > 0
        cash_fortress = cash_fortress_col.reindex(out.index).fillna(False)
        ibd = pd.to_numeric(out.get("interest_bearing_debt"), errors="coerce")
        cash = pd.to_numeric(out.get("cash_and_equivalents"), errors="coerce")
        net_cash_abs = cash.fillna(0) - ibd.fillna(0)
        dist_total = rfcf.fillna(0) + 0.1 * net_cash_abs
        prev_div_ps = pd.to_numeric(out["prev_year_div_per_share"], errors="coerce") if "prev_year_div_per_share" in out.columns else pd.Series(0.0, index=out.index)
        two_ago_div_ps = pd.to_numeric(out["_two_years_ago_div_per_share"], errors="coerce") if "_two_years_ago_div_per_share" in out.columns else pd.Series(0.0, index=out.index)
        div_3y_avg_ps = (fyd_ps.fillna(0) + prev_div_ps.fillna(0) + two_ago_div_ps.fillna(0)) / 3.0
        div_3y_total = div_3y_avg_ps * shs.fillna(0)
        dist_cover = ((div_3y_total > 0) & (dist_total > div_3y_total)).fillna(False)
        rfcf_pos = rfcf.fillna(-1) > 0
        cf_exempt = cash_fortress & dist_cover & rfcf_pos
        drop_mask = (~exempt) & (~cf_exempt) & cond_low_yield & cond_3y_under & cond_ttm_under
        out = out[~drop_mask]
    # B32-UI v5: frfc 已废弃（与 FCF 评级多选语义重复），通过 fcf_rating_keep 实现
    # B32-UI: FCF 评级多选过滤（仅保留勾选的评级）
    if fcf_rating_keep is not None and len(fcf_rating_keep) > 0:
        rating = out.get("fcf_rating")
        if rating is not None:
            keep_mask = rating.astype(str).isin(fcf_rating_keep)
            out = out[keep_mask]
    if filter_momentum:
        # B20: 动量过滤（B24-followup: zscore 基于全池 df，避免叠加后池缩水导致 z 偏移）
        from web.momentum import apply_momentum_filter
        out = apply_momentum_filter(out, full_df=df)
    if filter_price_3y_low_3x:
        # 当前股价 ≤ 3 年(36个月)最低收盘价 × 3；空值（无价格历史）一律剔除
        out = _filter_by_low(out, months=36, multiplier=3.0)
    if filter_price_6m_low_18x:
        out = _filter_by_low(out, months=6, multiplier=1.6)
    if filter_price_2m_low_13x:
        out = _filter_by_low(out, months=2, multiplier=1.25)
    return out


def _filter_by_low(out: pd.DataFrame, months: int, multiplier: float) -> pd.DataFrame:
    """仅保留 当前 close_qfq ≤ 近 N 月最低 close_qfq × multiplier。空值剔除。
    B24: 当前价与历史最低价均在 qfq 空间，跨除权点连续。
    """
    low_map = _load_low_map(months)
    latest_map = _load_latest_qfq_map()
    if "code" in out.columns and low_map and latest_map:
        codes = out["code"].astype(str).str.zfill(5)
        low = codes.map(low_map)
        cp = codes.map(latest_map)
        cp = pd.to_numeric(cp, errors="coerce")
        low = pd.to_numeric(low, errors="coerce")
        mask = (cp.notna()) & (low.notna()) & (cp <= low * multiplier)
        return out[mask]
    return out


@app.route("/")
def index():
    periods = list_ranking_periods()
    selected = request.args.get("period", periods[0] if periods else None)

    # 筛选项
    filter_div_growth = request.args.get("fdg") == "1"
    filter_return_growth = request.args.get("frg") == "1"
    filter_real_buyback = request.args.get("frb") == "1"
    filter_revenue = request.args.get("frv") == "1"
    filter_price_3y_low_3x = request.args.get("f3l") == "1"
    filter_price_6m_low_18x = request.args.get("f6l") == "1"
    filter_price_2m_low_13x = request.args.get("f2l") == "1"
    filter_ocf = request.args.get("focf") == "1"
    filter_momentum = request.args.get("fmom") == "1"
    filter_net_debt_hard = request.args.get("fnd") == "1"
    filter_net_debt_z = request.args.get("fndz") == "1"
    filter_growth_factor = request.args.get("fgf") == "1"
    filter_roe_z = request.args.get("froez") == "1"
    filter_turnover_z = request.args.get("ftoz") == "1"
    filter_cash_conversion = request.args.get("fcc") == "1"
    filter_dividend_coverage = request.args.get("fdcc") == "1"
    filter_real_fcf_coverage = request.args.get("frfc") == "1"
    # B32-UI: cash_fortress 豁免开关（默认开）；FCF 评级多选
    # checkbox + hidden trick：URL 可能同时出现 fcfx=0&fcfx=1（勾上）或仅 fcfx=0（未勾）或都没传（代表不溉默认）
    _fcfx_vals = request.args.getlist("fcfx")
    cash_fortress_exempt = "1" in _fcfx_vals if _fcfx_vals else True
    fcf_rating_keep = request.args.getlist("frating")  # [] = 不限
    sort_by = request.args.get("sort", "composite")  # composite | return | sustainable

    df = pd.DataFrame()
    if selected:
        df = load_ranking(selected)
        df = _attach_cashflow_factors(df)

    total_unfiltered = len(df)
    df = _apply_filters(df, filter_div_growth, filter_return_growth, filter_real_buyback, filter_revenue, filter_price_3y_low_3x, filter_price_6m_low_18x, filter_price_2m_low_13x, filter_ocf, filter_momentum, filter_net_debt_hard, filter_net_debt_z, filter_growth_factor, filter_roe_z, filter_turnover_z, filter_cash_conversion, filter_dividend_coverage, filter_real_fcf_coverage, cash_fortress_exempt=cash_fortress_exempt, fcf_rating_keep=fcf_rating_keep or None)

    # B28: 排序切换
    if not df.empty:
        if sort_by == "sustainable" and "sustainable_return_rate" in df.columns:
            df = df.sort_values(
                ["sustainable_return_rate", "return_rate"],
                ascending=[False, False], na_position="last", kind="mergesort",
            )
        elif sort_by == "return" and "return_rate" in df.columns:
            df = df.sort_values(
                ["return_rate"], ascending=[False], na_position="last", kind="mergesort",
            )
        else:
            # composite: 按重算后的 composite_score 重排
            if "composite_score" in df.columns:
                df = df.sort_values(
                    ["composite_score", "return_rate"],
                    ascending=[False, False], na_position="last", kind="mergesort",
                )
        # 重算 rank（与当前可见顺序对齐）
        if "rank" in df.columns:
            df = df.reset_index(drop=True)
            df["rank"] = df.index + 1

    # 分页
    page = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 50))
    total = len(df)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))

    start = (page - 1) * per_page
    end = start + per_page
    page_df = df.iloc[start:end].copy()
    # B31: 内部辅助列不展示
    if "_cash_fortress" in page_df.columns:
        page_df = page_df.drop(columns=["_cash_fortress"])
    # 覆盖率展示百分比（保留 1 位）；real_fcf_yi 保留 1 位
    if "coverage_ratio" in page_df.columns:
        page_df["coverage_ratio"] = pd.to_numeric(page_df["coverage_ratio"], errors="coerce").apply(
            lambda v: f"{v*100:.1f}%" if pd.notna(v) else None
        )
    if "real_fcf_yi" in page_df.columns:
        page_df["real_fcf_yi"] = pd.to_numeric(page_df["real_fcf_yi"], errors="coerce").round(1)
    # B32: drop_ratio 格式化为百分比（保留 0 位整数）
    if "drop_ratio" in page_df.columns:
        page_df["drop_ratio"] = pd.to_numeric(page_df["drop_ratio"], errors="coerce").apply(
            lambda v: f"{v*100:.0f}%" if pd.notna(v) else None
        )
    # B32: cash_buffer_ratio 格式化为百分比（保留 1 位）
    if "cash_buffer_ratio" in page_df.columns:
        page_df["cash_buffer_ratio"] = pd.to_numeric(page_df["cash_buffer_ratio"], errors="coerce").apply(
            lambda v: f"{v*100:.1f}%" if pd.notna(v) else None
        )
    # 移除内部原始 real_fcf / real_fcf_ttm 列（仅展示亿口径 / drop_ratio）
    if "real_fcf" in page_df.columns:
        page_df = page_df.drop(columns=["real_fcf"])
    if "real_fcf_ttm" in page_df.columns:
        page_df = page_df.drop(columns=["real_fcf_ttm"])

    display_df = rename_for_display(page_df) if not page_df.empty else page_df

    return render_template(
        "index.html",
        periods=periods,
        selected_period=selected,
        table=display_df.to_dict(orient="records"),
        columns=display_df.columns.tolist() if not display_df.empty else [],
        page=page,
        total_pages=total_pages,
        total=total,
        total_unfiltered=total_unfiltered,
        per_page=per_page,
        filter_div_growth=filter_div_growth,
        filter_return_growth=filter_return_growth,
        filter_real_buyback=filter_real_buyback,
        filter_revenue=filter_revenue,
        filter_price_3y_low_3x=filter_price_3y_low_3x,
        filter_price_6m_low_18x=filter_price_6m_low_18x,
        filter_price_2m_low_13x=filter_price_2m_low_13x,
        filter_ocf=filter_ocf,
        filter_momentum=filter_momentum,
        filter_net_debt_hard=filter_net_debt_hard,
        filter_net_debt_z=filter_net_debt_z,
        filter_growth_factor=filter_growth_factor,
        filter_roe_z=filter_roe_z,
        filter_turnover_z=filter_turnover_z,
        filter_cash_conversion=filter_cash_conversion,
        filter_dividend_coverage=filter_dividend_coverage,
        filter_real_fcf_coverage=filter_real_fcf_coverage,
        cash_fortress_exempt=cash_fortress_exempt,
        fcf_rating_keep=fcf_rating_keep,
        sort_by=sort_by,
    )


@app.route("/api/ranking")
def api_ranking():
    """JSON API，供外部调用"""
    periods = list_ranking_periods()
    period = request.args.get("period", periods[0] if periods else None)
    if not period:
        return jsonify({"error": "no data"}), 404
    df = load_ranking(period)
    df = _attach_cashflow_factors(df)
    total_unfiltered = len(df)
    filter_div_growth = request.args.get("fdg") == "1"
    filter_return_growth = request.args.get("frg") == "1"
    filter_real_buyback = request.args.get("frb") == "1"
    filter_revenue = request.args.get("frv") == "1"
    filter_price_3y_low_3x = request.args.get("f3l") == "1"
    filter_price_6m_low_18x = request.args.get("f6l") == "1"
    filter_price_2m_low_13x = request.args.get("f2l") == "1"
    filter_ocf = request.args.get("focf") == "1"
    filter_momentum = request.args.get("fmom") == "1"
    filter_net_debt_hard = request.args.get("fnd") == "1"
    filter_net_debt_z = request.args.get("fndz") == "1"
    filter_growth_factor = request.args.get("fgf") == "1"
    filter_roe_z = request.args.get("froez") == "1"
    filter_turnover_z = request.args.get("ftoz") == "1"
    filter_cash_conversion = request.args.get("fcc") == "1"
    filter_dividend_coverage = request.args.get("fdcc") == "1"
    filter_real_fcf_coverage = request.args.get("frfc") == "1"
    _fcfx_vals = request.args.getlist("fcfx")
    cash_fortress_exempt = "1" in _fcfx_vals if _fcfx_vals else True
    fcf_rating_keep = request.args.getlist("frating")
    df = _apply_filters(df, filter_div_growth, filter_return_growth, filter_real_buyback, filter_revenue, filter_price_3y_low_3x, filter_price_6m_low_18x, filter_price_2m_low_13x, filter_ocf, filter_momentum, filter_net_debt_hard, filter_net_debt_z, filter_growth_factor, filter_roe_z, filter_turnover_z, filter_cash_conversion, filter_dividend_coverage, filter_real_fcf_coverage, cash_fortress_exempt=cash_fortress_exempt, fcf_rating_keep=fcf_rating_keep or None)
    # 按重算后的 composite_score 重排，并重写 rank
    if not df.empty and "composite_score" in df.columns:
        df = df.sort_values(
            ["composite_score", "return_rate"],
            ascending=[False, False], na_position="last", kind="mergesort",
        ).reset_index(drop=True)
        df["rank"] = df.index + 1
    return jsonify({
        "period": period,
        "total": len(df),
        "total_unfiltered": total_unfiltered,
        "filters": {"fdg": filter_div_growth, "frg": filter_return_growth, "frb": filter_real_buyback, "frv": filter_revenue, "f3l": filter_price_3y_low_3x, "f6l": filter_price_6m_low_18x, "f2l": filter_price_2m_low_13x, "focf": filter_ocf, "fmom": filter_momentum, "fnd": filter_net_debt_hard, "fndz": filter_net_debt_z, "fgf": filter_growth_factor, "froez": filter_roe_z, "ftoz": filter_turnover_z, "fcc": filter_cash_conversion, "fdcc": filter_dividend_coverage, "frfc": filter_real_fcf_coverage, "fcfx": cash_fortress_exempt, "frating": fcf_rating_keep},
        "data": df.to_dict(orient="records"),
    })


@app.route("/download/<period>")
def download_excel(period):
    """下载 Excel 文件"""
    path = RANKING_EXCEL.format(period=period)
    if not os.path.exists(path):
        return "文件不存在", 404
    return send_file(path, as_attachment=True, download_name=f"hk_ranking_{period}.xlsx")


@app.route("/api/periods")
def api_periods():
    return jsonify(list_ranking_periods())


# =====================================================================
# 回测页面
# =====================================================================

@app.route("/backtest")
def backtest_page():
    periods = list_ranking_periods()
    return render_template("backtest.html", periods=sorted(periods))


@app.route("/api/backtest")
def api_backtest():
    """运行 TOP-N 回测并返回 JSON"""
    from backtest.runner import run_topn_with_report

    start = request.args.get("start")
    end = request.args.get("end")
    top_n = int(request.args.get("top_n", 20))
    weight = request.args.get("weight", "equal")
    cost_bps = float(request.args.get("cost_bps", 15))
    with_bench = request.args.get("benchmark", "1") == "1"
    if not start or not end:
        return jsonify({"error": "missing start/end (YYYY-MM)"}), 400
    try:
        report = run_topn_with_report(
            start, end, top_n=top_n, weight_scheme=weight,
            transaction_cost_bps=cost_bps, with_benchmark=with_bench,
            report_name=f"web_top{top_n}_{start}_{end}_{weight}",
        )
        return jsonify(report)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/backtest/quintile")
def api_backtest_quintile():
    """运行五分位回测并返回 JSON"""
    from backtest.runner import run_quintile_with_report

    start = request.args.get("start")
    end = request.args.get("end")
    n_q = int(request.args.get("n_quantiles", 5))
    weight = request.args.get("weight", "equal")
    cost_bps = float(request.args.get("cost_bps", 15))
    if not start or not end:
        return jsonify({"error": "missing start/end (YYYY-MM)"}), 400
    try:
        report = run_quintile_with_report(
            start, end, n_quantiles=n_q, weight_scheme=weight,
            transaction_cost_bps=cost_bps,
            report_name=f"web_q{n_q}_{start}_{end}_{weight}",
        )
        return jsonify(report)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=WEB_PORT, debug=WEB_DEBUG)
