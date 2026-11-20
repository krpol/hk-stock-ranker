"""
回报率排序引擎

回报率公式：
  return_rate = (fy_div_per_share + buyback_per_share) / current_price * 100
               - annual_dilution_rate * 100

其中：
  - fy_div_per_share:                  TTM 分红 = 最近一个完整财年的分红总和
                                       （current_fy_year_end - 1年, current_fy_year_end] 内所有期）
                                       已经包含了 HKEX 公告的预告分红 (forward_div_override)
  - prev_year_div_per_share:           上一完整财年的分红总和（用于同比对比）
  - latest_period_div_per_share:       最近一期财报（任意期，可能是中报）的分红
  - latest_period_yoy_div_per_share:   最近一期同期上一年的分红（用于环比对比）
  - forward_div_per_share:             已公告但尚未除息的预分红（仅展示，不进入回报率公式，避免双计）
  - buyback_per_share:                 当前财年每股回购金额
  - annual_dilution_rate:              最近3年最大年度股份增发率
  - current_price:                     当前收盘价（不复权，港元）
"""
import logging
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# 排序结果中展示的列及顺序
OUTPUT_COLUMNS = [
    "rank",
    "code",
    "name",
    "industry",
    "current_price",
    "composite_score",
    "return_zscore",
    "return_rate",
    "ttm_return_rate",
    "prev_ttm_return_rate",
    "real_ttm_return_rate",
    "fy_div_per_share",
    "prev_year_div_per_share",
    "latest_period_div_per_share",
    "latest_period_yoy_div_per_share",
    "forward_div_per_share",
    "real_buyback_per_share",
    "buyback_per_share",
    "prev_buyback_per_share",
    "net_share_change_rate",
    "annual_dilution_rate",
    "revenue_yoy",
    "operating_income_yoy",
    "operating_cashflow_yoy",
    "ttm_return_yoy",
    "growth_zscore_revenue",
    "growth_zscore_operating_income",
    "growth_zscore_operating_cashflow",
    "growth_zscore_ttm_return",
    "growth_zscore_fcf_equity",
    "growth_zscore_fcf_momentum",
    "growth_zscore_net_cash_change",
    "real_fcf_ttm",
    "prev_real_fcf_ttm",
    "fcf_to_equity",
    "fcf_momentum",
    "net_cash_change_rate",
    "growth_factor",
    "cash_buffer_ratio",
    "cash_buffer_zscore",
    "pe_ttm",
    "pb",
    "net_debt_ratio",
    "total_liability_ratio",
    "debt_to_equity",
    "roe",
    "market_cap",
    "current_fy_end",
    "prev_fy_end",
    "latest_period_end",
    "latest_period_yoy_end",
    "shares_outstanding",
    "prev_shares_outstanding",
    "total_buyback",
    "cash_and_equivalents",
    "interest_bearing_debt",
]

