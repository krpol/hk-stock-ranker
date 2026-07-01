"""
港股财务数据采集：分红、回购、有息负债率
来源：
  - 分红历史：yfinance（除息日精确）
  - 回购金额：yfinance cashflow（RepurchaseOfCapitalStock）
  - 有息负债率：yfinance balance_sheet
  - 预分红（已公告未除息）：yfinance actions / info.forward_dividend
"""
import os
import time
import random
import logging
from typing import Optional, Tuple
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

from config.settings import YFINANCE_TIMEOUT, YFINANCE_MAX_RETRIES, AKSHARE_SLEEP

logger = logging.getLogger(__name__)


# B16: 子维度→所属字段映射（用于按 domain 精准更新 row 字段）
DOMAIN_FIELDS: dict = {
    "dividend": [
        "fy_div_per_share", "prev_year_div_per_share", "two_years_ago_div_per_share",
        "latest_period_div_per_share", "latest_period_yoy_div_per_share",
        "forward_div_per_share", "current_fy_end", "prev_fy_end",
        "latest_period_end", "latest_period_yoy_end", "latest_attributed_fy",
        "fiscal_periods", "forward_div_source",
    ],
    "buyback": [
        "total_buyback", "shares_outstanding", "buyback_per_share",
        "prev_buyback_per_share", "prev_total_buyback", "prev_shares_outstanding",
        "splits_ratio_in_window", "splits_ratio_calculated",
    ],
    "debt": ["net_debt_ratio", "total_liability_ratio", "debt_to_equity", "roe", "cash_and_equivalents", "interest_bearing_debt",
             "ocf_3y_avg", "capex_3y_avg", "dep_amortization_3y_avg", "current_lease_obligation_prev", "equity_latest",
             "capex_ttm", "da_ttm", "current_lease_obligation_ttm", "prev_capex", "prev_da",
             "fcf_as_of", "debt_as_of"],
    "dilution": ["annual_dilution_rate"],
    "growth": [
        "ttm_revenue", "prev_ttm_revenue", "ttm_revenue_t2", "revenue_yoy",
        "ttm_operating_income", "prev_ttm_operating_income", "ttm_operating_income_t2", "operating_income_yoy",
        "ttm_operating_cashflow", "prev_ttm_operating_cashflow", "ttm_operating_cashflow_t2", "operating_cashflow_yoy",
    ],
}
ALL_DOMAINS = list(DOMAIN_FIELDS.keys())


# A+H 双重上市港股名单（港股代码）
# yfinance 对这些股票的股份数数据在 A+H 总股本和仅 H 股之间切换，
# 导致 pct_change 出现虚假的大幅波动，不可用于合股/增发检测。
AH_DUAL_LISTED_STOCKS: set[str] = {
    "00168", "00177", "00317", "00338", "00347", "00358", "00386", "00390",
    "00525", "00548", "00564", "00568", "00588", "00598", "00670", "00719",
    "00728", "00753", "00763", "00788", "00811", "00857", "00874", "00895",
    "00902", "00914", "00916", "00921", "00939", "00991", "00995", "00998",
    "01033", "01053", "01057", "01065", "01071", "01072", "01088", "01108",
    "01138", "01157", "01171", "01186", "01211", "01288", "01330", "01336",
    "01339", "01349", "01375", "01385", "01398", "01456", "01476", "01513",
    "01528", "01578", "01618", "01635", "01766", "01776", "01787", "01799",
    "01812", "01816", "01898", "01919", "01958", "02009", "02016", "02039",
    "02068", "02196", "02202", "02208", "02218", "02238", "02318", "02328",
    "02333", "02338", "02357", "02607", "02611", "02628", "02727", "02866",
    "02880", "02883", "02899", "03328", "03369", "03399", "03618", "03678",
    "03898", "03948", "03958", "03968", "03969", "03988", "06030", "06066",
    "06099", "06178", "06196", "06806", "06818", "06837", "06869", "06886",
    "08115",
}

# 美股 + 港股双重上市豁免（yfinance 在两个市场股本口径间切换，造成虚假股本跳增）
US_HK_DUAL_LISTED_STOCKS: set[str] = {
    "06686",  # 诺亚控股 NOAH (美股私有化 + 港股二次上市，2023-10 股本 10× 跳增为市场切换而非稀释)
}

# 合集：所有双重上市豁免名单
DUAL_LISTED_EXEMPT: set[str] = AH_DUAL_LISTED_STOCKS | US_HK_DUAL_LISTED_STOCKS


def _hk_ticker(code: str) -> str:
    """
    将5位港股代码转为 Yahoo Finance 格式。
    规则：去掉首位数字（5位→4位），如 00700 → 0700.HK，09988 → 9988.HK
    yfinance 使用的 HK ticker 为 int(code) 左补零到至少4位。
    """
    num = int(code)
    # 4位补齐（港股主板代码最多4位有效数字）
    return f"{num:04d}.HK"


def _get_ticker(code: str) -> yf.Ticker:
    return yf.Ticker(_hk_ticker(code))


def get_fiscal_year_dates(code: str) -> Tuple[Optional[datetime], Optional[datetime]]:
    """
    获取最新财报日期（current_fy_end）和上一年财报日期（prev_fy_end）。

    yfinance 不同接口的数据更新进度不一致（income_stmt 经常滞后于 balance_sheet
    和 info.lastFiscalYearEnd），如果只用 income_stmt 会导致 current_fy_end
    被识别为一年前的财年，分红被严重高估。

    策略：从 income_stmt / balance_sheet / cashflow / info.lastFiscalYearEnd
    四个数据源中取最大日期作为 current_fy_end，prev_fy_end = current - 1 year。

    Returns:
        (prev_fy_end, current_fy_end)  均为 datetime 或 None
    """
    try:
        t = _get_ticker(code)
        candidates = []

        # 三大财报表
        for src_name, getter in [
            ("income_stmt", lambda: t.income_stmt),
            ("balance_sheet", lambda: t.balance_sheet),
            ("cashflow", lambda: t.cashflow),
        ]:
            try:
                df = getter()
                if df is not None and not df.empty:
                    candidates.append(pd.Timestamp(max(df.columns)))
            except Exception:
                continue

        # info.lastFiscalYearEnd
        try:
            info = t.info
            ts = info.get("lastFiscalYearEnd")
            if ts:
                candidates.append(pd.to_datetime(ts, unit="s"))
        except Exception:
            pass

        if not candidates:
            return None, None

        current_fy_end = max(candidates)
        prev_fy_end = current_fy_end - pd.DateOffset(years=1)
        return prev_fy_end, current_fy_end
    except Exception as e:
        logger.warning(f"[{code}] 获取财报日期失败: {e}")
        return None, None


def has_dividend_history(code: str, years: int = 3, min_years_with_div: int = 2,
                         as_of_date: datetime = None) -> bool:
    """
    检查股票在过去 N 年内是否至少有 min_years_with_div 个不同年份有分红记录。

    例如默认参数：过去3年中至少2年有分红，才返回 True。

    使用 t.history() 获取历史行情+分红动作，避免 t.dividends 在
    yfinance 1.2.x 中对部分 ticker 触发 'Period max is invalid' 的 bug。

    Args:
        as_of_date: 基准日期，默认为今天。用于历史回填快照（如 datetime(2022, 12, 31)
                    表示在 2022 年底视角下判断该股是否合格）。
    """
    end_date = as_of_date or datetime.now()
    cutoff_date = end_date - timedelta(days=years * 365)

    max_retries = 5
    for attempt in range(max_retries):
        try:
            t = _get_ticker(code)
            # 历史回填使用 [cutoff, end] 有界 range；当前调用使用 N+1 年节省流量
            if as_of_date is not None:
                hist = t.history(
                    start=cutoff_date.strftime("%Y-%m-%d"),
                    end=(end_date + timedelta(days=1)).strftime("%Y-%m-%d"),
                    actions=True,
                )
            else:
                hist = t.history(period=f"{years + 1}y", actions=True)

            if hist is None or hist.empty or "Dividends" not in hist.columns:
                time.sleep(random.uniform(1.0, 2.5))
                return False

            divs = hist["Dividends"]
            divs = divs[divs > 0]

            if divs.empty:
                time.sleep(random.uniform(1.0, 2.5))
                return False

            # 移除时区信息以便比较
            if divs.index.tz is not None:
                divs.index = divs.index.tz_localize(None)

            # 在 [cutoff_date, end_date] 区间内统计
            recent_divs = divs[(divs.index >= cutoff_date) & (divs.index <= end_date)]

            # 统计有分红的不同年份数量
            years_with_div = recent_divs.index.year.nunique()
            time.sleep(random.uniform(1.0, 2.5))
            return years_with_div >= min_years_with_div

        except Exception as e:
            err_str = str(e)
            if "Too Many Requests" in err_str or "Rate limit" in err_str.lower():
                wait = 30 + random.uniform(10, 20) * (attempt + 1)
                logger.warning(f"[{code}] Yahoo Finance 限速，等待 {wait:.0f}s 后重试 ({attempt+1}/{max_retries})...")
                time.sleep(wait)
            elif any(kw in err_str for kw in ("Could not resolve", "timed out", "Connection aborted",
                                               "RemoteDisconnected", "Failed to perform",
                                               "ConnectionError", "curl: (6)", "curl: (28)", "curl: (7)")):
                # 网络类错误：等待后重试，不直接放弃
                wait = 15 + random.uniform(5, 15) * (attempt + 1)
                logger.warning(f"[{code}] 网络异常，等待 {wait:.0f}s 后重试 ({attempt+1}/{max_retries}): {err_str[:80]}")
                time.sleep(wait)
            else:
                # 数据类错误（股票已退市/无数据等）：直接跳过，不重试
                logger.warning(f"[{code}] 检查分红历史失败: {e}")
                time.sleep(random.uniform(1.0, 2.0))
                return False

    logger.warning(f"[{code}] 检查分红历史：达到最大重试次数，返回 False")
    return False


