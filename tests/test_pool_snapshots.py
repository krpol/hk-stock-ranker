"""
A1+A2: 股票池快照存储 + has_* 函数 as_of_date 参数
"""
from datetime import datetime

import pandas as pd
import pytest


# ============================================================
# A1: storage snapshot
# ============================================================

def test_S1_save_load_snapshot_roundtrip(tmp_data_dir):
    """S1: save/load snapshot 写读一致"""
    from storage.store import (
        save_stock_pool_snapshot,
        load_stock_pool_snapshot,
    )

    df_in = pd.DataFrame([
        {"code": "00001", "name": "A"},
        {"code": "00002", "name": "B"},
        {"code": "9999", "name": "C"},   # 故意不补零，验证 save 时补零
    ])
    as_of = datetime(2022, 12, 31)

    path = save_stock_pool_snapshot(df_in, as_of)
    assert path.endswith("hk_stock_pool_2022-12-31.csv")

    df_out = load_stock_pool_snapshot(as_of)
    assert len(df_out) == 3
    assert set(df_out["code"].tolist()) == {"00001", "00002", "09999"}
    assert (df_out["pool_build_date"] == "2022-12-31").all()


def test_S1_save_empty_skipped(tmp_data_dir):
    """S1b: 空 DataFrame 不写盘"""
    from storage.store import save_stock_pool_snapshot, load_stock_pool_snapshot

    path = save_stock_pool_snapshot(pd.DataFrame(), datetime(2022, 12, 31))
    assert path == ""
    assert load_stock_pool_snapshot(datetime(2022, 12, 31)).empty


def test_S2_list_pool_snapshots_sorted(tmp_data_dir):
    """S2: list_pool_snapshots 升序返回所有 snapshot 日期"""
    from storage.store import save_stock_pool_snapshot, list_pool_snapshots

    df = pd.DataFrame([{"code": "00001", "name": "A"}])
    # 故意乱序写入
    for d in ["2024-12-31", "2020-12-31", "2022-12-31", "2021-12-31"]:
        save_stock_pool_snapshot(df, d)

    dates = list_pool_snapshots()
    assert dates == ["2020-12-31", "2021-12-31", "2022-12-31", "2024-12-31"]


def test_S2b_empty_dir_returns_empty(tmp_data_dir):
    """S2b: 空目录返回空列表"""
    from storage.store import list_pool_snapshots
    assert list_pool_snapshots() == []


# ============================================================
# A2: has_dividend_history / has_share_dilution as_of_date
# ============================================================

class _FakeTickerDivOnly:
    """模拟 yfinance Ticker：history 返回带 Dividends 列的 DataFrame"""
    def __init__(self, div_dates: list[str], div_amounts: list[float]):
        self._div_dates = div_dates
        self._div_amounts = div_amounts

    def history(self, period=None, actions=False, **kwargs):
        idx = pd.to_datetime(self._div_dates)
        return pd.DataFrame({"Dividends": self._div_amounts}, index=idx)


class _FakeTickerShares:
    """模拟 yfinance Ticker：get_shares_full 返回月度股份数 Series"""
    def __init__(self, shares_series: pd.Series):
        self._shares = shares_series

    def get_shares_full(self, start=None, end=None):
        s = self._shares
        if start:
            s = s[s.index >= pd.Timestamp(start)]
        if end:
            s = s[s.index <= pd.Timestamp(end)]
        return s


def test_S3_has_dividend_history_as_of_date(monkeypatch):
    """S3: as_of_date=2022-12-31 → 仅看 2019~2022 区间分红"""
    from collectors import hk_financial

    # 构造分红时间序列：2018-06、2020-06、2021-06、2024-06（共 4 年）
    fake = _FakeTickerDivOnly(
        div_dates=["2018-06-15", "2020-06-15", "2021-06-15", "2024-06-15"],
        div_amounts=[0.5, 0.5, 0.5, 0.5],
    )
    monkeypatch.setattr(hk_financial, "_get_ticker", lambda code: fake)

    # as_of=2022-12-31, years=3 → cutoff=2019-12-31, end=2022-12-31
    # → 命中 2020-06、2021-06 共 2 个不同年份 → True
    assert hk_financial.has_dividend_history(
        "00001", years=3, min_years_with_div=2, as_of_date=datetime(2022, 12, 31)
    ) is True

    # as_of=2019-12-31, years=3 → cutoff=2016-12-31, end=2019-12-31
    # → 命中 2018-06 共 1 个年份 → False
    assert hk_financial.has_dividend_history(
        "00001", years=3, min_years_with_div=2, as_of_date=datetime(2019, 12, 31)
    ) is False


