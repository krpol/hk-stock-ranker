# scripts/ 索引

> 2026-11 精简：一次性数据修补 / 迁移脚本已归档到 `_archived/`（保留历史，需找回时 `git mv` 回来即可）。

## 常驻脚本（根目录）

| 脚本 | 用途 | 触发方式 |
|---|---|---|
| `backfill_pools.py` | 历史股票池回填 | `main.py --backfill-pools` 内部 import |
| `backfill_rankings.py` | 历史 ranking 回填 | `main.py --backfill-rankings` 内部 import |
| `backfill_missing_month.py` | 补刷缺失月末价（`--period YYYY-MM`，缺省当前月） | 月度流程 Step 2（见 docs/MONTHLY_ROUTINE.md） |
| `data_quality_check.py` | 数据质量体检（依赖 field_registry） | 测试 import |
| `patch_missing_fields.py` | 重拉 hk_financial.csv 中采集失败的行 | 手动 |
| `refresh_dividend_only.py` | 单只/批量重算派息归属（`recompute_dividends_for_code`） | 测试 import + 手动 |
| `refresh_financial_failed.py` | **财务三态容错重试 CLI**（`--retry-failed` / `--domain X` / `--code X`） | 手动（B16 主力） |
| `scrape_forward_div_em_only.py` | 港交所披露易预分红爬虫 | 手动 / `main.py --scrape-forward-div` 复用 |
| `update_financial_only.py` | 全量/指定码重刷财务数据 | 手动 |
| `retry_financial_update.sh` | 财务更新失败自动重试包装 | 手动 / 定时 |

## 归档（`_archived/`）

31 个一次性 `patch_*` / `backfill_*` / `refresh_*` / `verify_*` 脚本，多为开发期的数据迁移/修补，已被 B16 `refresh_financial_failed.py --domain X` 机制取代。不再在根目录维护，避免误导。

## 约定

- 新的一次性脚本用完即归档，勿长期堆在根目录。
- 常驻脚本应能被 `python -m scripts.xxx` 或 `main.py` 稳定调用，且带 `argparse` 说明。
