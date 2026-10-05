"""
港股预分红爬虫
数据来源：港交所披露易（hkexnews.hk）公司行动公告

策略：
  1. 搜索披露易中标题含"派息"/"股息"/"股利"等关键词的公告
  2. 解析公告标题和摘要中的每股金额与除息日
  3. 若除息日 > 今天，视为"已公告未除息"的预分红
  4. 结果保存到 data/csv/hk_forward_dividend.csv，可被 hk_financial.py 读取覆盖

注意：
  - 港交所不允许高频爬取，每次请求间隔 >= 1 秒
  - 标题关键词解析覆盖常见格式，但无法覆盖100%；建议对重点股票人工复核
"""

import re
import time
import logging
import os
from datetime import datetime, date
from typing import Optional
from zoneinfo import ZoneInfo

import requests
import akshare as ak
import pandas as pd
from bs4 import BeautifulSoup

from config.settings import CSV_DIR, AKSHARE_SLEEP

logger = logging.getLogger(__name__)

HKT = ZoneInfo("Asia/Hong_Kong")

# 披露易公告搜索 API
HKEX_SEARCH_URL = "https://www.hkexnews.hk/sdw/search/searchsdw.aspx"
HKEX_SEARCH_API = "https://www.hkexnews.hk/sdw/search/getsearchresultxml.aspx"

# 搜索关键词（公司行动 - 派息相关）
DIVIDEND_KEYWORDS = ["末期息", "末期股息", "特别息", "特别股息", "中期息", "中期股息", "股息"]

# 公告标题解析：提取金额（港元/股）
# 例："每股派发末期息港币 1.60 元" / "末期股息每股0.24港元" / "每股派息HK$0.84"
AMOUNT_PATTERNS = [
    r"(?:港幣|港元|港币|HK\$|HKD)\s*([0-9]+(?:\.[0-9]+)?)\s*(?:元|港元|港幣)?",
    r"([0-9]+(?:\.[0-9]+)?)\s*(?:港元|港幣|港币)(?:每股|\/股)",
    r"每股[^0-9]*([0-9]+(?:\.[0-9]+)?)\s*(?:港元|港幣|港币|元)",
]

# 除息日/股权登记日解析
DATE_PATTERNS = [
    r"除息日[為为：:]\s*(\d{4}年\d{1,2}月\d{1,2}日)",
    r"除息日[為为：:]\s*(\d{4}[-/]\d{1,2}[-/]\d{1,2})",
    r"(\d{4}年\d{1,2}月\d{1,2}日)\s*(?:為|为|作為|作为)?除息",
]

FORWARD_DIV_CSV = os.path.join(CSV_DIR, "hk_forward_dividend.csv")


def _parse_amount(text: str) -> Optional[float]:
    """从公告文本中提取每股金额（港元）"""
    for pattern in AMOUNT_PATTERNS:
        m = re.search(pattern, text)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                continue
    return None


def _parse_date(text: str) -> Optional[date]:
    """从公告文本中提取除息日"""
    for pattern in DATE_PATTERNS:
        m = re.search(pattern, text)
        if m:
            raw = m.group(1)
            # 中文格式
            chinese = re.match(r"(\d{4})年(\d{1,2})月(\d{1,2})日", raw)
            if chinese:
                try:
                    return date(int(chinese.group(1)), int(chinese.group(2)), int(chinese.group(3)))
                except ValueError:
                    continue
            # 数字格式
            nums = re.split(r"[-/]", raw)
            if len(nums) == 3:
                try:
                    return date(int(nums[0]), int(nums[1]), int(nums[2]))
                except ValueError:
                    continue
    return None


