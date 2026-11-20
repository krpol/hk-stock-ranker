"""
Point-in-Time 数据层（PIT）：为回测提供「时点正确」的历史因子输入。

背景与经济学逻辑
----------------
回测的黄金准则是「只用 t 时点已公开的信息做 t 时点的决策」，否则产生
前视偏差（look-ahead bias）——这是任何严谨量化研究（AQR、Fama-French
等）的第一原则。此前的 backfill_rankings.py 用「今日财务 ÷ 历史股价」
回填历史排名，正是典型的 look-ahead：2022 年的 return_rate 用了
2026 年才知道的分红，导致回测 alpha 系统性高估。

本模块用「天然时点」重建历史因子值：
- 分红：分红的信息事件是「首次公告日」（随年报/中报，少数单独公告），
  而非「除息日」——除息日只是价格机械调整（-D），市场在公告日就已重新
  定价。故优先用公告日（announce_date）切片；缺公告日的分红回退 ex_date
  （保守：宁可晚识别、不可提前用），不产生前视偏差。
- 回购 / 财务 / 股本：用「报告期 + 保守披露滞后」近似公告日（港股年报
  通常在财政年结后 3~4 个月披露），避免用到尚未公告的数据。

设计
----
数据以「历史序列」存储（一只股票多行，一行一个事件时点），回测时按
as_of(t) 动态切片。这样数据只存一份，时点口径由切片函数统一保证，低耦合高内聚。

    data/csv/pit/dividend_history.csv : code, ex_date, div_per_share, announce_date
    （announce_date 为首次公告日，由同花顺 stock_hk_fhpx_detail_ths 补齐；
      回购 / 财务 / 股本序列后续追加）
"""
from __future__ import annotations

import logging
import os
import re
from typing import Iterable, Optional

import pandas as pd

from config.settings import CSV_DIR

logger = logging.getLogger(__name__)

PIT_DIR = os.path.join(CSV_DIR, "pit")
DIVIDEND_HISTORY_CSV = os.path.join(PIT_DIR, "dividend_history.csv")
SPECIAL_DIVIDEND_CSV = os.path.join(PIT_DIR, "special_dividend_history.csv")

# 港股年报披露滞后的保守近似（财政年结后推 N 个月视为「已公告」）。
# 港股主板上市规则：年报须在财政年结后 4 个月内刊发，中期 3 个月内。
# 这里取偏保守的 4 个月，宁可少用一期、不可提前用。
_FINANCIAL_DISCLOSURE_LAG_MONTHS = 4


def _ensure_pit_dir():
    os.makedirs(PIT_DIR, exist_ok=True)


