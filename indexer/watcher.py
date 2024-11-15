import asyncio
import contextlib
import datetime as dt
import json
import logging
import time

from indexer import (
    cache,
    f95zone,
)
from modules import error

WATCH_INTERVAL = dt.timedelta(minutes=5).total_seconds()
WATCH_CATEGORIES = (
    "games",
    "comics",
    "animations",
)

logger = logging.getLogger(__name__)

LAST_WATCH = "LAST_WATCH"


@contextlib.asynccontextmanager
async def lifespan():
    watch_task = asyncio.create_task(watch_latest_updates())

    try:
        yield
    finally:

        watch_task.cancel()


async def watch_latest_updates():
    while True:
        try:
            # Calculate how far back we need to get caught up
            now = time.time()
            last_watch = int(await cache.redis.get(LAST_WATCH) or 0)
            if now - last_watch > cache.CACHE_TTL:
                last_watch = now - cache.CACHE_TTL
            logger.info("Poll updates start")

            for category in WATCH_CATEGORIES:
                page = 0
                caught_up = False
                while not caught_up:
                    page += 1
                    logger.debug(f"Poll {category} updates page {page}")

                    async with f95zone.session.get(
                        f95zone.LATEST_URL.format(cat=category, page=page),
                        allow_redirects=True,
                        max_redirects=10,
                        cookies=f95zone.cookies,
                    ) as req:
                        res = await req.read()

                    if index_error := f95zone.check_error(res):
                        raise Exception(index_error.error_flag)

                    updates = json.loads(res)
                    if updates["status"] != "ok":
                        raise Exception(f"Latest updates returned an error: {updates}")

                    for update in updates["msg"]["data"]:
                        # No reliable timestamp, only relative human readable
                        # time, need to half-assedly parse it here
                        diff = update["date"]
                        delta = dt.timedelta()
                        if "min" in diff:
                            delta += dt.timedelta(minutes=int(diff.split(" ")[0]))
                        elif "hr" in diff:
                            delta += dt.timedelta(hours=int(diff.split(" ")[0]))
                        elif "Yesterday" in diff:
                            delta += dt.timedelta(days=1)
                        elif "day" in diff:
                            delta += dt.timedelta(days=int(diff.split(" ")[0]))
                        elif "week" in diff:
                            delta += dt.timedelta(weeks=int(diff.split(" ")[0]), days=1)
                        else:
                            caught_up = True
                            break
                        date = time.time() - (delta.total_seconds())
                        if date < last_watch:
                            caught_up = True
                            break

                        # Clear cache instead of fetching new data, no point
                        # fetching it if no one cares is tracking it
                        name = cache.NAME_FORMAT.format(id=update["thread_id"])
                        if await cache.redis.hdel(name, cache.LAST_CACHED):
                            logger.info(f"Cleared cache for {name}")
                        else:
                            logger.debug(f"Nothing cached for {name}")

            await cache.redis.set(LAST_WATCH, int(now))
            logger.info("Poll updates done")

        except Exception:
            logger.error(error.traceback())

        await asyncio.sleep(WATCH_INTERVAL)
