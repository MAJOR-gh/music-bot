"""MusicCog: слэш-команды управления музыкой."""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import shutil
import time
from urllib.parse import urlparse

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

import config
from . import player as player_module
from . import pot, sources, updater, ytdl
from .player import MusicPlayer
from .queue import GuildMusicState, RepeatMode
from .track import SearchResult, Track, format_duration, parse_time, short_title
from .ui import PlayerView

logger = logging.getLogger("music_bot.cog")

# Discord показывает максимум 25 вариантов автокомплита.
AUTOCOMPLETE_LIMIT = 25


def suggestion_label(r: SearchResult) -> str:
    """«Название - Исполнитель (3:45)», не длиннее 100 символов (лимит Discord)."""
    tail = f" ({r.duration_str})" if r.duration else ""
    head = r.title or "Unknown"
    if r.uploader:
        head = f"{head} - {r.uploader}"
    if len(head) + len(tail) > 100:
        head = head[:100 - len(tail) - 1].rstrip() + "…"
    return head + tail


def suggestion_value(r: SearchResult) -> str:
    """Что вернёт Discord при выборе подсказки (≤100 символов): ссылка или «spotify:…»."""
    if r.url and len(r.url) <= 100:
        return r.url
    prefix = sources.normalize_source(r.source) if r.source else "track"
    return (f"{prefix}:" + f"{r.uploader or ''} - {r.title}".strip(" -"))[:100]


SOURCE_CHOICES = [app_commands.Choice(name=label, value=key) for key, label in sources.SOURCES.items()]


def _is_url(query: str) -> bool:
    """Похоже ли на прямую ссылку (а не текстовый запрос для поиска)."""
    q = query.strip().lower()
    return q.startswith("http://") or q.startswith("https://")


def _is_public_url(url: str) -> bool:
    """Ссылка ведёт в интернет, а не внутрь хостинга (localhost, 10.x, 169.254.x…).

    yt-dlp открывает любой адрес, и название страницы уходит в чат — без этой
    проверки через /play можно было заглянуть во внутреннюю сеть контейнера.
    """
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    if not host or host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return True     # обычное доменное имя


