"""Автообновление yt-dlp.

YouTube регулярно меняет защиту, и через несколько недель старый yt-dlp
получает 403 на скачивание звука — трек пропадает или рвётся. Поэтому при
старте и раз в 12 часов сверяемся с PyPI и при необходимости обновляемся.
Бот вызывает yt-dlp отдельными процессами (ytdl.py), так что новая версия
работает сразу, без перезапуска.
"""
from __future__ import annotations

import asyncio
import importlib.metadata
import importlib.util
import logging
import os
import sys

import aiohttp

logger = logging.getLogger("music_bot.updater")

CHECK_EVERY = 12 * 60 * 60
PACKAGES = ["yt-dlp", "yt-dlp-ejs"]


def installed_version() -> str | None:
    try:
        return importlib.metadata.version("yt-dlp")
    except importlib.metadata.PackageNotFoundError:
        return None


def version_tuple(v: str | None) -> tuple[int, ...]:
    parts = []
    for p in (v or "0").split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(digits or 0))
    return tuple(parts)


async def latest_version() -> str | None:
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            async with s.get("https://pypi.org/pypi/yt-dlp/json") as resp:
                if resp.status == 200:
                    return (await resp.json())["info"]["version"]
    except Exception as e:  # noqa: BLE001
        logger.warning("[update] PyPI недоступен: %s", e)
    return None


def _pip_target_args() -> list[str]:
    """На панелях (Pterodactyl) пакеты стоят в .local рядом с ботом — обновляем туда же."""
    spec = importlib.util.find_spec("yt_dlp")
    origin = os.path.abspath(spec.origin) if spec and spec.origin else ""
    bot_local = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".local")
    if origin.startswith(bot_local + os.sep):
        return ["--prefix", bot_local]
    return []


async def update_if_needed() -> None:
    current, latest = installed_version(), await latest_version()
    if not latest or version_tuple(current) >= version_tuple(latest):
        logger.info("[update] yt-dlp %s — актуальная версия", current)
        return
    logger.info("[update] yt-dlp %s устарел, ставлю %s…", current, latest)
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "pip", "install", "-U", "--quiet",
        "--disable-pip-version-check", *_pip_target_args(), *PACKAGES,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, err = await asyncio.wait_for(proc.communicate(), 300)
    except asyncio.TimeoutError:
        proc.kill()
        logger.warning("[update] pip не уложился в 5 минут")
        return
    if proc.returncode == 0:
        logger.info("[update] ✅ yt-dlp обновлён: %s → %s", current, installed_version())
    else:
        logger.warning("[update] pip не смог обновить yt-dlp: %s",
                       err.decode("utf-8", "replace").strip()[-300:])


async def updater_loop() -> None:
    while True:
        try:
            await update_if_needed()
        except Exception as e:  # noqa: BLE001
            logger.warning("[update] ошибка проверки: %s", e)
        await asyncio.sleep(CHECK_EVERY)