def test_S4_has_share_dilution_as_of_date(monkeypatch):
    """S4: as_of_date=2022-12-31 → 仅用 2019~2022 区间股份数"""
    from collectors import hk_financial

    # 构造股份数序列：从 2019-01 起每月 1e9，2022-06 涨到 2e9（合计 +100% 增发）
    idx = pd.date_range("2019-01-01", "2025-12-31", freq="ME")
    shares = pd.Series(
        [1e9] * 41 + [2e9] * (len(idx) - 41),
        index=idx,
    )
    fake = _FakeTickerShares(shares)
    # 不在 AH 双重上市豁免名单内
    monkeypatch.setattr(hk_financial, "AH_DUAL_LISTED_STOCKS", set())
    monkeypatch.setattr(hk_financial, "_get_ticker", lambda code: fake)

    # as_of=2022-12-31, years=3 → 应观察到 2022-06 的增发
    should_filter, _ = hk_financial.has_share_dilution(
        "00001", years=3, as_of_date=datetime(2022, 12, 31)
    )
    assert should_filter is True

    # as_of=2020-12-31, years=3 → 仅看到 1e9 平稳，无增发
    should_filter2, _ = hk_financial.has_share_dilution(
        "00001", years=3, as_of_date=datetime(2020, 12, 31)
    )
    assert should_filter2 is False


# ============================================================
# A3: scripts/backfill_pools.py
# ============================================================

def test_S_backfill_mock(tmp_data_dir, monkeypatch):
    """A3 mock: 给定 5 只样本和确定结果的 has_* mock，验证 backfill_one_pool 选股正确"""
    from scripts import backfill_pools

    full_list = pd.DataFrame([
        {"code": "00001", "name": "Pass"},
        {"code": "00002", "name": "NoDiv"},
        {"code": "00003", "name": "FallbackPass"},
        {"code": "00004", "name": "Diluted"},
        {"code": "00005", "name": "PassAlso"},
    ])

    def fake_has_div(code, years=3, min_years_with_div=2, as_of_date=None):
        return code in ("00001", "00004", "00005")

    def fake_has_recent(code, months=12, as_of_date=None):
        # NoDiv 没有近期分红；FallbackPass 有
        return code == "00003"

    def fake_has_dilution(code, years=3, as_of_date=None):
        # 00004 被剔除
        return (code == "00004", 0.0)

    monkeypatch.setattr(backfill_pools, "has_dividend_history", fake_has_div)
    monkeypatch.setattr(backfill_pools, "has_recent_dividend_or_forward", fake_has_recent)
    monkeypatch.setattr(backfill_pools, "has_share_dilution", fake_has_dilution)

    pool = backfill_pools.backfill_one_pool(datetime(2022, 12, 31), full_list)
    codes = set(pool["code"].astype(str).str.zfill(5).tolist())
    # 期望: 00001 (div ok), 00003 (fallback), 00005 (div ok); 排除 00002 (无分红+无近期), 00004 (被增发剔除)
    assert codes == {"00001", "00003", "00005"}


def test_S_list_year_end_dates():
    """A3: list_year_end_dates 升序返回正确数量"""
    from scripts import backfill_pools

    dates = backfill_pools.list_year_end_dates(years_back=5)
    # 应包含 6 个基准日（years_back + 当前年的上一年末），全部为 12-31
    assert all(d.month == 12 and d.day == 31 for d in dates)
    assert all(dates[i] < dates[i + 1] for i in range(len(dates) - 1))
    assert len(dates) == 6
    # 不应回填未来：最大基准日 <= 今天
    assert dates[-1] <= datetime.now()


