"""MusicPlayer: извлечение аудиопотока через yt-dlp и цикл воспроизведения."""
from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

import discord
import yt_dlp

from .queue import GuildMusicState, RepeatMode
from .track import SearchResult, Track

logger = logging.getLogger("music_bot.player")

# Выделенный пул потоков под блокирующие вызовы yt-dlp (resolve/search). Свой пул,
# чтобы тяжёлые extract_info не конкурировали с дефолтным executor event-loop'а и
# нагрузка была предсказуемой. 4 воркера с запасом покрывают несколько серверов.
_YTDL_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ytdl")

# ── Настройки yt-dlp ──────────────────────────────────────────────────────────
# Получаем ТОЛЬКО метаданные + прямой URL потока, без скачивания на диск.
YTDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "nocheckcertificate": True,
    "ignoreerrors": False,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",   # текстовый запрос → поиск на YouTube
    "source_address": "0.0.0.0",    # обход некоторых проблем с IPv6
    "skip_download": True,
    "cachedir": False,              # не плодить кэш на диске
    # Лёгкие клиенты YouTube быстрее отдают потоки и реже ловят
    # «Sign in to confirm / 403», чем дефолтный web-клиент.
    "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
}

# ── Настройки FFmpeg ──────────────────────────────────────────────────────────
# reconnect-флаги критичны: прямые ссылки на поток нестабильны и рвутся.
FFMPEG_BEFORE_OPTS = (
    "-reconnect 1 -reconnect_streamed 1 "
    "-reconnect_delay_max 5"
)
FFMPEG_OPTS = "-vn"

# Один общий экземпляр YoutubeDL (потокобезопасен для extract_info)
_ytdl = yt_dlp.YoutubeDL(YTDL_OPTS)

# Отдельный «лёгкий» экземпляр для поиска вариантов: extract_flat не лезет в
# каждое видео за потоком (это было бы медленно), отдаёт только метаданные списка.
YTDL_SEARCH_OPTS = {
    **YTDL_OPTS,
    "extract_flat": True,
    "noplaylist": False,
}
_ytdl_search = yt_dlp.YoutubeDL(YTDL_SEARCH_OPTS)