def has_recent_dividend_or_forward(code: str, months: int = 12,
                                   as_of_date: datetime = None) -> bool:
    """
    检查股票是否有近期实际分红或预分red公告。

    用于 rebuild-pool 的 fallback 逻辑：
    即使不满足"3年2年分红"条件，只要满足以下任一条件也纳入股票池：
    1. 近 N 个月内有除息日
    2. 已有预分红（forward dividend）公告记录

    Args:
        as_of_date: 基准日期，默认为今天。
    """
    end_date = as_of_date or datetime.now()
    cut_date = end_date - timedelta(days=months * 30)

    try:
        t = _get_ticker(code)
        # 历史回填用有界 range；当前调用用窗口
        if as_of_date is not None:
            hist = t.history(
                start=cut_date.strftime("%Y-%m-%d"),
                end=(end_date + timedelta(days=1)).strftime("%Y-%m-%d"),
                actions=True,
            )
        else:
            hist = t.history(period=f"{max(months // 12, 1)}y", actions=True)
        if hist is not None and not hist.empty and "Dividends" in hist.columns:
            divs = hist["Dividends"]
            divs = divs[divs > 0]
            if not divs.empty:
                if divs.index.tz is not None:
                    divs.index = divs.index.tz_localize(None)
                # 区间 [cut_date, end_date]
                recent = divs[(divs.index >= cut_date) & (divs.index <= end_date)]
                if not recent.empty:
                    return True
    except Exception:
        pass

    # 检查2: forward dividend CSV（仅在当前视角下生效，历史回填时跳过）
    if as_of_date is None:
        try:
            from collectors.hk_forward_dividend import FORWARD_DIV_CSV
            if os.path.exists(FORWARD_DIV_CSV):
                fwd = pd.read_csv(FORWARD_DIV_CSV, dtype={"code": str})
                fwd["code"] = fwd["code"].astype(str).str.zfill(5)
                if code in fwd["code"].values:
                    return True
        except Exception:
            pass

    return False


def has_share_dilution(code: str, years: int = 3,
                       dilution_threshold: float = 0.08,
                       consolidation_threshold: float = 0.50,
                       as_of_date: datetime = None) -> tuple[bool, float]:
    """
    检测过去 N 年内是否存在合股、配售、增发等稀释/缩减行为。

    判断依据：历史股份数（get_shares_full）的年度变化幅度。
    使用非对称阈值区分不同行为：
    - 增发/配售（股份数增加）：年度增幅中位数 >= dilution_threshold（默认 8%）
    - 合股（股份数减少）：年度降幅中位数 >= 50%

    注意：
    1. A+H 双重上市股票直接豁免（yfinance 数据在 A+H 口径间切换，不可靠）
    2. 排除最近 3 个月数据（避免 yfinance 数据口径切换的尾部异常）
    3. 使用中位数替代最大值，过滤单点数据跳变

    Args:
        as_of_date: 基准日期，默认为今天。用于历史回填快照。

    Returns:
        (should_filter, max_decrease_pct)
        - should_filter: True=应从股票池剔除, False=保留
        - max_decrease_pct: 最大年度股份数降幅（正数，如0.15代表减少15%）；
          调用方可通过 0.10 < max_decrease_pct < 0.30 识别需人工验证的灰色股票
    """
    # 双重上市股票豁免（A+H 或 美股+港股）：yfinance 股份数口径不可靠
    if code in DUAL_LISTED_EXEMPT:
        logger.debug(f"[{code}] 双重上市，跳过股份稀释检测")
        return (False, 0.0)

    end_date = as_of_date or datetime.now()
    start_year = end_date.year - years

    max_retries = 3
    for attempt in range(max_retries):
        try:
            t = _get_ticker(code)
            if as_of_date is not None:
                shares = t.get_shares_full(
                    start=f"{start_year}-01-01",
                    end=end_date.strftime("%Y-%m-%d"),
                )
            else:
                shares = t.get_shares_full(start=f"{start_year}-01-01")

            if shares is None or len(shares) < 2:
                time.sleep(random.uniform(0.5, 1.5))
                return (False, 0.0)  # 数据不足：保守不剔除

            shares = shares.sort_index()
            # 移除时区信息（如有）
            try:
                if shares.index.tz is not None:
                    shares.index = shares.index.tz_localize(None)
            except Exception:
                pass
            # 历史快照模式：明确截断到 end_date
            if as_of_date is not None:
                shares = shares[shares.index <= pd.Timestamp(end_date)]
            # 取月末股份数，至少需要 13 个月才能算年度同比
            monthly = shares.resample("ME").last().dropna()
            if len(monthly) < 13:
                time.sleep(random.uniform(0.5, 1.5))
                return (False, 0.0)

            pct_change = monthly.pct_change(12).dropna()
            if pct_change.empty:
                time.sleep(random.uniform(0.5, 1.5))
                return (False, 0.0)

            # 排除最近 3 个月数据，避免 yfinance 数据口径切换导致的尾部异常
            recent_cutoff = monthly.index[-1] - pd.DateOffset(months=3)
            stable_pct = pct_change[pct_change.index < recent_cutoff]
            if stable_pct.empty:
                # 数据太短，保守不剔除
                time.sleep(random.uniform(0.5, 1.5))
                return (False, 0.0)

            # B10 修复：先用 EPSILON 把微抖动归零，避免 0.0001 级别的浮点噪声计入
            #          再要求 up_vals / down_vals 至少 2 个同向样本才采信中位数判定，
            #          否则单点抖动 N=1 时 median 等于该点自身、完全没起到过滤作用（01245 NIRAKU 案例）
            EPSILON = 0.005
            MIN_SAMPLES = 2
            stable_pct = stable_pct[stable_pct.abs() >= EPSILON]

            up_vals = stable_pct[stable_pct > 0]
            down_vals = stable_pct[stable_pct < 0]

            median_up = float(up_vals.median()) if len(up_vals) >= MIN_SAMPLES else 0.0
            median_down = float(down_vals.median()) if len(down_vals) >= MIN_SAMPLES else 0.0

            # 非对称判断：增发用低阈值，合股用高阈值
            has_dilution = median_up >= dilution_threshold          # 增发/配售
            has_consolidation = -median_down >= consolidation_threshold  # 合股（median_down 为负）

            should_filter = has_dilution or has_consolidation
            # 灰色地带检测仍用原始最大值（保留向后兼容）
            max_down = float(pct_change.min()) if not pct_change.empty else 0.0
            max_decrease_pct = float(-max_down) if max_down < 0 else 0.0

            time.sleep(random.uniform(0.5, 1.5))
            return (should_filter, max_decrease_pct)

        except Exception as e:
            err_str = str(e)
            if any(kw in err_str for kw in ("Could not resolve", "timed out", "Connection aborted",
                                             "RemoteDisconnected", "Failed to perform",
                                             "curl: (6)", "curl: (28)", "curl: (7)")):
                wait = 15 + random.uniform(5, 10) * (attempt + 1)
                logger.warning(f"[{code}] 股份数网络异常，等待 {wait:.0f}s 后重试 ({attempt+1}/{max_retries}): {err_str[:60]}")
                time.sleep(wait)
            else:
                logger.debug(f"[{code}] 股份数检查跳过: {err_str[:80]}")
                time.sleep(random.uniform(0.5, 1.0))
                return (False, 0.0)  # 无数据则保守不剔除

    logger.warning(f"[{code}] 股份数检查达到最大重试次数，保守返回 False")
    return (False, 0.0)


def _bs_shares_history(t: yf.Ticker) -> pd.Series:
    """从 balance_sheet（年报）拿历史已发行股本，按日期升序返回 Series。

    优先字段：Ordinary Shares Number > Share Issued > Common Stock。
    yfinance balance_sheet 通常含 4 列（最近 4 个年报截止日）。
    """
    try:
        bs = t.balance_sheet
        if bs is None or bs.empty:
            return pd.Series(dtype=float)
        for key in ["Ordinary Shares Number", "Share Issued", "Common Stock"]:
            if key in bs.index:
                row = bs.loc[key].dropna()
                if row.empty:
                    continue
                pairs = [(pd.Timestamp(c), float(v)) for c, v in row.items()
                         if pd.notna(v) and float(v) > 0]
                if not pairs:
                    continue
                idx, vals = zip(*pairs)
                return pd.Series(vals, index=idx, dtype=float).sort_index()
        return pd.Series(dtype=float)
    except Exception:
        return pd.Series(dtype=float)


