# 数据质量治理手册（DATA QUALITY PLAYBOOK）

> **一句话**：任何进入 `data/csv/hk_financial.csv` 的字段，都必须在 `collectors/field_registry.py` 登记，并通过 `scripts/data_quality_check.py` 三维体检。

---

## 1. 背景与教训

历史上多次因数据源问题导致下游打分异常：

| 日期 | 现象 | 根因 |
|---|---|---|
| — | 永升服务 real_fcf 只有 1 亿 | yfinance 只有 2024 数据，未拉到 2025 |
| — | 部分股票 ttm_operating_cashflow=NaN | yfinance quarterly HK 数据缺失 |
| — | 派息 yoy 突增 100 倍 | 财年切换 + AKShare 累计口径差异 |
| — | shares_outstanding 单日翻倍 | A+H 双重上市股本口径切换 |

**共同教训**：数据问题很少「爆炸性失败」，多数是**静默漂移** —— 覆盖率降 5%、报告期滞后一期、单只极端值污染排名。必须**主动体检**。

---

## 2. 治理架构（Three Pillars）

```
┌────────────────────────────────────────────────────────────┐
│              collectors/field_registry.py                  │
│              （单一事实源 · Single Source of Truth）        │
│   每字段元信息：source / caliber / coverage / lag / range   │
└─────────────────────┬──────────────────────────────────────┘
                      │
        ┌─────────────┼─────────────┐
        ▼             ▼             ▼
  ┌──────────┐  ┌──────────┐  ┌──────────┐
  │ 采集侧    │  │ 体检器    │  │ 单测      │
  │ collectors│  │ scripts/  │  │ tests/    │
  │ 输出 _as_of│  │ data_qual │  │ test_data │
  │           │  │ ity_check │  │ _quality  │
  └──────────┘  └──────────┘  └──────────┘
```

**三维体检**：

1. **Coverage（覆盖率）**：字段非空率 vs `min_coverage`
2. **Freshness（时效）**：`*_as_of` 报告期距今 vs `max_lag_days`
3. **Sanity（合理性）**：值域越界 / 负值违规 / 量纲异常

---

## 3. 新字段接入清单（Definition of Done）

**开发新字段时按顺序完成 8 步，缺一不可**：

### ✅ 3.1 数据源与口径调研
- [ ] 明确数据源（yfinance / AKShare / 计算派生）
- [ ] 明确口径（TTM / 3y_avg / point_in_time / ratio / event）
- [ ] **如是港股财务字段**：必须评估是否需要 **AKShare + yfinance 双源 combine**（AKShare 更新更快，yfinance 历史更全）

### ✅ 3.2 采集器实现
- [ ] 在 `collectors/hk_financial.py` 添加采集逻辑
- [ ] **返回 `xxx_as_of` 报告期字段**（数据血缘，用于时效体检）
- [ ] 处理 NaN/inf/负值等边界

### ✅ 3.3 登记 field_registry
- [ ] 在 `FIELD_REGISTRY` 里新增条目：
    ```python
    "new_field": FieldMeta(
        "字段中文描述", "akshare+yfinance", "ttm",
        min_coverage=0.90,
        max_lag_days=550,           # 仅日期字段
        sane_range=(0, 100.0),
        allow_negative=False,
        severity="warn",            # 或 "fail"（会让 --strict exit 1）
    ),
    ```
- [ ] 极端值/负值场景请在注释中说明为何允许

### ✅ 3.4 DOMAIN_FIELDS 挂载
- [ ] 把字段加到 `hk_financial.py` 的 `DOMAIN_FIELDS[domain]`
- [ ] 若字段属于新 domain，同步更新 `ALL_DOMAINS`

### ✅ 3.5 backfill 脚本更新
- [ ] 在对应 backfill 脚本的 `FIELDS` 列表加入新字段（含 as_of 列）
- [ ] backfill 统计循环支持字符串列（`_as_of`）不做 `to_numeric`

### ✅ 3.6 单测
- [ ] 添加 `tests/test_data_quality.py` 断言：`TestRegistryConsistency::test_no_unregistered_fields` 会自动覆盖
- [ ] 若字段是新维度（如首个 `ratio`），补一条 sanity 单测

