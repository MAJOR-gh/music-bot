"""Автообновление yt-dlp.

YouTube регулярно меняет защиту, и через несколько недель старый yt-dlp
получает 403 на скачивание звука — трек пропадает или рвётся. Поэтому при
старте и раз в 12 часов сверяемся с PyPI и при необходимости обновляемся.
По умолчанию ставим ночные сборки (nightly): исправления под YouTube выходят
в них сразу, а в стабильную версию — через недели. YTDLP_NIGHTLY=0 — только stable.
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
NIGHTLY = os.getenv("YTDLP_NIGHTLY", "1").strip().lower() not in ("0", "false", "no", "off")


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
                    return pick_latest(await resp.json(), NIGHTLY)
    except Exception as e:  # noqa: BLE001
        logger.warning("[update] PyPI недоступен: %s", e)
    return None


def pick_latest(data: dict, nightly: bool) -> str | None:
    """Последняя версия из ответа PyPI; с nightly — включая .dev-сборки."""
    stable = (data.get("info") or {}).get("version")
    if not nightly:
        return stable
    versions = [v for v, files in (data.get("releases") or {}).items()
                if files and not all(f.get("yanked") for f in files)]
    return max(versions + ([stable] if stable else []), key=version_tuple, default=stable)


def _pip_target_args() -> list[str]:
    """На панелях (Pterodactyl) пакеты стоят в .local рядом с ботом — обновляем туда же."""
    spec = importlib.util.find_spec("yt_dlp")
    origin = os.path.abspath(spec.origin) if spec and spec.origin else ""
    # …/.local/lib/pythonX.Y/site-packages/yt_dlp/__init__.py → …/.local
    marker = os.sep + ".local" + os.sep
    if marker in origin:
        return ["--prefix", origin[:origin.index(marker) + len(marker) - 1]]
    return []


async def update_if_needed() -> None:
    current, latest = installed_version(), await latest_version()
    if not latest or version_tuple(current) >= version_tuple(latest):
        logger.info("[update] yt-dlp %s — актуальная версия", current)
        return
    logger.info("[update] yt-dlp %s устарел, ставлю %s…", current, latest)
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "pip", "install", "-U", "--quiet",
        "--disable-pip-version-check", *(["--pre"] if NIGHTLY else []),
        *_pip_target_args(), *PACKAGES,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, err = await asyncio.wait_for(proc.communicate(), 300)
    except TimeoutError:
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