# ──────────────────────────────────────────────────────────────────
# 分红历史序列
# ──────────────────────────────────────────────────────────────────
def collect_dividend_history(codes: Iterable[str], force: bool = False,
                             max_workers: int = 6) -> pd.DataFrame:
    """批量采集分红历史序列（ex-dividend date + 每股分红），增量缓存。

    已采集过的股票（非 force）跳过，复用缓存；只拉缺失的，符合「成功先缓存、
    只重拉失败」原则。

    Returns:
        DataFrame[code, ex_date, div_per_share]，按 (code, ex_date) 去重。
    """
    import yfinance as yf
    from concurrent.futures import ThreadPoolExecutor, as_completed

    _ensure_pit_dir()
    existing = load_dividend_history()
    have_codes = set(existing["code"]) if not existing.empty else set()

    codes = [str(c).zfill(5) for c in codes]
    todo = [c for c in codes if force or c not in have_codes]

    if not todo:
        logger.info("分红历史：%d 只全部已缓存，跳过采集", len(codes))
        return existing

    def _one(code: str):
        try:
            t = yf.Ticker(f"{int(code):04d}.HK")
            d = t.dividends
            if d is None or len(d) == 0:
                return code, []
            d = d.copy()
            if isinstance(d, pd.DataFrame):
                col = d.columns[0]
                s = d[col]
            else:
                s = d
            s = s.dropna()
            if s.empty:
                return code, []
            rows = []
            for dt, v in s.items():
                dt = pd.to_datetime(dt).tz_localize(None).normalize()
                rows.append({"code": code, "ex_date": dt, "div_per_share": float(v)})
            return code, rows
        except Exception as e:
            logger.warning("[%s] 分红历史采集失败: %s", code, str(e)[:80])
            return code, None

    records: list = []
    failed: list = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(_one, c): c for c in todo}
        for f in as_completed(futs):
            code, rows = f.result()
            if rows is None:
                failed.append(code)
            else:
                records.extend(rows)

    new_df = pd.DataFrame(records, columns=["code", "ex_date", "div_per_share"]) if records else pd.DataFrame()
    if new_df.empty:
        logger.info("分红历史：本次 %d 只全部为空/失败（失败 %d 只）", len(todo), len(failed))
        return existing

    new_df["ex_date"] = pd.to_datetime(new_df["ex_date"])
    new_df["code"] = new_df["code"].astype(str).str.zfill(5)

    if not existing.empty:
        combined = pd.concat([existing, new_df], ignore_index=True)
    else:
        combined = new_df
    combined = combined.drop_duplicates(subset=["code", "ex_date"], keep="last")
    combined = combined.sort_values(["code", "ex_date"]).reset_index(drop=True)
    combined.to_csv(DIVIDEND_HISTORY_CSV, index=False, encoding="utf-8-sig")

    logger.info("分红历史已保存：%d 只、%d 条除息记录（失败 %d 只）",
                combined["code"].nunique(), len(combined), len(failed))
    return combined


def load_dividend_history() -> pd.DataFrame:
    """加载分红历史序列缓存（无则返回空 DataFrame）。

    列：code, ex_date, div_per_share, announce_date（首次公告日，可能 NaT）。
    """
    if not os.path.exists(DIVIDEND_HISTORY_CSV):
        return pd.DataFrame(columns=["code", "ex_date", "div_per_share", "announce_date"])
    df = pd.read_csv(DIVIDEND_HISTORY_CSV, dtype={"code": str})
    df["ex_date"] = pd.to_datetime(df["ex_date"])
    if "announce_date" in df.columns:
        df["announce_date"] = pd.to_datetime(df["announce_date"])
    else:
        df["announce_date"] = pd.NaT
    return df