def get_annual_dilution_rate(code: str, years: int = 3) -> float:
    """获取最近 N 年内年化股份增发率（稀释比例），用于排名公式减去稀释影响。

    [B12-A1] 数据源从 yfinance get_shares_full() 月度时序改为 balance_sheet
    年报快照，规避月度噪音（01245 NIRAKU 因 2024-08 月末虚高被错算 8.20%）。

    算法：
      1. A+H 双重上市豁免
      2. 取 balance_sheet 年报快照（按日期升序）
      3. 首尾两个快照（跨度 >= 0.9 年）算 CAGR = (last/first)**(1/n) - 1
      4. CAGR <= 0（净回购/合股）返回 0
      5. balance_sheet < 2 行则回退 get_shares_full + B10 噪音过滤
    """
    if code in DUAL_LISTED_EXEMPT:
        return 0.0

    max_retries = 3
    for attempt in range(max_retries):
        try:
            t = _get_ticker(code)

            # === 主路径：balance_sheet CAGR ===
            bs_shares = _bs_shares_history(t)
            if len(bs_shares) >= 2:
                first_ts, last_ts = bs_shares.index[0], bs_shares.index[-1]
                span_years = (last_ts - first_ts).days / 365.25
                if span_years >= 0.9:
                    first_v = float(bs_shares.iloc[0])
                    last_v = float(bs_shares.iloc[-1])
                    if first_v > 0 and last_v > 0:
                        ratio = last_v / first_v
                        if ratio > 1.0:
                            cagr = ratio ** (1.0 / span_years) - 1.0
                            return float(round(cagr, 6))
                        return 0.0

            # === Fallback：get_shares_full + B10 噪音过滤 ===
            start_year = datetime.now().year - years
            shares = t.get_shares_full(start=f"{start_year}-01-01")
            if shares is None or len(shares) < 2:
                return 0.0
            shares = shares.sort_index()
            try:
                if shares.index.tz is not None:
                    shares.index = shares.index.tz_localize(None)
            except Exception:
                pass
            monthly = shares.resample("ME").last().dropna()
            if len(monthly) < 13:
                return 0.0
            pct_change = monthly.pct_change(12).dropna()
            if pct_change.empty:
                return 0.0
            recent_cutoff = monthly.index[-1] - pd.DateOffset(months=3)
            stable_pct = pct_change[pct_change.index < recent_cutoff]
            if stable_pct.empty:
                return 0.0
            EPSILON = 0.005
            MIN_SAMPLES = 2
            stable_pct = stable_pct[stable_pct.abs() >= EPSILON]
            up_vals = stable_pct[stable_pct > 0]
            if len(up_vals) < MIN_SAMPLES:
                return 0.0
            median_up = float(up_vals.median())
            return median_up if median_up > 0 else 0.0
        except Exception:
            time.sleep(random.uniform(0.5, 1.5))
    return 0.0


def _attribute_dividend_to_fy(ex_date: pd.Timestamp, fy_end_month: int = 12) -> int:
    """
    [LEGACY] 半年法则归属财年（保留作为 fallback）。
    新算法优先使用 _get_fiscal_period_ends() 拿到真实财报截止日。

    港股股息归属规则（年报截止日为 12-31 的标准情形）：
      - 除息日落在 1-6 月  → 归属【上一财年】的【末期股息】
      - 除息日落在 7-12 月 → 归属【当年财年】的【中期股息】
    """
    if fy_end_month == 12:
        return ex_date.year - 1 if ex_date.month <= 6 else ex_date.year
    return ex_date.year if ex_date.month > fy_end_month else ex_date.year - 1


def _get_fiscal_period_ends(code: str):
    """获取该股票所有财报截止日 + 财年锚点。

    数据源：balance_sheet/income_stmt/cashflow（年度+季度）+ info
    并通过推断【财年锚点】（最常出现 month-day）+ 6 个月间隔补齐缺失期。

    Returns:
        (periods_desc, fy_anchor)
          periods_desc: 按日期倒序排列的 Timestamp 列表
          fy_anchor:    (month, day) 年报锚点；无数据时为 None
    """
    try:
        t = _get_ticker(code)
        raw_periods = set()

        for getter in [
            lambda: t.balance_sheet,
            lambda: t.quarterly_balance_sheet,
            lambda: t.income_stmt,
            lambda: t.quarterly_income_stmt,
            lambda: t.cashflow,
            lambda: t.quarterly_cashflow,
        ]:
            try:
                df = getter()
                if df is not None and not df.empty:
                    for c in df.columns:
                        try:
                            raw_periods.add(pd.Timestamp(c).normalize())
                        except Exception:
                            continue
            except Exception:
                continue

        try:
            info = t.info
            for key in ("lastFiscalYearEnd", "mostRecentQuarter"):
                ts = info.get(key)
                if ts:
                    try:
                        raw_periods.add(pd.to_datetime(ts, unit="s").normalize())
                    except Exception:
                        continue
        except Exception:
            pass

        if not raw_periods:
            return [], None

        # 推断年报锚点 = 最高频 (month, day)
        from collections import Counter
        md_counter = Counter((p.month, p.day) for p in raw_periods)
        fy_anchor = md_counter.most_common(1)[0][0]

        # 以年报锚点 + 6 个月间隔补齐
        today = pd.Timestamp(datetime.now().date())
        years = [p.year for p in raw_periods]
        min_year = min(years) - 1
        max_year = max(today.year + 1, max(years) + 1)
        cutoff_future = today + pd.DateOffset(months=18)

        completed = set(raw_periods)
        for y in range(min_year, max_year + 1):
            try:
                annual = pd.Timestamp(year=y, month=fy_anchor[0], day=fy_anchor[1]).normalize()
                if annual <= cutoff_future:
                    completed.add(annual)
                interim = (annual - pd.DateOffset(months=6)).normalize()
                if interim <= cutoff_future:
                    completed.add(interim)
            except Exception:
                continue

        # B30: 去重相邻日期（差 ≤ 5 天视为同一期，保留更晚那个）
        # 例 01161 奥思 fy_anchor=(9,30)，半年 = 3-30；但实际 yfinance 报 3-31，导致同一期重复入选
        sorted_asc = sorted(completed)
        deduped = []
        for p in sorted_asc:
            if deduped and (p - deduped[-1]).days <= 5:
                deduped[-1] = p  # 保留更晚的（一般是真实报告日）
            else:
                deduped.append(p)
        return sorted(deduped, reverse=True), fy_anchor
    except Exception as e:
        logger.warning(f"[{code}] 获取财报截止日失败: {e}")
        return [], None


def get_fiscal_year_dividends(code: str, forward_div_override: Optional[float] = None) -> dict:
    """
    获取最近 1 年内已披露的财报对应的分红 + 预分红（按真实财报截止日精确归属）。

    【C1 重构后】本函数改为薄包装器：
    1. 拉 yfinance + AKShare 两源原始派息记录
    2. 转换为统一 DividendRecord 列表
    3. 调用 engine.dividend_attribution.compute_dividend_fields 完成所有 R1~R5 规则

    Args:
        code: 港股代码
        forward_div_override: HKEX 爬虫得到的预告分红（更准确）。

    Returns:
        见 compute_dividend_fields 返回值。失败返回 EMPTY。
    """
    from engine.dividend_attribution import DividendRecord, compute_dividend_fields

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

    try:
        t = _get_ticker(code)
        today = pd.Timestamp(datetime.now().date())

        # ---- Step 1: 拿到所有财报截止日 + 年报锚点 ----
        periods, fy_anchor = _get_fiscal_period_ends(code)

        # 兼容回落
        if not periods:
            prev_fy_end, current_fy_end = get_fiscal_year_dates(code)
            if current_fy_end is not None:
                periods = [pd.Timestamp(current_fy_end).normalize()]
                if prev_fy_end is not None:
                    periods.append(pd.Timestamp(prev_fy_end).normalize())
                fy_anchor = (periods[0].month, periods[0].day)
            if not periods:
                # B16: yfinance 没返回任何财报截止日，多半是网络/限流 → 抛 fail
                raise RuntimeError("no fiscal periods found (likely yfinance unavailable)")

        # ---- Step 2: 收集派息记录（yfinance + AKShare 两源融合）----
        records: list = []

        # Step 2a: AKShare 派息表（含财政年度精确归属）
        ak_seen_dates: set = set()
        try:
            from collectors.hk_dividend_history import get_hk_dividends_with_fy_attribution
            ak_records = get_hk_dividends_with_fy_attribution(code)
        except Exception as e:
            logger.debug(f"[{code}] AKShare 派息表获取失败: {e}")
            ak_records = []

        for r in ak_records or []:
            ex_date = r.get("ex_date")
            # NaT 也保留（compute_dividend_fields 的 R1 会跳过）
            if pd.notna(ex_date):
                ex_date = pd.Timestamp(ex_date).normalize()
                ak_seen_dates.add(ex_date)
            records.append(DividendRecord(
                ex_date=ex_date,
                amount_hkd=float(r.get("amount_hkd") or 0.0),
                fiscal_year=r.get("fiscal_year"),
                distribution_type=str(r.get("distribution_type") or ""),
                source="akshare",
            ))

        # Step 2b: yfinance 兜底（仅补 AKShare 缺失的派息）
        try:
            divs = t.dividends
        except Exception:
            divs = None
        if divs is not None and not divs.empty:
            ddf = divs.reset_index()
            ddf.columns = ["date", "amount"]
            ddf["date"] = pd.to_datetime(ddf["date"])
            if ddf["date"].dt.tz is not None:
                ddf["date"] = ddf["date"].dt.tz_localize(None)
            for _, row in ddf.iterrows():
                d = pd.Timestamp(row["date"]).normalize()
                a = float(row["amount"]) if pd.notna(row["amount"]) else 0.0
                if a <= 0:
                    continue
                if d in ak_seen_dates:
                    continue  # AKShare 已收录，跳过避免双计
                records.append(DividendRecord(
                    ex_date=d,
                    amount_hkd=a,
                    fiscal_year=None,  # yfinance 无财年信息 → 走启发式
                    source="yfinance",
                ))

        # ---- Step 3: 调用统一权威计算函数 ----
        return compute_dividend_fields(
            records=records,
            periods=periods,
            fy_anchor=fy_anchor,
            today=today,
            forward_override=forward_div_override,
        )
    except Exception as e:
        logger.warning(f"[{code}] 获取分红数据失败: {e}")
        raise  # B16: 让上层 collect_financial_data 标记为 fail
        # noqa: 下面的代码不再触达
        return dict(EMPTY)