def test_S_run_backfill_writes_snapshots(tmp_data_dir, monkeypatch):
    """A3: run_backfill 将每个基准日写入 snapshot 目录"""
    from scripts import backfill_pools
    from storage import store as _store

    full = pd.DataFrame([
        {"code": "00001", "name": "A"},
        {"code": "00002", "name": "B"},
    ])

    monkeypatch.setattr(backfill_pools, "get_hk_stock_list", lambda: full)

    # A10 批量路径：mock 内部决策函数让全部通过
    monkeypatch.setattr(backfill_pools, "_prefetch_ticker_data",
                        lambda c, oc, le: (pd.Series(dtype=float), pd.Series(dtype=float)))
    monkeypatch.setattr(backfill_pools, "_decide_div_history", lambda divs, dt, **kw: True)
    monkeypatch.setattr(backfill_pools, "_decide_recent_div", lambda divs, dt, **kw: True)
    monkeypatch.setattr(backfill_pools, "_decide_share_dilution",
                        lambda ms, dt, code, **kw: False)

    summary = backfill_pools.run_backfill(years_back=2, skip_existing=False)
    # years_back=2 → 3 个基准日
    assert len(summary) == 3
    for date_str, val in summary.items():
        assert val == 2
        # 验证文件存在
        assert _store.list_pool_snapshots()  # 至少存了一些


def test_S_run_backfill_skip_existing(tmp_data_dir, monkeypatch):
    """A3: skip_existing=True 时已存在快照不再覆盖"""
    from scripts import backfill_pools
    from storage import store as _store

    full = pd.DataFrame([{"code": "00001", "name": "A"}])
    monkeypatch.setattr(backfill_pools, "get_hk_stock_list", lambda: full)
    # A10 mock 路径
    monkeypatch.setattr(backfill_pools, "_prefetch_ticker_data",
                        lambda c, oc, le: (pd.Series(dtype=float), pd.Series(dtype=float)))
    monkeypatch.setattr(backfill_pools, "_decide_div_history", lambda divs, dt, **kw: True)
    monkeypatch.setattr(backfill_pools, "_decide_recent_div", lambda divs, dt, **kw: True)
    monkeypatch.setattr(backfill_pools, "_decide_share_dilution",
                        lambda ms, dt, code, **kw: False)

    # 预先写入 2024-12-31 快照
    _store.save_stock_pool_snapshot(
        pd.DataFrame([{"code": "99999", "name": "Existing"}]),
        datetime(2024, 12, 31),
    )

    summary = backfill_pools.run_backfill(
        years_back=0,
        only_as_of=datetime(2024, 12, 31),
        skip_existing=True,
    )
    assert summary["2024-12-31"] == "skipped"
    # 验证旧快照未被覆盖
    df = _store.load_stock_pool_snapshot(datetime(2024, 12, 31))
    assert "99999" in df["code"].tolist()


# ============================================================
# A10: 批量预取 + 切片
# ============================================================

def test_S7_batched_slicing_decisions():
    """A10: 同一序列对不同 as_of_date 切片应给出独立决策"""
    from scripts import backfill_pools as bp

    # 构造一个分红序列：仅 2018/2019/2020 三年有分红
    div_idx = pd.to_datetime(["2018-06-15", "2019-06-15", "2020-06-15"])
    divs = pd.Series([0.5, 0.5, 0.5], index=div_idx)

    # as_of=2020-12-31：cutoff=2017-12，命中 3 年都有分红 → True
    assert bp._decide_div_history(divs, datetime(2020, 12, 31)) is True
    # as_of=2023-12-31：cutoff=2020-12 → 仅 2020 一年 → False（< 2 年）
    assert bp._decide_div_history(divs, datetime(2023, 12, 31)) is False
    # as_of=2025-12-31：cutoff=2022-12 → 0 年 → False
    assert bp._decide_div_history(divs, datetime(2025, 12, 31)) is False