def collect_dividend_announce_dates(codes: Iterable[str], force: bool = False,
                                    max_workers: int = 4) -> pd.DataFrame:
    """为分红历史补齐「首次公告日」（announce_date），使因子在时点上可交易。

    经济学背景（用户口径 2026-11-20）：
      红利因子的信息事件是「公告日」（一般随年报/中报，少数单独公告），不是
      「除息日」。除息日只是价格机械调整，市场在公告日就已对分红重新定价。
      若用除息日回填因子，等于在「公告~除息」之间系统性地晚于市场识别分红，
      遗漏公告日跳空收益，且让因子的分子（TTM 分红）滞后于分母（已反映分红
      的价格），系统性低估真实股息率。

    数据来源：
      同花顺 stock_hk_fhpx_detail_ths 提供「公告日期 + 除净日 + 进度 + 类型」。
      已校验 除净日 与 yfinance ex_date 一一对应（yfinance 的 ex_date 全部能
      在 THS 除净日 里精确匹配到），故用 (code, 除净日) 做 join 补齐公告日，
      金额仍用 yfinance 的干净港元口径，不做文本重解析。

    Returns:
        DataFrame[code, ex_date, div_per_share, announce_date]，其中
        announce_date 为该笔分红的首次公告日；无法匹配者填 NaT（切片时回退
        ex_date，保守不产生前视）。
    """
    import akshare as ak
    from concurrent.futures import ThreadPoolExecutor, as_completed

    _ensure_pit_dir()
    hist = load_dividend_history()
    if hist.empty:
        logger.info("分红历史为空，跳过公告日采集")
        return hist

    codes = [str(c).zfill(5) for c in codes]
    hist_codes = set(hist["code"])
    # 增量：已有非空公告日的股票跳过（force 时重拉）
    if not force:
        have = set(hist.loc[hist["announce_date"].notna(), "code"])
    else:
        have = set()
    todo = [c for c in codes if c in hist_codes and c not in have]

    if not todo:
        logger.info("公告日：%d 只全部已补齐，跳过采集", len(codes))
        return hist

    def _one(code: str):
        try:
            symbol = str(int(code)).zfill(4)  # 00001→0001, 00316→0316, 09988→9988
            df = ak.stock_hk_fhpx_detail_ths(symbol=symbol)
            if df is None or df.empty or "除净日" not in df.columns:
                return code, None
            df = df.copy()
            df["除净日"] = pd.to_datetime(df["除净日"], errors="coerce")
            df["公告日期"] = pd.to_datetime(df["公告日期"], errors="coerce")
            paid = df[df["除净日"].notna() & df["公告日期"].notna()]
            if paid.empty:
                return code, None
            # 同一除净日可能有多行（普通+特别同除净日），取最早公告日
            paid = paid.sort_values("公告日期")
            m = paid.drop_duplicates("除净日", keep="first").set_index("除净日")["公告日期"]
            return code, m
        except Exception as e:
            logger.warning("[%s] 公告日采集失败: %s", code, str(e)[:80])
            return code, None

    maps, failed = {}, []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(_one, c): c for c in todo}
        for f in as_completed(futs):
            code, m = f.result()
            if m is None or len(m) == 0:
                failed.append(code)
            else:
                maps[code] = m

    if not maps:
        logger.info("公告日：本次 %d 只全部失败/为空（失败 %d 只）", len(todo), len(failed))
        return hist

    hist["ex_date"] = pd.to_datetime(hist["ex_date"])
    filled = 0
    for code, m in maps.items():
        idx = hist["code"] == code
        ann = hist.loc[idx, "ex_date"].map(m)
        hist.loc[idx, "announce_date"] = ann.fillna(hist.loc[idx, "announce_date"])
        filled += int(ann.notna().sum())

    hist["announce_date"] = pd.to_datetime(hist["announce_date"])
    hist.to_csv(DIVIDEND_HISTORY_CSV, index=False, encoding="utf-8-sig")
    n_cov = int(hist["announce_date"].notna().sum())
    logger.info("公告日已补齐：%d/%d 条有公告日（本次填充 %d 条，失败 %d 只）",
                n_cov, len(hist), filled, len(failed))
    return hist


# ──────────────────────────────────────────────────────────────────
# 时点切片（as-of）
# ──────────────────────────────────────────────────────────────────
def _slice_dates(hist: pd.DataFrame, event_col: str) -> pd.Series:
    """事件切片日期：优先 event_col，缺失/异常处回退 ex_date（保守，不产生前视）。

    event_col="announce_date" 时，用首次公告日识别分红进入 TTM 的时点。
    两条保守规则：
    1. 公告日缺失（NaT）→ 回退 ex_date（晚于公告日，只会漏算、不会提前算）；
    2. 公告日晚于除息日（同花顺数据异常，真实公告必早于除息）→ 回退 ex_date。
    """
    ex = pd.to_datetime(hist["ex_date"])
    if event_col == "ex_date" or event_col not in hist.columns:
        return ex
    ann = pd.to_datetime(hist[event_col])
    return ann.where(ann <= ex, ex).fillna(ex)


def ttm_dividend_as_of(code: str, as_of: pd.Timestamp, months: int = 12,
                       div_history: Optional[pd.DataFrame] = None,
                       event_col: str = "ex_date") -> Optional[float]:
    """t 时点前 `months` 个月内的 TTM 每股分红（point-in-time 正确）。

    event_col 指定事件时点口径：ex_date（除息日，机械）或 announce_date
    （首次公告日，信息事件）。只用 event ∈ (as_of - months, as_of] 的分红，
    天然排除未来数据。
    """
    if div_history is None:
        div_history = load_dividend_history()
    if div_history.empty:
        return None
    sub = div_history[div_history["code"] == str(code).zfill(5)]
    if sub.empty:
        return 0.0
    as_of = pd.Timestamp(as_of)
    start = as_of - pd.DateOffset(months=months)
    ev = _slice_dates(sub, event_col)
    mask = (ev <= as_of) & (ev > start)
    win = sub[mask]
    return float(win["div_per_share"].sum()) if len(win) else 0.0