def _get_splits_ratio_in_window(ticker, start_date) -> float:
    """
    获取从 start_date（不含）到现在累计的 splits ratio。
    用于检测合股（reverse split）/ 拆股（split），调整 prev_shares 用。

    yfinance Ticker.splits:
      - 0.5 = 2:1 合股（每 2 股变 1 股）
      - 2.0 = 1:2 拆股（每 1 股变 2 股）
      - 累乘后即"上年末股本应等效现在的多少倍"

    Returns:
        float: 累计 split ratio。无 split 或异常返回 1.0
    """
    if start_date is None:
        return 1.0
    try:
        splits = ticker.splits
        if splits is None or len(splits) == 0:
            return 1.0
        sdt = pd.Timestamp(start_date)
        # 时区对齐：splits 的索引通常带时区，sdt 不带
        if splits.index.tz is not None and sdt.tz is None:
            sdt = sdt.tz_localize("UTC").tz_convert(splits.index.tz)
        in_window = splits[splits.index > sdt]
        if in_window.empty:
            return 1.0
        # 累乘所有窗口内 split ratio
        prod_val = in_window.prod()
        if hasattr(prod_val, "iloc"):
            prod_val = prod_val.iloc[0]
        return float(prod_val)
    except Exception as e:
        logger.warning(f"[splits_ratio] 获取 splits 失败: {e}")
        return 1.0


def get_fiscal_year_buyback(code: str) -> dict:
    """
    获取当前财年与上一财年每股回购金额（港元）。
    
    来源：yfinance cashflow 表中 "Repurchase Of Capital Stock"
    该行为负值（现金流出），取绝对值。
    再除以期初总股本得到每股回购金额。

    Returns:
        {
            "total_buyback": float,            # 当财年回购总金额（港元）
            "shares_outstanding": float,       # 期初总股本
            "buyback_per_share": float,        # 当财年每股回购金额（港元/股）
            "prev_total_buyback": float,       # 上财年回购总金额（港元）
            "prev_buyback_per_share": float,   # 上财年每股回购金额（港元/股）
        }
    """
    EMPTY = {
        "total_buyback": 0.0, "shares_outstanding": None, "buyback_per_share": 0.0,
        "prev_total_buyback": 0.0, "prev_buyback_per_share": 0.0,
        "prev_shares_outstanding": None,
        "splits_ratio_in_window": 1.0,
    }
    try:
        prev_fy_end, current_fy_end = get_fiscal_year_dates(code)
        if current_fy_end is None:
            # B16: fy_dates 拿不到，多半是网络/yfinance 异常 → 抛 fail
            raise RuntimeError("get_fiscal_year_dates returned None (likely yfinance unavailable)")

        t = _get_ticker(code)
        cf = t.cashflow  # columns = fiscal year end dates (descending)
        if cf is None or cf.empty:
            return dict(EMPTY)

        # 找当前财年列与上一财年列
        cf_cols = sorted(cf.columns.tolist(), reverse=True)
        target_col = None
        prev_target_col = None
        for col in cf_cols:
            ts = pd.Timestamp(col)
            if ts == current_fy_end and target_col is None:
                target_col = col
            elif prev_fy_end is not None and ts == prev_fy_end and prev_target_col is None:
                prev_target_col = col
        # 如果没有精确匹配，取最新两列
        if target_col is None and cf_cols:
            target_col = cf_cols[0]
        if prev_target_col is None and len(cf_cols) >= 2:
            # 用比 target_col 更早的第一列
            for col in cf_cols:
                if col != target_col:
                    prev_target_col = col
                    break

        buyback_row_keys = [
            "Repurchase Of Capital Stock",
            "repurchaseOfCapitalStock",
            "CommonStockRepurchased",
        ]

        def _read(col):
            if col is None:
                return 0.0
            for key in buyback_row_keys:
                if key in cf.index:
                    val = cf.loc[key, col]
                    if pd.notna(val):
                        return abs(float(val))
                    return 0.0
            return 0.0

        total_buyback = _read(target_col)
        prev_total_buyback = _read(prev_target_col)

        # 期初总股本：用当前财年的 shares_outstanding
        shares = None
        prev_shares = None
        try:
            bs = t.balance_sheet
            if bs is not None and not bs.empty:
                bs_cols = sorted(bs.columns.tolist(), reverse=True)
                for col in bs_cols:
                    ts = pd.Timestamp(col)
                    for key in ["Ordinary Shares Number", "CommonStock", "ShareIssued"]:
                        if key in bs.index:
                            v = bs.loc[key, col]
                            if pd.notna(v) and float(v) > 0:
                                if ts == current_fy_end and shares is None:
                                    shares = float(v)
                                elif prev_fy_end is not None and ts == prev_fy_end and prev_shares is None:
                                    prev_shares = float(v)
                                break
        except Exception:
            pass

        # fallback: fast_info.shares 给当前；上年股本不做 fallback，留 None 以避免污染 net_share_change_rate
        if shares is None:
            try:
                shares = float(t.fast_info.shares) or None
            except Exception:
                shares = None
        # 仅 prev_buyback_per_share 计算允许用 shares 兑底（留 prev_shares 原值处理股本变化）
        denom_prev = prev_shares if (prev_shares and prev_shares > 0) else shares

        buyback_per_share = round(total_buyback / shares, 6) if shares and shares > 0 else 0.0
        prev_buyback_per_share = round(prev_total_buyback / denom_prev, 6) if denom_prev and denom_prev > 0 else 0.0

        # 获取财年区间内的 splits 累计 ratio（用于合股/拆股调整真实回购计算）
        splits_ratio = _get_splits_ratio_in_window(t, prev_fy_end)

        return {
            "total_buyback": round(total_buyback, 2),
            "shares_outstanding": shares,
            "buyback_per_share": buyback_per_share,
            "prev_total_buyback": round(prev_total_buyback, 2),
            "prev_buyback_per_share": prev_buyback_per_share,
            "prev_shares_outstanding": prev_shares,
            "splits_ratio_in_window": splits_ratio,
        }
    except Exception as e:
        logger.warning(f"[{code}] 获取回购数据失败: {e}")
        raise  # B16: 让上层 collect_financial_data 标记为 fail（而非 empty）


def get_net_debt_ratio(code: str) -> Optional[float]:
    """
    计算有息负债率 = 有息负债 / 总资产
    
    有息负债 = 短期借款 + 长期借款 + 应付债券（近似）
    
    Returns:
        float 或 None（无法获取时）
    """
    ratios = get_debt_ratios(code)
    return ratios.get("net_debt_ratio") if ratios else None


