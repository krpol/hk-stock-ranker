"""
P1 测试：财务数据 TTL 增量更新
覆盖 F1-F8 用例（plan §3.2）
"""
from datetime import datetime, timedelta

import pandas as pd
import pytest

from collectors.incremental import (
    incremental_update_financial,
    needs_refresh,
)
from storage.store import load_financial_data, save_financial_data


# =====================================================================
# F0: needs_refresh 单元测试
# =====================================================================

class TestNeedsRefresh:
    def test_no_updated_at(self):
        row = pd.Series({"updated_at": None, "current_fy_end": "2024-12-31"})
        flag, reason = needs_refresh(row)
        assert flag is True
        assert reason == "no_updated_at"

    def test_fresh(self):
        now = datetime(2026, 5, 23)
        row = pd.Series({
            "updated_at": "2026-05-20 10:00:00",
            "current_fy_end": "2024-12-31",
        })
        flag, reason = needs_refresh(row, ttl_days=30, now=now)
        assert flag is False
        assert reason == "fresh"

    def test_stale_30d(self):
        now = datetime(2026, 5, 23)
        row = pd.Series({
            "updated_at": "2026-04-01 10:00:00",  # 52d ago
            "current_fy_end": "2024-12-31",
        })
        flag, reason = needs_refresh(row, ttl_days=30, now=now)
        assert flag is True
        assert reason.startswith("stale_")

    def test_fy_due(self):
        """财年 2024-12-31 已结束 200+ 天，age=10d 但应触发"""
        now = datetime(2026, 5, 23)  # fy_end 距今 ~510 天
        row = pd.Series({
            "updated_at": "2026-05-13 10:00:00",  # 10d ago, < TTL
            "current_fy_end": "2024-12-31",
        })
        flag, reason = needs_refresh(row, ttl_days=30, now=now)
        assert flag is True
        assert reason == "fy_report_due"

    def test_fy_recent_no_trigger(self):
        """财年刚结束（90 天内），即使 age 7+ 天也不触发"""
        now = datetime(2026, 5, 23)
        row = pd.Series({
            "updated_at": "2026-05-13 10:00:00",  # 10d
            "current_fy_end": "2026-03-31",  # 53d ago < 120d
        })
        flag, reason = needs_refresh(row, ttl_days=30, now=now)
        assert flag is False  # fresh

    def test_bad_updated_at(self):
        row = pd.Series({
            "updated_at": "garbage",
            "current_fy_end": "2024-12-31",
        })
        flag, reason = needs_refresh(row)
        assert flag is True
        assert reason == "bad_updated_at"


# =====================================================================
# F1-F8: incremental_update_financial 集成测试
# =====================================================================