### ✅ 3.7 跑体检
```bash
python scripts/data_quality_check.py --strict
```
必须 **FAIL=0** 才可合入。

### ✅ 3.8 文档
- [ ] `docs/DATA_DICTIONARY.md`（若存在）记录字段
- [ ] 若涉及打分逻辑，同步 `docs/SCORING_LOGIC.md`

---

## 4. 常见陷阱（Common Pitfalls）

### 4.1 yfinance HK 数据滞后
- **症状**：yfinance `income_stmt` / `cashflow` 只到上一个财年，AKShare 已有最新
- **对策**：对港股财务字段一律走 **AKShare + yfinance combine**（见 `_combine_series`）
- **验证**：
    ```python
    from collectors.hk_financial import _get_hk_report_akshare, _ak_series, _yf_series, _combine_series
    ```

### 4.2 财年切换 & 累计口径
- **症状**：yoy 突增 100+ 倍 / 派息突然变零
- **对策**：`fy_end` 变化后不用 pct_change 直接算，走 TTM 或时间对齐

### 4.3 A+H 双重上市股本切换
- **症状**：`shares_outstanding` 单日翻倍
- **对策**：见 `AH_DUAL_LISTED_STOCKS` 白名单，对该组股票禁用 pct 判定

### 4.4 单位不一致
- **症状**：AKShare 返回「万元」，yfinance 返回「元」
- **对策**：单元测试写死一个已知案例值，回归时对比

### 4.5 静默漂移
- **症状**：某字段覆盖率从 98% 降到 92%，仍在 min_coverage 之上但下游打分变差
- **对策**：定期跑 `python scripts/data_quality_check.py --cross-check 20`（抽样对比源数据）

### 4.6 registry 陈旧（Ghost Fields）
- **症状**：字段被删了但 registry 里还在，或 CSV 有列但没登记
- **对策**：`test_no_unregistered_fields` + `test_no_ghost_fields` 自动阻止

### 4.7 日期列被误判为 NaN
- **症状**：`_as_of` 列在 coverage 体检里显示 0%
- **对策**：`check_coverage` 已支持日期列不做 `to_numeric`

---

## 5. 例行运维（Recurring Checks）

| 频率 | 命令 | 目的 |
|---|---|---|
| 每次 backfill 完成 | `python scripts/data_quality_check.py` | 快速体检 |
| CI（PR 检查） | `python scripts/data_quality_check.py --strict` | 阻断严重问题 |
| 每周（人工） | `python scripts/data_quality_check.py --cross-check 30` | 抽样对比源数据，识别静默漂移 |
| 每月 | 抽 3 只知名股（0700 / 1024 / 9988）人工对比雪球/东财 | 端到端回归 |

**报告输出**：`data/quality_report.json`（含所有明细，供 CI 挂制品）

---

## 6. 问题处置流程

体检发出 WARN/FAIL 时的响应流程：

```
1. 定位维度：coverage / freshness / sanity
2. 判断类型：
   - 数据源问题（滞后/缺失） → 走 combine / 拉长兜底
   - 采集逻辑 bug（口径错） → 修 collector + 补单测
   - 真实极端值（如 01128 负资产） → 保留 warn，评估打分器兼容性
3. 修复后重跑 backfill + 体检
4. 若为「静默漂移」，考虑收紧 registry 阈值
```

---

## 7. 附：关键文件索引

| 文件 | 职责 |
|---|---|
| `collectors/field_registry.py` | 字段元数据（单一事实源） |
| `collectors/hk_financial.py` | 采集器 + `_combine_series` 双源合并 |
| `scripts/data_quality_check.py` | 三维体检 + 交叉验证 |
| `scripts/backfill_fcf_components.py` | FCF/负债回填脚本 |
| `tests/test_data_quality.py` | 治理体系单测（含 registry ↔ CSV 一致性） |
| `data/quality_report.json` | 最新体检报告 |

---

**最后更新**：2026-07-01

**如何贡献**：新增陷阱案例请提 PR 到 §4，附症状/根因/对策三段式。
