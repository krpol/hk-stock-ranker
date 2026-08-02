"""
统一的分红字段计算引擎（C1 重构）

设计目标
========
彻底替代两条历史并行实现：
- collectors/hk_financial.py:get_fiscal_year_dividends 中的 Step 2~6
- scripts/refresh_dividend_only.py:recompute_dividends_for_code

收口为单一权威函数 compute_dividend_fields(records, periods, fy_anchor, today, override)。

核心规则（按优先级）
====================
R1 跳过 ex_date == NaT（未表决预案，B2 修复语义）
R2 records 已带 fiscal_year → 按 fy 直接归属到对应年报期
R3 否则用时间窗口启发式 (fy_end, fy_end + 8 月] 回落
R4 forward_override 与 records 用「fy_total」比较去重（B4 修复）
R5 ex_date <= today → done；ex_date > today → pending

设计哲学
========
1. 输入是「派息记录列表 + 财年期列表」，不依赖任何具体数据源（解耦数据源/计算逻辑）
2. 单一 compute 函数 → 两条调用路径输出 byte-for-byte 一致
3. 所有 R1-R5 规则在同一处实现 → 修复一次永久生效
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

# 时间窗口启发式：派息日落在 (fy_end, fy_end + 8 月] 内归属到该期
MAX_PAYOUT_GAP_MONTHS = 8


@dataclass
class DividendRecord:
    """统一派息记录格式（不依赖具体数据源）

    Attributes:
        ex_date: 除净日。pd.NaT 表示「董事会建议但股东大会未通过」（R1 跳过）
        amount_hkd: 折算到 HKD 的金额
        fiscal_year: 财政年度（int）。None 表示无财年信息，走时间窗口启发式
        distribution_type: "年度" / "中期" / "特别" / "其他" / ""
        source: "akshare" / "yfinance" / "hkex_forward"
    """
    ex_date: Optional[pd.Timestamp]
    amount_hkd: float
    fiscal_year: Optional[int] = None
    distribution_type: str = ""
    source: str = ""


def compute_dividend_fields(
    records: List[DividendRecord],
    periods: List[pd.Timestamp],
    fy_anchor: Optional[Tuple[int, int]],
    today: Optional[pd.Timestamp] = None,
    forward_override: Optional[float] = None,
) -> Dict:
    """
    单一权威分红字段计算函数。

    Args:
        records: 派息记录列表（来自 yfinance/AKShare/HKEX 任意混合）
        periods: 财报期截止日列表（年报+中报锚点，已过滤季度期）
        fy_anchor: 年报锚点 (month, day)，例如 (12, 31)。None → 不做 AKShare 财年归属，仅走时间窗口
        today: 今日。None → 取当前日期
        forward_override: HKEX 爬虫得到的预告分红。与 records 去重后注入到 current_fy 的 pending

    Returns:
        {
            "fy_div_per_share":              当前完整财年的 TTM 分红
            "prev_year_div_per_share":       上一完整财年的分红
            "two_years_ago_div_per_share":   再前一财年的分红
            "latest_period_div_per_share":   最近一期财报的分红（可能是中报）
            "latest_period_yoy_div_per_share": 最近期同期上一年的分红
            "forward_div_per_share":         已宣派未除息部分（含 override 增量）
            "current_fy_end":                "YYYY-MM-DD" 或 None
            "prev_fy_end":                   ...
            "latest_period_end":             ...
            "latest_period_yoy_end":         ...
            "latest_attributed_fy":          int 或 None
            "fiscal_periods":                "YYYY-MM-DD,..." 当前财年所有期
        }
    """
    EMPTY = {
        "fy_div_per_share": 0.0,
        "prev_year_div_per_share": 0.0,
        "two_years_ago_div_per_share": 0.0,
        "latest_period_div_per_share": 0.0,
        "latest_period_yoy_div_per_share": 0.0,
        "forward_div_per_share": 0.0,
        "current_fy_end": None,
        "prev_fy_end": None,
        "latest_period_end": None,
        "latest_period_yoy_end": None,
        "latest_attributed_fy": None,
        "fiscal_periods": "",
    }

    if not periods:
        return dict(EMPTY)

    if today is None:
        today = pd.Timestamp.today().normalize()
    else:
        today = pd.Timestamp(today).normalize()

    # 标准化 periods（去重 + 排序：从新到旧，便于"按 fy_end 由新到旧分配"）
    periods = sorted(
        {pd.Timestamp(p).normalize() for p in periods if p is not None},
        reverse=True,
    )

    # ---- 过滤季度期：仅保留年报锚点 + 中报锚点 ----
    # 港股几乎都是年报+中报派息节奏，部分中概回港股有季度期（如 03660）
    # 若把季度期作为归属候选，会导致跨年派息被错误归属
    #
    # B5 修复：用「月份匹配 + 月末容差」替代 (month, day) 严格相等。
    # 旧实现用 2024 闰年算 interim_md，得到 (2,29)，会过滤掉 (2,28) 的非闰年中报期。
    def _is_md_match(p_md: Tuple[int, int], target_md: Tuple[int, int]) -> bool:
        if p_md == target_md:
            return True
        if p_md[0] != target_md[0]:
            return False
        # 月末容差：28/29/30/31 视为同一月末锚点
        return p_md[1] >= 28 and target_md[1] >= 28

    if fy_anchor is not None:
        fy_md = fy_anchor
        try:
            # 用闰年（2024）算出 interim 的标准月日；下面 _is_md_match 处理月末漂移
            _fy_dt = pd.Timestamp(year=2024, month=fy_anchor[0], day=fy_anchor[1])
            _interim_dt = (_fy_dt - pd.DateOffset(months=6)).normalize()
            interim_md = (_interim_dt.month, _interim_dt.day)
        except Exception:
            interim_md = None
        targets_md = [fy_md]
        if interim_md is not None:
            targets_md.append(interim_md)
        filtered = [
            p for p in periods
            if any(_is_md_match((p.month, p.day), t) for t in targets_md)
        ]
        if filtered:
            periods = filtered
    else:
        interim_md = None

    # ---- R1: 过滤 ex_date == NaT ----
    valid_records: List[DividendRecord] = []
    for r in records:
        if r.amount_hkd is None or r.amount_hkd <= 0:
            continue
        if r.ex_date is None or pd.isna(r.ex_date):
            continue  # B2: 未表决预案
        valid_records.append(r)

    # ---- 归属到 periods ----
    period_done: Dict[pd.Timestamp, float] = {p: 0.0 for p in periods}
    period_pending: Dict[pd.Timestamp, float] = {p: 0.0 for p in periods}
    used = [False] * len(valid_records)

    # year -> 该年的年报期（fy_anchor 月日匹配，B5 使用月末容差）
    year_to_period: Dict[int, pd.Timestamp] = {}
    if fy_anchor is not None:
        for p in periods:
            if _is_md_match((p.month, p.day), fy_anchor):
                year_to_period[p.year] = p

    # ---- R2 (B11 重写): 按 distribution_type + ex_date 归属到对应期类型 ----
    # B11 根因：AKShare/东方财富 `fiscal_year` 字段对部分公司语义错位（偏移一年），
    # 直接信任会导致跨财年中报派息被错归到年报期，进而 fy_div 双计 + 中报期 latest_period_div 缺失。
    # 新策略：
    #   - distribution_type 含 "中" → 归到距 ex_date 最近的中报期
    #   - distribution_type 含 "年"/"末"/"特别" → 归到距 ex_date 最近的年报期
    #   - 其他 → 留给 R3 启发式
    # 归属窗口：ex_date 必须落在 [target - 3m, target + MAX_PAYOUT_GAP_MONTHS] 内
    annual_periods = [p for p in periods if _is_md_match((p.month, p.day), fy_anchor)] if fy_anchor else []
    interim_periods = (
        [p for p in periods if interim_md and _is_md_match((p.month, p.day), interim_md)]
        if interim_md else []
    )

    def _classify_distribution(dtype: str) -> str:
        """返回 'interim' / 'annual' / 'unknown'"""
        s = (dtype or "").strip()
        if not s:
            return "unknown"
        # 中期 / 中报 / 中
        if "中" in s:
            return "interim"
        # 年度 / 末期 / 特别派息 / 特别股息（按年报期归属）
        if any(kw in s for kw in ("年", "末", "特别", "特殊")):
            return "annual"
        return "unknown"

    def _pick_nearest_period(target_periods: List[pd.Timestamp],
                              ex_date: pd.Timestamp,
                              max_after_days: int = 240) -> Optional[pd.Timestamp]:
        """从 target_periods 中挑选 ex_date 归属期：
           允许 ex_date 在 p 之前 90 天 ~ p 之后 max_after_days 天范围内。
           优先选 ex_date 之前最近的（典型派息节奏：先有财报期，后派息）。
        """
        if not target_periods:
            return None
        candidates = [
            p for p in target_periods
            if -90 <= (ex_date - p).days <= max_after_days
        ]
        if not candidates:
            return None
        # 取 |ex_date - p| 最小的；平局优先选 ex_date 之前的（更符合派息节奏）
        return min(candidates, key=lambda p: (abs((ex_date - p).days), -(ex_date - p).days))

    def _period_for_fy_kind(fy: int, kind: str) -> Optional[pd.Timestamp]:
        """根据 fiscal_year + 分配类型 定位目标期（不依赖 ex_date 距离）"""
        if not fy or fy_anchor is None:
            return None
        if kind == "annual":
            # 年报期 = fy 年末（如 8 月财年：fy=2025 → 2025-08-31）
            return year_to_period.get(int(fy))
        elif kind == "interim" and interim_md is not None:
            # 中报期 = fy 年报前 6 个月；如 8 月财年 fy=2025 → 2025-02-28
            annual_p = year_to_period.get(int(fy))
            if annual_p is None:
                return None
            for p in interim_periods:
                # 中报在年报前 6 个月：(annual_p - 8月) < p < annual_p
                delta = (annual_p - p).days
                if 150 <= delta <= 210:  # ~6 个月 ± 1 个月
                    return p
            return None
        return None

    # B50: fiscal_year 映射结果必须经 ex_date 裁决。
    #
    # 背景：东财 fiscal_year 对**跨年财年**用「财年开始的自然年」，而
    # year_to_period 按「财年结束年」建索引 → 非 12 月财年天然差一年。
    # 错位程度随财年末月份提前而放大：
    #   01890 财年 8-31：延迟 +97~+335 天，落在合理区间，映射可用
    #   01627 财年 3-31：延迟 +518~+521 天，错位整整一年 → 必须修正
    #
    # 关键业务规则 —— 年度与中期的除息时序约束不同：
    #   annual  ：**只能延后**。年度分红必须等年报出具 + 股东大会通过才能除息，
    #             典型财年结束后 2~5 个月，最长约 12 个月。不可能在财年结束前除息。
    #   interim ：**可以提前**。中期分红常在半年期结束前就宣派除息
    #             （如 01890 的 2024-12-06 属于 2025-02-28 中报期，提前 84 天）。
    #
    # 裁决：在 fy 与 fy+1 两个候选中，先按类型窗口过滤，再选 |延迟| 最小者。
    # 都不合格则返回 None，交由 _pick_nearest_period 按距离归属。
    #
    # 效果验证：
    #   01627 年度 fy=2024 → 2024-03-31(+519 超窗) vs 2025-03-31(+154) → 后者 ✅
    #   01890 年度 fy=2024 → 2024-08-31(+335)      vs 2025-08-31(-30 提前，annual 不允许) → 前者 ✅
    #   01890 中期 fy=2024 → 2024-02-29(+281)      vs 2025-02-28(-84) → 后者（|84|<|281|）✅
    #   01890 中期 fy=2025 → 2025-02-28(+371 超窗) vs 2026-02-28(+6)  → 后者 ✅
    #   02602 中期 fy=2023 → 2023-06-30(+186)      vs 2024-06-30(-180 超 -90 窗) → 前者 ✅
    _FY_DELAY_MAX = 365
    _FY_DELAY_MIN_ANNUAL = 0      # 年度/末期分红不可能在财年结束前除息（需等年报+股东大会）
    # B50-r2: 中期下界不能设得太宽。中报期间隔仅 ~182 天，若下界取 -180，
    # 相邻两个中报期几乎必然同时落入窗口，而「提前」那个 |delay| 反而更小 → 错归。
    # 反例：02602 2024-01-02 中期 fy=2023，2023-06-30(+186) vs 2024-06-30(-180)，
    # -180 窗下会选中后者 → prev_year 从 2.079 虚高到 2.4251。
    # 实测 01890 的最大提前量是 -84 天，与 _pick_nearest_period 既有的 -90 容差一致。
    _FY_DELAY_MIN_INTERIM = -90
    # 特别分配不依赖年报节奏，可随时宣派，但 _classify_distribution 将其归为 annual。
    # 若用 annual 的 [0,365) 硬边界会误踢提前除息的特别分配。
    _FY_DELAY_MIN_SPECIAL = -90

    def _is_special(dtype: str) -> bool:
        s = (dtype or "").strip()
        return ("特别" in s) or ("特殊" in s)

    def _period_for_fy_validated(fy, kind: str, ex_date: pd.Timestamp,
                                  dtype: str = "") -> Optional[pd.Timestamp]:
        if not fy:
            return None
        try:
            fy_int = int(fy)
        except (TypeError, ValueError):
            return None
        if kind == "interim":
            delay_min = _FY_DELAY_MIN_INTERIM
        elif _is_special(dtype):
            delay_min = _FY_DELAY_MIN_SPECIAL
        else:
            delay_min = _FY_DELAY_MIN_ANNUAL
        best = None
        best_abs = None
        for cand_fy in (fy_int, fy_int + 1):
            p = _period_for_fy_kind(cand_fy, kind)
            if p is None:
                continue
            delay = (ex_date - p).days
            if not (delay_min <= delay < _FY_DELAY_MAX):
                continue
            if best_abs is None or abs(delay) < best_abs:
                best, best_abs = p, abs(delay)
        return best

    for i, rec in enumerate(valid_records):
        ex_date = pd.Timestamp(rec.ex_date).normalize()
        kind = _classify_distribution(rec.distribution_type)

        # B30 修复：AKShare 将"春季除息的末期股息"错标为"中期分配"。
        # 典型模式：12-31 年报后 1-5 个月内除息 → 实为上一年的末期股息，而非新一年的中期。
        # 判定：kind=="interim" 且 ex_date.month ∈ {3,4,5}，且距最近 annual_period 0~150 天 → 改归 annual
        if kind == "interim" and ex_date.month in (3, 4, 5) and annual_periods:
            nearest_annual = _pick_nearest_period(annual_periods, ex_date, max_after_days=150)
            if nearest_annual is not None and 0 < (ex_date - nearest_annual).days <= 150:
                kind = "annual"

        if kind == "interim" and interim_periods:
            # B39-r8: 优先用 fiscal_year 定位（避免 8 月财年 fy2025 中期延迟 1 年派息时被错归到 fy2026）
            # B50: 映射结果需经 ex_date 延迟裁决（见上方 _period_for_fy_validated）
            target_period = _period_for_fy_validated(rec.fiscal_year, "interim", ex_date,
                                                      rec.distribution_type)
            if target_period is None:
                # 中报派息典型 0~8 月内，窗口 8 月
                target_period = _pick_nearest_period(interim_periods, ex_date, max_after_days=240)
        elif kind == "annual" and annual_periods:
            # B39-r8: 优先用 fiscal_year 定位
            # B50: 同上，需经 ex_date 延迟裁决
            target_period = _period_for_fy_validated(rec.fiscal_year, "annual", ex_date,
                                                      rec.distribution_type)
            if target_period is None:
                # 年度派息可能延迟到 12 个月后（如 09616 case），但窗口不能超过 12 月
                target_period = _pick_nearest_period(annual_periods, ex_date, max_after_days=365)
        else:
            target_period = None  # R3 兜底

        if target_period is None:
            continue

        is_done = ex_date <= today
        if is_done:
            period_done[target_period] += rec.amount_hkd
        else:
            period_pending[target_period] += rec.amount_hkd
        used[i] = True

    # ---- R3: 兜底窗口启发式（仅处理未归属的）----
    for fy_end in periods:  # 从新到旧
        window_end = fy_end + pd.DateOffset(months=MAX_PAYOUT_GAP_MONTHS)
        for i, rec in enumerate(valid_records):
            if used[i]:
                continue
            ex_date = pd.Timestamp(rec.ex_date).normalize()
            if fy_end < ex_date <= window_end:
                is_done = ex_date <= today
                if is_done:
                    period_done[fy_end] += rec.amount_hkd
                else:
                    period_pending[fy_end] += rec.amount_hkd
                used[i] = True

    def period_sum(p):
        return period_done.get(p, 0.0) + period_pending.get(p, 0.0)

    # ---- 识别当前/上一/再上一完整财年（B5 使用月末容差）----
    if fy_anchor is not None:
        year_anchors = sorted(
            [p for p in periods
             if _is_md_match((p.month, p.day), fy_anchor) and p <= today],
            reverse=True,
        )
    else:
        year_anchors = []

    if year_anchors:
        current_fy_year_end = year_anchors[0]
        prev_fy_year_end = year_anchors[1] if len(year_anchors) > 1 else None
        two_yrs_ago_fy_year_end = year_anchors[2] if len(year_anchors) > 2 else None
    else:
        current_fy_year_end = next((p for p in periods if p <= today), periods[0])
        prev_fy_year_end = None
        two_yrs_ago_fy_year_end = None

    def fy_periods_for(year_end):
        """返回 (year_end - 1年, year_end] 内的所有财报期"""
        if year_end is None:
            return []
        one_yr_ago = year_end - pd.DateOffset(years=1)
        return [p for p in periods if one_yr_ago < p <= year_end]

    curr_fy_periods = fy_periods_for(current_fy_year_end)
    prev_fy_periods = fy_periods_for(prev_fy_year_end)
    two_yrs_ago_fy_periods = fy_periods_for(two_yrs_ago_fy_year_end)

    # ---- B17: TTM 分红 = 最新两个半年期相加（半年期归属，方案 A） ----
    # periods 已过滤为 [年报+中报]，按时间从新到旧排序
    past_periods = [p for p in periods if p <= today]

    # B38 修复：新滚入的 **中报期** 归属分红=0 时，视为「派息尚未除息」而非「不派息」，
    # 从 TTM 窗口跳过该期，避免 6→7 月中报期越过 today 导致 fy_div 假腰斩。
    #
    # 注意：**年报期**归属=0 是港股常态（分红要下一年才除息），不做 shift。
    #
    # 判定条件（要同时满足才跳过）：
    #   1) 有已识别的 interim_md（中报锚点已知）
    #   2) past_periods 至少 3 期（后面还有真数据）
    #   3) 最新期 past_periods[0] 是中报期（月日匹配 interim_md）
    #   4) 该中报期归属分红 = 0
    #   5) 最新期为「刚过 today 不久」的期（≤ 90 天）——防止真无派息误判
    #   6) 存在同类型历史中报期且派息>0——防止误伤本来就零派息公司
    shift = 0
    if (
        interim_md is not None
        and len(past_periods) >= 3
        and _is_md_match((past_periods[0].month, past_periods[0].day), interim_md)
        and period_sum(past_periods[0]) == 0.0
    ):
        p0 = past_periods[0]
        days_since = (today - p0).days
        if 0 <= days_since <= 90:
            historical_interim = [
                p for p in past_periods[1:]
                if _is_md_match((p.month, p.day), interim_md)
            ]
            if historical_interim and any(period_sum(p) > 0 for p in historical_interim):
                shift = 1
                logger.debug(f"TTM 窗口跳过中报期 {p0}（归属=0，视为派息尚未除息）")

    def _slice_sum(offset: int, count: int) -> float:
        """从 past_periods[shift+offset] 开始取 count 期求和；不足时取到末尾。"""
        start = shift + offset
        end = min(start + count, len(past_periods))
        if start >= len(past_periods):
            return 0.0
        return sum(period_sum(past_periods[i]) for i in range(start, end))

    ttm_div = _slice_sum(0, 2)
    prev_year_div = _slice_sum(2, 2)
    two_yrs_ago_div = _slice_sum(4, 2)

    # ---- 最近一期财报（任意期，可能是中报） ----
    # B38：同样跳过头部无派息的"财报期到但未除息"期
    if shift < len(past_periods):
        latest_period = past_periods[shift]
    else:
        latest_period = None
    if latest_period is not None:
        latest_period_div = period_sum(latest_period)
        latest_period_yoy_end = latest_period - pd.DateOffset(years=1)
        latest_period_yoy_div = period_sum(latest_period_yoy_end)
    else:
        latest_period_div = 0.0
        latest_period_yoy_end = None
        latest_period_yoy_div = 0.0

    # ---- forward_div = 当前财年未除息 + 未来财年期 pending ----
    # R5: ex_date > today → pending
    future_periods = [p for p in periods if p > today]
    forward_from_curr_fy = sum(period_pending.get(p, 0.0) for p in curr_fy_periods)
    forward_from_future = sum(period_pending.get(p, 0.0) for p in future_periods)
    forward_div = forward_from_curr_fy + forward_from_future

    # ---- R4: forward_override 注入（B4 修复）----
    # 比较 override 与「当前财年总额（done+pending）」：
    # - override > existing_total → AKShare 缺这笔，补差到 pending
    # - override <= existing_total → AKShare 已收录，不重复注入
    if (
        forward_override is not None
        and forward_override > 0
        and current_fy_year_end is not None
    ):
        override_amt = float(forward_override)
        existing_done = period_done.get(current_fy_year_end, 0.0)
        existing_pending = period_pending.get(current_fy_year_end, 0.0)
        existing_total = existing_done + existing_pending
        if override_amt > existing_total:
            delta = override_amt - existing_total
            period_pending[current_fy_year_end] = existing_pending + delta
            # B17: 重算 TTM = 最新两期相加（保持新口径 + B38 shift）
            ttm_div = _slice_sum(0, 2)
            prev_year_div = _slice_sum(2, 2)
            if latest_period is not None:
                latest_period_div = period_sum(latest_period)
                if latest_period_yoy_end is not None:
                    latest_period_yoy_div = period_sum(latest_period_yoy_end)
            forward_from_curr_fy = sum(period_pending.get(p, 0.0) for p in curr_fy_periods)
            forward_div = forward_from_curr_fy + forward_from_future
        forward_div = max(forward_div, override_amt)

    return {
        "fy_div_per_share": round(ttm_div, 6),
        "prev_year_div_per_share": round(prev_year_div, 6),
        "two_years_ago_div_per_share": round(two_yrs_ago_div, 6),
        "latest_period_div_per_share": round(latest_period_div, 6),
        "latest_period_yoy_div_per_share": round(latest_period_yoy_div, 6),
        "forward_div_per_share": round(forward_div, 6),
        "current_fy_end": current_fy_year_end.strftime("%Y-%m-%d") if current_fy_year_end is not None else None,
        "prev_fy_end": prev_fy_year_end.strftime("%Y-%m-%d") if prev_fy_year_end is not None else None,
        "latest_period_end": latest_period.strftime("%Y-%m-%d") if latest_period is not None else None,
        "latest_period_yoy_end": latest_period_yoy_end.strftime("%Y-%m-%d") if latest_period_yoy_end is not None else None,
        "latest_attributed_fy": current_fy_year_end.year if current_fy_year_end is not None else None,
        "fiscal_periods": ",".join(p.strftime("%Y-%m-%d") for p in curr_fy_periods),
    }