COLUMN_LABELS = {
    "rank": "排名",
    "code": "股票代码",
    "name": "公司名称",
    "industry": "所属行业",
    "current_price": "当前收盘价(HKD)",
    "return_rate": "回报率(%)",
    "sustainable_return_rate": "可持续回报率(%)",
    "return_zscore": "回报 Z",
    "composite_score": "综合得分",
    "fy_div_per_share": "TTM分红(HKD)",
    "ttm_return_rate": "TTM回报(HKD)",
    "prev_year_div_per_share": "上年TTM分红(HKD)",
    "prev_ttm_return_rate": "上年TTM回报(HKD)",
    "latest_period_div_per_share": "最近期分红(HKD)",
    "latest_period_yoy_div_per_share": "同期上年分红(HKD)",
    "forward_div_per_share": "预分红(HKD)",
    "buyback_per_share": "每股回购(HKD)",
    "prev_buyback_per_share": "上年每股回购(HKD)",
    "net_share_change_rate": "股本净变化(%)",
    "real_buyback_per_share": "真实每股回购(HKD)",
    "real_ttm_return_rate": "真实TTM回报(HKD)",
    "annual_dilution_rate": "年化稀释率",
    "revenue_yoy": "TTM营收同比",
    "operating_income_yoy": "TTM营业利润同比",
    "operating_cashflow_yoy": "TTM经营现金流同比",
    "cash_conversion": "现金转化率",
    "dividend_coverage": "分红现金覆盖",
    "real_fcf_yi": "剩余CF(亿)",
    "coverage_ratio": "剩余CF/净债",
    "fcf_rating": "FCF评级",
    "drop_ratio": "近1年突降",
    "cash_buffer_ratio": "现金缓冲比",
    "cash_buffer_zscore": "现金缓冲 Z",
    "cash_buffer_ratio_v2": "现金缓冲比v2",
    "fcf_yield": "FCF收益率",
    "cash_fortress_factor": "现金堡垒因子",
    "fcf_to_equity": "FCF/净资产",
    "fcf_momentum": "现金流动量",
    "real_fcf_ttm": "TTM剩余CF",
    "prev_real_fcf_ttm": "上年TTM剩余CF",
    "_ocf_3y_pos": "3年OCF为正",
    "_two_years_ago_div_per_share": "前年每股分红(HKD)",
    "ttm_return_yoy": "TTM回报金额同比",
    "growth_zscore_revenue": "营收增长 Z",
    "growth_zscore_operating_income": "营业利润增长 Z",
    "growth_zscore_operating_cashflow": "经营现金流增长 Z",
    "growth_zscore_ttm_return": "TTM回报增长 Z",
    "growth_zscore_fcf_equity": "FCF/净资产 Z",
    "growth_zscore_fcf_momentum": "现金流动量 Z",
    "growth_zscore_net_cash_change": "净现金变化 Z",
    "net_cash_change_rate": "净现金变化率",
    "growth_factor": "增长因子",
    "pe_ttm": "PE(TTM)",
    "pb": "PB",
    "net_debt_ratio": "有息负债率",
    "total_liability_ratio": "资产负债率",
    "debt_to_equity": "有息负债/净资产",
    "roe": "ROE",
    "market_cap": "总市值(HKD)",
    "current_fy_end": "当前财年末",
    "prev_fy_end": "上年财年末",
    "latest_period_end": "最近财报日",
    "latest_period_yoy_end": "同期上年财报日",
    "shares_outstanding": "总股本",
    "prev_shares_outstanding": "上年总股本",
    "total_buyback": "回购总额(HKD)",
}


def compute_return_rate(
    current_price: Optional[float],
    fy_div_per_share: float,
    forward_div_per_share: float,
    buyback_per_share: float,
    annual_dilution_rate: float = 0.0,
) -> Optional[float]:
    """
    计算回报率，返回百分比形式（如 5.23 表示 5.23%）
    公式：(TTM分红 + 回购) / 股价 * 100 - 稀释率 * 100

    注：TTM 分红本身已包含预告分红（在 collectors/hk_financial.py
    的 get_fiscal_year_dividends 中将 forward_div_override 注入 current_fy_year_end
    的 pending），因此不能再 + forward_div_per_share，否则双计。
    forward_div_per_share 作为展示列保留，不进入公式。
    若价格缺失或为0则返回 None
    """
    if not current_price or current_price <= 0:
        return None
    numerator = fy_div_per_share + buyback_per_share
    gross_return = numerator / current_price * 100
    dilution_penalty = annual_dilution_rate * 100
    return round(gross_return - dilution_penalty, 4)