def test_S7_batched_run_writes_all_pools(tmp_data_dir, monkeypatch):
    """A10: backfill_all_pools_batched 对多基准日各产出对应 pool"""
    from scripts import backfill_pools as bp
    from storage import store as _store

    full = pd.DataFrame([
        {"code": "00001", "name": "A"},
        {"code": "00002", "name": "B"},
    ])

    # 让 00001 在 2022 通过、2023 不通过（fallback 也不通过）
    # 让 00002 全部通过
    def fake_prefetch(code, oc, le):
        return (pd.Series(dtype=float), pd.Series(dtype=float))

    def fake_div_hist(divs, dt, **kw):
        # 仅在 dt 年份 == 2022 时为 True
        if dt.year == 2022:
            return True  # 都通过
        return False

    def fake_recent(divs, dt, **kw):
        # 2023 年时只有 00002 通过 fallback —— 因为我们没法在这层区分 code
        # 实际 code 在 backfill_all_pools_batched 内不传给 _decide_recent_div
        # 所以这里都 True 就行；实际差异通过 _decide_share_dilution 模拟
        return True

    def fake_dilution(ms, dt, code, **kw):
        # 2023-12-31 时 00001 被剔除
        return (dt.year == 2023 and code == "00001")

    monkeypatch.setattr(bp, "_prefetch_ticker_data", fake_prefetch)
    monkeypatch.setattr(bp, "_decide_div_history", fake_div_hist)
    monkeypatch.setattr(bp, "_decide_recent_div", fake_recent)
    monkeypatch.setattr(bp, "_decide_share_dilution", fake_dilution)

    targets = [datetime(2022, 12, 31), datetime(2023, 12, 31)]
    pools = bp.backfill_all_pools_batched(targets, full)

    p2022 = pools["2022-12-31"]
    p2023 = pools["2023-12-31"]
    # 2022 全部通过
    assert sorted(p2022["code"].tolist()) == ["00001", "00002"]
    # 2023 仅 00002（00001 被 dilution 剔除，但 fake_div_hist 返回 False，
    # 所以走 fallback 都通过；之后 dilution 把 00001 剔除）
    assert p2023["code"].tolist() == ["00002"]


# ============================================================
# A4: _select_pool_for_signal_period & _filter_by_pool
# ============================================================

def test_S5_select_pool_for_signal_period(tmp_data_dir):
    """S5: signal_period='2024-06' → snapshot 2023-12-31 命中"""
    from storage import store as _store
    from backtest.portfolio import _select_pool_for_signal_period

    pool_2023 = pd.DataFrame([{"code": "00001", "name": "A2023"}])
    pool_2024 = pd.DataFrame([{"code": "00002", "name": "A2024"}])
    _store.save_stock_pool_snapshot(pool_2023, datetime(2023, 12, 31))
    _store.save_stock_pool_snapshot(pool_2024, datetime(2024, 12, 31))

    p = _select_pool_for_signal_period("2024-06")
    assert "00001" in p["code"].tolist()
    assert "00002" not in p["code"].tolist()

    p2 = _select_pool_for_signal_period("2025-03")
    assert "00002" in p2["code"].tolist()


def test_S6_select_pool_fallback(tmp_data_dir):
    """S6: 找不到对应年份 snapshot 时 fallback"""
    from storage import store as _store
    from backtest.portfolio import _select_pool_for_signal_period

    # 没有任何 snapshot，也没有当前 pool → 返回空
    p_empty = _select_pool_for_signal_period("2024-06")
    assert p_empty.empty

    # 有当前 pool 但没 snapshot → fallback 到当前 pool
    _store.save_stock_pool(pd.DataFrame([{"code": "99999", "name": "Cur"}]))
    p_cur = _select_pool_for_signal_period("2024-06")
    assert "99999" in p_cur["code"].tolist()

    # 有早期 snapshot 但没目标年份 → 用最近的旧 snapshot
    _store.save_stock_pool_snapshot(
        pd.DataFrame([{"code": "00001", "name": "Old"}]),
        datetime(2020, 12, 31),
    )
    p_older = _select_pool_for_signal_period("2024-06")  # 期望 2023 没有 → 退化到 2020
    assert "00001" in p_older["code"].tolist()


def test_S6b_build_top_n_filters_by_pool(tmp_data_dir):
    """S6b: build_top_n 真实场景下会被池过滤"""
    from storage import store as _store
    from backtest.portfolio import build_top_n

    # 写入一个 ranking
    rank_df = pd.DataFrame([
        {"code": "00001", "name": "InPool", "composite_score": 10.0, "rank": 1},
        {"code": "00002", "name": "InPool2", "composite_score": 9.0, "rank": 2},
        {"code": "00099", "name": "OutPool", "composite_score": 100.0, "rank": 0},  # 池外但分数最高
    ])
    rank_path = _store.RANKING_CSV.format(period="2024-06")
    rank_df.to_csv(rank_path, index=False, encoding="utf-8-sig")

    # 写入 2023-12-31 snapshot 仅含 00001/00002
    _store.save_stock_pool_snapshot(
        pd.DataFrame([{"code": "00001"}, {"code": "00002"}]),
        datetime(2023, 12, 31),
    )

    holdings = build_top_n("2024-06", top_n=3, weight_scheme="equal")
    codes = holdings["code"].tolist()
    # 00099 的 composite_score 最高但应被过滤
    assert "00099" not in codes
    assert set(codes) == {"00001", "00002"}