def get_debt_ratios(code: str) -> Optional[dict]:
    """
    B3: 一次性从 yfinance balance_sheet 获取负债率（避免重复调用）

    返回：
      {
        "net_debt_ratio": (短借+长借)/总资产,    # 有息负债率
        "total_liability_ratio": 总负债/总资产,  # 资产负债率
        "debt_to_equity": (短借+长借)/净资产,    # B19: 有息负债/净资产
        "roe": net_income / equity_期末,    # B22: ROE（A 方案：期末）
      }
    or None when balance_sheet 不可用。
    """
    try:
        t = _get_ticker(code)
        bs = t.balance_sheet
        ist = t.income_stmt
        # B34: 双源 combine（AKShare 优先，yfinance 兜底）
        ak_bs = _get_hk_report_akshare(code, "资产负债表")
        ak_is = _get_hk_report_akshare(code, "利润表")

        _as_of_dates: list = []

        def _latest(ak_keys, yf_keys, sum_items=False, src_is=False):
            yf_df = ist if src_is else bs
            ak_df = ak_is if src_is else ak_bs
            seq = _combine_series(
                _ak_series(ak_df, ak_keys, sum_items=sum_items),
                _yf_series(yf_df, yf_keys),
            )
            if seq:
                _as_of_dates.append(seq[0][0])
                return seq[0][1]
            return None

        total_assets = _latest(AK_ITEM_MAP["total_assets"],
                               ["Total Assets", "TotalAssets"]) or 0.0
        short_term_debt = _latest(AK_ITEM_MAP["short_term_debt"], [
            "Current Debt", "ShortTermDebt",
            "Short Term Debt And Current Portion Of Long Term Debt",
        ]) or 0.0
        long_term_debt = _latest(AK_ITEM_MAP["long_term_debt"], [
            "Long Term Debt", "LongTermDebt",
            "Long Term Debt And Capital Lease Obligation",
        ]) or 0.0
        total_liabilities = _latest(AK_ITEM_MAP["total_liabilities"], [
            "Total Liabilities Net Minority Interest",
            "TotalLiabilitiesNetMinorityInterest",
            "Total Liab", "TotalLiab",
        ]) or 0.0
        total_equity = _latest(AK_ITEM_MAP["stockholders_equity"], [
            "Stockholders Equity", "StockholdersEquity",
            "Total Equity Gross Minority Interest",
            "TotalEquityGrossMinorityInterest",
            "Common Stock Equity", "CommonStockEquity",
        ]) or 0.0
        # 兜底：净资产 = 总资产 - 总负债
        if total_equity <= 0 and total_assets > 0 and total_liabilities > 0:
            total_equity = total_assets - total_liabilities

        if total_assets <= 0:
            return None

        net_debt = short_term_debt + long_term_debt
        net_debt_ratio = round(net_debt / total_assets, 4)
        # 总负债字段缺失时返回 None（不强行用 0）
        total_liability_ratio = (
            round(total_liabilities / total_assets, 4)
            if total_liabilities > 0 else None
        )
        # B29: 现金及等价物（仅纯现金，不含短投，保守口径）
        cash_and_equivalents = _latest(AK_ITEM_MAP["cash_and_equivalents"], [
            "Cash And Cash Equivalents", "CashAndCashEquivalents", "Cash",
        ]) or 0.0

        # B19/B32: 净资产缺失或非正时返回 None
        # B32: 改为 Net D/E = max(IBD − Cash, 0) / Equity
        debt_to_equity = (
            round(max(net_debt - float(cash_and_equivalents or 0), 0.0) / total_equity, 4)
            if total_equity > 0 else None
        )

        # B22: ROE = net_income / equity_期末（A 方案）
        roe = None
        if total_equity and total_equity > 0:
            try:
                ni = _latest(AK_ITEM_MAP["net_income"], [
                    "Net Income", "NetIncome",
                    "Net Income Common Stockholders", "NetIncomeCommonStockholders",
                    "Net Income From Continuing Operation Net Minority Interest",
                ], src_is=True)
                if ni and ni != 0.0:
                    roe = round(float(ni) / total_equity, 4)
            except Exception as e:
                logger.debug(f"[{code}] ROE 计算跳过: {e}")
        # B35: 数据血缘 —— 资产负债表科目实际使用的最新报告期
        _debt_as_of = None
        if _as_of_dates:
            _debt_as_of = max(_as_of_dates).strftime("%Y-%m-%d")

        return {
            "net_debt_ratio": net_debt_ratio,
            "total_liability_ratio": total_liability_ratio,
            "debt_to_equity": debt_to_equity,
            "roe": roe,
            "cash_and_equivalents": float(cash_and_equivalents) if cash_and_equivalents else None,
            "interest_bearing_debt": float(net_debt) if net_debt else None,
            "debt_as_of": _debt_as_of,
        }
    except Exception as e:
        logger.warning(f"[{code}] 获取负债率失败: {e}")
        raise  # B16: 让上层 collect_financial_data 标记为 fail


# ============================================================================
# B34: yfinance + AKShare 双源 combine 基础设施
# ----------------------------------------------------------------------------
# 问题：yfinance 港股年报严重滞后（如 01995 缺 2025 年报），导致 3 年平均口径
#       误用含旧崩盘年的过时数据。AKShare「年度」接口更新及时（已含 2025）。
# 方案：构建「按报告期对齐」的年度科目序列，AKShare 优先、yfinance 兜底，
#       逐期 union 合并。所有 3y_avg / latest / prev 取值均从合并序列读取。
# ============================================================================

# 模块级缓存：避免同一 code 多个采集函数重复请求 AKShare（{code: {symbol: df}}）
_AK_REPORT_CACHE: dict = {}

# AKShare 港股年度报表科目 → 标准字段名映射（多候选名，按优先级）
# 单位与 yfinance 一致（元）。capex/da/lease 取绝对值由调用方处理。
AK_ITEM_MAP: dict = {
    # 现金流量表
    "operating_cashflow": ["经营业务现金净额"],
    "capex": ["购建固定资产", "购建无形资产及其他资产"],  # 两项相加
    "depreciation_amortization": ["折旧及摊销"],
    "lease_repayment": ["偿还融资租赁"],
    # 资产负债表
    "total_assets": ["总资产"],
    "total_liabilities": ["总负债"],
    "stockholders_equity": ["股东权益", "总权益"],
    "cash_and_equivalents": ["现金及等价物"],
    "current_lease_obligation": ["融资租赁负债(流动)"],
    "short_term_debt": ["短期贷款", "短期借款", "一年内到期的非流动负债"],
    "long_term_debt": ["长期贷款", "长期借款"],
    # 利润表
    "net_income": ["股东应占溢利", "归属于母公司股东的净利润", "本公司拥有人应占溢利"],
}


def _get_hk_report_akshare(code: str, symbol: str) -> Optional[pd.DataFrame]:
    """B34: 拉取 AKShare 港股「年度」报表（带模块级缓存）。

    symbol ∈ {"资产负债表", "现金流量表", "利润表"}
    返回原始 DataFrame（列含 REPORT_DATE / STD_ITEM_NAME / AMOUNT），失败返回 None。
    """
    cache_key = symbol
    if code in _AK_REPORT_CACHE and cache_key in _AK_REPORT_CACHE[code]:
        return _AK_REPORT_CACHE[code][cache_key]
    df = None
    try:
        import akshare as ak
        df = ak.stock_financial_hk_report_em(stock=code, symbol=symbol, indicator="年度")
        if df is None or df.empty:
            df = None
    except Exception as e:
        logger.debug(f"[{code}] AKShare {symbol} 年度拉取失败: {e}")
        df = None
    _AK_REPORT_CACHE.setdefault(code, {})[cache_key] = df
    return df


def _ak_series(df: Optional[pd.DataFrame], item_keys, sum_items: bool = False) -> dict:
    """B34: 从 AKShare 年度 df 提取某科目的 {date(Timestamp): value(float)} 序列。

    item_keys: 候选科目名列表。
    sum_items=True 时把所有命中的候选科目「相加」（如 capex = 购建固定资产 + 购建无形）；
    sum_items=False 时按优先级取第一个命中的科目。
    """
    if df is None or df.empty or "STD_ITEM_NAME" not in df.columns:
        return {}
    out: dict = {}
    if sum_items:
        # 对每个候选科目分别取序列，再按日期相加
        for k in item_keys:
            sub = df[df["STD_ITEM_NAME"] == k]
            for _, r in sub.iterrows():
                d = pd.to_datetime(r.get("REPORT_DATE"), errors="coerce")
                a = r.get("AMOUNT")
                if pd.notna(d) and pd.notna(a):
                    d = pd.Timestamp(d).normalize()
                    out[d] = out.get(d, 0.0) + float(a)
        return out
    # 优先级取第一个命中的科目
    for k in item_keys:
        sub = df[df["STD_ITEM_NAME"] == k]
        if sub.empty:
            continue
        for _, r in sub.iterrows():
            d = pd.to_datetime(r.get("REPORT_DATE"), errors="coerce")
            a = r.get("AMOUNT")
            if pd.notna(d) and pd.notna(a):
                d = pd.Timestamp(d).normalize()
                if d not in out:
                    out[d] = float(a)
        if out:
            return out
    return {}


def _yf_series(df: Optional[pd.DataFrame], keys) -> dict:
    """B34: 从 yfinance 报表 df（列=财报日期，行=科目）提取 {date: value} 序列。"""
    if df is None or df.empty:
        return {}
    for k in keys:
        if k in df.index:
            out: dict = {}
            for col in df.columns:
                v = df.loc[k, col]
                d = pd.to_datetime(col, errors="coerce")
                if pd.notna(d) and pd.notna(v):
                    out[pd.Timestamp(d).normalize()] = float(v)
            if out:
                return out
    return {}


def _combine_series(ak_ser: dict, yf_ser: dict) -> list:
    """B34: 按报告期 union 合并两源年度序列，AKShare 优先、yfinance 兜底。

    返回按日期降序的 [(Timestamp, value), ...]。
    用户语义：最新一期通常只有 AKShare 有 → 用 AKShare；
              更老的某些期可能 AKShare 缺 → 用 yfinance 补。
    """
    merged: dict = {}
    # 先放 yfinance（兜底），再用 AKShare 覆盖（优先）
    for d, v in (yf_ser or {}).items():
        merged[d] = v
    for d, v in (ak_ser or {}).items():
        merged[d] = v
    return sorted(merged.items(), key=lambda x: x[0], reverse=True)


