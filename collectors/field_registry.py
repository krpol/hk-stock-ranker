"""
字段元数据登记表 —— 数据质量治理的单一事实源。

每个进入 hk_financial.csv 的财务字段都应在此登记其元信息：
  - 数据源（source）
  - 口径（caliber）
  - 覆盖率下限（min_coverage）
  - 时效容忍（max_lag_days，相对报告期 as_of）
  - 合理值域（sane_range）
  - 是否允许负值 / NaN

用途：
  1. 作为 scripts/data_quality_check.py 体检器的规则来源
  2. 作为 docs/DATA_DICTIONARY.md 的机读补充
  3. 单测断言「所有 CSV 财务字段都已登记」，防止新字段漏检

【流程约束】新接入任何财务字段，必须先在此登记（见 docs/DATA_QUALITY_PLAYBOOK.md）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Tuple


@dataclass(frozen=True)
class FieldMeta:
    """单个字段的质量元信息。"""
    desc: str                                   # 字段含义（简）
    source: str                                 # 数据源：yfinance / akshare / akshare+yfinance / computed
    caliber: str = ""                           # 口径：ttm / 3y_avg / point_in_time / ratio / event
    min_coverage: float = 0.90                  # 非空率下限（0-1）；低于此告警
    max_lag_days: Optional[int] = None          # 相对 as_of 报告期允许的最大滞后天数；None=不校验时效
    sane_range: Tuple[Optional[float], Optional[float]] = (None, None)  # 合理值域 [lo, hi]，None=该侧不限
    allow_negative: bool = True                 # 是否允许负值（False 时负值告警）
    allow_nan: bool = True                      # 是否允许 NaN（覆盖率外的语义标记）
    severity: str = "warn"                      # 违规级别：warn / fail（fail 会让 --strict 退出非0）

    def check_value(self, v: float) -> Optional[str]:
        """校验单个数值，返回违规描述或 None。"""
        import math
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return None  # NaN 由覆盖率维度处理
        lo, hi = self.sane_range
        if not self.allow_negative and v < 0:
            return f"负值 {v:.4g}（不允许）"
        if lo is not None and v < lo:
            return f"低于下限 {v:.4g} < {lo}"
        if hi is not None and v > hi:
            return f"超过上限 {v:.4g} > {hi}"
        return None


# ---------------------------------------------------------------------------
# 全字段登记表
# key = CSV 列名
# ---------------------------------------------------------------------------
FIELD_REGISTRY: dict[str, FieldMeta] = {
    # ── 派息（yfinance actions + AKShare 派息表；事件驱动，时效好）──────────
    "fy_div_per_share": FieldMeta(
        "当前财年 TTM 每股分红", "akshare+yfinance", "ttm",
        min_coverage=0.95, sane_range=(0, None), allow_negative=False),
    "prev_year_div_per_share": FieldMeta(
        "上一财年每股分红", "akshare+yfinance", "ttm",
        min_coverage=0.95, sane_range=(0, None), allow_negative=False),
    "two_years_ago_div_per_share": FieldMeta(
        "再上一财年每股分红", "akshare+yfinance", "ttm",
        min_coverage=0.90, sane_range=(0, None), allow_negative=False),
    "latest_period_div_per_share": FieldMeta(
        "最近一期每股分红", "akshare+yfinance", "event",
        min_coverage=0.90, sane_range=(0, None), allow_negative=False),
    "forward_div_per_share": FieldMeta(
        "已宣派未除净每股分红", "hkex+yfinance", "event",
        min_coverage=0.90, sane_range=(0, None), allow_negative=False),
    "has_dividend_history": FieldMeta(
        "近3年是否派息", "yfinance", "event", min_coverage=0.95),

    # ── 财报截止日（时效锚点）─────────────────────────────────────────────
    "current_fy_end": FieldMeta(
        "当前财年年报截止日", "yfinance", "point_in_time",
        min_coverage=0.95, max_lag_days=550),
    "latest_period_end": FieldMeta(
        "最近一期财报截止日（含中报）", "yfinance", "point_in_time",
        min_coverage=0.95, max_lag_days=400),

    # ── 回购 / 股本（yfinance）────────────────────────────────────────────
    "total_buyback": FieldMeta("当前财年回购金额", "yfinance", "ttm", min_coverage=0.90),
    "shares_outstanding": FieldMeta(
        "总股本", "yfinance", "point_in_time",
        min_coverage=0.95, sane_range=(0, None), allow_negative=False),
    "annual_dilution_rate": FieldMeta(
        "年化稀释率", "computed", "ratio", min_coverage=0.90, sane_range=(-0.5, 2.0)),

    # ── 成长指标（AKShare 优先 + yfinance 兜底）───────────────────────────
    "ttm_revenue": FieldMeta(
        "TTM 营收", "akshare+yfinance", "ttm",
        min_coverage=0.95, sane_range=(0, None), allow_negative=False),
    "revenue_yoy": FieldMeta(
        "营收同比", "computed", "ratio",
        min_coverage=0.90, sane_range=(-1.0, 20.0)),  # yoy 极端标记
    "ttm_operating_income": FieldMeta(
        "TTM 营业利润", "akshare+yfinance", "ttm", min_coverage=0.95),
    "operating_income_yoy": FieldMeta(
        "营业利润同比", "computed", "ratio",
        min_coverage=0.90, sane_range=(-50.0, 50.0)),  # 分母极小易爆炸
    "ttm_operating_cashflow": FieldMeta(
        "TTM 经营现金流", "akshare+yfinance", "ttm", min_coverage=0.95),
    "operating_cashflow_yoy": FieldMeta(
        "经营现金流同比", "computed", "ratio",
        min_coverage=0.90, sane_range=(-50.0, 100.0)),

    # ── 负债 / ROE（B34: AKShare 优先 + yfinance 兜底）────────────────────
    "net_debt_ratio": FieldMeta(
        "有息负债率", "akshare+yfinance", "ratio",
        min_coverage=0.95, sane_range=(0, 1.5), allow_negative=False),
    "total_liability_ratio": FieldMeta(
        "资产负债率", "akshare+yfinance", "ratio",
        min_coverage=0.95, sane_range=(0, 1.2), allow_negative=False),
    "debt_to_equity": FieldMeta(
        "净有息负债/净资产", "akshare+yfinance", "ratio",
        min_coverage=0.90, sane_range=(0, 10.0), allow_negative=False),
    "roe": FieldMeta(
        "净资产收益率", "akshare+yfinance", "ratio",
        min_coverage=0.90, sane_range=(-2.0, 2.0)),
    "cash_and_equivalents": FieldMeta(
        "现金及等价物", "akshare+yfinance", "point_in_time",
        min_coverage=0.95, sane_range=(0, None), allow_negative=False),
    "interest_bearing_debt": FieldMeta(
        "有息负债", "akshare+yfinance", "point_in_time",
        min_coverage=0.90, sane_range=(0, None), allow_negative=False),
    # B40: 上年值（用于 net_cash_change_rate）
    "prev_cash_and_equivalents": FieldMeta(
        "上年现金及等价物", "akshare+yfinance", "point_in_time",
        min_coverage=0.90, sane_range=(0, None), allow_negative=False),
    "prev_interest_bearing_debt": FieldMeta(
        "上年有息负债", "akshare+yfinance", "point_in_time",
        min_coverage=0.85, sane_range=(0, None), allow_negative=False),
    # B41: 流动金融资产（定期+短投+FVTPL 流动）—— 广义现金增量
    "liquid_financial_assets": FieldMeta(
        "流动金融资产", "akshare+yfinance", "point_in_time",
        min_coverage=0.70, sane_range=(0, None), allow_negative=False),
    "prev_liquid_financial_assets": FieldMeta(
        "上年流动金融资产", "akshare+yfinance", "point_in_time",
        min_coverage=0.60, sane_range=(0, None), allow_negative=False),

    # ── FCF 组件（B34: AKShare 优先 + yfinance 兜底）──────────────────────
    "ocf_3y_avg": FieldMeta(
        "经营现金流3年平均", "akshare+yfinance", "3y_avg",
        min_coverage=0.95, allow_negative=True),  # 可为负（真实经营困难）
    "capex_3y_avg": FieldMeta(
        "资本开支3年平均", "akshare+yfinance", "3y_avg",
        min_coverage=0.95, sane_range=(0, None), allow_negative=False),
    "dep_amortization_3y_avg": FieldMeta(
        "折旧摊销3年平均", "akshare+yfinance", "3y_avg",
        min_coverage=0.90, sane_range=(0, None), allow_negative=False),
    "equity_latest": FieldMeta(
        "最新净资产", "akshare+yfinance", "point_in_time",
        min_coverage=0.95, allow_negative=True),  # 可为负（资不抵债，下游会过滤）
    "capex_ttm": FieldMeta(
        "TTM 资本开支", "akshare+yfinance", "ttm",
        min_coverage=0.95, sane_range=(0, None), allow_negative=False),
    "da_ttm": FieldMeta(
        "TTM 折旧摊销", "akshare+yfinance", "ttm",
        min_coverage=0.90, sane_range=(0, None), allow_negative=False),
    "prev_capex": FieldMeta(
        "上年资本开支", "akshare+yfinance", "annual",
        min_coverage=0.90, sane_range=(0, None), allow_negative=False),
    "prev_da": FieldMeta(
        "上年折旧摊销", "akshare+yfinance", "annual",
        min_coverage=0.90, sane_range=(0, None), allow_negative=False),
    "current_lease_obligation_prev": FieldMeta(
        "上年一年内到期租赁", "akshare+yfinance", "point_in_time",
        min_coverage=0.90, sane_range=(0, None), allow_negative=False),
    "current_lease_obligation_ttm": FieldMeta(
        "最新一年内到期租赁", "akshare+yfinance", "point_in_time",
        min_coverage=0.90, sane_range=(0, None), allow_negative=False),

    # ── B42: 融资活动净利息 ──────────────────────────────────────────────
    "interest_paid_financing_3y": FieldMeta(
        "3年平均融资活动已付利息", "akshare", "annual",
        min_coverage=0.50, sane_range=(0, None), allow_negative=False),
    "interest_received_investing_3y": FieldMeta(
        "3年平均投资活动已收利息", "akshare", "annual",
        min_coverage=0.30, sane_range=(0, None), allow_negative=False),
    "interest_paid_financing_ttm": FieldMeta(
        "TTM 融资活动已付利息", "akshare", "ttm",
        min_coverage=0.50, sane_range=(0, None), allow_negative=False),
    "interest_received_investing_ttm": FieldMeta(
        "TTM 投资活动已收利息", "akshare", "ttm",
        min_coverage=0.30, sane_range=(0, None), allow_negative=False),

    # ── 数据血缘（B35: 报告期锚点）────────────────────────────────────────
    "fcf_as_of": FieldMeta(
        "FCF组件实际使用的最新报告期", "computed", "point_in_time",
        min_coverage=0.90, max_lag_days=550),
    "debt_as_of": FieldMeta(
        "负债/ROE实际使用的最新报告期", "computed", "point_in_time",
        min_coverage=0.90, max_lag_days=550),
}


# 不纳入体检的字段（元数据/中间列/已废弃），显式列出便于「漏登记」断言排除
IGNORED_FIELDS: set[str] = {
    "code", "name", "updated_at",
    # 派息内部/衍生
    "latest_period_yoy_div_per_share", "latest_period_yoy_end",
    "prev_fy_end", "latest_attributed_fy", "fiscal_periods", "forward_div_source",
    # 回购衍生
    "buyback_per_share", "prev_buyback_per_share", "prev_total_buyback",
    "prev_shares_outstanding", "splits_ratio_in_window", "splits_ratio_calculated",
    # 成长 t2 / prev（衍生，随主字段一起校验）
    "prev_ttm_revenue", "ttm_revenue_t2",
    "prev_ttm_operating_income", "ttm_operating_income_t2",
    "prev_ttm_operating_cashflow", "ttm_operating_cashflow_t2",
    # 已废弃 / 别处维护
    "current_price",
}


def registered_fields() -> set[str]:
    return set(FIELD_REGISTRY.keys())