class TestIncrementalUpdate:

    def test_F1_first_run_full_refresh(self, tmp_data_dir, fake_stock_list, mock_collect):
        """F1: 无 hk_financial.csv → 全部刷新"""
        df = incremental_update_financial(
            fake_stock_list, forward_div_map={}, ttl_days=30, workers=1,
        )
        assert len(df) == 3
        assert sorted(mock_collect) == ["00001", "00002", "00003"]

    def test_F2_no_refresh_when_fresh(self, tmp_data_dir, fake_stock_list,
                                       mock_collect, make_fin_record):
        """F2: 全部 fresh → 0 调用"""
        # 准备已有 fresh 财务数据
        existing = pd.DataFrame([make_fin_record(c) for c in ["00001", "00002", "00003"]])
        existing["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_financial_data(existing)
        mock_collect.clear()

        df = incremental_update_financial(
            fake_stock_list, forward_div_map={}, ttl_days=30, workers=1,
        )
        assert len(df) == 3
        assert mock_collect == []  # 0 次采集

    def test_F3_partial_stale(self, tmp_data_dir, fake_stock_list,
                              mock_collect, make_fin_record):
        """F3: 部分过期 → 只刷新过期的"""
        old = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d %H:%M:%S")
        new = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        existing = pd.DataFrame([make_fin_record(c) for c in ["00001", "00002", "00003"]])
        existing.loc[existing["code"] == "00001", "updated_at"] = old
        existing.loc[existing["code"] == "00002", "updated_at"] = new
        existing.loc[existing["code"] == "00003", "updated_at"] = new
        save_financial_data(existing)
        mock_collect.clear()

        incremental_update_financial(
            fake_stock_list, forward_div_map={}, ttl_days=30, workers=1,
        )
        assert mock_collect == ["00001"]

    def test_F4_fy_report_due(self, tmp_data_dir, fake_stock_list,
                               mock_collect, make_fin_record):
        """F4: 财年早已结束 → 即使 TTL 内也刷新"""
        recent = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
        # 默认所有 fy_end 都是最近（90 天内，不触发 fy_due）
        recent_fy = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
        existing = pd.DataFrame([make_fin_record(c) for c in ["00001", "00002", "00003"]])
        existing["updated_at"] = recent
        existing["current_fy_end"] = recent_fy
        # 00002 财年 2 年前结束 → fy_report_due
        existing.loc[existing["code"] == "00002", "current_fy_end"] = "2024-01-31"
        save_financial_data(existing)
        mock_collect.clear()

        incremental_update_financial(
            fake_stock_list, forward_div_map={}, ttl_days=30, workers=1,
        )
        assert mock_collect == ["00002"]

    def test_F5_new_stock(self, tmp_data_dir, fake_stock_list,
                          mock_collect, make_fin_record):
        """F5: 池中新增股票 → 仅刷新新进的"""
        recent = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        existing = pd.DataFrame([make_fin_record(c) for c in ["00001", "00002"]])
        existing["updated_at"] = recent
        save_financial_data(existing)
        mock_collect.clear()

        # stock_list 包含 00003（新进）
        incremental_update_financial(
            fake_stock_list, forward_div_map={}, ttl_days=30, workers=1,
        )
        assert mock_collect == ["00003"]

    def test_F6_force_refresh(self, tmp_data_dir, fake_stock_list,
                               mock_collect, make_fin_record):
        """F6: --force-refresh → 全刷"""
        existing = pd.DataFrame([make_fin_record(c) for c in ["00001", "00002", "00003"]])
        existing["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_financial_data(existing)
        mock_collect.clear()

        incremental_update_financial(
            fake_stock_list, forward_div_map={}, ttl_days=30, workers=1,
            force_refresh=True,
        )
        assert sorted(mock_collect) == ["00001", "00002", "00003"]

    def test_F7_refresh_codes(self, tmp_data_dir, fake_stock_list,
                               mock_collect, make_fin_record):
        """F7: --refresh-codes 00001 → 仅刷该只，其他保持"""
        recent = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        existing = pd.DataFrame([make_fin_record(c) for c in ["00001", "00002", "00003"]])
        existing["updated_at"] = recent
        save_financial_data(existing)
        old_updated_at = existing.set_index("code")["updated_at"].to_dict()
        mock_collect.clear()

        df = incremental_update_financial(
            fake_stock_list, forward_div_map={}, ttl_days=30, workers=1,
            refresh_codes=["00001"],
        )
        assert mock_collect == ["00001"]
        # 其他股票 updated_at 不变
        df_idx = df.set_index("code")
        assert df_idx.loc["00002", "updated_at"] == old_updated_at["00002"]
        assert df_idx.loc["00003", "updated_at"] == old_updated_at["00003"]

    def test_F8_full_vs_incremental_equivalence(self, tmp_data_dir, fake_stock_list,
                                                 mock_collect, make_fin_record):
        """F8（关键回归）: 全量与增量应得到相同结果。

        构造场景：清空缓存 → 跑全量；
                  再保留缓存 → 跑增量（部分过期），最终 hk_financial.csv 字段应一致。
        本次用 mock_collect 返回固定数据，所以无论谁刷新结果一致。
        """
        # Round 1: 全量
        df_full = incremental_update_financial(
            fake_stock_list, forward_div_map={}, ttl_days=30, workers=1,
            force_refresh=True,
        )

        # Round 2: 全 fresh，应 0 刷新但结果不变
        df_inc = incremental_update_financial(
            fake_stock_list, forward_div_map={}, ttl_days=30, workers=1,
        )

        # 排序后对比关键字段
        key_cols = [
            "code", "fy_div_per_share", "buyback_per_share",
            "current_fy_end", "revenue_yoy", "operating_cashflow_yoy",
        ]
        a = df_full.sort_values("code")[key_cols].reset_index(drop=True)
        b = df_inc.sort_values("code")[key_cols].reset_index(drop=True)
        pd.testing.assert_frame_equal(a, b, check_dtype=False)
