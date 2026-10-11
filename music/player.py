"""MusicPlayer: подготовка трека (поиск, скачивание) и цикл воспроизведения.

Как играет трек:
  1. Подготовка (_prepare): если трек из Spotify/Last.fm — находим его на YT Music;
     дальше либо берём прямую ссылку на поток, либо (FORCE_PIPE / датацентровый
     IP) скачиваем звук целиком во временный файл через yt-dlp.
  2. Пока играет трек, следующий в очереди готовится заранее (prefetch) —
     между треками нет паузы на скачивание.
  3. FFmpeg приводит любой источник к stereo Opus 48 кГц с пакетами по 20 мс.
"""
from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import random
import subprocess
import tempfile
import uuid
from dataclasses import dataclass

import aiohttp
import discord

import config
from . import sources, ytdl
from .queue import GuildMusicState, RepeatMode
from .track import Track

logger = logging.getLogger("music_bot.player")

# reconnect-флаги критичны: прямые ссылки на поток нестабильны и рвутся.
# rw_timeout (мкс) — не висеть вечно на «мёртвом» соединении.
FFMPEG_BEFORE_OPTS = (
    "-reconnect 1 -reconnect_streamed 1 "
    "-reconnect_delay_max 5 -rw_timeout 15000000"
)
# discord.py отправляет один пакет каждые 20 мс и увеличивает RTP timestamp
# на 960 samples. Копирование исходного Opus НЕ меняет длину пакетов и число
# каналов: входные 40/60 мс не подходят этому таймингу. Такие перекодируем (см. _can_copy).
# discord.py (FFmpegOpusAudio) жёстко добавляет «-fec true -packet_loss 15»:
# libopus резервирует часть битрейта под избыточность на 15% потерь, и при
# перекодировании музыки слышен хрип (замер на тоне: SNR 20 дБ против 37–44 дБ).
# Наши options идут после аргументов discord.py, поэтому последнее значение
# выигрывает — выключаем FEC и ожидаемые потери.
FFMPEG_OPTS = "-vn -frame_duration 20 -fec false -packet_loss 0"
# Для stream copy encoder-опции libopus не нужны.
COPY_OPTS = "-vn"
# Битрейт перекодирования (кбит/с). 128 заметно портит уже сжатые AAC/MP3
# (двойное сжатие); 256 — запас качества, пакеты ~640 байт.
ENCODE_BITRATE = int(os.getenv("AUDIO_BITRATE", "256"))
# Сколько первых пакетов проверять перед stream copy.
PROBE_PACKETS = 50

# «Прямые ссылки googlevideo не работают» — запоминаем на весь процесс (сеть
# не меняется). FORCE_PIPE=1 выставляет сразу: на датацентровых IP и через
# туннели сразу качаем через yt-dlp, не тратя время на мёртвые ссылки.
_PREFER_PIPE = config.FORCE_PIPE

# Треки длиннее часа не качаем на диск, а играем живым потоком.
MAX_CACHE_DURATION = 60 * 60
TMP_PREFIX = "musicbot-"
# Сколько ждать, пока discord.py сам переподключит голос после обрыва.
RECONNECT_WAIT = 30
# Кто «заказал» трек, подобранный автоплеем; из скольких самых похожих выбирать.
AUTOPLAY_NAME = "📻 Автоплей"
AUTOPLAY_PICK_FROM = 5
# Длина одного Opus-пакета — по ним считаем, сколько трека уже прозвучало.
PACKET_SECONDS = 0.02


def prefer_pipe() -> bool:
    return _PREFER_PIPE


def cleanup_stale_files() -> int:
    """Удалить временные файлы, оставшиеся после падения прошлого запуска."""
    removed = 0
    for path in glob.glob(os.path.join(tempfile.gettempdir(), TMP_PREFIX + "*.audio")):
        if ytdl.remove_file(path):
            removed += 1
    return removed


@dataclass(slots=True)
class Prepared:
    """Готовый к игре трек: прямой поток, файл на диске или живой yt-dlp pipe."""

    kind: str                  # "direct" | "file" | "pipe"
    url: str | None = None     # direct: ссылка на поток; pipe: страница трека
    path: str | None = None    # file: путь к скачанному звуку

    def discard(self) -> None:
        ytdl.discard_file(self.path)


