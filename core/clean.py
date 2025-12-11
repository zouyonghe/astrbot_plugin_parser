import asyncio
import zoneinfo
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.star.context import Context

from .utils import safe_unlink


class CacheCleaner:
    """
    每天固定时间自动清理插件缓存目录的调度器封装。
    """
    JOBNAME = "CacheCleaner"
    def __init__(self, context: Context, config: AstrBotConfig):
        self.clean_cron = config["clean_cron"]
        self.cache_dir = Path(config["cache_dir"])

        tz = context.get_config().get("timezone")
        self.timezone = (
            zoneinfo.ZoneInfo(tz) if tz else zoneinfo.ZoneInfo("Asia/Shanghai")
        )
        self.scheduler = AsyncIOScheduler(timezone=self.timezone)
        self.scheduler.start()

        self.register_task()

        logger.info(f"{self.JOBNAME} 已启动，任务周期：{self.clean_cron}")

    def register_task(self):
        try:
            self.trigger = CronTrigger.from_crontab(self.clean_cron)
            self.scheduler.add_job(
                func=self._clean_plugin_cache,
                trigger=self.trigger,
                name=f"{self.JOBNAME}_scheduler",
                max_instances=1,
            )
        except Exception as e:
            logger.error(f"[{self.JOBNAME}] Cron 格式错误：{e}")

    async def _clean_plugin_cache(self) -> None:
        """真正的清理逻辑。"""
        try:
            files = [f for f in self.cache_dir.iterdir() if f.is_file()]
            if not files:
                logger.info("No cache files to clean.")
                return

            await asyncio.gather(*(safe_unlink(f) for f in files))
            logger.info(f"Successfully cleaned {len(files)} cache files.")
        except Exception:
            logger.exception("Error while cleaning cache files.")

    async def stop(self):
        self.scheduler.remove_all_jobs()
        logger.info(f"[{self.JOBNAME}] 已停止")