class MusicPlayer:
    """Управляет воспроизведением для одного сервера.

    Запускает фоновую корутину _player_loop, которая последовательно берёт
    треки из GuildMusicState и проигрывает их через FFmpegOpusAudio.
    """

    def __init__(
        self,
        voice_client: discord.VoiceClient,
        state: GuildMusicState,
        idle_timeout: int,
        on_disconnect,
        on_track_change=None,
    ):
        self.voice_client = voice_client
        self.state = state
        self.idle_timeout = idle_timeout
        self._on_disconnect = on_disconnect  # async callback(guild_id) при простое
        # async callback(guild_id): дёргаем при смене того, что играет, — чтобы
        # cog обновил живую панель-плеер.
        self._on_track_change = on_track_change
        self._loop = asyncio.get_running_loop()

    async def _notify_change(self) -> None:
        if self._on_track_change is not None:
            try:
                await self._on_track_change(self.state.guild_id)
            except Exception as e:  # noqa: BLE001
                logger.warning("[player] on_track_change failed: %s", e)

    # ── Извлечение трека через yt-dlp (в отдельном потоке) ────────────────
    @staticmethod
    async def resolve(query: str, requester: str) -> Track | None:
        """Получить Track по ссылке или поисковому запросу. None при ошибке."""
        loop = asyncio.get_running_loop()
        try:
            # extract_info блокирующий → выносим в выделенный пул, чтобы не вешать loop
            data = await loop.run_in_executor(
                _YTDL_EXECUTOR, lambda: _ytdl.extract_info(query, download=False)
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[resolve] yt-dlp error for %r: %s", query, e)
            return None

        if data is None:
            return None

        # Поиск/плейлист возвращает 'entries' — берём первый результат
        if "entries" in data:
            entries = [e for e in data["entries"] if e]
            if not entries:
                return None
            data = entries[0]

        stream_url = data.get("url")
        if not stream_url:
            logger.warning("[resolve] no stream url for %r", query)
            return None

        return Track(
            title=data.get("title", "Unknown"),
            stream_url=stream_url,
            webpage_url=data.get("webpage_url", query),
            duration=data.get("duration"),
            uploader=data.get("uploader"),
            thumbnail=data.get("thumbnail"),
            requested_by=requester,
        )

    @staticmethod
    async def search(query: str, limit: int = 5) -> list[SearchResult]:
        """Найти несколько вариантов по тексту (longmix, sped up, slowed, remix…).

        Возвращает до `limit` результатов без скачивания потоков (быстро).
        Поток получаем позже, только для выбранного варианта (resolve).
        """
        loop = asyncio.get_running_loop()
        try:
            data = await loop.run_in_executor(
                _YTDL_EXECUTOR,
                lambda: _ytdl_search.extract_info(f"ytsearch{limit}:{query}", download=False),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[search] yt-dlp error for %r: %s", query, e)
            return []

        entries = (data or {}).get("entries") or []
        out: list[SearchResult] = []
        for e in entries:
            if not e:
                continue
            url = e.get("url") or e.get("webpage_url")
            if not url:
                continue
            out.append(SearchResult(
                title=e.get("title", "Unknown"),
                url=url,
                duration=e.get("duration"),
                uploader=e.get("uploader") or e.get("channel"),
            ))
        return out

    # ── Основной цикл воспроизведения ─────────────────────────────────────
    async def player_loop(self) -> None:
        """Берёт треки из очереди и проигрывает их по очереди.

        Завершается сам при простое дольше idle_timeout — тогда вызывает
        on_disconnect для отключения от голосового канала.
        """
        while True:
            self.state.next_event.clear()

            track = self.state.get_nowait()
            if track is None:
                # Очередь пуста — ждём новый трек или таймаут простоя
                try:
                    await asyncio.wait_for(
                        self.state.next_event.wait(), timeout=self.idle_timeout
                    )
                except asyncio.TimeoutError:
                    logger.info(
                        "[player_loop] idle timeout on guild %s — disconnecting",
                        self.state.guild_id,
                    )
                    await self._on_disconnect(self.state.guild_id)
                    return
                continue

            self.state.current = track

            try:
                # method="fallback" — если ffprobe недоступен/тормозит, не падаем,
                # а определяем кодек запасным способом.
                source = await discord.FFmpegOpusAudio.from_probe(
                    track.stream_url,
                    method="fallback",
                    before_options=FFMPEG_BEFORE_OPTS,
                    options=FFMPEG_OPTS,
                )
            except Exception as e:  # noqa: BLE001
                logger.error("[player_loop] FFmpeg source error: %s", e)
                self.state.current = None
                continue

            # Колбэк after вызывается из другого потока → пробрасываем в loop
            def _after(error: Exception | None) -> None:
                if error:
                    logger.error("[player_loop] playback error: %s", error)
                self._loop.call_soon_threadsafe(self.state.next_event.set)

            if not self.voice_client.is_connected():
                logger.info("[player_loop] voice disconnected, stopping loop")
                return

            self.voice_client.play(source, after=_after)
            logger.info(
                "[player_loop] now playing %r on guild %s",
                track.title, self.state.guild_id,
            )
            await self._notify_change()  # обновить панель: заиграл новый трек

            # Ждём окончания трека (event выставит _after)
            await self.state.next_event.wait()

            # Повтор: трек завершился — решаем его судьбу по режиму повтора.
            # skipped=True (через /skip или кнопку) перебивает repeat-one и
            # repeat-all для ЭТОГО трека: пропуск всегда идёт к следующему.
            if not self.state.skipped:
                if self.state.repeat is RepeatMode.ONE:
                    self.state.add_front(track)   # тот же трек снова
                elif self.state.repeat is RepeatMode.ALL:
                    self.state.add(track)          # в конец — крутим всю очередь
            self.state.skipped = False

            self.state.current = None
            await self._notify_change()  # обновить панель: трек закончился