async def _stream_reachable(url: str) -> bool:
    """Быстро (≤4с) проверить, открывается ли прямая ссылка на поток.

    На мёртвой ссылке ffprobe висит до 20с, а потом бот молчит — дешевле
    заранее спросить 1 КБ по HTTP.
    """
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=4)) as s:
            async with s.get(url, headers={"Range": "bytes=0-1023"}) as resp:
                return resp.status in (200, 206)
    except Exception:  # noqa: BLE001
        return False


class DiscordOpusAudio(discord.FFmpegOpusAudio):
    """В голос отправляем Opus-аудио, а не служебные заголовки Ogg.

    packets — сколько пакетов по 20 мс уже ушло в голос (позиция в треке;
    на паузе discord.py не читает, так что счётчик стоит).
    """

    packets = 0

    def read(self) -> bytes:
        while True:
            packet = super().read()
            if not packet.startswith((b"OpusHead", b"OpusTags")):
                if packet:
                    self.packets += 1
                return packet


class MusicPlayer:
    """Воспроизведение для одного сервера: фоновая корутина player_loop."""

    def __init__(
        self,
        guild: discord.Guild,
        state: GuildMusicState,
        idle_timeout: int,
        on_disconnect,
        on_track_change=None,
        on_track_error=None,
    ):
        self.guild = guild
        self.state = state
        self.idle_timeout = idle_timeout
        self._on_disconnect = on_disconnect      # async (guild_id) — простой/потеря голоса
        self._on_track_change = on_track_change  # async (guild_id) — обновить панель
        self._on_track_error = on_track_error    # async (guild_id, track) — трек не сыграл
        self._loop = asyncio.get_running_loop()
        # Заранее готовящиеся треки: id(track) → (track, задача/future с Prepared).
        # Сам трек храним, чтобы не спутать с новым объектом, получившим тот же id.
        self._prefetch: dict[int, tuple[Track, asyncio.Future]] = {}
        # Что играет прямо сейчас — чтобы прибрать за собой при отмене плеера.
        self._playing: tuple[Prepared, subprocess.Popen | None] | None = None
        # Подготовка текущего трека (поиск/скачивание) — её обрывает «Пропустить».
        self._prep_task: asyncio.Task | None = None
        # Позиция: источник, который сейчас звучит, и с какой секунды он начат.
        self._source = None
        self._offset = 0.0
        # Перемотка: куда перейти (секунды) — плеер переоткроет тот же трек с -ss.
        self._seek_to: float | None = None
        # Автоплей: фоновый подбор следующего трека и сид, для которого ничего не нашлось.
        self._ap_task: asyncio.Task | None = None
        self._ap_failed: Track | None = None

    @property
    def voice_client(self) -> discord.VoiceClient | None:
        """Всегда актуальный голос сервера (после /leave + /join объект новый)."""
        return self.guild.voice_client  # type: ignore[return-value]

    async def _notify(self, callback, *args) -> None:
        if callback is not None:
            try:
                await callback(self.state.guild_id, *args)
            except Exception as e:  # noqa: BLE001
                logger.warning("[player] callback %s failed: %s", callback.__name__, e)

    # ── Подготовка трека ──────────────────────────────────────────────────
    @staticmethod
    async def _fill_from_match(track: Track) -> bool:
        """Трек из Spotify/Last.fm → найти играбельную версию на YT Music."""
        if track.webpage_url:
            return True
        artist, title = track.match or ("", track.title)
        found = await sources.find_on_ytmusic(artist, title, track.duration)
        if found is None:
            logger.warning("[prepare] не нашёл на YouTube Music: %s — %s", artist, title)
            return False
        track.webpage_url = found.url
        track.duration = track.duration or found.duration
        track.thumbnail = track.thumbnail or found.thumbnail
        return True

    @staticmethod
    def _tmp_path() -> str:
        return os.path.join(tempfile.gettempdir(), f"{TMP_PREFIX}{uuid.uuid4().hex}.audio")

    @staticmethod
    def _download_timeout(track: Track) -> float:
        # Не меньше 3 минут, а для длинных треков — не меньше их длительности.
        return max(180.0, float(track.duration or 0))

    @staticmethod
    async def _download_with_fallback(track: Track) -> Prepared | None:
        path = MusicPlayer._tmp_path()
        timeout = MusicPlayer._download_timeout(track)
        if await ytdl.download(track.webpage_url, path, timeout):
            return Prepared("file", path=path)
        # YouTube отказал (403, «Sign in to confirm…») — ищем ту же песню на SoundCloud.
        if config.SOUNDCLOUD_FALLBACK and "youtu" in (track.webpage_url or ""):
            if track.match:
                artist, title = track.match
            else:
                artist, title = sources.split_artist_title(track.title, track.uploader)
            alts = await sources.soundcloud_alternatives(artist, title, track.duration)
            for alt in alts[:3]:
                logger.info("[prepare] YouTube не отдал %r — играю с SoundCloud", track.title)
                path = MusicPlayer._tmp_path()
                if await ytdl.download(alt, path, timeout):
                    return Prepared("file", path=path)
        return None

    @staticmethod
    async def _prepare(track: Track) -> Prepared | None:
        """Довести трек до состояния «можно играть». None — не получилось."""
        global _PREFER_PIPE
        if not await MusicPlayer._fill_from_match(track):
            return None
        if not _PREFER_PIPE:
            try:
                info = ytdl.first_entry(await ytdl.extract(track.webpage_url))
            except ytdl.YtdlError as e:
                logger.warning("[prepare] yt-dlp: %s", e)
                info = None
            if info and info.get("url"):
                track.duration = track.duration or info.get("duration")
                track.live = track.live or bool(info.get("is_live"))
                if await _stream_reachable(info["url"]):
                    return Prepared("direct", url=info["url"])
                logger.warning("[prepare] прямая ссылка недоступна (датацентровый IP/туннель?) "
                               "— дальше качаю через yt-dlp")
                _PREFER_PIPE = True
        if track.live or (track.duration and track.duration > MAX_CACHE_DURATION):
            return Prepared("pipe", url=track.webpage_url)   # эфир или очень длинный трек
        return await MusicPlayer._download_with_fallback(track)

    # ── Предзагрузка следующего трека ─────────────────────────────────────
    def _drop_prefetch(self, key: int) -> None:
        entry = self._prefetch.pop(key, None)
        if entry is None:
            return
        fut = entry[1]
        if not fut.done():
            fut.cancel()            # ytdl.download сам удалит недокачанный файл
        elif not fut.cancelled() and fut.exception() is None and fut.result():
            fut.result().discard()

    def drop_all_prefetch(self) -> None:
        for key in list(self._prefetch):
            self._drop_prefetch(key)

    def schedule_prefetch(self) -> None:
        """Начать готовить следующий трек очереди (пока играет текущий)."""
        upcoming = self.state.upcoming
        nxt = upcoming[0] if upcoming else None
        for key, (track, _) in list(self._prefetch.items()):
            if track is not nxt:
                self._drop_prefetch(key)
        if nxt is not None and id(nxt) not in self._prefetch:
            self._prefetch[id(nxt)] = (nxt, asyncio.ensure_future(self._prepare(nxt)))

    async def _take_prepared(self, track: Track) -> Prepared | None:
        entry = self._prefetch.pop(id(track), None)
        if entry is not None and entry[0] is track:
            try:
                prepared = await entry[1]
                if prepared is not None:
                    return prepared
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
            except Exception as e:  # noqa: BLE001
                logger.warning("[prefetch] %r: %s", track.title, e)
        elif entry is not None:
            self._prefetch[id(track)] = entry
            self._drop_prefetch(id(track))
        return await self._prepare(track)

    # ── Источник звука ────────────────────────────────────────────────────
    @staticmethod
    async def _can_copy(source: str, before_options: str | None = None) -> bool:
        """Можно ли отдать Opus без перекодирования (как в v1, звук без потерь).

        Да, если это Opus 48 кГц и все первые пакеты ровно по 20 мс — именно так
        отдаёт YouTube (формат 251). Иначе (AAC/MP3 с SoundCloud, Opus 40/60 мс)
        перекодируем.
        """
        args = ["ffprobe", "-v", "error"]
        if before_options:
            args += before_options.split()
        args += ["-select_streams", "a:0",
                 "-show_entries", "stream=codec_name,sample_rate:packet=duration_time",
                 "-read_intervals", f"%+#{PROBE_PACKETS}", "-of", "json", source]
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(proc.communicate(), 15)
            data = json.loads(out or b"{}")
            stream = (data.get("streams") or [{}])[0]
            if stream.get("codec_name") != "opus" or str(stream.get("sample_rate")) != "48000":
                return False
            durs = [p.get("duration_time") for p in data.get("packets", [])]
            return bool(durs) and all(d is not None and abs(float(d) - 0.02) < 1e-4 for d in durs)
        except Exception as e:  # noqa: BLE001
            if proc is not None and proc.returncode is None:
                proc.kill()
            logger.debug("[probe] %s", e)
            return False

    @staticmethod
    async def _opus_source(source, *, pipe: bool = False, start: float = 0,
                           **kwargs) -> discord.FFmpegOpusAudio:
        """Источник → Opus 48 кГц с пакетами по 20 мс.

        Opus 20 мс (YouTube) копируется как есть — без потери качества.
        Остальное перекодируется в stereo Opus ENCODE_BITRATE без FEC.
        codec=None принудительно включает libopus. start — с какой секунды (перемотка).
        """
        copy = not pipe and await MusicPlayer._can_copy(source, kwargs.get("before_options"))
        # Перемотка: -ss до -i — ffmpeg прыгает по индексу, а не читает всё подряд.
        # При копировании он встаёт на начало кластера (до пары секунд раньше);
        # «-ss 0» на выходе выбрасывает эти пакеты — позиция точная до 20 мс.
        seek = ""
        if start > 0:
            kwargs["before_options"] = f"{kwargs.get('before_options') or ''} -ss {start:.2f}".strip()
            seek = " -ss 0"
        if copy:
            logger.info("[audio] Opus 20 мс — копирую без перекодирования")
            return DiscordOpusAudio(source, codec="copy", options=COPY_OPTS + seek, **kwargs)
        logger.info("[audio] перекодирую в Opus %s кбит/с", ENCODE_BITRATE)
        return DiscordOpusAudio(
            source, codec=None, bitrate=ENCODE_BITRATE,
            options=FFMPEG_OPTS + seek, pipe=pipe, **kwargs)

    @staticmethod
    async def _make_source(prepared: Prepared, start: float = 0):
        """→ (AudioSource, процесс yt-dlp для pipe или None). start — только direct/file."""
        if prepared.kind == "direct":
            src = await MusicPlayer._opus_source(
                prepared.url, before_options=FFMPEG_BEFORE_OPTS, start=start)
            return src, None
        if prepared.kind == "file":
            return await MusicPlayer._opus_source(prepared.path, start=start), None
        proc = ytdl.spawn_pipe(prepared.url)
        try:
            src = await MusicPlayer._opus_source(proc.stdout, pipe=True)
        except BaseException:
            MusicPlayer._kill(proc)
            if proc.stdout is not None:
                proc.stdout.close()
            raise
        return src, proc

    @staticmethod
    def _kill(proc: subprocess.Popen | None) -> None:
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

    async def _wait_voice(self) -> bool:
        """Голос отвалился (туннель моргнул) — даём discord.py переподключиться."""
        for _ in range(RECONNECT_WAIT):
            vc = self.voice_client
            if vc is not None and vc.is_connected():
                return True
            await asyncio.sleep(1)
        vc = self.voice_client
        return vc is not None and vc.is_connected()

    def interrupt_preparing(self) -> None:
        """«Пропустить»/«Стоп», пока трек ещё качается, — не ждать конца скачивания."""
        if self._prep_task is not None and not self._prep_task.done():
            self._prep_task.cancel()

    def _release_playing(self) -> None:
        """Прибрать за играющим треком: оборвать звук, убить yt-dlp, удалить файл."""
        if self._playing is None:
            return
        prepared, proc = self._playing
        self._playing = None
        vc = self.voice_client
        if vc is not None and (vc.is_playing() or vc.is_paused()):
            vc.stop()
        self._kill(proc)
        prepared.discard()

    # ── Позиция и перемотка ───────────────────────────────────────────────
    @property
    def position(self) -> float | None:
        """Сколько секунд трека уже прозвучало (None — ничего не играет)."""
        if self._source is None or self._playing is None:
            return None
        return self._offset + getattr(self._source, "packets", 0) * PACKET_SECONDS

    def seek(self, seconds: float) -> str | None:
        """Перемотать текущий трек. None — получилось, иначе текст ошибки для чата."""
        vc = self.voice_client
        track = self.state.current
        if self._playing is None or track is None or vc is None \
                or not (vc.is_playing() or vc.is_paused()):
            return "Сейчас ничего не играет."
        if self._playing[0].kind == "pipe":
            return "Эфиры и треки длиннее часа перематывать не умею."
        seconds = max(0.0, float(seconds))
        if track.duration and seconds >= track.duration:
            return "Это уже за концом трека — для следующего есть /skip."
        self._seek_to = seconds
        vc.stop()             # → after → плеер переоткроет трек с нужной секунды
        return None

    # ── Автоплей ──────────────────────────────────────────────────────────
    def _seen_keys(self) -> set[str]:
        keys: set[str] = set()
        for t in (*self.state.history, *self.state.upcoming, self.state.current):
            if t is not None:
                keys |= sources.song_keys(t.title, t.uploader, t.webpage_url, t.match)
        return keys

    async def _autoplay_pick(self, seed: Track) -> Track | None:
        """Похожий на seed трек, который ещё не играл и не стоит в очереди."""
        artist, title = seed.match or sources.split_artist_title(seed.title, seed.uploader)
        results = await sources.similar_tracks(artist, title, seed.webpage_url)
        seen = self._seen_keys()
        fresh = [r for r in results if not sources.song_keys(
            r.title, r.uploader, r.url, None if r.url else (r.uploader or "", r.title)) & seen]
        if not fresh:
            logger.info("[autoplay] нет новых похожих на %r", seed.title)
            return None
        track = random.choice(fresh[:AUTOPLAY_PICK_FROM]).to_track(AUTOPLAY_NAME)
        track.autoplay = True
        return track

    def _autoplay_wanted(self) -> bool:
        return self.state.autoplay and not self.state.halted and self.state.is_empty

    async def _autoplay_topup(self, seed: Track) -> None:
        try:
            track = await self._autoplay_pick(seed)
        except Exception as e:  # noqa: BLE001
            logger.warning("[autoplay] %r: %s", seed.title, e)
            track = None
        if track is None:
            self._ap_failed = seed
            return
        if self._autoplay_wanted():
            logger.info("[autoplay] дальше: %s — %s", track.uploader, track.title)
            self.state.add(track)
            self.schedule_prefetch()

    def schedule_autoplay(self, seed: Track | None = None) -> None:
        """Очередь пуста, автоплей включён — заранее подобрать следующий трек."""
        seed = seed or self.state.current or (self.state.history[-1] if self.state.history else None)
        if seed is None or seed is self._ap_failed or not self._autoplay_wanted():
            return
        if self._ap_task is None or self._ap_task.done():
            self._ap_task = asyncio.ensure_future(self._autoplay_topup(seed))

    async def _autoplay_next(self) -> Track | None:
        """Очередь кончилась: дождаться подбора автоплея и взять трек."""
        self.schedule_autoplay()
        if self._ap_task is not None and not self._ap_task.done():
            try:
                await asyncio.shield(self._ap_task)
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
        return self.state.get_nowait()

    # ── Основной цикл ─────────────────────────────────────────────────────
    async def player_loop(self) -> None:
        """Берёт треки из очереди и играет по одному. Сам выходит при простое."""
        try:
            await self._run()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("[player] плеер упал на сервере %s", self.state.guild_id)
            self.state.current = None
            await self._notify(self._on_track_change)
        finally:
            if self._ap_task is not None and not self._ap_task.done():
                self._ap_task.cancel()
            self.interrupt_preparing()
            self._release_playing()
            self.drop_all_prefetch()

    async def _run(self) -> None:
        global _PREFER_PIPE
        while True:
            self.state.next_event.clear()
            track = self.state.get_nowait()
            if track is None and self.state.autoplay:
                track = await self._autoplay_next()
            if track is None:
                try:
                    await asyncio.wait_for(self.state.next_event.wait(), timeout=self.idle_timeout)
                except TimeoutError:
                    logger.info("[player] простой на сервере %s — отключаюсь", self.state.guild_id)
                    await self._on_disconnect(self.state.guild_id)
                    return
                continue

            generation = self.state.generation
            self.state.current = track
            self._prep_task = asyncio.ensure_future(self._take_prepared(track))
            try:
                prepared = await self._prep_task
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
                prepared = None             # оборвали «Пропустить»/«Стоп»
            finally:
                self._prep_task = None
            if self.state.generation != generation:     # пока готовили — «Стоп»/«Пропустить»
                if prepared:
                    prepared.discard()
                self.state.current = None
                self.schedule_prefetch()
                continue
            if prepared is None:
                logger.error("[player] не удалось подготовить %r — пропускаю", track.title)
                self.state.current = None
                await self._notify(self._on_track_error, track)
                continue

            try:
                source, pipe_proc = await self._make_source(prepared)
            except Exception as e:  # noqa: BLE001
                logger.error("[player] FFmpeg не открыл %r: %s", track.title, e)
                prepared.discard()
                self.state.current = None
                await self._notify(self._on_track_error, track)
                continue

            if not await self._wait_voice():
                logger.info("[player] голос потерян — останавливаю плеер")
                source.cleanup()
                self._kill(pipe_proc)
                prepared.discard()
                self.state.current = None
                await self._on_disconnect(self.state.guild_id)
                return

            def _after(error: Exception | None) -> None:
                if error:
                    logger.error("[player] ошибка воспроизведения: %s", error)
                self._loop.call_soon_threadsafe(self.state.next_event.set)

            # Пропуск/«Стоп», нажатые до этого момента, уже отработали через
            # generation выше; дальше считаем только то, что нажмут во время игры.
            self.state.skipped = False
            self.state.next_event.clear()
            try:
                self.voice_client.play(source, after=_after)
            except Exception as e:  # noqa: BLE001
                logger.error("[player] не смог запустить %r: %s", track.title, e)
                source.cleanup()
                self._kill(pipe_proc)
                prepared.discard()
                self.state.current = None
                await self._notify(self._on_track_error, track)
                continue
            self._playing = (prepared, pipe_proc)
            self._source, self._offset, self._seek_to = source, 0.0, None
            started_at = self._loop.time()
            logger.info("[player] играет %r (%s) на сервере %s",
                        track.title, prepared.kind, self.state.guild_id)
            self.state.record_started(track)
            await self._notify(self._on_track_change)
            self.schedule_prefetch()
            self.schedule_autoplay(track)

            seeked = await self._wait_end(prepared, _after)
            self._playing = None
            self._source = None
            self._kill(pipe_proc)
            played = self._loop.time() - started_at

            # Прямой поток открылся, но умер почти сразу (IP режут) — не считаем
            # трек отыгранным, повторяем его же через скачивание.
            if (prepared.kind == "direct" and not self.state.skipped and not seeked
                    and played < 12 and (track.duration or 999) > 30):
                logger.warning("[player] трек оборвался за %.1fс — повтор через yt-dlp", played)
                _PREFER_PIPE = True
                self.state.add_front(track)
                self.state.current = None
                continue

            # Повтор: /skip, ⏮️ и «Стоп» ставят skipped — тогда трек не возвращаем
            # (⏮️ сам кладёт трек обратно в начало очереди).
            if not self.state.skipped:
                if self.state.repeat is RepeatMode.ONE:
                    self.state.add_front(track)
                elif self.state.repeat is RepeatMode.ALL:
                    self.state.add(track)
            self.state.skipped = False

            upcoming = self.state.upcoming
            if prepared.kind == "file" and upcoming and upcoming[0] is track:
                # Тот же трек сыграет снова (повтор, ⏮️) — не качаем его заново.
                done = self._loop.create_future()
                done.set_result(prepared)
                self._prefetch[id(track)] = (track, done)
            else:
                prepared.discard()

            self.state.current = None
            await self._notify(self._on_track_change)

    async def _wait_end(self, prepared: Prepared, after) -> bool:
        """Ждать конца трека; перемотку отработать тут же (тот же трек с -ss).

        → True, если трек перематывали (тогда «оборвался через 12с» — не обрыв).
        """
        seeked = False
        while True:
            await self.state.next_event.wait()
            target, self._seek_to = self._seek_to, None
            if target is None or self.state.skipped:
                return seeked
            self.state.next_event.clear()
            try:
                source, _ = await self._make_source(prepared, target)
                if self.state.skipped:          # пока открывали — «Пропустить»/«Стоп»
                    source.cleanup()
                    return seeked
                self.voice_client.play(source, after=after)
            except Exception as e:  # noqa: BLE001
                logger.error("[player] перемотка на %.0fс не удалась: %s", target, e)
                return seeked
            self._source, self._offset = source, target
            seeked = True
            logger.info("[player] перемотка на %.0fс", target)
