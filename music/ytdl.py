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
import re
import subprocess
import sys
import time
from dataclasses import dataclass

import config
from . import pot

logger = logging.getLogger("music_bot.ytdl")

_ENV = pot.env_path({**os.environ, "PYTHONIOENCODING": "utf-8"})


class YtdlError(Exception):
    pass


# ── Плавающие отказы YouTube для клиентов с куками ────────────────────────────
# «The page needs to be reloaded» (yt-dlp#17405) и «Requested format is not
# available» зависят от player_client. Перебираем способы по очереди и
# запоминаем сработавший — следующие треки сразу идут через него.
RETRY_MARKERS = ("page needs to be reloaded", "requested format is not available",
                 # попытки без кук с IP хостинга могут упереться в проверку на бота
                 "confirm you\u2019re not a bot", "confirm you're not a bot")
# Ручная настройка клиентов для всех запросов (по умолчанию пусто — как yt-dlp).
EXTRACTOR_ARGS = os.getenv("YTDLP_EXTRACTOR_ARGS", "").strip()


@dataclass(frozen=True, slots=True)
class Attempt:
    name: str
    extra: tuple[str, ...] = ()
    music: bool = False          # открыть ролик как music.youtube.com


# Отключает куки для отдельной попытки (последний флаг в командной строке побеждает).
_NO_COOKIES = ("--no-cookies", "--no-cookies-from-browser")

# Источник обходов — yt-dlp#17389: с куками ломается клиент tv_downgraded
# («page needs to be reloaded»), а на части аккаунтов это происходит всегда.
ATTEMPTS: tuple[Attempt, ...] = (
    Attempt("клиенты по умолчанию"),
    # mweb отдаёт обычные https-форматы, если есть PO Token (music/pot.py).
    Attempt("mweb + PO Token", ("--extractor-args", "youtube:player_client=mweb")),
    # Совет мейнтейнеров yt-dlp для кук.
    Attempt("default + web_embedded",
            ("--extractor-args", "youtube:player_client=default,web_embedded")),
    # Без сломанного tv_downgraded.
    Attempt("web + web_embedded без tv",
            ("--extractor-args", "youtube:player_client=web,web_embedded,-tv_downgraded")),
    # На «проблемных» аккаунтах в issue работал только музыкальный клиент.
    Attempt("YouTube Music (web_music)",
            ("--extractor-args", "youtube:player_client=web_music"), music=True),
    Attempt("web_safari", ("--extractor-args", "youtube:player_client=web_safari")),
    # «page needs to be reloaded» привязана к сессии аккаунта: без кук её нет,
    # а от проверки на бота с IP хостинга иногда спасает PO Token.
    Attempt("без кук: mweb + PO Token",
            _NO_COOKIES + ("--extractor-args", "youtube:player_client=mweb")),
)
_preferred = 0   # индекс способа, сработавшего последним
# Если не сработал ни один способ, следующие ~10 минут пробуем только один,
# чтобы не ждать по минуте перед запасным SoundCloud на каждом треке.
COOLDOWN = 600.0
_all_failed_at = 0.0


_YT_ID_RE = re.compile(r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/)|youtu\.be/)([\w-]{11})")


def _youtube_id(url: str) -> str | None:
    m = _YT_ID_RE.search(url or "")
    return m.group(1) if m and "music.youtube.com" not in url else None


def _should_retry(message: str | None) -> bool:
    low = (message or "").lower()
    return any(m in low for m in RETRY_MARKERS)


def _plan(url: str) -> list[tuple[int, list[str], str]]:
    """(индекс, доп. флаги, ссылка) — начиная со сработавшего в прошлый раз."""
    vid = _youtube_id(url)
    if vid is None:                       # не YouTube (SoundCloud и т. п.) — одна попытка
        return [(0, [], url)]
    order = [_preferred] + [i for i in range(len(ATTEMPTS)) if i != _preferred]
    if _all_failed_at and time.monotonic() - _all_failed_at < COOLDOWN:
        order = order[:1]
    plan = []
    for i in order:
        att = ATTEMPTS[i]
        target = f"https://music.youtube.com/watch?v={vid}" if att.music else url
        plan.append((i, list(att.extra), target))
    return plan