def ttm_dividend_matrix(div_history: pd.DataFrame, as_of_dates: Iterable[pd.Timestamp],
                        months: int = 12, event_col: str = "ex_date") -> pd.DataFrame:
    """批量：返回 index=code、columns=时点 的 TTM 分红矩阵（向量化）。

    对每个时点 t，计算「事件日 ∈ (t-months, t]」的分红按股票求和。
    event_col 控制事件时点口径（ex_date / announce_date）。
    缺失的股票填 0（该时点无分红）。供 backfill 一次性重建多期排名。
    """
    hist = div_history.copy()
    hist["_ev"] = _slice_dates(hist, event_col)
    out = {}
    for t in as_of_dates:
        t = pd.Timestamp(t)
        start = t - pd.DateOffset(months=months)
        win = hist[(hist["_ev"] <= t) & (hist["_ev"] > start)]
        out[t.strftime("%Y-%m")] = win.groupby("code")["div_per_share"].sum()
    mat = pd.DataFrame(out)
    return mat.fillna(0.0)


def dividend_as_of_series(code: str, as_of_dates: Iterable[pd.Timestamp],
                          months: int = 12,
                          div_history: Optional[pd.DataFrame] = None,
                          event_col: str = "ex_date") -> pd.Series:
    """批量：对一组时点返回对应的 TTM 分红序列（回测调仓用）。"""
    if div_history is None:
        div_history = load_dividend_history()
    sub = div_history[div_history["code"] == str(code).zfill(5)].copy()
    if not sub.empty:
        sub["_ev"] = _slice_dates(sub, event_col)
    out = {}
    for t in as_of_dates:
        t = pd.Timestamp(t)
        if sub.empty:
            out[t] = 0.0
            continue
        start = t - pd.DateOffset(months=months)
        win = sub[(sub["_ev"] <= t) & (sub["_ev"] > start)]
        out[t] = float(win["div_per_share"].sum()) if len(win) else 0.0
    return pd.Series(out)


# ──────────────────────────────────────────────────────────────────
# 特别股息序列（用于可持续回报率，剔除一次性特别股息）
# ──────────────────────────────────────────────────────────────────
_SPECIAL_AMOUNT_PATTERNS = [
    # 美元分红：每股派美元X元(相当于港币Y元) → 取港币口径 Y
    re.compile(r"相当于港币\s*([\d.]+)\s*元"),
    # 港币直接分红：每股派港币X元 / 每股派港元X元
    re.compile(r"每股派港币\s*([\d.]+)\s*元"),
    re.compile(r"每股派港元\s*([\d.]+)\s*元"),
]


def _parse_special_div_amount(text) -> Optional[float]:
    """从 akshare 分红方案文本解析「每股特别股息金额（港元）」。

    只识别现金型特别股息；实物分派（分股票，无现金金额，yfinance 里本来
    就没有这条分红）返回 None，不参与剔除。
    """
    if not isinstance(text, str):
        return None
    for pat in _SPECIAL_AMOUNT_PATTERNS:
        m = pat.search(text)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                return None
    return None


