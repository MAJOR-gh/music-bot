"""yt-dlp в отдельных процессах.

Почему не внутри бота: разбор страниц YouTube грузит CPU и держит GIL — поток,
который каждые 20 мс отдаёт звук в Discord, начинает опаздывать, и звук
подёргивается, пока кто-то ищет трек. Отдельный процесс этого не делает, а ещё
всегда берёт текущую установленную версию yt-dlp: автообновление (updater.py)
подхватывается без перезапуска бота.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time

import config

logger = logging.getLogger("music_bot.ytdl")

_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}


class YtdlError(Exception):
    pass


def base_args() -> list[str]:
    """Общие флаги: JS-рантайм для обхода защиты YouTube, куки, IPv4."""
    args = [
        sys.executable, "-m", "yt_dlp",
        "--no-warnings",
        "--no-playlist",
        "--force-ipv4",
        # YouTube требует решать JS-challenge (sig/n); солвер — пакет yt-dlp-ejs.
        "--js-runtimes", "deno",
        "--js-runtimes", "node",
    ]
    if config.YTDLP_COOKIES:
        args += ["--cookies", config.YTDLP_COOKIES]
    elif config.YTDLP_COOKIES_FROM_BROWSER:
        args += ["--cookies-from-browser", config.YTDLP_COOKIES_FROM_BROWSER]
    return args


# ── «Sign in to confirm you're not a bot» ─────────────────────────────────────
# YouTube не доверяет IP (датацентр, VPN-выход) и требует вход в аккаунт.
# Лечится куками залогиненного аккаунта (cookies.txt). Запоминаем, когда это
# было в последний раз, — показываем в /ping и подсказываем в логе.
BOT_CHECK_MARKERS = ("confirm you're not a bot", "confirm you’re not a bot",
                     "sign in to confirm")
last_bot_check: float | None = None
_last_hint = 0.0


def cookies_mode() -> str:
    if config.YTDLP_COOKIES:
        return "cookies.txt"
    if config.YTDLP_COOKIES_FROM_BROWSER:
        return f"из браузера ({config.YTDLP_COOKIES_FROM_BROWSER})"
    return ""


def note_error(message: str) -> None:
    """Если YouTube требует вход — запомнить и (не чаще раза в 10 минут) подсказать, что делать."""
    global last_bot_check, _last_hint
    low = message.lower()
    if not any(m in low for m in BOT_CHECK_MARKERS):
        return
    last_bot_check = time.time()
    if time.monotonic() - _last_hint < 600:
        return
    _last_hint = time.monotonic()
    if cookies_mode():
        logger.warning("[youtube] YouTube требует вход, хотя куки заданы (%s) — куки протухли "
                       "или аккаунт разлогинило: экспортируй cookies.txt заново.", cookies_mode())
    else:
        logger.warning("[youtube] YouTube считает IP ботом («Sign in to confirm you're not a bot»). "
                       "Лечится куками аккаунта YouTube: положи cookies.txt рядом с ботом "
                       "(или YTDLP_COOKIES_FROM_BROWSER=firefox в .env при запуске на своём ПК). "
                       "Пока что бот ищет такие треки на SoundCloud.")


def _last_error_line(stderr: bytes) -> str:
    lines = [ln for ln in stderr.decode("utf-8", "replace").splitlines() if ln.strip()]
    errors = [ln for ln in lines if "ERROR" in ln]
    return (errors or lines or ["неизвестная ошибка"])[-1][:300]


async def _kill(proc: asyncio.subprocess.Process) -> None:
    """Убить процесс yt-dlp (он мог уже завершиться сам) и дождаться выхода."""
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    await proc.wait()


async def _run(args: list[str], timeout: float) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=_ENV,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except BaseException:           # таймаут или отмена (трек убрали, бот выходит)
        await _kill(proc)
        raise
    if proc.returncode != 0:
        message = _last_error_line(err)
        note_error(message)
        raise YtdlError(message)
    return out


async def extract(target: str, *, flat: bool = False, timeout: float = 60) -> dict:
    """Метаданные по ссылке или запросу (`ytsearch5:…`). flat — без похода в каждое видео."""
    args = base_args() + ["-J", "-f", "bestaudio/best"]
    if flat:
        args.append("--flat-playlist")
    args.append(target)
    try:
        out = await _run(args, timeout)
    except asyncio.TimeoutError:
        raise YtdlError(f"таймаут {timeout:.0f}с") from None
    try:
        return json.loads(out)
    except ValueError:
        raise YtdlError("yt-dlp вернул не JSON") from None


def first_entry(data: dict | None) -> dict | None:
    """Поиск/плейлист возвращает entries — берём первый непустой."""
    if data and "entries" in data:
        entries = [e for e in data["entries"] if e]
        return entries[0] if entries else None
    return data


def best_thumbnail(info: dict) -> str | None:
    if info.get("thumbnail"):
        return info["thumbnail"]
    thumbs = [t for t in info.get("thumbnails") or [] if t.get("url")]
    return thumbs[-1]["url"] if thumbs else None


def download_cmd(url: str) -> list[str]:
    """Команда: лучший звук трека → stdout."""
    return base_args() + ["-f", "bestaudio/best", "--quiet", "-o", "-", url]


async def download(url: str, path: str, timeout: float = 300) -> bool:
    """Скачать звук целиком в файл. True — получилось и файл не пустой.

    При любой неудаче и при отмене (трек убрали из очереди) файл удаляется.
    """
    ok = False
    try:
        with open(path, "wb") as f:
            proc = await asyncio.create_subprocess_exec(
                *download_cmd(url), stdout=f, stderr=asyncio.subprocess.PIPE, env=_ENV,
            )
            try:
                _, err = await asyncio.wait_for(proc.communicate(), timeout)
            except BaseException:
                await _kill(proc)
                raise
        ok = proc.returncode == 0 and os.path.getsize(path) > 0
        if not ok:
            message = _last_error_line(err)
            note_error(message)
            logger.warning("[download] %s: %s", url, message)
    except asyncio.TimeoutError:
        logger.warning("[download] %s: таймаут %.0fс", url, timeout)
    except OSError as e:
        logger.warning("[download] %s: %s", url, e)
    finally:
        if not ok:
            remove_file(path)
    return ok


def spawn_pipe(url: str) -> subprocess.Popen:
    """yt-dlp, льющий звук в stdout (живой поток — для эфиров и очень длинных треков)."""
    return subprocess.Popen(
        download_cmd(url), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=_ENV,
    )


def remove_file(path: str | None) -> bool:
    """Удалить файл. True — файла больше нет."""
    if not path:
        return True
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError:
        return False
    return True


_pending_removals: set[asyncio.Task] = set()


async def _remove_with_retries(path: str) -> None:
    for _ in range(30):
        await asyncio.sleep(1)
        if remove_file(path):
            return
    logger.warning("[cleanup] не смог удалить %s — удалится при следующем запуске", path)


def discard_file(path: str | None) -> None:
    """Удалить временный файл трека, даже если его ещё держит FFmpeg.

    discord.py зовёт after-колбэк ДО того, как убивает FFmpeg, а Windows не даёт
    удалить открытый файл. Поэтому если сразу не вышло — пробуем ещё раз в фоне.
    """
    if remove_file(path):
        return
    try:
        task = asyncio.get_running_loop().create_task(_remove_with_retries(path))
    except RuntimeError:        # нет event loop (выход из бота) — подчистит старт
        return
    _pending_removals.add(task)
    task.add_done_callback(_pending_removals.discard)