def _remember(i: int) -> None:
    global _preferred
    if i != _preferred:
        logger.info("[youtube] дальше качаю через: %s", ATTEMPTS[i].name)
        _preferred = i


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
    if EXTRACTOR_ARGS:
        args += ["--extractor-args", EXTRACTOR_ARGS]
    args += pot.ytdlp_args()   # плагин PO Token (если bgutil-pot установлен)
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
    """Запуск yt-dlp (последний аргумент — ссылка); на плавающих отказах YouTube — другие клиенты."""
    plan = _plan(args[-1])
    for n, (i, extra, target) in enumerate(plan):
        try:
            out = await _run_once(args[:-1] + extra + [target], timeout)
            if len(plan) > 1:
                _remember(i)
            return out
        except YtdlError as e:
            if not _should_retry(str(e)) or n == len(plan) - 1:
                raise
            logger.info("[youtube] %s — пробую: %s", str(e)[-80:], ATTEMPTS[plan[n + 1][0]].name)
    raise AssertionError("unreachable")


async def _run_once(args: list[str], timeout: float) -> bytes:
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
    args = base_args() + ["-J", "-f", format_for(target)]
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
    return base_args() + ["-f", format_for(url), "--quiet", "-o", "-", url]


# SoundCloud отдаёт за трек Go+ только 30-секундный отрывок (format_id «preview»).
# Такой формат не берём: пусть лучше скачивание упадёт и бот возьмёт другой трек.
_SC_FORMAT = "bestaudio[format_id!*=preview]/best[format_id!*=preview]"


def format_for(url: str) -> str:
    return _SC_FORMAT if "soundcloud.com" in (url or "") else "bestaudio/best"


async def download(url: str, path: str, timeout: float = 300) -> bool:
    """Скачать звук целиком в файл. True — получилось и файл не пустой.

    При любой неудаче и при отмене (трек убрали из очереди) файл удаляется.
    На плавающих отказах YouTube перебирает клиенты (см. ATTEMPTS).
    """
    global _all_failed_at
    plan = _plan(url)
    for n, (i, extra, target) in enumerate(plan):
        ok, message = await _download_once(target, path, timeout, extra)
        if ok:
            if len(plan) > 1 or _all_failed_at:
                _remember(i)
            _all_failed_at = 0.0
            return True
        if not _should_retry(message):
            return False
        if n == len(plan) - 1:
            if len(plan) > 1:
                _all_failed_at = time.monotonic()
                logger.info("[youtube] все способы отказали — %d мин пробую только «%s», "
                            "дальше снова все", int(COOLDOWN // 60), ATTEMPTS[_preferred].name)
                await _log_diagnosis(url)
            return False
        logger.info("[download] пробую: %s", ATTEMPTS[plan[n + 1][0]].name)
    return False


_last_diag = 0.0


async def _log_diagnosis(url: str) -> None:
    """Не чаще раза в 30 минут: что именно ответил YouTube (для разбора)."""
    global _last_diag
    if time.monotonic() - _last_diag < 1800 and _last_diag:
        return
    _last_diag = time.monotonic()
    logger.warning("[youtube] ни один способ не сработал, диагностика yt-dlp -v:")
    for line in await diagnose(url):
        logger.warning("[youtube]   %s", line)


async def _download_once(url: str, path: str, timeout: float,
                         extra: list[str]) -> tuple[bool, str | None]:
    ok = False
    message = None
    cmd = download_cmd(url)
    cmd = cmd[:-1] + extra + cmd[-1:]
    try:
        with open(path, "wb") as f:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=f, stderr=asyncio.subprocess.PIPE, env=_ENV,
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
    return ok, message


async def diagnose(url: str) -> list[str]:
    """Короткая выжимка `yt-dlp -v` для лога, когда YouTube не отдал трек ни одним способом."""
    keys = ("JS runtimes", "Found YouTube account cookies", "playability status",
            "formats have been skipped", "PO Token", "yt-dlp version", "ERROR")
    args = base_args() + ["-v", "--simulate", "-f", "bestaudio/best", url]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE, env=_ENV)
        _, err = await asyncio.wait_for(proc.communicate(), 90)
    except Exception as e:  # noqa: BLE001
        return [f"диагностика не удалась: {e}"]
    lines = err.decode("utf-8", "replace").splitlines()
    out = []
    for ln in lines:
        if any(k in ln for k in keys):
            out.append(ln.strip()[:220])
    return out[-15:]


def js_runtime() -> str | None:
    """Какой JS-рантайм найдёт yt-dlp (нужен для решения JS-challenge YouTube)."""
    import shutil
    for name in ("deno", "node"):
        path = shutil.which(name)
        if path:
            try:
                ver = subprocess.run([path, "--version"], capture_output=True, text=True,
                                     timeout=5).stdout.split()[0:2]
                return f"{name} {' '.join(ver)}".strip()
            except Exception:  # noqa: BLE001
                return name
    return None


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
