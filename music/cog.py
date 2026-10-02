"""MusicCog: слэш-команды управления музыкой."""
from __future__ import annotations

import asyncio
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
from . import sources, updater, ytdl
from .player import MusicPlayer
from .queue import GuildMusicState, RepeatMode
from .track import Track, short_title
from .ui import PlayerView, SearchView

logger = logging.getLogger("music_bot.cog")

SOURCE_CHOICES = [app_commands.Choice(name=label, value=key) for key, label in sources.SOURCES.items()]


def _is_url(query: str) -> bool:
    """Похоже ли на прямую ссылку (а не текстовый запрос для поиска)."""
    q = query.strip().lower()
    return q.startswith("http://") or q.startswith("https://")


def _to_int(v) -> int | None:
    try:
        return int(float(v)) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


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
            logger.error("[voice] connect failed on guild %s: %s", interaction.guild.id, e)
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

    # ── Живая панель-плеер ────────────────────────────────────────────────
    def _now_embed(self, state: GuildMusicState) -> discord.Embed:
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
        embed.add_field(name="Длительность", value=track.duration_str, inline=True)
        if track.uploader:
            embed.add_field(name="Автор", value=short_title(track.uploader, 100), inline=True)
        embed.add_field(name="Заказал", value=track.requested_by, inline=True)
        if track.source not in ("YouTube", "SoundCloud"):
            embed.add_field(name="Источник", value=track.source, inline=True)
        if track.thumbnail:
            embed.set_thumbnail(url=track.thumbnail)

        upcoming = state.upcoming[:5]
        if upcoming:
            lines = [f"`{i}.` {short_title(t.title)} `[{t.duration_str}]`"
                     for i, t in enumerate(upcoming, start=1)]
            embed.add_field(name="📋 Далее в очереди", value=_field(lines, len(state)), inline=False)
        if state.repeat is not RepeatMode.OFF:
            embed.set_footer(text=f"{state.repeat.emoji} Повтор: {state.repeat.label}")
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
        if was_idle:
            return f"▶️ Запускаю: **{short_title(track.title, 100)}** `[{track.duration_str}]`"
        return f"➕ В очередь: **{short_title(track.title, 100)}** `[{track.duration_str}]`"

    async def _track_from_url(self, url: str, requester: str) -> Track | None:
        """Ссылка YouTube/SoundCloud/… → Track с названием и длительностью."""
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
                duration=_to_int(info.get("duration")),
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
    @app_commands.command(name="join", description="Подключить бота к твоему голосовому каналу")
    async def join(self, interaction: discord.Interaction):
        await interaction.response.defer()
        vc = await self._ensure_voice(interaction)
        if vc is None:
            return
        await interaction.followup.send(
            f"✅ Подключился к **{vc.channel.name}**."
        )

    # ── /leave ────────────────────────────────────────────────────────────
    @app_commands.command(name="leave", description="Отключить бота и очистить очередь")
    async def leave(self, interaction: discord.Interaction):
        if interaction.guild.voice_client is None:
            await interaction.response.send_message(
                "❌ Я не в голосовом канале.", ephemeral=True
            )
            return
        await interaction.response.send_message("👋 Отключаюсь и очищаю очередь.")
        await self._disconnect(interaction.guild.id)

    # ── /play ─────────────────────────────────────────────────────────────
    @app_commands.command(name="play", description="Воспроизвести трек по ссылке или поисковому запросу")
    @app_commands.describe(
        query="Ссылка (YouTube, SoundCloud, Spotify) или текст для поиска",
        source="Где искать текст (по умолчанию — YouTube)",
    )
    @app_commands.choices(source=SOURCE_CHOICES)
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

        # Ссылка → играем сразу. Текст → показываем выбор из нескольких вариантов.
        if _is_url(query):
            track = await self._track_from_url(query, requester)
            if track is None:
                await interaction.followup.send(f"❌ Не удалось загрузить: `{query}`")
                return
            was_idle = await self.enqueue(interaction, [track])
            if was_idle is not None:
                await interaction.followup.send(self.enqueue_message(was_idle, track))
            return

        key = source.value if source else config.DEFAULT_SEARCH
        if key not in sources.SOURCES:
            key = "youtube"
        label = sources.SOURCES[key]
        try:
            results = await sources.search(key, query, config.SEARCH_RESULTS)
        except sources.SourceNotConfigured as e:
            await interaction.followup.send(f"⚙️ {e}")
            return
        except Exception as e:  # noqa: BLE001
            logger.warning("[search] %s %r: %s", label, query, e)
            await interaction.followup.send(f"❌ Поиск ({label}) не удался: {str(e)[:200]}")
            return
        if not results:
            await interaction.followup.send(f"❌ Ничего не нашёл ({label}) по запросу: `{query}`")
            return

        view = SearchView(
            self, results, requester=requester, page_size=config.SEARCH_PAGE_SIZE,
        )
        view.message = await interaction.followup.send(
            f"🔎 {label}: нашёл {len(results)} вариантов по запросу **{query[:100]}** — выбери нужный:",
            view=view, wait=True,
        )

    # ── /skip ─────────────────────────────────────────────────────────────
    @app_commands.command(name="skip", description="Пропустить текущий трек")
    async def skip_cmd(self, interaction: discord.Interaction):
        if not self.skip(interaction.guild):
            await interaction.response.send_message(
                "❌ Сейчас ничего не играет.", ephemeral=True
            )
            return
        await interaction.response.send_message("⏭️ Пропущено.")

    # ── /pause ────────────────────────────────────────────────────────────
    @app_commands.command(name="pause", description="Поставить на паузу")
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
    @app_commands.command(name="resume", description="Продолжить воспроизведение")
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
    @app_commands.command(name="stop", description="Остановить и очистить очередь (бот остаётся в канале)")
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
    @app_commands.command(name="queue", description="Показать очередь")
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
                value=f"**{short_title(state.current.title, 200)}** `[{state.current.duration_str}]`",
                inline=False,
            )
        upcoming = state.upcoming[:10]
        if upcoming:
            lines = [
                f"`{i}.` {short_title(t.title)} `[{t.duration_str}]` — {short_title(t.requested_by, 32)}"
                for i, t in enumerate(upcoming, start=1)
            ]
            embed.add_field(name="Далее", value=_field(lines, len(state)), inline=False)
        if state.repeat is not RepeatMode.OFF:
            embed.set_footer(text=f"{state.repeat.emoji} Повтор: {state.repeat.label}")
        await interaction.response.send_message(embed=embed)

    # ── /nowplaying ───────────────────────────────────────────────────────
    @app_commands.command(name="nowplaying", description="Что играет сейчас")
    async def nowplaying(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild.id)
        if state.current is None:
            await interaction.response.send_message(
                "❌ Сейчас ничего не играет.", ephemeral=True
            )
            return
        await interaction.response.send_message(embed=self._now_embed(state))

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

    @app_commands.command(name="ping", description="Сервисная проверка: задержки, FFmpeg, режим доставки звука")
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
        # Считаем людей (не ботов) в канале бота
        humans = [m for m in vc.channel.members if not m.bot]
        if not humans:
            logger.info("[voice] no humans left on guild %s — disconnecting", guild.id)
            await self._disconnect(guild.id)


async def setup_cog(bot: commands.Bot, idle_timeout: int) -> MusicCog:
    cog = MusicCog(bot, idle_timeout)
    await bot.add_cog(cog)
    return cog
