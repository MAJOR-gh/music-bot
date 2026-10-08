"""Загрузка конфигурации из переменных окружения (.env)."""
from __future__ import annotations

import os
import shutil

from dotenv import load_dotenv

load_dotenv()

# Токен Discord-бота (обязательно)
DISCORD_TOKEN: str | None = os.getenv("DISCORD_TOKEN")

# ID сервера для МГНОВЕННОЙ синхронизации слэш-команд (опционально).
# Если не задан — команды синхронизируются глобально (может занять до ~1 часа).
_guild_id_raw = os.getenv("GUILD_ID", "").strip()
GUILD_ID: int | None = int(_guild_id_raw) if _guild_id_raw.isdigit() else None

# Уровень логирования
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

# Автоотключение при бездействии (секунды). По умолчанию 5 минут.
IDLE_TIMEOUT: int = int(os.getenv("IDLE_TIMEOUT", "300"))

# Сколько вариантов показывать в выпадающем списке текстового поиска (/play <текст>).
# Список перелистывается по SEARCH_PAGE_SIZE штук на страницу.
SEARCH_RESULTS: int = max(1, int(os.getenv("SEARCH_RESULTS", "25")))
SEARCH_PAGE_SIZE: int = 10  # пунктов на странице выпадашки (Discord max — 25)

# Где по умолчанию искать текст из /play: youtube | ytmusic | spotify | lastfm.
# (В самой команде источник можно выбрать параметром source.)
DEFAULT_SEARCH: str = os.getenv("DEFAULT_SEARCH", "youtube").strip().lower() or "youtube"

# Поиск по Spotify — нужны ключи приложения (бесплатно: developer.spotify.com →
# Dashboard → Create app). Ссылки на Spotify (трек/альбом/плейлист) работают и без них.
SPOTIFY_CLIENT_ID: str = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
SPOTIFY_CLIENT_SECRET: str = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()

# Поиск по Last.fm — нужен API-ключ (бесплатно: last.fm/api/account/create).
LASTFM_API_KEY: str = os.getenv("LASTFM_API_KEY", "").strip()

# Сколько треков максимум брать из плейлиста/альбома Spotify за раз.
MAX_PLAYLIST_TRACKS: int = max(1, int(os.getenv("MAX_PLAYLIST_TRACKS", "100")))

# Автообновление yt-dlp (раз в 12 часов). YouTube постоянно ломает старые версии:
# устаревший yt-dlp получает 403 на скачивание — звук рвётся/пропадает.
YTDLP_AUTO_UPDATE: bool = os.getenv("YTDLP_AUTO_UPDATE", "1").strip().lower() not in ("0", "false", "no")

# Форсировать yt-dlp pipe (минуя прямые ссылки googlevideo). Ставь 1 на
# хостинге с датацентровым IP: googlevideo такие IP режет, прямой поток
# умирает через пару секунд, и бот молчит/панель исчезает.
FORCE_PIPE: bool = os.getenv("FORCE_PIPE", "").strip().lower() in ("1", "true", "yes")

# Диагностический выбор кодировщика. opus: FFmpeg выдаёт готовый Opus;
# pcm: FFmpeg выдаёт PCM, а discord.py кодирует его системной libopus.
AUDIO_BACKEND: str = os.getenv("AUDIO_BACKEND", "opus").strip().lower()
if AUDIO_BACKEND not in ("opus", "pcm"):
    raise ValueError("AUDIO_BACKEND должен быть opus или pcm")

# Папка с ffmpeg.exe/ffprobe.exe. Если задана — добавляем её в PATH процесса,
# чтобы discord.py нашёл и ffmpeg, и ffprobe без правки системного PATH.
FFMPEG_DIR: str = os.getenv("FFMPEG_DIR", "").strip()
if FFMPEG_DIR and os.path.isdir(FFMPEG_DIR):
    os.environ["PATH"] = FFMPEG_DIR + os.pathsep + os.environ.get("PATH", "")

# cookies.txt YouTube-аккаунта (формат Netscape). YouTube режет датацентровые IP
# («Sign in to confirm you're not a bot»), с куками залогиненного аккаунта пускает.
# По умолчанию берём cookies.txt рядом с ботом, если он есть.
_BOT_DIR = os.path.dirname(os.path.abspath(__file__))
_cookies = os.getenv("YTDLP_COOKIES", "").strip() or os.path.join(_BOT_DIR, "cookies.txt")
YTDLP_COOKIES: str | None = _cookies if os.path.isfile(_cookies) else None
# Или брать куки прямо из браузера на этом ПК (firefox, chrome, edge…) — только
# если файла cookies.txt нет. На хостинге браузера нет — там нужен cookies.txt.
YTDLP_COOKIES_FROM_BROWSER: str = os.getenv("YTDLP_COOKIES_FROM_BROWSER", "").strip()

# Хостинги-панели (Pterodactyl) ставят пакеты через `pip --prefix .local`:
# бинарники из pip (deno — JS-рантайм для yt-dlp) лежат в .local/bin,
# которого нет в PATH.
_LOCAL_BIN = os.path.join(_BOT_DIR, ".local", "bin")
if os.path.isdir(_LOCAL_BIN):
    os.environ["PATH"] = _LOCAL_BIN + os.pathsep + os.environ.get("PATH", "")

# Нет системного ffmpeg (на панели не поставить apt) — берём статическую
# сборку ffmpeg+ffprobe из пакета static-ffmpeg (скачивается при 1-м запуске).
if not shutil.which("ffmpeg"):
    try:
        import static_ffmpeg

        static_ffmpeg.add_paths()
    except ImportError:
        pass