def collect_special_dividend_history(codes: Iterable[str], force: bool = False,
                                     max_workers: int = 4) -> pd.DataFrame:
    """采集特别股息序列（akshare 分红方案「特别分配」的除净日 + 每股金额），增量缓存。

    特别股息是一过性（出售资产、一次性派发），不应计入「可持续回报率」。
    本函数用 akshare 的「分配类型=特别分配」精确识别，解析每股港币金额。

    Returns:
        DataFrame[code, ex_date, special_div_per_share]
    """
    import akshare as ak
    from concurrent.futures import ThreadPoolExecutor, as_completed

    _ensure_pit_dir()
    existing = load_special_dividend_history()
    have_codes = set(existing["code"]) if not existing.empty else set()

    codes = [str(c).zfill(5) for c in codes]
    todo = [c for c in codes if force or c not in have_codes]
    if not todo:
        logger.info("特别股息：%d 只全部已缓存，跳过采集", len(codes))
        return existing

    def _one(code: str):
        try:
            df = ak.stock_hk_dividend_payout_em(symbol=code)
            if df is None or df.empty or "分配类型" not in df.columns:
                return code, []
            sp = df[df["分配类型"].astype(str).str.contains("特别", na=False)]
            rows = []
            for _, r in sp.iterrows():
                ex = pd.to_datetime(r.get("除净日"), errors="coerce")
                amt = _parse_special_div_amount(r.get("分红方案"))
                if pd.notna(ex) and amt is not None:
                    rows.append({"code": code, "ex_date": ex.normalize(),
                                 "special_div_per_share": amt})
            return code, rows
        except Exception as e:
            logger.warning("[%s] 特别股息采集失败: %s", code, str(e)[:80])
            return code, None

    records, failed = [], []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(_one, c): c for c in todo}
        for f in as_completed(futs):
            code, rows = f.result()
            if rows is None:
                failed.append(code)
            else:
                records.extend(rows)

    new_df = pd.DataFrame(records, columns=["code", "ex_date", "special_div_per_share"]) if records else pd.DataFrame()
    if new_df.empty:
        logger.info("特别股息：本次 %d 只全部无特别股息/失败（失败 %d 只）", len(todo), len(failed))
        return existing

    new_df["ex_date"] = pd.to_datetime(new_df["ex_date"])
    new_df["code"] = new_df["code"].astype(str).str.zfill(5)
    combined = pd.concat([existing, new_df], ignore_index=True) if not existing.empty else new_df
    combined = combined.drop_duplicates(subset=["code", "ex_date"], keep="last")
    combined = combined.sort_values(["code", "ex_date"]).reset_index(drop=True)
    combined.to_csv(SPECIAL_DIVIDEND_CSV, index=False, encoding="utf-8-sig")

    logger.info("特别股息已保存：%d 只、%d 条（失败 %d 只）",
                combined["code"].nunique(), len(combined), len(failed))
    return combined


def load_special_dividend_history() -> pd.DataFrame:
    """加载特别股息序列缓存（无则返回空 DataFrame）。"""
    if not os.path.exists(SPECIAL_DIVIDEND_CSV):
        return pd.DataFrame(columns=["code", "ex_date", "special_div_per_share"])
    df = pd.read_csv(SPECIAL_DIVIDEND_CSV, dtype={"code": str})
    df["ex_date"] = pd.to_datetime(df["ex_date"])
    return df


def ttm_special_dividend_matrix(special_history: pd.DataFrame,
                                as_of_dates: Iterable[pd.Timestamp],
                                months: int = 12,
                                event_col: str = "ex_date") -> pd.DataFrame:
    """批量：index=code、columns=时点 的 TTM 特别股息矩阵（向量化）。

    供 sustainable_return_rate 使用：可持续分红 = TTM 分红 − TTM 特别股息。
    event_col 同 ttm_dividend_matrix（特别股息暂只有 ex_date，未来可补公告日）。
    """
    hist = special_history.copy()
    hist["_ev"] = _slice_dates(hist, event_col)
    out = {}
    for t in as_of_dates:
        t = pd.Timestamp(t)
        start = t - pd.DateOffset(months=months)
        win = hist[(hist["_ev"] <= t) & (hist["_ev"] > start)]
        out[t.strftime("%Y-%m")] = win.groupby("code")["special_div_per_share"].sum()
    mat = pd.DataFrame(out)
    return mat.fillna(0.0)
