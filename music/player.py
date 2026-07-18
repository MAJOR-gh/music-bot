"""MusicPlayer: извлечение аудиопотока через yt-dlp и цикл воспроизведения."""
from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import aiohttp
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
    # ВНИМАНИЕ: не форсируем player_client. Принудительный android-клиент YouTube
    # отдаёт throttled-потоки → музыка лагает/заикается. Пусть yt-dlp сам выбирает
    # лучший рабочий клиент — так поток стабильнее.
    # YouTube теперь требует JS-рантайм для решения sig/n-challenge; без него
    # yt-dlp отдаёт тротлённые или «мёртвые» ссылки — трек обрывается через
    # пару секунд, бот молчит. Разрешаем deno (дефолт) и node (стоит у нас);
    # солвер ставится пакетом yt-dlp-ejs (см. requirements.txt).
    "js_runtimes": {"deno": {}, "node": {}},
}

# ── Настройки FFmpeg ──────────────────────────────────────────────────────────
# reconnect-флаги критичны: прямые ссылки на поток нестабильны и рвутся.
# rw_timeout (микросекунды) — не висеть вечно на «мёртвом» соединении:
# на хостингах googlevideo часто не отвечает датацентровым IP, и без таймаута
# ffmpeg молча ждёт, а бот «играет тишину».
FFMPEG_BEFORE_OPTS = (
    "-reconnect 1 -reconnect_streamed 1 "
    "-reconnect_delay_max 5 -rw_timeout 15000000"
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

# Глобальный (на процесс) флаг «прямые ссылки googlevideo не работают».
# Раньше жил в экземпляре MusicPlayer и сбрасывался при каждом переподключении
# к войсу — бот заново наступал на те же грабли (30+ секунд таймаутов ffprobe/
# ffmpeg на каждый первый трек). Сеть хостинга за время жизни процесса не
# меняется, так что запоминаем один раз.
_PREFER_PIPE = False


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

    @staticmethod
    async def _stream_reachable(url: str) -> bool:
        """Быстрая проверка (≤4с), открывается ли прямая ссылка googlevideo.

        Обязательна перед from_probe: на мёртвой ссылке ffprobe висит до 20с,
        а fallback-проба discord.py «успешно» возвращает дефолтный кодек —
        источник создаётся, ffmpeg умирает, и бот молчит. Дешевле спросить
        1 КБ по HTTP заранее, чем терять полминуты на таймауты.
        """
        try:
            timeout = aiohttp.ClientTimeout(total=4)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    url, headers={"Range": "bytes=0-1023"}
                ) as resp:
                    return resp.status in (200, 206)
        except Exception:  # noqa: BLE001
            return False

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

    # ── Создание аудио-источника ──────────────────────────────────────────
    def _spawn_pipe(self, track: Track) -> subprocess.Popen:
        """Запустить yt-dlp, который льёт аудио в stdout (для pipe-режима).

        Качает через сам yt-dlp: чанками, с обходом троттлинга и --force-ipv4 —
        работает там, где прямая ссылка для ffmpeg недоступна (датацентровые IP).
        """
        cmd = [
            sys.executable, "-m", "yt_dlp",
            "-f", "bestaudio/best",
            "--no-playlist",
            "--force-ipv4",
            "--js-runtimes", "deno",
            "--js-runtimes", "node",
            "--quiet", "--no-warnings",
            "-o", "-",
            track.webpage_url,
        ]
        return subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )

    @staticmethod
    def _kill_pipe(proc: subprocess.Popen | None) -> None:
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

    async def _make_source(
        self, track: Track
    ) -> tuple[discord.AudioSource | None, bool, subprocess.Popen | None]:
        """Создать источник звука. Возвращает (source, used_pipe, ytdlp_proc).

        Сначала пробуем прямой поток (дёшево: без перекодирования, если opus).
        Если он не открывается — переходим на pipe через yt-dlp и запоминаем
        это в _PREFER_PIPE (глобально на процесс), чтобы не тратить время
        на мёртвый вариант при каждом треке/переподключении.
        """
        global _PREFER_PIPE
        if not _PREFER_PIPE:
            if not await self._stream_reachable(track.stream_url):
                logger.warning(
                    "[player_loop] прямая ссылка googlevideo недоступна "
                    "(датацентровый IP?) — переключаюсь на yt-dlp pipe"
                )
                _PREFER_PIPE = True
        if not _PREFER_PIPE:
            try:
                # from_probe (ffprobe) точно определяет кодек: если это opus,
                # FFmpeg копирует поток без перекодирования (дёшево).
                source = await discord.FFmpegOpusAudio.from_probe(
                    track.stream_url,
                    before_options=FFMPEG_BEFORE_OPTS,
                    options=FFMPEG_OPTS,
                )
                return source, False, None
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "[player_loop] прямой поток не открылся (%s) — "
                    "переключаюсь на yt-dlp pipe", e,
                )
                _PREFER_PIPE = True

        try:
            proc = self._spawn_pipe(track)
            # Без from_probe: пайп нельзя «прощупать» дважды. FFmpeg сам
            # перекодирует в opus — чуть дороже по CPU, зато надёжно.
            source = discord.FFmpegOpusAudio(
                proc.stdout, pipe=True, options=FFMPEG_OPTS
            )
            return source, True, proc
        except Exception as e:  # noqa: BLE001
            logger.error("[player_loop] pipe source error: %s", e)
            return None, True, None

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

            source, used_pipe, pipe_proc = await self._make_source(track)
            if source is None:
                logger.error("[player_loop] FFmpeg source error, skip %r", track.title)
                self.state.current = None
                continue

            # Колбэк after вызывается из другого потока → пробрасываем в loop
            def _after(error: Exception | None) -> None:
                if error:
                    logger.error("[player_loop] playback error: %s", error)
                self._loop.call_soon_threadsafe(self.state.next_event.set)

            if not self.voice_client.is_connected():
                self._kill_pipe(pipe_proc)
                logger.info("[player_loop] voice disconnected, stopping loop")
                return

            started_at = self._loop.time()
            self.voice_client.play(source, after=_after)
            logger.info(
                "[player_loop] now playing %r on guild %s%s",
                track.title, self.state.guild_id,
                " (yt-dlp pipe)" if used_pipe else "",
            )
            await self._notify_change()  # обновить панель: заиграл новый трек

            # Ждём окончания трека (event выставит _after)
            await self.state.next_event.wait()
            self._kill_pipe(pipe_proc)

            played = self._loop.time() - started_at
            # «Молчащий бот»: прямой поток открылся, но умер почти сразу
            # (датацентровый IP, Connection timed out). Не считаем трек
            # отыгранным — пробуем его же ещё раз, уже через yt-dlp pipe.
            if (
                not used_pipe
                and not self.state.skipped
                and played < 5
                and (track.duration or 999) > 10
            ):
                logger.warning(
                    "[player_loop] трек оборвался за %.1fс — повтор через pipe",
                    played,
                )
                global _PREFER_PIPE
                _PREFER_PIPE = True
                self.state.add_front(track)
                self.state.current = None
                continue

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
