"""
港股派息历史采集（带「财政年度」精确归属）

替代原 yfinance + 时间窗口启发式归属，使用 AKShare 的
`stock_hk_dividend_payout_em`（来自东方财富对 HKEX 公告的结构化解析），
其中 `财政年度` 字段直接给出该派息属于哪一年的财报，避免「年报派息晚发
~8 个月被错归到中报期」类 bug（典型案例：09616 海伦司）。

核心 API:
    get_hk_dividends_with_fy_attribution(code) -> List[Dict]
        返回每条派息记录: {
            "ex_date": pd.Timestamp,            # 除净日
            "fiscal_year": int,                  # 归属财政年度（关键！）
            "amount_hkd": float,                 # 已统一换算到 HKD
            "distribution_type": str,            # 年度/中期/特别/一季度/...
            "raw_scheme": str,                   # 分红方案原文（保留供审计）
        }
"""
import logging
import re
import time
import random
from typing import List, Dict, Optional

import pandas as pd

logger = logging.getLogger(__name__)


# ---- 金额解析 ----

# 优先级：先抓「相当于港币 X.X 元」（多币种已换算值），
# 否则抓「每股派港币 X.X 元」（原始港币派息）。
_PATTERNS_HKD = [
    # 相当于港币 1.027545 元 / 相当于港币 1.027545 元(计算值) / 相当于每股派 18.13 港元
    # 注意末尾「元」字 optional：AKShare 部分公告写法不含「元」字
    # 例如 02602 万物云 2024 中期：每股派人民币0.562元(相当于港币0.622)
    re.compile(r"相当于(?:每股派)?港币\s*([\d.]+)\s*元?"),
    re.compile(r"相当于每股派\s*([\d.]+)\s*港元?"),
    # 每股派港币 0.388 元 / 每股派 0.388 港元
    re.compile(r"每股派港币\s*([\d.]+)\s*元?"),
    re.compile(r"每股派\s*([\d.]+)\s*港元?"),
]


def parse_dividend_amount_hkd(scheme_text: str) -> Optional[float]:
    """
    从「分红方案」字符串中提取 HKD 金额。

    支持：
    - 每股派港币5.3元 -> 5.3
    - 每股派美元0.45元(相当于港币3.522942元) -> 3.522942
    - 每股派美元0.13125元(相当于港币1.027545元(计算值)) -> 1.027545
    - 特殊说明:每10股分派1股美团B类普通股股份(相当于每股派18.13港元) -> 18.13

    无法解析时返回 None（记录日志，调用方决定是否跳过）。
    """
    if not scheme_text or not isinstance(scheme_text, str):
        return None
    s = scheme_text.strip()
    for pat in _PATTERNS_HKD:
        m = pat.search(s)
        if m:
            try:
                v = float(m.group(1))
                return v if v > 0 else None
            except (TypeError, ValueError):
                continue
    return None


# ---- 主接口 ----

def get_hk_dividends_with_fy_attribution(code: str,
                                          max_retries: int = 3) -> List[Dict]:
    """
    拉取港股的全量派息记录，每条带「财政年度」精确归属。

    Args:
        code: 5 位港股代码（如 "09616"）

    Returns:
        派息记录列表，按除净日降序。失败/无数据时返回 []。
        每条记录字段：
            ex_date: pd.Timestamp（除净日；可为 NaT 表示尚未除净的预公告）
            fiscal_year: int（财政年度，如 2024）
            amount_hkd: float
            distribution_type: str（"年度分配"/"中期分配"/"特别分配"/"一季度分配"/...）
            raw_scheme: str（原文，便于审计）
    """
    try:
        import akshare as ak
    except ImportError:
        logger.error("akshare 未安装")
        return []

    # AKShare 测试发现：必须用原始（含前导 0 的）5 位代码，去前导 0 反而返回空
    code_str = str(code).zfill(5)

    last_err = None
    for attempt in range(max_retries):
        try:
            df = ak.stock_hk_dividend_payout_em(symbol=code_str)
            break
        except Exception as e:
            last_err = e
            time.sleep(random.uniform(0.5, 1.5) * (attempt + 1))
    else:
        logger.warning(f"[{code}] 拉取派息表失败: {last_err}")
        return []

    if df is None or df.empty:
        return []

    # 必要列存在性检查
    required = {"财政年度", "分红方案", "分配类型", "除净日"}
    if not required.issubset(df.columns):
        logger.warning(f"[{code}] 派息表缺列: {required - set(df.columns)}")
        return []

    out: List[Dict] = []
    for _, row in df.iterrows():
        scheme = row["分红方案"]
        amount = parse_dividend_amount_hkd(scheme)
        if amount is None:
            logger.debug(f"[{code}] 无法解析派息金额: {scheme!r}")
            continue

        # 财政年度：可能是字符串如 "2024" 或 int
        fy_raw = row["财政年度"]
        try:
            fy_year = int(str(fy_raw).strip())
        except (TypeError, ValueError):
            logger.debug(f"[{code}] 财政年度无效: {fy_raw!r}")
            continue

        # 除净日：可能为空（尚未除净的预公告）
        try:
            ex_date = pd.to_datetime(row["除净日"], errors="coerce")
        except Exception:
            ex_date = pd.NaT

        out.append({
            "ex_date": ex_date,
            "fiscal_year": fy_year,
            "amount_hkd": float(amount),
            "distribution_type": str(row["分配类型"]) if pd.notna(row["分配类型"]) else "",
            "raw_scheme": str(scheme),
        })

    # 按除净日降序（NaT 排到最前，这样未除净的预公告作为 "未来" 派息）
    out.sort(key=lambda r: (
        pd.Timestamp.max if pd.isna(r["ex_date"]) else r["ex_date"]
    ), reverse=True)

    return out