def get_fcf_components(code: str) -> Optional[dict]:
    """
    B31: 采集"剩余可用现金流偿债覆盖率"指标所需的 5 个组件。

    返回:
      {
        "ocf_3y_avg": float,                       # 经营现金流 3 年平均
        "capex_3y_avg": float,                     # 资本开支 3 年平均（abs 转正）
        "dep_amortization_3y_avg": float,          # 折旧摊销 3 年平均（abs 转正）
        "current_lease_obligation_prev": float,    # 期初(倒数第2列)一年内到期租赁负债
        "equity_latest": float,                    # 最新期净资产（用于 ε 计算）
      }
    缺失项置 None（不是 0），上游决定如何降级。
    """
    try:
        t = _get_ticker(code)
        cf = t.cashflow
        bs = t.balance_sheet
        # B34: yfinance 任一缺失不再直接 return None —— AKShare 可独立兜底

        # === B34: 构建双源 combine 年度序列 ===
        ak_cf = _get_hk_report_akshare(code, "现金流量表")
        ak_bs = _get_hk_report_akshare(code, "资产负债表")

        # OCF（不取绝对值，保留负号）
        ocf_seq = _combine_series(
            _ak_series(ak_cf, AK_ITEM_MAP["operating_cashflow"]),
            _yf_series(cf, ["Operating Cash Flow", "OperatingCashFlow",
                            "Cash Flow From Continuing Operating Activities"]),
        )
        # CapEx（AKShare 两项相加；yfinance 单科目；统一取绝对值）
        capex_seq = _combine_series(
            _ak_series(ak_cf, AK_ITEM_MAP["capex"], sum_items=True),
            _yf_series(cf, ["Capital Expenditure", "CapitalExpenditure",
                            "Capital Expenditure Reported"]),
        )
        # D&A（取绝对值）
        da_seq = _combine_series(
            _ak_series(ak_cf, AK_ITEM_MAP["depreciation_amortization"]),
            _yf_series(cf, ["Depreciation And Amortization", "DepreciationAndAmortization",
                            "Depreciation Amortization Depletion",
                            "DepreciationAmortizationDepletion", "Depreciation"]),
        )
        # 净资产（按报表日序列）
        equity_seq = _combine_series(
            _ak_series(ak_bs, AK_ITEM_MAP["stockholders_equity"]),
            _yf_series(bs, ["Stockholders Equity", "StockholdersEquity",
                            "Total Equity Gross Minority Interest",
                            "TotalEquityGrossMinorityInterest",
                            "Common Stock Equity", "CommonStockEquity"]),
        )
        # 一年内到期租赁负债（按报表日序列）
        lease_seq = _combine_series(
            _ak_series(ak_bs, AK_ITEM_MAP["current_lease_obligation"]),
            _yf_series(bs, ["Current Capital Lease Obligation",
                            "CurrentCapitalLeaseObligation"]),
        )
        # 兜底净资产用：总资产 / 总负债序列
        ta_seq = _combine_series(
            _ak_series(ak_bs, AK_ITEM_MAP["total_assets"]),
            _yf_series(bs, ["Total Assets", "TotalAssets"]),
        )
        tl_seq = _combine_series(
            _ak_series(ak_bs, AK_ITEM_MAP["total_liabilities"]),
            _yf_series(bs, ["Total Liabilities Net Minority Interest",
                            "TotalLiabilitiesNetMinorityInterest",
                            "Total Liab", "TotalLiab"]),
        )

        # 全部源都空 → 无数据
        if not any([ocf_seq, capex_seq, da_seq, equity_seq]):
            return None

        # === 序列取值工具 ===
        def _avg_n(seq, n=3, take_abs=False):
            """取最近 n 期均值。"""
            if not seq:
                return None
            vals = [abs(v) if take_abs else v for _, v in seq[:n]]
            return sum(vals) / len(vals) if vals else None

        def _nth(seq, idx, take_abs=False):
            """取第 idx 期值（0=最新, 1=上年）。"""
            if not seq or len(seq) <= idx:
                return None
            v = seq[idx][1]
            return abs(v) if take_abs else v

        ocf_3y = _avg_n(ocf_seq, 3, take_abs=False)
        capex_3y = _avg_n(capex_seq, 3, take_abs=True)
        dep_3y = _avg_n(da_seq, 3, take_abs=True)

        # 期初租赁本金 = 上年(第2期)报表日的一年内到期租赁负债
        current_lease_prev = _nth(lease_seq, 1, take_abs=True)
        if current_lease_prev is None:
            current_lease_prev = 0.0

        # 最新净资产
        equity_latest = _nth(equity_seq, 0, take_abs=False)
        if equity_latest is None or equity_latest <= 0:
            ta = _nth(ta_seq, 0) or 0
            tl = _nth(tl_seq, 0) or 0
            if ta > 0 and tl > 0:
                equity_latest = ta - tl

        # B32: TTM (最新一期) 字段
        capex_ttm = _nth(capex_seq, 0, take_abs=True)
        da_ttm = _nth(da_seq, 0, take_abs=True)

        # B33: 上年同期 capex / da（现金流动量）
        prev_capex = _nth(capex_seq, 1, take_abs=True)
        prev_da = _nth(da_seq, 1, take_abs=True)

        # 最新期 Current Capital Lease Obligation
        current_lease_ttm = _nth(lease_seq, 0, take_abs=True)
        if current_lease_ttm is None:
            current_lease_ttm = 0.0

        # B35: 数据血缘 —— 记录 OCF 序列实际使用的最新报告期（时效锚点）
        _as_of = None
        if ocf_seq:
            _as_of = ocf_seq[0][0].strftime("%Y-%m-%d")
        elif equity_seq:
            _as_of = equity_seq[0][0].strftime("%Y-%m-%d")

        return {
            "ocf_3y_avg": ocf_3y,
            "capex_3y_avg": capex_3y,
            "dep_amortization_3y_avg": dep_3y,
            "current_lease_obligation_prev": current_lease_prev,
            "equity_latest": equity_latest,
            # B32: TTM 字段
            "capex_ttm": capex_ttm,
            "da_ttm": da_ttm,
            "current_lease_obligation_ttm": current_lease_ttm,
            # B33: 上年同期 capex/da（现金流动量）
            "prev_capex": prev_capex,
            "prev_da": prev_da,
            # B35: 数据血缘
            "fcf_as_of": _as_of,
        }
    except Exception as e:
        logger.warning(f"[{code}] 获取 FCF 组件失败: {e}")
        return None


