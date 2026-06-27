"""
P2 测试：月度价格存储/裁剪
"""
from datetime import datetime, timedelta

import pandas as pd
import pytest

from storage.store import (
    save_monthly_price,
    load_monthly_price,
    prune_old_data,
)


def _mk_price_row(code, date_str, close=10.0):
    return {
        "code": code,
        "date": date_str,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 1000,
    }


class TestPriceStorage:

    def test_P1_no_duplicate_after_overlap(self, tmp_data_dir):
        """P1: 两次写入有重叠，按 (code, date) 去重，新值覆盖"""
        df1 = pd.DataFrame([
            _mk_price_row("00001", "2025-01-31", 10.0),
            _mk_price_row("00001", "2025-02-28", 11.0),
        ])
        save_monthly_price(df1)

        # 同一日期写新值
        df2 = pd.DataFrame([
            _mk_price_row("00001", "2025-02-28", 12.5),
            _mk_price_row("00001", "2025-03-31", 13.0),
        ])
        save_monthly_price(df2)

        loaded = load_monthly_price()
        # 总行数应为 3（去重后）
        assert len(loaded) == 3
        feb = loaded[(loaded["code"] == "00001") &
                     (loaded["date"] == pd.Timestamp("2025-02-28"))]
        assert len(feb) == 1
        # 新值覆盖旧值
        assert float(feb["close"].iloc[0]) == 12.5

    def test_P2_prune_keeps_recent(self, tmp_data_dir):
        """P2: prune_old_data 仅保留 N 年内"""
        old_date = (datetime.now() - timedelta(days=365 * 6)).strftime("%Y-%m-%d")
        recent_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        df = pd.DataFrame([
            _mk_price_row("00001", old_date, 5.0),
            _mk_price_row("00001", recent_date, 12.0),
        ])
        save_monthly_price(df)

        prune_old_data(keep_years=5)
        loaded = load_monthly_price()
        assert len(loaded) == 1
        assert loaded["close"].iloc[0] == 12.0

    def test_P3_save_zfills_code(self, tmp_data_dir):
        """P3: 写入时 code 补零（确保 '1' → '00001'）"""
        df = pd.DataFrame([
            {**_mk_price_row("1", "2025-01-31"), "code": "1"},
        ])
        save_monthly_price(df)
        loaded = load_monthly_price()
        assert loaded["code"].iloc[0] == "00001"