def search_hkex_announcements(
    stock_code: str,
    keyword: str = "股息",
    date_from: str = "",
    date_to: str = "",
) -> list[dict]:
    """
    搜索披露易公告列表。

    Args:
        stock_code: 港股代码（4位，如 "0700"），不含前导零到5位
        keyword: 搜索关键词
        date_from / date_to: 日期范围，格式 "YYYYMMDD"，空字符串表示不限

    Returns:
        公告列表，每项包含 title, date, url
    """
    # 披露易用4位代码
    code4 = stock_code.lstrip("0") or "0"
    code4 = code4.zfill(4)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.hkexnews.hk/sdw/search/searchsdw.aspx",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    params = {
        "lang": "chi",
        "StockID": code4,
        "DateOfRelease_From": date_from,
        "DateOfRelease_To": date_to,
        "Keyword": keyword,
        "TitleOnly": "0",
        "FormType": "",
        "Category": "0",
        "Market": "SEHK",
    }

    results = []
    try:
        resp = requests.get(
            HKEX_SEARCH_API,
            params=params,
            headers=headers,
            timeout=20,
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        # 披露易返回 XML/HTML，公告列表在 <result> 或 <tr> 元素中
        rows = soup.find_all("result") or soup.find_all("tr", class_=re.compile(r"odd|even"))
        for row in rows:
            title_el = row.find("title") or row.find("td", class_=re.compile("title", re.I))
            date_el = row.find("date") or row.find("td", class_=re.compile("date", re.I))
            url_el = row.find("url") or row.find("a", href=True)

            title = title_el.get_text(strip=True) if title_el else ""
            pub_date = date_el.get_text(strip=True) if date_el else ""
            url = url_el["href"] if url_el and url_el.get("href") else ""

            if title:
                results.append({"title": title, "date": pub_date, "url": url})

    except Exception as e:
        logger.warning(f"[{stock_code}] 搜索公告失败({keyword}): {e}")

    time.sleep(AKSHARE_SLEEP)
    return results


def fetch_announcement_text(url: str) -> str:
    """
    获取公告正文（纯文本），用于精确提取金额和日期。
    仅抓取 HTML 正文，不下载 PDF。
    """
    if not url or url.endswith(".pdf"):
        return ""
    if not url.startswith("http"):
        url = "https://www.hkexnews.hk" + url

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
    }
    try:
        resp = requests.get(url, headers=headers, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        # 去掉脚本和样式
        for tag in soup(["script", "style"]):
            tag.decompose()
        return soup.get_text(separator=" ", strip=True)
    except Exception as e:
        logger.warning(f"获取公告正文失败({url}): {e}")
        return ""


def get_forward_dividend(code: str, lookback_days: int = 180) -> Optional[dict]:
    """
    搜索指定股票最近 lookback_days 天内的"已公告未除息"预分红。

    返回最新一条未除息记录，格式：
    {
        "code": str,
        "forward_div_per_share": float,   # 港元/股
        "ex_date": str,                   # 预计除息日 "YYYY-MM-DD"（若能解析）
        "announcement_title": str,
        "announcement_date": str,
        "source": "hkexnews"
    }
    若无则返回 None。
    """
    today = datetime.now(HKT).date()
    date_from = (today - pd.DateOffset(days=lookback_days)).strftime("%Y%m%d")
    date_to = today.strftime("%Y%m%d")

    candidates = []

    for kw in DIVIDEND_KEYWORDS:
        announcements = search_hkex_announcements(code, keyword=kw, date_from=date_from, date_to=date_to)
        for ann in announcements:
            title = ann.get("title", "")
            # 先从标题快速解析
            amount = _parse_amount(title)
            ex_date = _parse_date(title)

            # 若标题解析不出金额，尝试抓正文（限制为前3条，避免超时）
            if amount is None and ann.get("url") and len(candidates) < 3:
                text = fetch_announcement_text(ann["url"])
                if text:
                    amount = _parse_amount(text)
                    if ex_date is None:
                        ex_date = _parse_date(text)
                time.sleep(AKSHARE_SLEEP)

            if amount is not None and amount > 0:
                # 若除息日已解析且在今天之后，确认为预分红
                # 若除息日未解析，也保留（不确定是否已除息，标注 unknown）
                is_future = (ex_date is None) or (ex_date > today)
                if is_future:
                    candidates.append({
                        "code": code,
                        "forward_div_per_share": amount,
                        "ex_date": ex_date.isoformat() if ex_date else "unknown",
                        "announcement_title": title,
                        "announcement_date": ann.get("date", ""),
                        "source": "hkexnews",
                    })

        if candidates:
            break  # 找到就停，不继续搜其他关键词

    if not candidates:
        return None

    # 返回最新公告的那条（按公告日期降序）
    candidates.sort(key=lambda x: x["announcement_date"], reverse=True)
    return candidates[0]


# ── 东方财富预分红 ─────────────────────────────────────────────
def get_forward_div_from_em(code: str) -> Optional[dict]:
    """
    从东方财富（AKShare stock_hk_dividend_payout_em）获取预分红。
    优势：结构化数据，自动处理多币种到港元换算，速度快于 HKEX 爬虫。

    Returns:
        {
            "code": str,
            "forward_div_per_share": float,   # 港元/股
            "ex_date": str,                   # 预计除息日 "YYYY-MM-DD"
            "currency": str,                  # 原始币种 "HKD"/"USD"/"RMB"
            "fiscal_year": str,               # 财政年度
            "plan_text": str,                 # 原始分红方案文本
            "source": "eastmoney"
        }
        若无预分红返回 None。
    """
    try:
        df = ak.stock_hk_dividend_payout_em(symbol=code)
        if df.empty:
            return None

        today = datetime.now(HKT).date()

        for _, row in df.iterrows():
            ex_date_str = str(row.get("除净日", ""))
            if not ex_date_str or ex_date_str == "nan" or ex_date_str == "NaT":
                continue

            try:
                ex_date = datetime.strptime(ex_date_str.strip()[:10], "%Y-%m-%d").date()
            except ValueError:
                continue

            # 除净日 > 今天 = 未除息的预分红
            if ex_date <= today:
                continue

            plan_text = str(row.get("分红方案", ""))
            amount_hkd, currency = _parse_em_div_amount(plan_text)

            if amount_hkd is None or amount_hkd <= 0:
                continue

            return {
                "code": code,
                "forward_div_per_share": amount_hkd,
                "ex_date": ex_date.isoformat(),
                "currency": currency,
                "fiscal_year": str(row.get("财政年度", "")),
                "plan_text": plan_text,
                "source": "eastmoney",
            }

        return None  # 有数据但无预分红
    except Exception as e:
        logger.debug(f"[{code}] 东方财富预分红获取失败: {e}")
        return None


def _parse_em_div_amount(plan_text: str) -> tuple[Optional[float], str]:
    """
    从东方财富的分红方案文本中提取港元金额和币种。

    例：
      "每股派港币5.3元"                              → (5.3, "HKD")
      "每股派美元0.1元(相当于港币0.783595元(计算值))"   → (0.783595, "USD")
      "每股派人民币2.276元(相当于港币2.52元)"           → (2.52, "RMB")

    Returns:
        (amount_hkd_or_None, currency_str)
    """
    if not plan_text:
        return None, ""

    # 优先匹配括号内港元等值（东方财富自动换算）
    m_hkd = re.search(r"相当于港币\s*([\d.]+)\s*元", plan_text)
    if m_hkd:
        return float(m_hkd.group(1)), _guess_currency(plan_text)

    # 直接提取港元
    m_direct = re.search(r"港币\s*([\d.]+)\s*元", plan_text)
    if m_direct:
        return float(m_direct.group(1)), "HKD"

    # 其他币种无港元换算时返回 None（极少数情况）
    return None, _guess_currency(plan_text)


def _guess_currency(plan_text: str) -> str:
    if "美元" in plan_text:
        return "USD"
    if "人民币" in plan_text or "人 民 币" in plan_text:
        return "RMB"
    return "HKD"


def collect_forward_dividends(
    codes: list[str],
    lookback_days: int = 180,
) -> pd.DataFrame:
    """
    双源采集股票池的预分红数据（HKEX 爬虫 + 东方财富），交叉验证后保存到 CSV。

    Args:
        codes:  港股代码列表（5位字符串）
        lookback_days: HKEX 向前搜索天数

    Returns:
        DataFrame with columns: code, forward_div_per_share, ex_date, source, ...
    """
    comparison_rows = []  # 双源对比
    records = []          # 最终使用的预分红记录

    # B49: HKEX 披露易 API 故障时（2026-08 返回 HTTP 500 Runtime Error）可跳过该源。
    # 决策逻辑本就是东财优先（见下方 L405），HKEX 仅作交叉验证，
    # 跳过后速度快 200 倍（902 只：9 小时 → 2.6 分钟）。
    # HKEX 恢复后不设该变量即自动恢复双源。
    _skip_hkex = os.environ.get("SKIP_HKEX_DIV") == "1"
    if _skip_hkex:
        logger.warning("SKIP_HKEX_DIV=1 → 跳过 HKEX 披露易爬取，仅用东方财富单源（无交叉验证）")

    for i, code in enumerate(codes):
        logger.info(f"[{i+1}/{len(codes)}] {code} 搜索预分红公告...")

        # 两路并行采集
        hkex_result = None if _skip_hkex else get_forward_dividend(code, lookback_days=lookback_days)
        em_result = get_forward_div_from_em(code)

        # ── 交叉验证 ──
        hkex_amount = hkex_result["forward_div_per_share"] if hkex_result else None
        em_amount = em_result["forward_div_per_share"] if em_result else None
        em_currency = em_result.get("currency", "") if em_result else ""

        # 计算差异
        diff_pct = 0.0
        if hkex_amount and em_amount and em_amount > 0:
            diff_pct = round(abs(hkex_amount - em_amount) / em_amount * 100, 2)

        comparison_rows.append({
            "code": code,
            "hkex_amount": hkex_amount,
            "em_amount": em_amount,
            "em_currency": em_currency,
            "diff_pct": diff_pct,
            "em_ex_date": em_result["ex_date"] if em_result else None,
            "hkex_ex_date": hkex_result["ex_date"] if hkex_result else None,
        })

        # ── 决策：使用哪个源 ──
        if em_result and em_amount and em_amount > 0:
            # 东方财富优先（结构化数据更可靠）
            if hkex_amount and diff_pct > 5:
                logger.warning(
                    f"  ⚠ {code}: 双源差异 {diff_pct:.1f}% | "
                    f"HKEX={hkex_amount} HKD | EM={em_amount} HKD | 采用东方财富"
                )
            else:
                logger.info(
                    f"  ✓ {code}: {em_amount} HKD/股 ({em_currency}), "
                    f"除息日={em_result['ex_date']}, 来源东方财富"
                )
            records.append({
                "code": code,
                "forward_div_per_share": em_amount,
                "ex_date": em_result["ex_date"],
                "announcement_title": em_result.get("plan_text", ""),
                "announcement_date": "",
                "source": "eastmoney",
            })
        elif hkex_result and hkex_amount and hkex_amount > 0:
            logger.info(
                f"  ✓ {code}: {hkex_amount} HKD/股, "
                f"除息日={hkex_result.get('ex_date', '?')}, 来源HKEX"
            )
            records.append(hkex_result)
        else:
            logger.info(f"  - {code}: 双源均无预分红")

    # ── 输出比较报告 ──
    df_comp = pd.DataFrame(comparison_rows)
    comp_path = os.path.join(CSV_DIR, "forward_div_comparison.csv")
    df_comp.to_csv(comp_path, index=False, encoding="utf-8-sig")
    logger.info(f"双源对比报告已保存: {comp_path}")

    # ── 保存最终预分红 CSV ──
    df = pd.DataFrame(records) if records else pd.DataFrame(columns=[
        "code", "forward_div_per_share", "ex_date",
        "announcement_title", "announcement_date", "source",
    ])

    os.makedirs(CSV_DIR, exist_ok=True)
    df["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    df.to_csv(FORWARD_DIV_CSV, index=False, encoding="utf-8-sig")
    logger.info(f"预分红数据已保存: {FORWARD_DIV_CSV}，共 {len(df)} 条")

    return df


def load_forward_dividends() -> pd.DataFrame:
    """读取已缓存的预分红数据"""
    if not os.path.exists(FORWARD_DIV_CSV):
        return pd.DataFrame()
    return pd.read_csv(FORWARD_DIV_CSV, dtype={"code": str})


def get_forward_div_map(today: Optional[pd.Timestamp] = None) -> dict:
    """
    返回 {code: forward_div_per_share} 字典，供 hk_financial.py 合并使用。

    B55: 只返回**除息日仍在未来**的记录。forward_div 的语义是「已宣派但未除息」，
    而本 CSV 是某次抓取的快照（可能是数月前），其中大部分条目的 ex_date 早已过去、
    那笔派息已被 AKShare 正常收录为已除息。若仍当 override 注入，
    R4 会误判「AKShare 缺这笔」→ 重复注入 → fy_div 双计。

    today: 基准日（默认系统当天），仅供测试注入固定时点。
    """
    df = load_forward_dividends()
    if df.empty:
        return {}
    today = pd.Timestamp(today).normalize() if today is not None \
        else pd.Timestamp.today().normalize()
    ex = pd.to_datetime(df.get("ex_date"), errors="coerce")
    df = df[ex > today]
    if df.empty:
        return {}
    return dict(zip(df["code"].str.zfill(5), df["forward_div_per_share"].fillna(0.0)))