def _get_growth_from_akshare(code: str) -> Optional[dict]:
    """
    B16+/B17: AKShare 港股 F10 兜底 + 通用 TTM 计算。

    通用 TTM 公式（适用任意频次/任意财年）：
      设最新可用累计期为 D（YTD 累计值 = X）：
        - 若 D 是该公司财年末日 → TTM = X（直接拿）
        - 否则 TTM = X + (上一财年末日累计 - 与 D 相同月日的上一年累计)

    YoY 分母（去年 TTM）= 去年同期累计 + (前年年报 - 前年同期累计)
    若 D 是财年末 → 去年 TTM = 去年年报

    Returns:
        {rev_curr, rev_prev, opinc_curr, opinc_prev, ocf_curr, ocf_prev} 港币
        失败/无数据 → None
    """
    try:
        import akshare as ak
        # B17: 主用「报告期」（YTD 累计，覆盖年报+半年报+季报）；辅助「年度」识别真正的财年末月日
        is_df = ak.stock_financial_hk_report_em(stock=code, symbol="利润表", indicator="报告期")
        cf_df = ak.stock_financial_hk_report_em(stock=code, symbol="现金流量表", indicator="报告期")
        try:
            is_yr = ak.stock_financial_hk_report_em(stock=code, symbol="利润表", indicator="年度")
        except Exception:
            is_yr = None
    except Exception as e:
        logger.warning(f"[{code}] AKShare 兜底拉取失败: {e}")
        return None

    if is_df is None or is_df.empty:
        return None

    # 推断该公司的财年末月日：取最近 5 年「年度」接口里出现频次最高的月日
    fy_md_global: Optional[tuple] = None
    if is_yr is not None and not is_yr.empty and "REPORT_DATE" in is_yr.columns:
        try:
            from collections import Counter
            yr_dates = pd.to_datetime(is_yr["REPORT_DATE"], errors="coerce").dropna()
            # 只看最近 5 年（防止公司中途变更财年末，旧月日干扰）
            yr_dates = sorted(set(yr_dates), reverse=True)[:5]
            md_cnt = Counter((d.month, d.day) for d in yr_dates)
            if md_cnt:
                fy_md_global = md_cnt.most_common(1)[0][0]
        except Exception:
            fy_md_global = None

    def _ttm_triple(df, item_keys):
        """
        B26: 从 df 中按 item_keys 优先级查找科目，返回 3 期 TTM (t, t-1, t-2)。
        df 列：REPORT_DATE, STD_ITEM_NAME, AMOUNT
        缺数据返回 None；2 期版本是该函数的 (t, t-1) 子集。
        """
        if df is None or df.empty:
            return None, None, None
        sub = None
        for k in item_keys:
            x = df[df["STD_ITEM_NAME"] == k]
            if not x.empty:
                sub = x.copy()
                break
        if sub is None or sub.empty:
            return None, None, None

        sub["REPORT_DATE"] = pd.to_datetime(sub["REPORT_DATE"], errors="coerce")
        sub = sub.dropna(subset=["REPORT_DATE"])
        sub = sub.sort_values("REPORT_DATE", ascending=False)
        if sub.empty:
            return None, None, None

        m = {}
        for _, r in sub.iterrows():
            d = pd.Timestamp(r["REPORT_DATE"]).normalize()
            a = r["AMOUNT"]
            if d not in m and pd.notna(a):
                m[d] = float(a)
        if not m:
            return None, None, None

        sorted_dates = sorted(m.keys(), reverse=True)
        latest_d = sorted_dates[0]
        latest_x = m[latest_d]

        fy_md = fy_md_global
        if fy_md is None:
            from collections import Counter
            md_counts = Counter((d.month, d.day) for d in m.keys())
            month_end_md = [(md, c) for md, c in md_counts.items() if md[1] >= 28]
            if month_end_md:
                fy_md = max(month_end_md, key=lambda x: x[1])[0]
            else:
                fy_md = (12, 31)
        fy_mm, fy_dd = fy_md
        is_annual = (latest_d.month == fy_mm and abs(latest_d.day - fy_dd) <= 3)

        def _find_match(target_y, target_m, target_d, tol=3):
            for d in sorted_dates:
                if d.year == target_y and d.month == target_m and abs(d.day - target_d) <= tol:
                    return m[d]
            return None

        if is_annual:
            # 年报路径：直接取过去 3 期年报
            t   = latest_x
            t1  = _find_match(latest_d.year - 1, latest_d.month, latest_d.day)
            t2  = _find_match(latest_d.year - 2, latest_d.month, latest_d.day)
            return t, t1, t2

        # 非年报路径：滚动 TTM 公式
        # TTM_t   = latest_x + (last_fy - last_fy_same_quarter)
        prev_fy_year = None
        for d in sorted_dates:
            if d < latest_d and d.month == fy_mm and abs(d.day - fy_dd) <= 3:
                prev_fy_year = d.year
                break
        if prev_fy_year is None:
            return latest_x, None, None

        prev_fy_amt = _find_match(prev_fy_year, fy_mm, fy_dd)
        prev_same_amt = _find_match(latest_d.year - 1, latest_d.month, latest_d.day)
        if prev_fy_amt is None or prev_same_amt is None:
            return latest_x, None, None
        ttm_t = latest_x + (prev_fy_amt - prev_same_amt)

        # TTM_t-1 = prev_same + (prev_prev_fy - prev_prev_same)
        prev_prev_fy_amt = _find_match(prev_fy_year - 1, fy_mm, fy_dd)
        prev_prev_same_amt = _find_match(latest_d.year - 2, latest_d.month, latest_d.day)
        if prev_prev_fy_amt is not None and prev_prev_same_amt is not None:
            ttm_t1 = prev_same_amt + (prev_prev_fy_amt - prev_prev_same_amt)
        else:
            ttm_t1 = None

        # TTM_t-2 = prev_prev_same + (prev_prev_prev_fy - prev_prev_prev_same)
        if prev_prev_same_amt is not None:
            ppp_fy = _find_match(prev_fy_year - 2, fy_mm, fy_dd)
            ppp_same = _find_match(latest_d.year - 3, latest_d.month, latest_d.day)
            if ppp_fy is not None and ppp_same is not None:
                ttm_t2 = prev_prev_same_amt + (ppp_fy - ppp_same)
            else:
                ttm_t2 = None
        else:
            ttm_t2 = None

        return ttm_t, ttm_t1, ttm_t2

    def _ttm_pair(df, item_keys):
        """兼容包装：保持旧接口返回 (t, t-1)"""
        t, t1, _ = _ttm_triple(df, item_keys)
        return t, t1

    rev_c, rev_p, rev_p2 = _ttm_triple(is_df, ["营运收入", "营业额", "营业收入", "经营收入总额"])
    opinc_c, opinc_p, opinc_p2 = _ttm_triple(is_df, ["经营溢利", "经营利润", "营业利润", "扣除准备前经营溢利"])
    ocf_c, ocf_p, ocf_p2 = _ttm_triple(cf_df, ["经营业务现金净额", "经营活动产生的现金流量净额", "经营产生现金"])

    return {
        "rev_curr": rev_c, "rev_prev": rev_p, "rev_prev2": rev_p2,
        "opinc_curr": opinc_c, "opinc_prev": opinc_p, "opinc_prev2": opinc_p2,
        "ocf_curr": ocf_c, "ocf_prev": ocf_p, "ocf_prev2": ocf_p2,
    }


def get_growth_metrics(code: str) -> dict:
    """
    采集 TTM 同比增长率：经营现金流 / 营业收入 / 营业利润

    【口径】港股大部分公司无季报，统一用年报：
        TTM ≈ 最新一个完整年报数；上年 TTM = 上一个年报数
    对于有季报数据的中概回港股（如 03660），仍按年报取，
    保证横截面口径一致、可比性更好。

    【YoY 公式】
        (curr - prev) / abs(prev)
        prev ≈ 0 或缺失 → NaN（避免除 0 / 符号反转）

    Returns:
        {
            "ttm_revenue":               最新年报营收
            "prev_ttm_revenue":          上年年报营收
            "revenue_yoy":               营收同比（小数，如 0.10 表示 +10%）
            "ttm_operating_income":      最新年报营业利润
            "prev_ttm_operating_income": 上年年报营业利润
            "operating_income_yoy":      营业利润同比
            "ttm_operating_cashflow":    最新年报经营现金流
            "prev_ttm_operating_cashflow": 上年年报经营现金流
            "operating_cashflow_yoy":    经营现金流同比
        }
    """
    EMPTY = {
        "ttm_revenue": None,
        "prev_ttm_revenue": None,
        "ttm_revenue_t2": None,
        "revenue_yoy": None,
        "ttm_operating_income": None,
        "prev_ttm_operating_income": None,
        "ttm_operating_income_t2": None,
        "operating_income_yoy": None,
        "ttm_operating_cashflow": None,
        "prev_ttm_operating_cashflow": None,
        "ttm_operating_cashflow_t2": None,
        "operating_cashflow_yoy": None,
    }

    def yoy(curr, prev):
        try:
            if curr is None or prev is None:
                return None
            import math
            if math.isnan(curr) or math.isnan(prev):
                return None
            if abs(prev) < 1e-6:
                return None
            return round((curr - prev) / abs(prev), 6)
        except Exception:
            return None

    def first_two_vals(df, candidate_keys):
        """从 df.index 中找第一个命中的科目，严格取按日期降序的最近两列 (curr, prev)。

        【B16 修复】此前会"跳过 NaN 取更老的两个有值列"，导致跨年份冒充新数据。
        新策略：只看排序后最近两列；若最新列(curr)是 NaN，认为数据源缺失最新年报数据，
        视为 fail 让上层重试（而不是用 2~3 年前的数据冒充）。

        Returns:
            (curr, prev, status)  status ∈ {'ok','empty','fail_latest_nan'}
            - 'ok'   : curr/prev 都非 NaN
            - 'empty': df 为空 或 candidate_keys 都不在 index（数据源真没披露）
            - 'fail_latest_nan': 最新列值 NaN（疑似数据源临时缺，需重试）
        """
        if df is None or df.empty:
            return None, None, 'empty'
        for k in candidate_keys:
            if k in df.index:
                cols_sorted = sorted(df.columns.tolist(), reverse=True)
                if not cols_sorted:
                    return None, None, 'empty'
                v_curr = df.loc[k, cols_sorted[0]]
                if pd.isna(v_curr):
                    return None, None, 'fail_latest_nan'
                v_prev = df.loc[k, cols_sorted[1]] if len(cols_sorted) >= 2 else None
                v_prev_f = float(v_prev) if v_prev is not None and pd.notna(v_prev) else None
                return float(v_curr), v_prev_f, 'ok'
        return None, None, 'empty'

    try:
        # B17: AKShare 优先（YTD 累计 → 通用 TTM 公式，覆盖年报/半年报/季报）
        ak_data = _get_growth_from_akshare(code)
        ak_rev_c = ak_data.get('rev_curr') if ak_data else None
        ak_opinc_c = ak_data.get('opinc_curr') if ak_data else None
        ak_ocf_c = ak_data.get('ocf_curr') if ak_data else None

        t = _get_ticker(code)
        ais = t.income_stmt
        acf = t.cashflow

        rev_curr, rev_prev, rev_st = first_two_vals(
            ais,
            ["Total Revenue", "TotalRevenue", "Operating Revenue", "OperatingRevenue"],
        )
        opinc_curr, opinc_prev, opinc_st = first_two_vals(
            ais,
            ["Operating Income", "OperatingIncome"],
        )
        ocf_curr, ocf_prev, ocf_st = first_two_vals(
            acf,
            [
                "Operating Cash Flow",
                "OperatingCashFlow",
                "Cash Flow From Continuing Operating Activities",
                "Total Cash From Operating Activities",
            ],
        )

        # B26: t-2 默认 None，仅 AKShare 路径有
        rev_prev2 = opinc_prev2 = ocf_prev2 = None

        # B16: 聚合三态 - 只要有任一字段 fail 则整体 fail（保留旧值）；全 empty 则 empty；否则 ok
        statuses = [rev_st, opinc_st, ocf_st]

        # B17: AKShare 优先策略 - 若 AKShare 有数据则覆盖 yfinance 结果（更新更准）
        # AKShare 缺哪一项 → 保留 yfinance 那一项（双源融合）
        # B26: t-2 仅 AKShare 路径提供
        if ak_data:
            if ak_rev_c is not None:
                rev_curr, rev_prev = ak_data['rev_curr'], ak_data.get('rev_prev')
                rev_prev2 = ak_data.get('rev_prev2')
                rev_st = 'ok_ak'
            if ak_opinc_c is not None:
                opinc_curr, opinc_prev = ak_data['opinc_curr'], ak_data.get('opinc_prev')
                opinc_prev2 = ak_data.get('opinc_prev2')
                opinc_st = 'ok_ak'
            if ak_ocf_c is not None:
                ocf_curr, ocf_prev = ak_data['ocf_curr'], ak_data.get('ocf_prev')
                ocf_prev2 = ak_data.get('ocf_prev2')
                ocf_st = 'ok_ak'
            statuses = [rev_st, opinc_st, ocf_st]

        # B16+: yfinance 任一项 fail_latest_nan 且 AKShare 也没补上 → 已无更多兜底

        if any(s == 'fail_latest_nan' for s in statuses):
            agg_status = 'fail'
            agg_reason = f"latest_nan: rev={rev_st},opinc={opinc_st},ocf={ocf_st}"
        elif all(s == 'empty' for s in statuses):
            agg_status = 'empty'
            agg_reason = 'all_empty'
        else:
            agg_status = 'ok'
            agg_reason = ''

        payload = {
            "ttm_revenue": rev_curr,
            "prev_ttm_revenue": rev_prev,
            "ttm_revenue_t2": rev_prev2,
            "revenue_yoy": yoy(rev_curr, rev_prev),
            "ttm_operating_income": opinc_curr,
            "prev_ttm_operating_income": opinc_prev,
            "ttm_operating_income_t2": opinc_prev2,
            "operating_income_yoy": yoy(opinc_curr, opinc_prev),
            "ttm_operating_cashflow": ocf_curr,
            "prev_ttm_operating_cashflow": ocf_prev,
            "ttm_operating_cashflow_t2": ocf_prev2,
            "operating_cashflow_yoy": yoy(ocf_curr, ocf_prev),
        }
        payload["_status"] = agg_status
        payload["_reason"] = agg_reason
        return payload
    except Exception as e:
        logger.warning(f"[{code}] 获取成长指标失败: {e}")
        # B16: 异常归 fail（区别于数据源真返回空）
        out = dict(EMPTY)
        out["_status"] = "fail"
        out["_reason"] = f"exception: {str(e)[:160]}"
        return out


