"""
月度调度任务（APScheduler）
- 每月 settings.MONTHLY_UPDATE_DAY 号自动执行全量更新
- 每年 1 月 5 日 02:00 自动构建上一年末（12-31）的股票池快照
"""
import logging
from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from config.settings import MONTHLY_UPDATE_DAY

logger = logging.getLogger(__name__)


def monthly_update_job():
    """月度更新任务入口"""
    logger.info("===== 月度定时更新开始 =====")
    # 延迟导入避免循环依赖
    from main import run_update
    try:
        run_update()
        logger.info("===== 月度定时更新完成 =====")
    except Exception as e:
        logger.error(f"月度更新失败: {e}", exc_info=True)


def yearly_pool_snapshot_job():
    """
    年度池快照任务：每年 1 月 5 日触发，构建上一年 12-31 的池快照。
    例如 2026-01-05 运行 → 写入 hk_stock_pool_2025-12-31.csv
    """
    logger.info("===== 年度池快照任务开始 =====")
    from main import run_rebuild_pool
    try:
        snapshot_date = datetime(datetime.now().year - 1, 12, 31)
        run_rebuild_pool(as_of_date=snapshot_date)
        logger.info(f"===== 年度池快照完成 ({snapshot_date:%Y-%m-%d}) =====")
    except Exception as e:
        logger.error(f"年度池快照失败: {e}", exc_info=True)


def start_scheduler():
    scheduler = BlockingScheduler(timezone="Asia/Hong_Kong")
    trigger = CronTrigger(day=MONTHLY_UPDATE_DAY, hour=8, minute=0)
    scheduler.add_job(monthly_update_job, trigger, id="monthly_update")
    logger.info(f"调度器启动，每月 {MONTHLY_UPDATE_DAY} 日 08:00 (HKT) 执行更新")

    yearly_trigger = CronTrigger(month=1, day=5, hour=2, minute=0)
    scheduler.add_job(yearly_pool_snapshot_job, yearly_trigger, id="yearly_pool_snapshot")
    logger.info("调度器：每年 1 月 5 日 02:00 (HKT) 构建上一年末股票池快照")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("调度器已停止")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    start_scheduler()
