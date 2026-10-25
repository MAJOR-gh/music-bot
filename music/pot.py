"""PO Token для YouTube: bgutil-pot (Rust) + плагин yt-dlp.

Зачем: с куками YouTube теперь требует PO Token для клиентов web/web_embedded
(«Detected experiment to bind GVS PO Token to video ID»), а без токена отдаёт
только SABR или «The page needs to be reloaded». bgutil-pot генерирует токены
локально (BotGuard), плагин yt-dlp забирает их с http://127.0.0.1:4416.

Всё ставится само при старте (Linux x86_64): бинарник и плагин скачиваются
в папку .pot рядом с ботом, сервер запускается фоном и перезапускается, если упал.
По умолчанию ВЫКЛЮЧЕН: сервер держит ~70 МБ памяти, а на тарифах с 512 МБ
из-за этого не хватало памяти deno (JS-challenge) и YouTube не играл вовсе.
Включить: POT_ENABLED=1 в .env.
"""
from __future__ import annotations

import asyncio
import logging
import os
import platform
import stat

import aiohttp

logger = logging.getLogger("music_bot.pot")

VERSION = os.getenv("POT_VERSION", "v0.8.1").strip()
PORT = int(os.getenv("POT_PORT", "4416"))
ENABLED = os.getenv("POT_ENABLED", "0").strip() not in ("0", "false", "no", "")
_RELEASES = "https://github.com/jim60105/bgutil-ytdlp-pot-provider-rs/releases/download"
BIN_ASSET = "bgutil-pot-linux-x86_64"
PLUGIN_ASSET = "bgutil-ytdlp-pot-provider-rs.zip"

_BOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POT_DIR = os.path.join(_BOT_DIR, ".pot")
BIN_PATH = os.path.join(POT_DIR, "bgutil-pot")
PLUGIN_DIR = os.path.join(POT_DIR, "plugins")
PLUGIN_PATH = os.path.join(PLUGIN_DIR, PLUGIN_ASSET)
VERSION_FILE = os.path.join(POT_DIR, "version")

_proc: asyncio.subprocess.Process | None = None
status: str = "выключен" if not ENABLED else "запускается…"


def supported() -> bool:
    return platform.system() == "Linux" and platform.machine() in ("x86_64", "AMD64")


def installed() -> bool:
    """Бинарник и плагин на месте — yt-dlp можно давать --plugin-dirs.

    Важно: плагин без бинарника в PATH валит yt-dlp целиком, поэтому только вместе.
    """
    return os.path.isfile(BIN_PATH) and os.access(BIN_PATH, os.X_OK) and os.path.isfile(PLUGIN_PATH)


def ytdlp_args() -> list[str]:
    if ENABLED and installed():
        return ["--plugin-dirs", PLUGIN_DIR]
    return []


def env_path(env: dict) -> dict:
    """PATH с папкой bgutil-pot (cli-часть плагина ищет бинарник в PATH)."""
    path = env.get("PATH", "")
    if POT_DIR not in path.split(os.pathsep):
        env["PATH"] = POT_DIR + os.pathsep + path
    return env


async def _download(session: aiohttp.ClientSession, url: str, dest: str) -> None:
    tmp = dest + ".part"
    async with session.get(url) as resp:
        resp.raise_for_status()
        with open(tmp, "wb") as f:
            async for chunk in resp.content.iter_chunked(1 << 16):
                f.write(chunk)
    os.replace(tmp, dest)


def _installed_version() -> str:
    try:
        with open(VERSION_FILE) as f:
            return f.read().strip()
    except OSError:
        return ""


async def install() -> bool:
    if installed() and _installed_version() == VERSION:
        return True
    os.makedirs(PLUGIN_DIR, exist_ok=True)
    logger.info("[pot] скачиваю bgutil-pot %s…", VERSION)
    timeout = aiohttp.ClientTimeout(total=300)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        await _download(s, f"{_RELEASES}/{VERSION}/{BIN_ASSET}", BIN_PATH)
        await _download(s, f"{_RELEASES}/{VERSION}/{PLUGIN_ASSET}", PLUGIN_PATH)
    os.chmod(BIN_PATH, os.stat(BIN_PATH).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    with open(VERSION_FILE, "w") as f:
        f.write(VERSION)
    logger.info("[pot] ✅ bgutil-pot %s установлен", VERSION)
    return True


async def _alive() -> bool:
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as s:
            async with s.get(f"http://127.0.0.1:{PORT}/ping") as resp:
                return resp.status == 200
    except Exception:  # noqa: BLE001
        return False


async def _start_server() -> None:
    global _proc
    _proc = await asyncio.create_subprocess_exec(
        BIN_PATH, "server", "--host", "127.0.0.1", "--port", str(PORT),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )


async def run_forever() -> None:
    """Фоновая задача: установить, запустить сервер и следить, чтобы он жил."""
    global status
    if not ENABLED:
        return
    if not supported():
        status = f"не поддерживается ({platform.system()} {platform.machine()})"
        logger.warning("[pot] %s — PO Token недоступен", status)
        return
    try:
        await install()
    except Exception as e:  # noqa: BLE001
        status = f"не скачался: {e}"
        logger.warning("[pot] %s", status)
        return
    while True:
        if not await _alive():
            if _proc is not None and _proc.returncode is None:
                _proc.kill()
                await _proc.wait()
            try:
                await _start_server()
            except OSError as e:
                status = f"не запустился: {e}"
                logger.warning("[pot] %s", status)
                await asyncio.sleep(60)
                continue
            for _ in range(20):
                await asyncio.sleep(0.5)
                if await _alive():
                    break
            if await _alive():
                status = f"✅ работает ({VERSION})"
                logger.info("[pot] сервер PO Token запущен на 127.0.0.1:%d", PORT)
            else:
                status = "не отвечает"
                logger.warning("[pot] сервер не ответил на /ping")
        await asyncio.sleep(30)


def stop() -> None:
    if _proc is not None and _proc.returncode is None:
        _proc.kill()