def rank_stocks(
    stock_list: pd.DataFrame,
    financial_df: pd.DataFrame,
    min_market_cap: Optional[float] = None,
    exclude_no_dividend: bool = False,
    require_dividend_history: bool = True,
    max_net_debt_ratio: Optional[float] = None,
    max_total_liability_ratio: Optional[float] = None,
    max_debt_to_equity: Optional[float] = None,
    industry_df: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """
    合并股票基本信息与财务数据，计算回报率，按降序排列。

    Args:
        stock_list:     股票列表（含 code, name, current_price/price, pe_ttm, pb, market_cap）
        financial_df:   财务数据（含 code, fy_div_per_share, forward_div_per_share, buyback_per_share, ...）
        min_market_cap: 最小市值过滤（港元）
        exclude_no_dividend: 是否过滤掉分红为0且回购为0的股票
        require_dividend_history: 是否要求有分红历史（默认 True）
        max_net_debt_ratio: 最大有息负债率，超过则过滤（None 表示不限）
        max_total_liability_ratio: 最大资产负债率（B3），超过则过滤（None 表示不限）
        max_debt_to_equity: 最大有息负债/净资产（B19），超过则过滤（None 表示不限）
        industry_df: 行业归属表（含 code, industry），left-join，缺失为 None

    Returns:
        排序后的 DataFrame（含排名列）
    """
    # 合并
    stock_list = stock_list.copy()
    financial_df = financial_df.copy()

    stock_list["code"] = stock_list["code"].astype(str).str.zfill(5)
    financial_df["code"] = financial_df["code"].astype(str).str.zfill(5)

    # 统一价格列名
    if "price" in stock_list.columns and "current_price" not in stock_list.columns:
        stock_list = stock_list.rename(columns={"price": "current_price"})

    merged = pd.merge(stock_list, financial_df, on="code", how="inner")

    # 行业 left-join（独立缓存，缺失为 NaN）
    if industry_df is not None and not industry_df.empty:
        ind = industry_df.copy()
        ind["code"] = ind["code"].astype(str).str.zfill(5)
        ind = ind[["code", "industry"]].drop_duplicates(subset=["code"], keep="last")
        merged = pd.merge(merged, ind, on="code", how="left")
    else:
        merged["industry"] = pd.NA

    # 填充缺失值
    for col in [
        "fy_div_per_share", "prev_year_div_per_share",
        "latest_period_div_per_share", "latest_period_yoy_div_per_share",
        "forward_div_per_share", "buyback_per_share", "prev_buyback_per_share", "annual_dilution_rate",
    ]:
        if col not in merged.columns:
            merged[col] = 0.0
        merged[col] = merged[col].fillna(0.0)

    # 流动性过滤：优先 amount（当日成交额），fallback market_cap。
    # Step 4（2026-11-20）：新版 akshare 的 market_cap 字段实际存的是成交额（与 amount
    # 完全一致），故显式用 amount 更清晰；阈值见 settings.min_market_cap（默认 100 万港元）。
    liq_col = "amount" if "amount" in merged.columns else (
        "market_cap" if "market_cap" in merged.columns else None)
    if min_market_cap and liq_col:
        before_count = len(merged)
        merged = merged[pd.to_numeric(merged[liq_col], errors="coerce").fillna(0) >= min_market_cap]
        after_count = len(merged)
        if before_count > after_count:
            logger.info(f"流动性过滤（{liq_col}）：过滤掉 {before_count - after_count} 只股票"
                        f"（阈值 {min_market_cap:,.0f} 港元）")

    # 过滤无分红历史的股票
    if require_dividend_history and "has_dividend_history" in merged.columns:
        before_count = len(merged)
        merged = merged[merged["has_dividend_history"] == True]
        after_count = len(merged)
        if before_count > after_count:
            logger.info(f"分红历史过滤：过滤掉 {before_count - after_count} 只股票（3年内无分红记录）")

    # 有息负债率过滤
    # net_debt_ratio = NaN 保留（无法获取资产负债表的股票不被过滤，避免漏掉热门新股）
    if max_net_debt_ratio is not None and "net_debt_ratio" in merged.columns:
        before_count = len(merged)
        ratio = pd.to_numeric(merged["net_debt_ratio"], errors="coerce")
        # 仅过滤 ratio 明确 > 阈值的；ratio 为 NaN 的保留
        keep_mask = ~(ratio > max_net_debt_ratio)
        merged = merged[keep_mask]
        after_count = len(merged)
        if before_count > after_count:
            logger.info(
                f"有息负债率过滤：过滤掉 {before_count - after_count} 只股票"
                f"（阈值 {max_net_debt_ratio*100:.0f}%）"
            )

    # B3: 资产负债率过滤（默认 80%）
    # total_liability_ratio = NaN 保留（与 net_debt_ratio 同语义）
    if max_total_liability_ratio is not None and "total_liability_ratio" in merged.columns:
        before_count = len(merged)
        ratio = pd.to_numeric(merged["total_liability_ratio"], errors="coerce")
        keep_mask = ~(ratio > max_total_liability_ratio)
        merged = merged[keep_mask]
        after_count = len(merged)
        if before_count > after_count:
            logger.info(
                f"资产负债率过滤：过滤掉 {before_count - after_count} 只股票"
                f"（阈值 {max_total_liability_ratio*100:.0f}%）"
            )

    # B19: 有息负债/净资产过滤（默认 50%）
    # debt_to_equity = NaN 保留（与 net_debt_ratio 同语义）
    if max_debt_to_equity is not None and "debt_to_equity" in merged.columns:
        before_count = len(merged)
        ratio = pd.to_numeric(merged["debt_to_equity"], errors="coerce")
        keep_mask = ~(ratio > max_debt_to_equity)
        merged = merged[keep_mask]
        after_count = len(merged)
        if before_count > after_count:
            logger.info(
                f"有息负债/净资产过滤：过滤掉 {before_count - after_count} 只股票"
                f"（阈值 {max_debt_to_equity*100:.0f}%）"
            )

    # 过滤无回报的股票
    if exclude_no_dividend:
        has_return = (
            (merged["fy_div_per_share"] > 0) |
            (merged["forward_div_per_share"] > 0) |
            (merged["buyback_per_share"] > 0)
        )
        merged = merged[has_return]

    # 计算 TTM 回报与上年 TTM 回报（绝对金额：分红 + 名义回购，不除股价）
    merged["ttm_return_rate"] = (
        merged["fy_div_per_share"].fillna(0.0) + merged["buyback_per_share"].fillna(0.0)
    ).round(4)
    merged["prev_ttm_return_rate"] = (
        merged["prev_year_div_per_share"].fillna(0.0) + merged["prev_buyback_per_share"].fillna(0.0)
    ).round(4)

    # 真实回购口径：考虑 SBC 增发/配股净影响 + splits/合股调整 + 阈值兜底
    # 股本净变化率 = (调整后上年末股本 - 本年末股本) / 调整后上年末股本，正=净注销，负=被稀释
    if "prev_shares_outstanding" not in merged.columns:
        merged["prev_shares_outstanding"] = pd.NA
    if "splits_ratio_in_window" not in merged.columns:
        merged["splits_ratio_in_window"] = 1.0
    cur_s = pd.to_numeric(merged["shares_outstanding"], errors="coerce")
    prev_s_raw = pd.to_numeric(merged["prev_shares_outstanding"], errors="coerce")
    splits_ratio = pd.to_numeric(merged["splits_ratio_in_window"], errors="coerce").fillna(1.0)
    # A 方案：用 splits ratio 调整上年末股本到"等效现在尺度"
    # 例如 2:1 合股（ratio=0.5），上年 2 亿股 → 等效现在的 1 亿股
    prev_s = prev_s_raw * splits_ratio
    valid = (prev_s > 0) & (cur_s > 0)
    merged["net_share_change_rate"] = 0.0
    merged.loc[valid, "net_share_change_rate"] = (
        (prev_s[valid] - cur_s[valid]) / prev_s[valid] * 100
    ).round(4)
    # 真实每股回购 = max(0, 净变化率) * 当前股价 / 100
    # B 方案：股本下降 + 当年有现金回购支出（buyback_per_share>0）才认定为真实回购
    # C 方案：股本变化超过 ±30% 视为异常事件（合股漏识别/私有化/合并增发等），归零
    cur_p = pd.to_numeric(merged["current_price"], errors="coerce").fillna(0.0)
    cur_buyback = pd.to_numeric(merged["buyback_per_share"], errors="coerce").fillna(0.0)
    pos_change = merged["net_share_change_rate"].clip(lower=0.0)
    has_real_cash_buyback = cur_buyback > 0
    ABNORMAL_THRESHOLD = 30.0  # %
    abnormal = merged["net_share_change_rate"].abs() > ABNORMAL_THRESHOLD
    share_change_value = (pos_change * cur_p / 100.0).round(4)
    # D 方案：取股本变化折算值 与 现金回购 两者较小值，避免高估历史库藏股注销等情形
    merged["real_buyback_per_share"] = share_change_value.combine(cur_buyback, min).round(4)
    # B：无现金回购 → 0
    merged.loc[~has_real_cash_buyback, "real_buyback_per_share"] = 0.0
    # C：异常阈值兜底 → 0
    merged.loc[abnormal, "real_buyback_per_share"] = 0.0
    # 真实 TTM 回报 = TTM 分红 + 真实每股回购
    merged["real_ttm_return_rate"] = (
        merged["fy_div_per_share"].fillna(0.0) + merged["real_buyback_per_share"].fillna(0.0)
    ).round(4)

    # 计算回报率（主排序口径）= TTM分红 + 真实每股回购，除以当前价
    merged["return_rate"] = merged.apply(
        lambda row: compute_return_rate(
            row.get("current_price"),
            row.get("fy_div_per_share", 0.0),
            row.get("forward_div_per_share", 0.0),
            row.get("real_buyback_per_share", 0.0),
            row.get("annual_dilution_rate", 0.0),
        ),
        axis=1,
    )

    # 过滤价格缺失的
    merged = merged[merged["return_rate"].notna()]

    # ============ 回报 Z + 成长 Z 合成综合得分 ============
    # 在过滤完后的样本内做横截面 zscore
    # winsorize 至 [1%, 99%] → zscore → 综合得分 = return_z * 1.0 + growth_z * 0.2
    def _winsorized_zscore(series: pd.Series) -> pd.Series:
        s = pd.to_numeric(series, errors="coerce")
        valid = s.dropna()
        if len(valid) < 10:
            return pd.Series([pd.NA] * len(s), index=s.index, dtype="object")
        lo, hi = valid.quantile(0.01), valid.quantile(0.99)
        s_w = s.clip(lower=lo, upper=hi)
        mu = s_w.mean(skipna=True)
        sd = s_w.std(skipna=True, ddof=0)
        if not sd or sd < 1e-9:
            return pd.Series([pd.NA] * len(s), index=s.index, dtype="object")
        return ((s_w - mu) / sd).round(4)

    # --- 成长因子（4 个 yoy 的 zscore 平均） ---
    # 1) 营收同比, 2) 营业利润同比, 3) 经营现金流同比, 4) TTM 回报金额同比
    # ttm_return_yoy 在样本内即时计算（避免回填 CSV）
    if "ttm_return_rate" in merged.columns and "prev_ttm_return_rate" in merged.columns:
        ttm_curr = pd.to_numeric(merged["ttm_return_rate"], errors="coerce")
        ttm_prev = pd.to_numeric(merged["prev_ttm_return_rate"], errors="coerce")
        # YoY = (curr - prev) / abs(prev)，prev≈0 → NaN（避免除零/符号反转）
        denom = ttm_prev.abs()
        merged["ttm_return_yoy"] = (
            ((ttm_curr - ttm_prev) / denom)
            .where(denom > 1e-6)
            .round(6)
        )
    else:
        merged["ttm_return_yoy"] = pd.NA

    # --- B33: TTM 版 real_fcf（两期）+ FCF/净资产 + 现金流动量 ---
    # real_fcf_ttm      = ocf_ttm      − min(capex_ttm + lease_ttm,  da_ttm)
    # prev_real_fcf_ttm = prev_ttm_ocf − min(prev_capex + lease_prev, prev_da)
    # fcf_to_equity     = real_fcf_ttm / equity_latest                  （水平）
    # fcf_momentum      = (real_fcf_ttm − prev_real_fcf_ttm) / equity   （动量）
    def _num(name: str) -> pd.Series:
        if name in merged.columns:
            return pd.to_numeric(merged[name], errors="coerce")
        import numpy as _np
        return pd.Series(_np.nan, index=merged.index, dtype="float64")

    _ocf_ttm = _num("ttm_operating_cashflow")
    _capex_ttm = _num("capex_ttm")
    _da_ttm = _num("da_ttm")
    _lease_ttm = _num("current_lease_obligation_ttm").fillna(0.0)
    _prev_ocf = _num("prev_ttm_operating_cashflow")
    _prev_capex = _num("prev_capex")
    _prev_da = _num("prev_da")
    _lease_prev = _num("current_lease_obligation_prev").fillna(0.0)
    _eq = _num("equity_latest")
    # B42: CFF 净利息 —— 若非空非零，说明 OCF 未扣利息，需再扣
    _int_paid_cff_ttm = _num("interest_paid_financing_ttm").fillna(0.0)
    _int_recv_inv_ttm = _num("interest_received_investing_ttm").fillna(0.0)
    _net_int_ttm = (_int_paid_cff_ttm - _int_recv_inv_ttm).clip(lower=0.0)

    # 当期 TTM real_fcf：D&A 上限取 min(capex+lease, da)，再扣净利息(CFF)
    _adj_ttm = pd.concat([(_capex_ttm + _lease_ttm), _da_ttm], axis=1).min(axis=1)
    real_fcf_ttm = _ocf_ttm - _adj_ttm - _net_int_ttm
    # 上年 TTM real_fcf（近似复用当期净利息 —— 无 prev_int 字段，先粗略处理）
    _adj_prev = pd.concat([(_prev_capex + _lease_prev), _prev_da], axis=1).min(axis=1)
    prev_real_fcf_ttm = _prev_ocf - _adj_prev - _net_int_ttm

    _eq_pos = _eq.where(_eq > 0)
    merged["real_fcf_ttm"] = real_fcf_ttm.round(2)
    merged["prev_real_fcf_ttm"] = prev_real_fcf_ttm.round(2)
    merged["fcf_to_equity"] = (real_fcf_ttm / _eq_pos).round(4)
    merged["fcf_momentum"] = ((real_fcf_ttm - prev_real_fcf_ttm) / _eq_pos).round(4)

    # --- B40: 净现金变化率 = (广义净现金当期 - 广义净现金上年) / equity ---
    # B41: 广义净现金 = cash_and_equivalents + liquid_financial_assets - interest_bearing_debt
    #      流动金融资产（定期存款 + 短期投资 + FVTPL 流动）计入，避免遗漏理财/结构性存款
    _cash = _num("cash_and_equivalents")
    _lfa  = _num("liquid_financial_assets")
    _ibd  = _num("interest_bearing_debt")
    _prev_cash = _num("prev_cash_and_equivalents")
    _prev_lfa  = _num("prev_liquid_financial_assets")
    _prev_ibd  = _num("prev_interest_bearing_debt")
    net_cash_curr = _cash.fillna(0) + _lfa.fillna(0) - _ibd.fillna(0)
    net_cash_prev = _prev_cash.fillna(0) + _prev_lfa.fillna(0) - _prev_ibd.fillna(0)
    # 仅当两期狭义现金数据均可用时才计算（lfa 可以为 NaN，视为 0）
    _both_cash_avail = _cash.notna() & _prev_cash.notna()
    merged["net_cash_change_rate"] = (
        ((net_cash_curr - net_cash_prev) / _eq_pos)
        .where(_eq_pos > 0)
        .where(_both_cash_avail)
        .round(4)
    )

    # B39-r2: 移除 operating_cashflow_yoy（已由 fcf_to_equity / fcf_momentum 覆盖）
    #         保留 operating_income_yoy —— 营业利润同比反映主业盈利趋势，与营收/回报角度互补
    _AUX_YOY = ["operating_cashflow_yoy"]
    for col in _AUX_YOY:
        if col not in merged.columns:
            merged[col] = pd.NA
    # 兼容旧 zscore 列名（外部可能引用），置 NaN 表示不参与计算
    merged["growth_zscore_operating_cashflow"] = pd.NA

    GROWTH_COLS = [
        "revenue_yoy",
        "operating_income_yoy",
        "ttm_return_yoy",
        "fcf_to_equity",
        "fcf_momentum",
        "net_cash_change_rate",
    ]
    ZSCORE_COLS = [
        "growth_zscore_revenue",
        "growth_zscore_operating_income",
        "growth_zscore_ttm_return",
        "growth_zscore_fcf_equity",
        "growth_zscore_fcf_momentum",
        "growth_zscore_net_cash_change",
    ]
    for col in GROWTH_COLS:
        if col not in merged.columns:
            merged[col] = pd.NA
    for src, dst in zip(GROWTH_COLS, ZSCORE_COLS):
        merged[dst] = _winsorized_zscore(merged[src])
    z_df = merged[ZSCORE_COLS].apply(pd.to_numeric, errors="coerce")
    # B39-r2 / B41: 加权平均 —— TTM 回报同比×3.0（主因子），营收/营业利润×1.0
    # B41-r2: FCF/净资产 权重 0.5（TTM 波动大, 3y 更稳），净现金变化 权重 1.0（广义现金强信号）
    # 用 NaN-safe 加权：sum(w_i * z_i) / sum(w_i for non-NaN)
    GROWTH_WEIGHTS = {
        "growth_zscore_revenue": 1.0,
        "growth_zscore_operating_income": 1.0,
        "growth_zscore_ttm_return": 3.0,
        "growth_zscore_fcf_equity": 0.5,
        "growth_zscore_fcf_momentum": 1.0,
        "growth_zscore_net_cash_change": 1.0,
    }
    weights = pd.Series(GROWTH_WEIGHTS)[ZSCORE_COLS]
    weighted_sum = z_df.mul(weights, axis=1).sum(axis=1, skipna=True, min_count=1)
    weight_total = z_df.notna().mul(weights, axis=1).sum(axis=1)
    merged["growth_factor"] = (
        weighted_sum.where(weight_total > 0) / weight_total.where(weight_total > 0)
    ).round(4)

    # --- 回报 zscore ---
    merged["return_zscore"] = _winsorized_zscore(merged["return_rate"])

    # --- B32: cash_buffer_ratio = (cash − ibd − 3×div) / market_cap ---
    def _as_series(name: str) -> pd.Series:
        if name in merged.columns:
            return pd.to_numeric(merged[name], errors="coerce")
        import numpy as _np
        return pd.Series(_np.nan, index=merged.index, dtype="float64")
    cash = _as_series("cash_and_equivalents")
    ibd = _as_series("interest_bearing_debt")
    fyd_ps = _as_series("fy_div_per_share").fillna(0)
    shs = _as_series("shares_outstanding")
    cp = _as_series("current_price")
    # 市值用 当前股价 × 总股本（market_cap 列单位不可靠）
    mcap = cp * shs
    annual_div_total = fyd_ps * shs.fillna(0)
    net_cash = cash.fillna(0) - ibd.fillna(0) - 3.0 * annual_div_total
    cash_buffer_ratio = net_cash / mcap.where(mcap > 0)
    merged["cash_buffer_ratio"] = cash_buffer_ratio.round(4)
    merged["cash_buffer_zscore"] = _winsorized_zscore(cash_buffer_ratio)

    # --- 综合得分（return:growth:cash_buffer = 1.0 : 0.2 : 0.2） ---
    # 成长/现金缓冲缺失时不惩罚（fillna 0）
    rz = pd.to_numeric(merged["return_zscore"], errors="coerce")
    gz = pd.to_numeric(merged["growth_factor"], errors="coerce").fillna(0.0)
    cbz = pd.to_numeric(merged["cash_buffer_zscore"], errors="coerce").fillna(0.0)
    merged["composite_score"] = (rz * 1.0 + gz * 0.2 + cbz * 0.2).round(4)
    # ============ 综合得分计算结束 ============

    # 按综合得分降序（退化后备：return_rate）
    merged = merged.sort_values(
        ["composite_score", "return_rate"],
        ascending=[False, False],
        na_position="last",
    ).reset_index(drop=True)
    merged.insert(0, "rank", range(1, len(merged) + 1))

    # 保留输出列（按需）
    out_cols = [c for c in OUTPUT_COLUMNS if c in merged.columns]
    result = merged[out_cols].copy()

    logger.info(f"排序完成，共 {len(result)} 只股票")
    return result


def rename_for_display(df: pd.DataFrame) -> pd.DataFrame:
    """将列名替换为中文显示名（用于 Web 展示）"""
    return df.rename(columns={k: v for k, v in COLUMN_LABELS.items() if k in df.columns})
