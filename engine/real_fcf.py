"""
B31 + B32: 剩余可用现金流偿债覆盖率指标。

主公式（B32 改用 CapEx_TTM 增强敏感性）：
    剩余 CF       = OCF_3y − min(CapEx_TTM + Lease_期初, D&A_3y)
    ε             = max(净资产 × 0.01, 1 亿)
    调整净债       = max(IBD − Cash, ε)
    覆盖率         = 剩余 CF / 调整净债

突降监测（B32 方案 A）：
    real_fcf_ttm  = OCF_TTM − min(CapEx_TTM + Lease_TTM, D&A_TTM)
    drop_ratio    = real_fcf_ttm / real_fcf_3y       (仅 3y > 0 时)

评级（原 5 级）：
    AAA      cash_fortress 成立（超级豁免）
    DROP     real_fcf < 0 或 覆盖率 < 5%
    WARN     5% ≤ 覆盖率 < 10%
    OK       10% ≤ 覆盖率 < 20%
    STRONG   覆盖率 ≥ 20%
    N/A      数据缺失

降级矩阵（B32 突降叠加，AAA/N/A 不降级）：
    drop_ratio < 0.3：评级下调一档
    drop_ratio < 0  ：评级下调两档
"""
from typing import Optional


EPS_RATIO = 0.01        # 1% × Equity
EPS_FLOOR = 1e8         # 1 亿港币
THRESHOLD_DROP = 0.10
THRESHOLD_WARN = 0.15
THRESHOLD_OK = 0.25

# B32: 突降阈值
DROP_RATIO_WARN = 0.30   # 当年 < 30% 3y 平均 → 下调一档
DROP_RATIO_SEVERE = 0.0  # 转负 → 下调两档

_RATING_ORDER = ["STRONG", "OK", "WARN", "DROP"]


def compute_real_fcf(
    ocf_3y: Optional[float],
    capex: Optional[float],
    dep_3y: Optional[float],
    lease_principal: Optional[float],
) -> Optional[float]:
    """剩余可用现金流 = OCF − min(CapEx + Lease, D&A)。

    OCF 缺失则返回 None；其他缺失按 0 处理。
    若 D&A 缺失/<=0 不取 min（直接用 capex + lease）。

    B32: 第二个参数从 capex_3y_avg 改为 capex_ttm 以提升敏感性；
    函数签名为兼容保留原参数名，调用方可传 capex_ttm 或 capex_3y。
    """
    if ocf_3y is None:
        return None
    cx = capex if capex is not None else 0.0
    lease = lease_principal if lease_principal is not None else 0.0
    deduct = cx + lease
    if dep_3y is not None and dep_3y > 0:
        deduct = min(deduct, dep_3y)
    return ocf_3y - deduct


def compute_real_fcf_ttm(
    ocf_ttm: Optional[float],
    capex_ttm: Optional[float],
    da_ttm: Optional[float],
    lease_ttm: Optional[float],
) -> Optional[float]:
    """B32: TTM 口径的剩余 CF，用于突降监测。

    real_fcf_ttm = OCF_TTM − min(CapEx_TTM + Lease_TTM, D&A_TTM)
    """
    if ocf_ttm is None:
        return None
    cx = capex_ttm if capex_ttm is not None else 0.0
    lease = lease_ttm if lease_ttm is not None else 0.0
    deduct = cx + lease
    if da_ttm is not None and da_ttm > 0:
        deduct = min(deduct, da_ttm)
    return ocf_ttm - deduct


def compute_drop_ratio(
    real_fcf_ttm: Optional[float],
    real_fcf_3y: Optional[float],
) -> Optional[float]:
    """B32: 突降比率 = real_fcf_ttm / real_fcf_3y。

    若 real_fcf_3y <= 0 或缺失：返回 None（无法对比，避免误报）。
    """
    if real_fcf_ttm is None or real_fcf_3y is None:
        return None
    if real_fcf_3y <= 0:
        return None
    return real_fcf_ttm / real_fcf_3y


def compute_coverage_ratio(
    real_fcf: Optional[float],
    ibd: Optional[float],
    cash: Optional[float],
    equity: Optional[float],
    eps_ratio: float = EPS_RATIO,
    eps_floor: float = EPS_FLOOR,
) -> Optional[float]:
    """覆盖率 = real_fcf / max(IBD − Cash, max(Equity × eps_ratio, eps_floor))。"""
    if real_fcf is None or ibd is None or cash is None:
        return None
    eq = equity if (equity is not None and equity > 0) else 0.0
    eps = max(eq * eps_ratio, eps_floor)
    denom = max(ibd - cash, eps)
    return real_fcf / denom


def _downgrade(rating: str, steps: int) -> str:
    """在 STRONG → OK → WARN → DROP 链上下调 steps 档；AAA/N/A/DROP 不动。"""
    if rating in ("AAA", "N/A", "DROP"):
        return rating
    if rating not in _RATING_ORDER:
        return rating
    idx = _RATING_ORDER.index(rating)
    new_idx = min(idx + steps, len(_RATING_ORDER) - 1)
    return _RATING_ORDER[new_idx]


def classify_rating(
    real_fcf: Optional[float],
    coverage_ratio: Optional[float],
    cash_fortress: bool = False,
    drop_ratio: Optional[float] = None,
) -> str:
    """返回评级字符串（B32：加入突降降级矩阵）。"""
    if cash_fortress:
        return "AAA"
    if real_fcf is None or coverage_ratio is None:
        return "N/A"
    if real_fcf < 0:
        return "DROP"
    if coverage_ratio < THRESHOLD_DROP:
        base = "DROP"
    elif coverage_ratio < THRESHOLD_WARN:
        base = "WARN"
    elif coverage_ratio < THRESHOLD_OK:
        base = "OK"
    else:
        base = "STRONG"

    # B32: 突降降级（AAA 已上面 return；N/A 不会进到这里）
    if drop_ratio is not None:
        if drop_ratio < DROP_RATIO_SEVERE:   # 转负 → 下调两档
            base = _downgrade(base, 2)
        elif drop_ratio < DROP_RATIO_WARN:   # < 30% → 下调一档
            base = _downgrade(base, 1)
    return base


def evaluate(
    ocf_3y: Optional[float],
    capex_3y: Optional[float],          # 兼容保留参数名；B32 推荐传 capex_ttm
    dep_3y: Optional[float],
    lease_principal: Optional[float],
    ibd: Optional[float],
    cash: Optional[float],
    equity: Optional[float],
    cash_fortress: bool = False,
    # B32: 突降监测相关 TTM 字段
    ocf_ttm: Optional[float] = None,
    capex_ttm: Optional[float] = None,
    da_ttm: Optional[float] = None,
    lease_ttm: Optional[float] = None,
) -> dict:
    """一次性计算 real_fcf / coverage / rating / real_fcf_ttm / drop_ratio。"""
    real_fcf = compute_real_fcf(ocf_3y, capex_3y, dep_3y, lease_principal)
    cov = compute_coverage_ratio(real_fcf, ibd, cash, equity)
    real_fcf_ttm = compute_real_fcf_ttm(ocf_ttm, capex_ttm, da_ttm, lease_ttm)
    drop_ratio = compute_drop_ratio(real_fcf_ttm, real_fcf)
    rating = classify_rating(real_fcf, cov, cash_fortress, drop_ratio)
    return {
        "real_fcf": real_fcf,
        "coverage_ratio": cov,
        "real_fcf_ttm": real_fcf_ttm,
        "drop_ratio": drop_ratio,
        "fcf_rating": rating,
    }