def _source_name(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if "soundcloud" in host:
        return "SoundCloud"
    if "youtu" in host:
        return "YouTube"
    return host.removeprefix("www.") or "ссылка"


def _field(lines: list[str], total: int, limit: int = 1024) -> str:
    """Строки → значение поля эмбеда, не длиннее лимита Discord (иначе 400 и команда падает)."""
    out: list[str] = []
    size = 0
    for line in lines:
        if size + len(line) + 1 > limit - 30:
            break
        out.append(line)
        size += len(line) + 1
    if total > len(out):
        out.append(f"… и ещё {total - len(out)}")
    return "\n".join(out)


class MusicCog(commands.Cog):
    """Все музыкальные команды + хранилище состояния по серверам."""

    def __init__(self, bot: commands.Bot, idle_timeout: int):
        self.bot = bot
        self.idle_timeout = idle_timeout
        self._states: dict[int, GuildMusicState] = {}
        self._players: dict[int, MusicPlayer] = {}
        # Автокомплит /play: запрос → результаты и ссылка → выбранный результат.
        self._ac_cache: dict[str, list[SearchResult]] = {}
        self._ac_results: dict[str, SearchResult] = {}

    # ── Вспомогательное ───────────────────────────────────────────────────
    def get_state(self, guild_id: int) -> GuildMusicState:
        state = self._states.get(guild_id)
        if state is None:
            state = GuildMusicState(guild_id)
            self._states[guild_id] = state
        return state

    async def _ensure_voice(
        self, interaction: discord.Interaction
    ) -> discord.VoiceClient | None:
        """Проверяет, что пользователь в голосовом, и подключает/перемещает бота.

        Вызывается ПОСЛЕ interaction.response.defer() — поэтому сообщения об
        ошибке шлём через followup. Возвращает VoiceClient или None.
        """
        user = interaction.user
        if not isinstance(user, discord.Member) or user.voice is None or user.voice.channel is None:
            await interaction.followup.send(
                "❌ Сначала зайди в голосовой канал.", ephemeral=True
            )
            return None

        channel = user.voice.channel
        vc = interaction.guild.voice_client

        try:
            if vc is None:
                # reconnect=True: если туннель/сеть моргнула, discord.py сам
                # переподключит голос, а не выкинет бота из канала молча.
                # self_deaf: боту не нужен чужой звук — меньше трафика через туннель.
                vc = await channel.connect(timeout=20.0, reconnect=True, self_deaf=True)
            elif vc.channel != channel:
                await vc.move_to(channel)
        except Exception as e:  # noqa: BLE001
            logger.error("[voice] не подключился к голосу на сервере %s: %s", interaction.guild.id, e)
            await interaction.followup.send(
                "❌ Не получилось подключиться к голосовому каналу "
                "(таймаут голосового рукопожатия). Подробности — в консоли бота.",
                ephemeral=True,
            )
            return None
        return vc

    def _start_player(self, guild: discord.Guild) -> MusicPlayer:
        """Запустить плеер для guild, если ещё не запущен (защита от дублей)."""
        state = self.get_state(guild.id)
        player = self._players.get(guild.id)
        if player is not None and state.player_task and not state.player_task.done():
            return player  # плеер уже работает
        player = MusicPlayer(
            guild=guild,
            state=state,
            idle_timeout=self.idle_timeout,
            on_disconnect=self._disconnect,
            on_track_change=self._on_track_change,
            on_track_error=self._on_track_error,
        )
        self._players[guild.id] = player
        state.player_task = asyncio.create_task(player.player_loop(), name=f"player-{guild.id}")
        return player

    async def _disconnect(self, guild_id: int) -> None:
        """Полное отключение: остановка плеера, очистка очереди, выход из канала."""
        state = self._states.get(guild_id)
        if state is not None:
            state.stop()
            state.current = None
            task, state.player_task = state.player_task, None
            # Плеер сам зовёт _disconnect при простое — свою же задачу не отменяем,
            # иначе отмена прилетит посреди отключения и бот останется в канале.
            if task and task is not asyncio.current_task() and not task.done():
                task.cancel()
            await self._remove_panel(state)

        guild = self.bot.get_guild(guild_id)
        if guild and guild.voice_client:
            await guild.voice_client.disconnect(force=True)
        self._players.pop(guild_id, None)

    # ── Управление (общее для слэш-команд и кнопок) ───────────────────────
    def skip(self, guild: discord.Guild) -> bool:
        """Пропустить текущий трек — играет он, стоит на паузе или ещё качается."""
        state = self.get_state(guild.id)
        vc = guild.voice_client
        playing = vc is not None and (vc.is_playing() or vc.is_paused())
        if not playing and state.current is None:
            return False
        state.skip_current()
        player = self._players.get(guild.id)
        if player is not None:
            player.interrupt_preparing()
        if playing:
            vc.stop()  # → after-колбэк → плеер берёт следующий трек
        return True

    def stop_all(self, guild: discord.Guild) -> None:
        """Остановить и очистить очередь. Повтор трек не вернёт (skipped)."""
        state = self.get_state(guild.id)
        state.stop()
        player = self._players.get(guild.id)
        if player is not None:
            player.interrupt_preparing()
            player.schedule_prefetch()   # очередь пуста → бросить предзагрузку
        vc = guild.voice_client
        if vc is not None and (vc.is_playing() or vc.is_paused()):
            vc.stop()

    def restart_current(self, guild: discord.Guild) -> bool:
        """Перезапустить текущий трек с начала (кнопка ⏮️).

        Кладём текущий трек обратно в НАЧАЛО очереди и обрываем воспроизведение.
        skipped — чтобы повтор не положил его в очередь второй раз.
        """
        state = self.get_state(guild.id)
        vc = guild.voice_client
        if state.current is None or vc is None or not (vc.is_playing() or vc.is_paused()):
            return False
        state.add_front(state.current)
        state.skipped = True
        vc.stop()
        return True

    def go_back(self, guild: discord.Guild) -> bool:
        """Предыдущий трек (кнопка ⏪, /back): он и текущий встают в начало очереди."""
        state = self.get_state(guild.id)
        history = state.history
        # Последний в истории — обычно сам текущий трек (если он уже заиграл).
        own = 1 if state.current is not None and history and history[-1] is state.current else 0
        if len(history) <= own:
            return False
        if own:
            history.pop()
        prev = history.pop()
        current = state.current
        if current is not None:
            state.add_front(current)
        state.add_front(prev)
        state.halted = False
        if current is not None and self.skip(guild):
            # skip() пометил трек «пропущенным»: повтор не вернёт его второй раз.
            return True
        self._wake(guild)
        return True

    def _wake(self, guild: discord.Guild) -> None:
        """Плеер простаивает (или не запущен) — пусть возьмёт трек из очереди."""
        state = self.get_state(guild.id)
        if guild.voice_client is None:
            return
        self._start_player(guild)
        if state.current is None:
            state.next_event.set()

    def position(self, guild_id: int) -> float | None:
        player = self._players.get(guild_id)
        return player.position if player is not None else None

    def seek(self, guild: discord.Guild, seconds: float) -> str | None:
        player = self._players.get(guild.id)
        if player is None:
            return "Сейчас ничего не играет."
        return player.seek(seconds)

    def set_autoplay(self, guild: discord.Guild, enabled: bool) -> None:
        """Включить/выключить автоплей. Выключили — убрать подобранные им треки."""
        state = self.get_state(guild.id)
        state.autoplay = enabled
        player = self._players.get(guild.id)
        if enabled:
            state.halted = False
            if player is not None:
                player.resume_autoplay()
            if state.current is None and state.is_empty and state.history:
                self._wake(guild)         # очередь уже кончилась — сразу подобрать трек
        else:
            state.drop_autoplay()
        if player is not None:
            player.schedule_prefetch()

    def queue_changed(self, guild: discord.Guild) -> None:
        """Очередь переставили/почистили — обновить предзагрузку (и подбор автоплея)."""
        player = self._players.get(guild.id)
        if player is not None:
            player.schedule_prefetch()
            player.schedule_autoplay()

    # ── Живая панель-плеер ────────────────────────────────────────────────
    @staticmethod
    def _footer(state: GuildMusicState) -> str | None:
        parts = []
        if state.repeat is not RepeatMode.OFF:
            parts.append(f"{state.repeat.emoji} Повтор: {state.repeat.label}")
        if state.autoplay:
            parts.append("📻 Автоплей")
        return " · ".join(parts) or None

    def _now_embed(self, state: GuildMusicState, show_position: bool = False) -> discord.Embed:
        """Эмбед «сейчас играет» по текущему состоянию сервера."""
        track = state.current
        guild = self.bot.get_guild(state.guild_id)
        vc = guild.voice_client if guild else None
        paused = bool(vc and vc.is_paused())

        title = "⏸️ На паузе" if paused else "▶️ Сейчас играет"
        embed = discord.Embed(
            title=title,
            description=f"**{track.link_title}**",
            color=0xED4245 if paused else 0x57F287,
        )
        length = track.duration_str
        pos = self.position(state.guild_id) if show_position else None
        if pos is not None and not track.live:
            length = f"{format_duration(int(pos)) if pos >= 1 else '0:00'} / {length}"
        embed.add_field(name="Длительность", value=length, inline=True)
        if track.uploader:
            embed.add_field(name="Автор", value=short_title(track.uploader, 100), inline=True)
        embed.add_field(name="Заказал", value=track.requested_by, inline=True)
        if track.source not in ("YouTube", "SoundCloud"):
            embed.add_field(name="Источник", value=track.source, inline=True)
        if track.thumbnail:
            embed.set_thumbnail(url=track.thumbnail)

        upcoming = state.upcoming[:5]
        if upcoming:
            lines = [f"`{i}.` {short_title(t.display_title)}{t.duration_tag}"
                     for i, t in enumerate(upcoming, start=1)]
            embed.add_field(name="📋 Далее в очереди", value=_field(lines, len(state)), inline=False)
        footer = self._footer(state)
        if footer:
            embed.set_footer(text=footer)
        return embed

    async def _remove_panel(self, state: GuildMusicState) -> None:
        """Удалить старое сообщение-панель, если есть."""
        if state.panel_message is not None:
            message, state.panel_message = state.panel_message, None
            try:
                await message.delete()
            except discord.HTTPException:
                pass

    async def _on_track_change(self, guild_id: int) -> None:
        """Колбэк плеера: что играет — изменилось. Перевыкладываем панель снизу."""
        state = self.get_state(guild_id)
        if state.current is None:
            await self._remove_panel(state)
            return
        if state.text_channel is None:
            return
        await self._remove_panel(state)  # «свежая панель» при новом треке
        embed = self._now_embed(state)
        view = PlayerView(self, guild_id)
        try:
            state.panel_message = await state.text_channel.send(embed=embed, view=view)
        except discord.HTTPException as e:
            logger.warning("[panel] не удалось отправить панель: %s", e)

    async def _on_track_error(self, guild_id: int, track: Track) -> None:
        """Колбэк плеера: трек не удалось сыграть — сказать в чат, а не молчать."""
        state = self.get_state(guild_id)
        if state.text_channel is None:
            return
        try:
            await state.text_channel.send(
                f"⚠️ Не получилось сыграть **{short_title(track.title, 100)}** — пропускаю."
            )
        except discord.HTTPException:
            pass

    async def _rerender_panel(self, state: GuildMusicState) -> None:
        """Перерисовать существующую панель (после /pause, /resume из слэш-команд)."""
        if state.panel_message is None or state.current is None:
            return
        try:
            await state.panel_message.edit(
                embed=self._now_embed(state), view=PlayerView(self, state.guild_id)
            )
        except discord.HTTPException:
            pass

    async def refresh_panel_inplace(self, interaction: discord.Interaction) -> None:
        """Обновить панель на месте (вызов из кнопки): редактируем то же сообщение."""
        state = self.get_state(interaction.guild.id)
        if state.current is None:
            await interaction.response.edit_message(
                content="⏹️ Ничего не играет.", embed=None, view=None
            )
            if state.panel_message is not None and state.panel_message.id == interaction.message.id:
                state.panel_message = None
            return
        embed = self._now_embed(state)
        view = PlayerView(self, interaction.guild.id)
        await interaction.response.edit_message(embed=embed, view=view)
        state.panel_message = interaction.message

    # ── Очередь ───────────────────────────────────────────────────────────
    async def enqueue(self, interaction: discord.Interaction, tracks: list[Track]) -> bool | None:
        """Поставить треки в очередь и запустить плеер.

        Используется и из /play, и из выпадающего списка поиска.
        True — очередь была пуста и трек запускается сразу, False — встал в
        очередь, None — не удалось подключиться к голосу.
        """
        if interaction.guild.voice_client is None:
            if await self._ensure_voice(interaction) is None:
                return None
        return self.enqueue_tracks(interaction.guild, tracks, interaction.channel)

    def enqueue_tracks(self, guild: discord.Guild, tracks: list[Track], text_channel=None) -> bool:
        """Сама постановка в очередь (без Discord-взаимодействия). True — запуск сразу."""
        state = self.get_state(guild.id)
        if text_channel is not None:
            state.text_channel = text_channel  # куда вешать живую панель
        # «Простаивает» = ничего не играет И очередь пуста (до добавления).
        was_idle = state.current is None and state.is_empty
        for track in tracks:
            state.add(track)
        player = self._start_player(guild)

        # Будим плеер ТОЛЬКО если он простаивает (ждёт новый трек). Если трек уже
        # играет, лишний set() оборвал бы ожидание его конца.
        if was_idle:
            state.next_event.set()
        else:
            player.schedule_prefetch()   # следующий трек начнёт качаться заранее
        return was_idle

    @staticmethod
    def enqueue_message(was_idle: bool, track: Track) -> str:
        head = "▶️ Запускаю" if was_idle else "➕ В очередь"
        return f"{head}: **{short_title(track.display_title, 100)}**{track.duration_tag}"

    async def _resolve(self, interaction: discord.Interaction, track: Track) -> bool:
        """Трек из Spotify/Last.fm → сразу найти его на YouTube Music.

        Так в чате видна настоящая длительность, а если трека нет — пишем об этом
        сразу, а не молча пропускаем его в очереди.
        """
        if track.webpage_url or not track.match:
            return True
        try:
            ok = await asyncio.wait_for(MusicPlayer._fill_from_match(track), 10)
        except Exception as e:  # noqa: BLE001 — не успели: найдёт плеер перед игрой
            logger.info("[play] YT Music не ответил вовремя для %r: %s", track.title, e)
            return True
        if not ok:
            await interaction.followup.send(
                f"❌ Не нашёл на YouTube Music: **{short_title(track.display_title, 100)}**")
        return ok

    async def _track_from_url(self, url: str, requester: str) -> Track | None:
        """Ссылка YouTube/SoundCloud/… → Track с названием и длительностью.

        Ролик YouTube сначала ищем в YouTube Music (~0,3 с): yt-dlp ради одного
        названия решает JS-challenge 5+ секунд, а звук плеер всё равно достанет сам.
        """
        vid = ytdl.video_id(url)
        if vid:
            found = await sources.youtube_info(vid)
            if found is not None:
                return found.to_track(requester)
        try:
            info = ytdl.first_entry(await ytdl.extract(url, flat=True, timeout=45))
        except ytdl.YtdlError as e:
            logger.warning("[play] yt-dlp не разобрал %s: %s", url, e)
            info = None
        if info:
            page = info.get("webpage_url") or info.get("url") or url
            return Track(
                title=info.get("title") or url,
                webpage_url=page,
                duration=sources.to_int(info.get("duration")),
                uploader=info.get("uploader") or info.get("channel"),
                thumbnail=ytdl.best_thumbnail(info),
                requested_by=requester,
                source=_source_name(page),
                live=bool(info.get("is_live")) or info.get("live_status") == "is_live",
            )
        # YouTube не пустил yt-dlp («Sign in to confirm…») — название берём через
        # oEmbed, а при игре плеер попробует скачать или найти трек на SoundCloud.
        if _source_name(url) == "YouTube":
            title = await sources.youtube_title(url)
            if title:
                return Track(title=title, webpage_url=url, duration=None, uploader=None,
                             thumbnail=None, requested_by=requester)
        return None

    # ── /join ─────────────────────────────────────────────────────────────
    @app_commands.command(name="join", description="Позвать бота в твой голосовой канал")
    async def join(self, interaction: discord.Interaction):
        await interaction.response.defer()
        vc = await self._ensure_voice(interaction)
        if vc is None:
            return
        await interaction.followup.send(
            f"✅ Подключился к **{vc.channel.name}**."
        )

    # ── /leave ────────────────────────────────────────────────────────────
    @app_commands.command(name="leave", description="Выгнать бота из канала и очистить очередь")
    async def leave(self, interaction: discord.Interaction):
        if interaction.guild.voice_client is None:
            await interaction.response.send_message(
                "❌ Я не в голосовом канале.", ephemeral=True
            )
            return
        await interaction.response.send_message("👋 Отключаюсь и очищаю очередь.")
        await self._disconnect(interaction.guild.id)

    # ── /play ─────────────────────────────────────────────────────────────
    # Подсказки при вводе: Discord ждёт ответ на автокомплит ≤3 с и шлёт запрос
    # на каждую букву, поэтому ищем в YouTube Music (быстро, без куки) и кэшируем.
    _AC_TIMEOUT = 2.5
    _AC_CACHE_MAX = 200

    @staticmethod
    def _ac_key(query: str, source: str) -> str:
        return source + "\0" + " ".join(query.lower().split())

    async def _suggest(self, query: str, source: str = "ytmusic") -> list[SearchResult]:
        key = self._ac_key(query, source)
        cache = self._ac_cache
        if key in cache:
            cache[key] = cache.pop(key)  # LRU: свежий — в конец
            return cache[key]
        if source == "lastfm":
            search = sources.search_lastfm
        elif source == "spotify":
            search = sources.search_spotify
        else:
            search = sources.search_ytmusic
        # У Spotify/Last.fm нет ссылки на звук: в value уйдёт «spotify:…»/«lastfm:…»,
        # а сам трек плеер найдёт на YouTube Music перед игрой.
        results = await asyncio.wait_for(search(query, AUTOCOMPLETE_LIMIT), self._AC_TIMEOUT)
        cache[key] = results
        for r in results:
            self._ac_results[suggestion_value(r)] = r
        while len(cache) > self._AC_CACHE_MAX:
            cache.pop(next(iter(cache)))
        while len(self._ac_results) > self._AC_CACHE_MAX * 5:
            self._ac_results.pop(next(iter(self._ac_results)))
        return results

    @staticmethod
    def _suggest_source(interaction: discord.Interaction) -> str:
        """Откуда подсказки: source (или DEFAULT_SEARCH) без учёта регистра.

        Spotify/Last.fm — у них (если заданы ключи), иначе — YouTube Music:
        поиск самого YouTube через yt-dlp не успевает в 3 секунды автокомплита.
        """
        ns = getattr(interaction, "namespace", None)
        chosen = sources.normalize_source(getattr(ns, "source", None) or config.DEFAULT_SEARCH)
        if chosen == "lastfm" and config.LASTFM_API_KEY:
            return "lastfm"
        if chosen == "spotify" and config.SPOTIFY_CLIENT_ID and config.SPOTIFY_CLIENT_SECRET:
            return "spotify"
        return "ytmusic"

    async def play_autocomplete(
        self, interaction: discord.Interaction, current: str,
    ) -> list[app_commands.Choice[str]]:
        current = (current or "").strip()
        if len(current) < 2 or _is_url(current):
            return []
        try:
            results = await self._suggest(current, self._suggest_source(interaction))
        except Exception as e:  # noqa: BLE001 — таймаут/ошибка: просто без подсказок
            logger.debug("[autocomplete] %r: %s", current, e)
            return []
        return [
            app_commands.Choice(name=suggestion_label(r), value=suggestion_value(r))
            for r in results
        ][:AUTOCOMPLETE_LIMIT]

    @app_commands.command(name="play", description="Включить трек: ссылка или поиск с подсказками")
    @app_commands.describe(
        query="Ссылка (YouTube, YouTube Music, Spotify) или название — выбери из подсказок",
        source="Где искать текст: YouTube, YouTube Music, Spotify или Last.fm",
    )
    @app_commands.choices(source=SOURCE_CHOICES)
    @app_commands.autocomplete(query=play_autocomplete)
    async def play(
        self, interaction: discord.Interaction, query: str,
        source: app_commands.Choice[str] | None = None,
    ):
        await interaction.response.defer()

        vc = await self._ensure_voice(interaction)
        if vc is None:
            return
        requester = interaction.user.display_name
        query = query.strip()

        # Spotify: трек, альбом или плейлист → (исполнитель, название); сам звук
        # плеер найдёт на YouTube Music прямо перед игрой.
        spotify = sources.parse_spotify_link(query)
        if spotify:
            try:
                name, sp_tracks = await sources.spotify_link_tracks(*spotify)
            except Exception as e:  # noqa: BLE001
                logger.warning("[play] Spotify %s: %s", query, e)
                await interaction.followup.send(f"❌ Не смог открыть ссылку Spotify: {e}")
                return
            if not sp_tracks:
                await interaction.followup.send("❌ В этой ссылке Spotify нет треков.")
                return
            tracks = [t.to_result().to_track(requester) for t in sp_tracks]
            was_idle = await self.enqueue(interaction, tracks)
            if was_idle is None:
                return
            if len(tracks) == 1:
                await interaction.followup.send(self.enqueue_message(was_idle, tracks[0]))
            else:
                await interaction.followup.send(
                    f"📋 Spotify «{short_title(name, 100)}»: добавил **{len(tracks)}** треков"
                    + (" — запускаю." if was_idle else " в очередь.")
                )
            return

        # Выбран вариант из подсказок → метаданные уже есть, yt-dlp не нужен.
        # Enter без выбора → первая подсказка, которую человек видел в списке.
        picked = self._ac_results.get(query)
        if picked is None and source is None and not _is_url(query):
            shown = self._ac_cache.get(self._ac_key(query, self._suggest_source(interaction)))
            picked = shown[0] if shown else None
        if picked is not None:
            track = picked.to_track(requester)
            if not await self._resolve(interaction, track):
                return
            was_idle = await self.enqueue(interaction, [track])
            if was_idle is not None:
                await interaction.followup.send(self.enqueue_message(was_idle, track))
            return

        # Ссылка → играем сразу. Текст без подсказок → первый результат поиска.
        if _is_url(query):
            if not _is_public_url(query):
                await interaction.followup.send("❌ Такие ссылки не открываю.")
                return
            track = await self._track_from_url(query, requester)
            if track is None:
                await interaction.followup.send(f"❌ Не удалось загрузить: `{query}`")
                return
            was_idle = await self.enqueue(interaction, [track])
            if was_idle is not None:
                await interaction.followup.send(self.enqueue_message(was_idle, track))
            return

        key = sources.normalize_source(source.value if source else config.DEFAULT_SEARCH)
        label = sources.SOURCES[key]
        try:
            results = await sources.search(key, query, 1)
        except sources.SourceNotConfigured as e:
            await interaction.followup.send(f"⚙️ {e}")
            return
        except Exception as e:  # noqa: BLE001
            logger.warning("[search] %s %r: %s", label, query, e)
            results = None
            if key == "youtube":
                # YouTube отшил поиск (бот-проверка и т.п.) — тот же запрос в YT Music.
                try:
                    results = await sources.search("ytmusic", query, 1)
                    label = f"{sources.SOURCES['ytmusic']} (YouTube не ответил)"
                except Exception as e2:  # noqa: BLE001
                    logger.warning("[search] запасной YT Music %r: %s", query, e2)
            if results is None:
                await interaction.followup.send(f"❌ Поиск ({label}) не удался: {str(e)[:200]}")
                return
        if not results:
            await interaction.followup.send(f"❌ Ничего не нашёл ({label}) по запросу: `{query}`")
            return

        track = results[0].to_track(requester)
        if not await self._resolve(interaction, track):
            return
        was_idle = await self.enqueue(interaction, [track])
        if was_idle is not None:
            await interaction.followup.send(self.enqueue_message(was_idle, track))

    # ── /skip ─────────────────────────────────────────────────────────────
    @app_commands.command(name="skip", description="Следующий трек")
    async def skip_cmd(self, interaction: discord.Interaction):
        if not self.skip(interaction.guild):
            await interaction.response.send_message(
                "❌ Сейчас ничего не играет.", ephemeral=True
            )
            return
        await interaction.response.send_message("⏭️ Пропущено.")

    # ── /pause ────────────────────────────────────────────────────────────
    @app_commands.command(name="pause", description="Пауза")
    async def pause(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc is None or not vc.is_playing():
            await interaction.response.send_message(
                "❌ Сейчас ничего не играет.", ephemeral=True
            )
            return
        vc.pause()
        await interaction.response.send_message("⏸️ Пауза.", ephemeral=True)
        await self._rerender_panel(self.get_state(interaction.guild.id))

    # ── /resume ───────────────────────────────────────────────────────────
    @app_commands.command(name="resume", description="Снять с паузы")
    async def resume(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc is None or not vc.is_paused():
            await interaction.response.send_message(
                "❌ Нечего возобновлять.", ephemeral=True
            )
            return
        vc.resume()
        await interaction.response.send_message("▶️ Продолжаю.", ephemeral=True)
        await self._rerender_panel(self.get_state(interaction.guild.id))

    # ── /stop ─────────────────────────────────────────────────────────────
    @app_commands.command(name="stop", description="Остановить музыку и очистить очередь (бот остаётся в канале)")
    async def stop(self, interaction: discord.Interaction):
        if interaction.guild.voice_client is None:
            await interaction.response.send_message(
                "❌ Я не в голосовом канале.", ephemeral=True
            )
            return
        self.stop_all(interaction.guild)
        await interaction.response.send_message("⏹️ Остановлено, очередь очищена.")
        await self._remove_panel(self.get_state(interaction.guild.id))

    # ── /queue ────────────────────────────────────────────────────────────
    @app_commands.command(name="queue", description="Что дальше в очереди")
    async def queue(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild.id)
        if state.current is None and state.is_empty:
            await interaction.response.send_message(
                "📭 Очередь пуста.", ephemeral=True
            )
            return

        embed = discord.Embed(title="🎵 Очередь", color=0x5865F2)
        if state.current:
            embed.add_field(
                name="Сейчас играет",
                value=f"**{short_title(state.current.display_title, 200)}**{state.current.duration_tag}",
                inline=False,
            )
        upcoming = state.upcoming[:10]
        if upcoming:
            lines = [
                f"`{i}.` {short_title(t.display_title)}{t.duration_tag} — {short_title(t.requested_by, 32)}"
                for i, t in enumerate(upcoming, start=1)
            ]
            embed.add_field(name="Далее", value=_field(lines, len(state)), inline=False)
        footer = self._footer(state)
        if footer:
            embed.set_footer(text=footer)
        await interaction.response.send_message(embed=embed)

    # ── /nowplaying ───────────────────────────────────────────────────────
    @app_commands.command(name="nowplaying", description="Что сейчас играет")
    async def nowplaying(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild.id)
        if state.current is None:
            await interaction.response.send_message(
                "❌ Сейчас ничего не играет.", ephemeral=True
            )
            return
        await interaction.response.send_message(embed=self._now_embed(state, show_position=True))

    # ── /repeat ───────────────────────────────────────────────────────────
    @app_commands.command(name="repeat", description="Повтор: выкл / один трек / вся очередь")
    @app_commands.describe(mode="Режим повтора (без аргумента — переключить по кругу)")
    @app_commands.choices(mode=[
        app_commands.Choice(name="Выключить", value=int(RepeatMode.OFF)),
        app_commands.Choice(name="Один трек", value=int(RepeatMode.ONE)),
        app_commands.Choice(name="Вся очередь", value=int(RepeatMode.ALL)),
    ])
    async def repeat(
        self, interaction: discord.Interaction,
        mode: app_commands.Choice[int] | None = None,
    ):
        state = self.get_state(interaction.guild.id)
        if mode is None:
            new = state.cycle_repeat()           # без аргумента — по кругу
        else:
            state.repeat = RepeatMode(mode.value)
            new = state.repeat
        await interaction.response.send_message(f"{new.emoji} Повтор: **{new.label}**.")
        await self._rerender_panel(state)        # обновить футер и кнопку 🔁 на панели

    # ── /back ─────────────────────────────────────────────────────────────
    @app_commands.command(name="back", description="Предыдущий трек")
    async def back(self, interaction: discord.Interaction):
        if interaction.guild.voice_client is None:
            await interaction.response.send_message("❌ Я не в голосовом канале.", ephemeral=True)
            return
        if not self.go_back(interaction.guild):
            await interaction.response.send_message("❌ Раньше ничего не играло.", ephemeral=True)
            return
        await interaction.response.send_message("⏪ Возвращаю предыдущий трек.")

    # ── Перемотка: /seek, /forward, /rewind ───────────────────────────────
    async def _seek_reply(self, interaction: discord.Interaction, target: float) -> None:
        state = self.get_state(interaction.guild.id)
        error = self.seek(interaction.guild, target)
        if error:
            await interaction.response.send_message(f"❌ {error}", ephemeral=True)
            return
        total = state.current.duration_str if state.current else "?:??"
        shown = format_duration(int(target)) if target >= 1 else "0:00"
        await interaction.response.send_message(f"⏩ Перемотал на **{shown}** / {total}.")

    @app_commands.command(name="seek", description="Перемотать на время: 90, 2:30 или 1:02:03")
    @app_commands.describe(position="Куда перемотать: секунды (90), минуты:секунды (2:30) или ч:мм:сс")
    async def seek_cmd(self, interaction: discord.Interaction, position: str):
        target = parse_time(position)
        if target is None:
            await interaction.response.send_message(
                "❌ Не понял время. Примеры: `90`, `2:30`, `1:02:03`.", ephemeral=True)
            return
        await self._seek_reply(interaction, target)

    async def _seek_relative(self, interaction: discord.Interaction, amount: str | None,
                             sign: int) -> None:
        delta = parse_time(amount) if amount else 10
        if delta is None:
            await interaction.response.send_message(
                "❌ Не понял время. Примеры: `10`, `30`, `1:00`.", ephemeral=True)
            return
        pos = self.position(interaction.guild.id)
        if pos is None:
            await interaction.response.send_message("❌ Сейчас ничего не играет.", ephemeral=True)
            return
        await self._seek_reply(interaction, max(0.0, pos + sign * delta))

    @app_commands.command(name="forward", description="Вперёд на N секунд (по умолчанию 10)")
    @app_commands.describe(amount="На сколько: секунды (30) или минуты:секунды (1:00)")
    async def forward(self, interaction: discord.Interaction, amount: str | None = None):
        await self._seek_relative(interaction, amount, +1)

    @app_commands.command(name="rewind", description="Назад на N секунд (по умолчанию 10)")
    @app_commands.describe(amount="На сколько: секунды (30) или минуты:секунды (1:00)")
    async def rewind(self, interaction: discord.Interaction, amount: str | None = None):
        await self._seek_relative(interaction, amount, -1)

    # ── Управление очередью ───────────────────────────────────────────────
    async def _queue_reply(self, interaction: discord.Interaction, text: str) -> None:
        self.queue_changed(interaction.guild)
        await interaction.response.send_message(text)
        await self._rerender_panel(self.get_state(interaction.guild.id))

    async def _no_track(self, interaction: discord.Interaction, number: int) -> None:
        size = len(self.get_state(interaction.guild.id))
        text = "📭 Очередь пуста." if not size else f"❌ Нет трека №{number} — в очереди {size}."
        await interaction.response.send_message(text, ephemeral=True)

    @app_commands.command(name="shuffle", description="Перемешать очередь")
    async def shuffle(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild.id)
        if len(state) < 2:
            await interaction.response.send_message(
                "❌ Перемешивать нечего — в очереди меньше двух треков.", ephemeral=True)
            return
        count = state.shuffle()
        await self._queue_reply(interaction, f"🔀 Перемешал очередь ({count} треков).")

    @app_commands.command(name="remove", description="Убрать трек из очереди по номеру")
    @app_commands.describe(number="Номер трека в /queue")
    async def remove(self, interaction: discord.Interaction, number: app_commands.Range[int, 1]):
        track = self.get_state(interaction.guild.id).remove(number)
        if track is None:
            await self._no_track(interaction, number)
            return
        await self._queue_reply(
            interaction, f"🗑️ Убрал из очереди: **{short_title(track.display_title, 100)}**")

    @app_commands.command(name="move", description="Переставить трек в очереди")
    @app_commands.rename(src="from", dst="to")
    @app_commands.describe(src="Какой номер переставить", dst="На какое место")
    async def move(self, interaction: discord.Interaction,
                   src: app_commands.Range[int, 1], dst: app_commands.Range[int, 1]):
        state = self.get_state(interaction.guild.id)
        track = state.move(src, dst)
        if track is None:
            await self._no_track(interaction, src)
            return
        place = state.upcoming.index(track) + 1
        await self._queue_reply(
            interaction, f"↕️ **{short_title(track.display_title, 100)}** теперь №{place}.")

    @app_commands.command(name="skipto", description="Перейти к треку из очереди по номеру")
    @app_commands.describe(number="Номер трека в /queue")
    async def skipto(self, interaction: discord.Interaction, number: app_commands.Range[int, 1]):
        state = self.get_state(interaction.guild.id)
        track = state.skip_to(number)
        if track is None:
            await self._no_track(interaction, number)
            return
        if not self.skip(interaction.guild):
            self._wake(interaction.guild)
        self.queue_changed(interaction.guild)
        await interaction.response.send_message(
            f"⏭️ Перехожу к **{short_title(track.display_title, 100)}**.")

    @app_commands.command(name="clear", description="Очистить очередь (текущий трек доиграет)")
    async def clear(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild.id)
        if state.is_empty:
            await interaction.response.send_message("📭 Очередь и так пуста.", ephemeral=True)
            return
        count = len(state)
        state.clear()
        await self._queue_reply(interaction, f"🧹 Очередь очищена ({count} треков).")

    # ── /autoplay ─────────────────────────────────────────────────────────
    @staticmethod
    def autoplay_text(enabled: bool) -> str:
        if not enabled:
            return "📻 Автоплей **выключен**."
        how = "Last.fm" if config.LASTFM_API_KEY else "радио YouTube Music"
        return (f"📻 Автоплей **включён**: когда очередь кончится, включу похожие треки "
                f"(подбирает {how}).")

    @app_commands.command(name="autoplay", description="Автоплей: похожие треки, когда очередь кончилась")
    @app_commands.describe(enabled="Вкл/выкл (без аргумента — переключить)")
    async def autoplay(self, interaction: discord.Interaction, enabled: bool | None = None):
        state = self.get_state(interaction.guild.id)
        enabled = (not state.autoplay) if enabled is None else enabled
        self.set_autoplay(interaction.guild, enabled)
        await interaction.response.send_message(self.autoplay_text(enabled))
        await self._rerender_panel(state)

    # ── /ping ─────────────────────────────────────────────────────────────
    @staticmethod
    async def _http_ping(url: str) -> float | None:
        """Время HTTP GET-запроса (мс) или None, если не ответил за 5с."""
        try:
            timeout = aiohttp.ClientTimeout(total=5)
            start = time.perf_counter()
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    await resp.read()
                    if resp.status >= 500:
                        return None
            return (time.perf_counter() - start) * 1000
        except Exception:  # noqa: BLE001
            return None

    @app_commands.command(name="ping", description="Диагностика: задержки, yt-dlp, куки, память хостинга")
    async def ping(self, interaction: discord.Interaction):
        await interaction.response.defer()

        def fmt(ms: float | None) -> str:
            return f"`{ms:.0f} мс`" if ms is not None else "❌ недоступен"

        # Пинги наружу — параллельно, чтобы команда отвечала быстро.
        youtube_ms, google_ms = await asyncio.gather(
            self._http_ping("https://www.youtube.com/generate_204"),
            self._http_ping("https://www.google.com/generate_204"),
        )

        ffmpeg_path = shutil.which("ffmpeg")
        vc = interaction.guild.voice_client
        state = self.get_state(interaction.guild.id)

        embed = discord.Embed(title="🏓 Понг! Диагностика", color=0x5865F2)
        embed.add_field(
            name="Discord Gateway",
            value=f"`{self.bot.latency * 1000:.0f} мс`",
            inline=True,
        )
        embed.add_field(name="YouTube", value=fmt(youtube_ms), inline=True)
        embed.add_field(name="Google", value=fmt(google_ms), inline=True)
        embed.add_field(
            name="FFmpeg",
            value="✅ найден" if ffmpeg_path else "❌ не найден в PATH",
            inline=True,
        )
        embed.add_field(
            name="Доставка звука",
            value="📦 yt-dlp (скачивание)" if player_module.prefer_pipe()
                  else "🔗 прямой поток",
            inline=True,
        )
        embed.add_field(name="yt-dlp", value=f"`{updater.installed_version() or '—'}`", inline=True)
        embed.add_field(name="Куки YouTube", value=ytdl.cookies_mode() or "нет", inline=True)
        js = await asyncio.to_thread(ytdl.js_runtime)
        embed.add_field(name="JS для YouTube", value=f"`{js}`" if js else "❌ нет deno/node",
                        inline=True)
        embed.add_field(name="PO Token", value=pot.status, inline=True)
        embed.add_field(name="Память", value=ytdl.memory_usage() or "—", inline=True)
        embed.add_field(name="Способ YouTube",
                        value=ytdl.ATTEMPTS[ytdl._preferred].name, inline=True)
        if ytdl.last_bot_check is not None:
            embed.add_field(
                name="⚠️ Бот-проверка YouTube",
                value=f"была <t:{int(ytdl.last_bot_check)}:R> — нужны свежие куки",
                inline=True,
            )
        embed.add_field(
            name="Голосовой канал",
            value=f"🔊 {vc.channel.name}" if vc and vc.is_connected() else "—",
            inline=True,
        )
        if state.current:
            embed.set_footer(text=f"Сейчас играет: {short_title(state.current.title, 200)}")
        await interaction.followup.send(embed=embed)

    # ── Авто-отключение, когда бот остался в канале один ──────────────────
    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        guild = member.guild
        if self.bot.user is not None and member.id == self.bot.user.id:
            # Бота выкинули из голосового (модератор, «Отключить») — прибрать
            # плеер и панель, иначе они висят до таймаута простоя.
            if before.channel is not None and after.channel is None:
                state = self._states.get(guild.id)
                if state is not None and (state.player_task or state.current or state.panel_message):
                    logger.info("[voice] бота отключили от голоса на сервере %s", guild.id)
                    await self._disconnect(guild.id)
            return
        if member.bot:
            return
        vc = guild.voice_client
        if vc is None or vc.channel is None:
            return
        if not any(not m.bot for m in vc.channel.members):
            logger.info("[voice] в канале не осталось людей на сервере %s — отключаюсь", guild.id)
            await self._disconnect(guild.id)


async def setup_cog(bot: commands.Bot, idle_timeout: int) -> MusicCog:
    cog = MusicCog(bot, idle_timeout)
    await bot.add_cog(cog)
    return cog
