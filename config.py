"""Загрузка конфигурации из переменных окружения (.env)."""
from __future__ import annotations

import os

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

# Папка с ffmpeg.exe/ffprobe.exe. Если задана — добавляем её в PATH процесса,
# чтобы discord.py нашёл и ffmpeg, и ffprobe без правки системного PATH.
FFMPEG_DIR: str = os.getenv("FFMPEG_DIR", "").strip()
if FFMPEG_DIR and os.path.isdir(FFMPEG_DIR):
    os.environ["PATH"] = FFMPEG_DIR + os.pathsep + os.environ.get("PATH", "")