def collect_financial_data(code: str, forward_div_override: Optional[float] = None, check_dividend_history: bool = True) -> dict:
    """
    汇总单只股票的财务数据（分红+回购+负债率+成长）

    B16: 增加 _status 字段（每个子维度 ok/empty/fail）+ _reason
    """
    from config.settings import HK_STOCK_FILTERS

    domain_status: dict = {}
    domain_reason: dict = {}

    # ── dividend ─────────────────────────────────────────────────
    try:
        div_data = get_fiscal_year_dividends(code, forward_div_override=forward_div_override)
        fps = div_data.get("fiscal_periods")
        has_fp = bool(fps) and (not isinstance(fps, str) or fps.strip() not in ("", "[]"))
        if has_fp or div_data.get("fy_div_per_share") or div_data.get("forward_div_per_share"):
            domain_status["dividend"] = "ok"
        else:
            domain_status["dividend"] = "empty"
    except Exception as e:
        logger.warning(f"[{code}] dividend 采集失败: {e}")
        div_data = {}
        domain_status["dividend"] = "fail"
        domain_reason["dividend"] = str(e)[:200]

    # ── buyback ──────────────────────────────────────────────────
    try:
        buyback_data = get_fiscal_year_buyback(code)
        if buyback_data.get("shares_outstanding"):
            domain_status["buyback"] = "ok"
        else:
            domain_status["buyback"] = "empty"
    except Exception as e:
        logger.warning(f"[{code}] buyback 采集失败: {e}")
        buyback_data = {}
        domain_status["buyback"] = "fail"
        domain_reason["buyback"] = str(e)[:200]

    # ── debt ─────────────────────────────────────────────────────
    try:
        debt_ratios = get_debt_ratios(code) or {}
        net_debt_ratio = debt_ratios.get("net_debt_ratio")
        total_liability_ratio = debt_ratios.get("total_liability_ratio")
        debt_to_equity = debt_ratios.get("debt_to_equity")
        roe = debt_ratios.get("roe")
        cash_and_equivalents = debt_ratios.get("cash_and_equivalents")
        interest_bearing_debt = debt_ratios.get("interest_bearing_debt")
        if (net_debt_ratio is not None
                or total_liability_ratio is not None
                or debt_to_equity is not None
                or roe is not None):
            domain_status["debt"] = "ok"
        else:
            domain_status["debt"] = "empty"
    except Exception as e:
        logger.warning(f"[{code}] debt 采集失败: {e}")
        net_debt_ratio = None
        total_liability_ratio = None
        debt_to_equity = None
        roe = None
        cash_and_equivalents = None
        interest_bearing_debt = None
        domain_status["debt"] = "fail"
        domain_reason["debt"] = str(e)[:200]

    # ── fcf components (B31 + B32) ───────────────────────────────
    try:
        fcf_comps = get_fcf_components(code) or {}
        ocf_3y_avg = fcf_comps.get("ocf_3y_avg")
        capex_3y_avg = fcf_comps.get("capex_3y_avg")
        dep_amortization_3y_avg = fcf_comps.get("dep_amortization_3y_avg")
        current_lease_obligation_prev = fcf_comps.get("current_lease_obligation_prev")
        equity_latest = fcf_comps.get("equity_latest")
        # B32: TTM 字段
        capex_ttm = fcf_comps.get("capex_ttm")
        da_ttm = fcf_comps.get("da_ttm")
        current_lease_obligation_ttm = fcf_comps.get("current_lease_obligation_ttm")
        # B33: 上年同期 capex/da
        prev_capex = fcf_comps.get("prev_capex")
        prev_da = fcf_comps.get("prev_da")
    except Exception as e:
        logger.warning(f"[{code}] fcf_components 采集失败: {e}")
        ocf_3y_avg = None
        capex_3y_avg = None
        dep_amortization_3y_avg = None
        current_lease_obligation_prev = None
        equity_latest = None
        capex_ttm = None
        da_ttm = None
        current_lease_obligation_ttm = None
        prev_capex = None
        prev_da = None

    # ── dilution ─────────────────────────────────────────────────
    try:
        annual_dilution_rate = get_annual_dilution_rate(code, years=3)
        domain_status["dilution"] = "ok" if annual_dilution_rate is not None else "empty"
    except Exception as e:
        logger.warning(f"[{code}] dilution 采集失败: {e}")
        annual_dilution_rate = None
        domain_status["dilution"] = "fail"
        domain_reason["dilution"] = str(e)[:200]

    # ── growth ───────────────────────────────────────────────────
    try:
        growth_data = get_growth_metrics(code)
        growth_status = growth_data.pop("_status", "ok")
        growth_reason = growth_data.pop("_reason", "")
        domain_status["growth"] = growth_status
        if growth_status == "fail" and growth_reason:
            domain_reason["growth"] = growth_reason
    except Exception as e:
        logger.warning(f"[{code}] growth 采集失败: {e}")
        growth_data = {}
        domain_status["growth"] = "fail"
        domain_reason["growth"] = str(e)[:200]

    result = {
        "code": code,
        **div_data,
        **buyback_data,
        **growth_data,
        "net_debt_ratio": net_debt_ratio,
        "total_liability_ratio": total_liability_ratio,
        "debt_to_equity": debt_to_equity,
        "roe": roe,
        "cash_and_equivalents": cash_and_equivalents,
        "interest_bearing_debt": interest_bearing_debt,
        "annual_dilution_rate": annual_dilution_rate,
        "ocf_3y_avg": ocf_3y_avg,
        "capex_3y_avg": capex_3y_avg,
        "dep_amortization_3y_avg": dep_amortization_3y_avg,
        "current_lease_obligation_prev": current_lease_obligation_prev,
        "equity_latest": equity_latest,
        "capex_ttm": capex_ttm,
        "da_ttm": da_ttm,
        "current_lease_obligation_ttm": current_lease_obligation_ttm,
        "prev_capex": prev_capex,
        "prev_da": prev_da,
    }

    if forward_div_override is not None:
        result["forward_div_source"] = "hkexnews_scraper"
    else:
        result["forward_div_source"] = "yfinance_estimate"

    if check_dividend_history:
        fy_div = float(div_data.get("fy_div_per_share", 0) or 0)
        fwd_div = float(div_data.get("forward_div_per_share", 0) or 0)
        prev_div = float(div_data.get("prev_year_div_per_share", 0) or 0)
        two_yrs_div = float(div_data.get("two_years_ago_div_per_share", 0) or 0)
        fy_count = (
            (1 if fy_div > 0 or fwd_div > 0 else 0)
            + (1 if prev_div > 0 else 0)
            + (1 if two_yrs_div > 0 else 0)
        )
        result["has_dividend_history"] = fy_count >= 2
    else:
        result["has_dividend_history"] = True

    result["_status"] = domain_status
    result["_reason"] = domain_reason

    time.sleep(AKSHARE_SLEEP)
    return result
