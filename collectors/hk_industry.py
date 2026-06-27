"""
港股行业（所属行业）采集

数据源：AKShare `stock_hk_company_profile_em(symbol)` 的「所属行业」字段
       （来源：东方财富对 HKEX 公司资料的结构化解析）

特性：
- 行业归属相对静态（数月不变），独立缓存到 data/csv/hk_industry.csv
- 与分红/财务采集解耦，避免互相争抢限流
- 增量友好：默认仅补采 industry 缺失或为空的股票

公开 API：
- get_industry(code: str) -> Optional[str]   # 单只
- collect_industries(codes, max_workers=8) -> dict[code, industry]
"""
import logging
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterable, Optional

import pandas as pd

logger = logging.getLogger(__name__)


def get_industry(code: str, max_retries: int = 3) -> Optional[str]:
    """
    拉取单只港股的「所属行业」。

    Args:
        code: 5 位港股代码（如 "00700"）

    Returns:
        所属行业字符串（如 "软件服务" / "工业工程" / "地产"），失败/无数据返回 None。
    """
    try:
        import akshare as ak
    except ImportError:
        logger.error("akshare 未安装")
        return None

    code_str = str(code).zfill(5)
    last_err = None
    for attempt in range(max_retries):
        try:
            df = ak.stock_hk_company_profile_em(symbol=code_str)
            if df is None or df.empty or "所属行业" not in df.columns:
                return None
            ind = df.iloc[0]["所属行业"]
            if pd.isna(ind) or not str(ind).strip():
                return None
            return str(ind).strip()
        except Exception as e:
            last_err = e
            time.sleep(random.uniform(0.5, 1.5) * (attempt + 1))
    logger.debug(f"[{code}] 拉取行业失败: {last_err}")
    return None


def collect_industries(
    codes: Iterable[str],
    max_workers: int = 8,
    log_every: int = 50,
) -> dict:
    """
    并发批量采集行业数据。

    Returns:
        {code: industry_or_None}
    """
    codes = [str(c).zfill(5) for c in codes]
    total = len(codes)
    if total == 0:
        return {}

    logger.info(f"开始采集 {total} 只港股行业数据（线程池 {max_workers}）")
    t0 = time.time()
    out: dict = {}
    succ = 0
    fail = 0

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(get_industry, c): c for c in codes}
        for i, fut in enumerate(as_completed(futures), 1):
            code = futures[fut]
            try:
                ind = fut.result()
            except Exception as e:
                ind = None
                logger.debug(f"[{code}] 异常: {e}")
            out[code] = ind
            if ind:
                succ += 1
            else:
                fail += 1

            if i % log_every == 0 or i == total:
                elapsed = time.time() - t0
                speed = i / elapsed if elapsed > 0 else 0
                eta = (total - i) / speed / 60 if speed > 0 else 0
                logger.info(
                    f"[{i}/{total}] 速度 {speed:.1f} 股/秒 ETA {eta:.1f} 分；"
                    f"成功 {succ} 失败 {fail}"
                )

    logger.info(f"行业采集完成：成功 {succ}/{total}，失败 {fail}，"
                f"总耗时 {(time.time()-t0)/60:.1f} 分钟")
    return out
