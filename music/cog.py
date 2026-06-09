"""MusicCog: слэш-команды управления музыкой."""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from .player import MusicPlayer
from .queue import GuildMusicState
from .track import Track
from .ui import PlayerView, SearchView

logger = logging.getLogger("music_bot.cog")


def _is_url(query: str) -> bool:
    """Похоже ли на прямую ссылку (а не текстовый запрос для поиска)."""
    q = query.strip().lower()
    return q.startswith("http://") or q.startswith("https://")


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
                vc = await channel.connect(timeout=20.0, reconnect=False)
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

    def _start_player(self, guild_id: int, vc: discord.VoiceClient) -> None:
        """Запустить плеер для guild, если ещё не запущен (защита от дублей)."""
        state = self.get_state(guild_id)
        if state.player_task and not state.player_task.done():
            return  # плеер уже работает
        player = MusicPlayer(
            voice_client=vc,
            state=state,
            idle_timeout=self.idle_timeout,
            on_disconnect=self._disconnect,
            on_track_change=self._on_track_change,
        )
        self._players[guild_id] = player
        state.player_task = self.bot.loop.create_task(player.player_loop())

    async def _disconnect(self, guild_id: int) -> None:
        """Полное отключение: остановка плеера, очистка очереди, выход из канала."""
        state = self._states.get(guild_id)
        if state:
            state.clear()
            state.current = None
            if state.player_task:
                state.player_task.cancel()
                state.player_task = None
            await self._remove_panel(state)

        guild = self.bot.get_guild(guild_id)
        if guild and guild.voice_client:
            await guild.voice_client.disconnect(force=True)
        self._players.pop(guild_id, None)

    # ── Живая панель-плеер ────────────────────────────────────────────────
    def _now_embed(self, state: GuildMusicState) -> discord.Embed:
        """Эмбед «сейчас играет» по текущему состоянию сервера."""
        track = state.current
        vc = self.bot.get_guild(state.guild_id)
        vc = vc.voice_client if vc else None
        paused = bool(vc and vc.is_paused())

        title = "⏸️ На паузе" if paused else "▶️ Сейчас играет"
        embed = discord.Embed(
            title=title,
            description=f"**[{track.title}]({track.webpage_url})**",
            color=0xED4245 if paused else 0x57F287,
        )
        embed.add_field(name="Длительность", value=track.duration_str, inline=True)
        if track.uploader:
            embed.add_field(name="Автор", value=track.uploader, inline=True)
        embed.add_field(name="Заказал", value=track.requested_by, inline=True)
        if track.thumbnail:
            embed.set_thumbnail(url=track.thumbnail)

        upcoming = state.upcoming[:5]
        if upcoming:
            lines = [f"`{i}.` {t.title} `[{t.duration_str}]`"
                     for i, t in enumerate(upcoming, start=1)]
            extra = len(state) - len(upcoming)
            if extra > 0:
                lines.append(f"… и ещё {extra}")
            embed.add_field(name="📋 Далее в очереди", value="\n".join(lines), inline=False)
        return embed

    async def _remove_panel(self, state: GuildMusicState) -> None:
        """Удалить старое сообщение-панель, если есть."""
        if state.panel_message is not None:
            try:
                await state.panel_message.delete()
            except discord.HTTPException:
                pass
            state.panel_message = None

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
            state.panel_message = None
            return
        embed = self._now_embed(state)
        view = PlayerView(self, interaction.guild.id)
        await interaction.response.edit_message(embed=embed, view=view)
        state.panel_message = interaction.message

    def restart_current(self, guild_id: int, vc: discord.VoiceClient) -> bool:
        """Перезапустить текущий трек с начала (кнопка ⏮️).

        Кладём текущий трек обратно в НАЧАЛО очереди и обрываем воспроизведение —
        after-колбэк плеера разбудит цикл, он возьмёт этот же трек заново.
        """
        state = self.get_state(guild_id)
        if state.current is None:
            return False
        state.add_front(state.current)
        vc.stop()  # → _after → next_event.set() → плеер берёт трек из начала
        return True

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
        await self._disconnect(interaction.guild.id)
        await interaction.response.send_message("👋 Отключился и очистил очередь.")

    async def enqueue_from_interaction(
        self, interaction: discord.Interaction, track: Track
    ) -> tuple[bool, str]:
        """Поставить готовый Track в очередь и запустить плеер.

        Используется и из /play (ссылка), и из выпадающего списка поиска.
        Возвращает (успех, текст-подтверждение).
        """
        vc = interaction.guild.voice_client
        if vc is None:
            vc = await self._ensure_voice(interaction)
            if vc is None:
                return False, "❌ Не удалось подключиться к голосовому каналу."

        state = self.get_state(interaction.guild.id)
        state.text_channel = interaction.channel  # куда вешать живую панель
        # «Простаивает» = ничего не играет И очередь пуста (до добавления).
        was_idle = state.current is None and state.is_empty
        state.add(track)
        self._start_player(interaction.guild.id, vc)

        # Будим плеер ТОЛЬКО если он простаивает (ждёт новый трек). Если трек уже
        # играет, плеер сам возьмёт следующий из очереди по окончании текущего —
        # лишний set() здесь оборвал бы текущий трек («Already playing»).
        if was_idle:
            state.next_event.set()
            return True, f"▶️ Запускаю: **{track.title}** `[{track.duration_str}]`"
        return True, f"➕ В очередь (#{len(state)}): **{track.title}** `[{track.duration_str}]`"

    # ── /play ─────────────────────────────────────────────────────────────
    @app_commands.command(name="play", description="Воспроизвести трек по ссылке или поисковому запросу")
    @app_commands.describe(query="YouTube/SoundCloud ссылка или текст для поиска")
    async def play(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer()

        vc = await self._ensure_voice(interaction)
        if vc is None:
            return

        # Ссылка → играем сразу. Текст → показываем выбор из нескольких вариантов.
        if _is_url(query):
            track = await MusicPlayer.resolve(query, requester=interaction.user.display_name)
            if track is None:
                await interaction.followup.send(f"❌ Не удалось загрузить: `{query}`")
                return
            ok, info = await self.enqueue_from_interaction(interaction, track)
            await interaction.followup.send(info)
            return

        results = await MusicPlayer.search(query, limit=5)
        if not results:
            await interaction.followup.send(f"❌ Ничего не нашёл по запросу: `{query}`")
            return

        view = SearchView(self, results, requester=interaction.user.display_name)
        await interaction.followup.send(
            f"🔎 Нашёл варианты по запросу **{query}** — выбери нужный:", view=view
        )

    # ── /skip ─────────────────────────────────────────────────────────────
    @app_commands.command(name="skip", description="Пропустить текущий трек")
    async def skip(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc is None or not vc.is_playing():
            await interaction.response.send_message(
                "❌ Сейчас ничего не играет.", ephemeral=True
            )
            return
        vc.stop()  # after-колбэк переключит на следующий трек
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
        await self._rerender_panel(self.get_state(interaction.guild.id))
        await interaction.response.send_message("⏸️ Пауза.", ephemeral=True)

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
        await self._rerender_panel(self.get_state(interaction.guild.id))
        await interaction.response.send_message("▶️ Продолжаю.", ephemeral=True)

    # ── /stop ─────────────────────────────────────────────────────────────
    @app_commands.command(name="stop", description="Остановить и очистить очередь (бот остаётся в канале)")
    async def stop(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc is None:
            await interaction.response.send_message(
                "❌ Я не в голосовом канале.", ephemeral=True
            )
            return
        state = self.get_state(interaction.guild.id)
        state.clear()
        if vc.is_playing() or vc.is_paused():
            vc.stop()
        await self._remove_panel(state)
        await interaction.response.send_message("⏹️ Остановлено, очередь очищена.")

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
                value=f"**{state.current.title}** `[{state.current.duration_str}]`",
                inline=False,
            )
        upcoming = state.upcoming[:10]
        if upcoming:
            lines = [
                f"`{i}.` {t.title} `[{t.duration_str}]` — {t.requested_by}"
                for i, t in enumerate(upcoming, start=1)
            ]
            extra = len(state) - len(upcoming)
            if extra > 0:
                lines.append(f"… и ещё {extra}")
            embed.add_field(name="Далее", value="\n".join(lines), inline=False)
        await interaction.response.send_message(embed=embed)

    # ── /nowplaying ───────────────────────────────────────────────────────
    @app_commands.command(name="nowplaying", description="Что играет сейчас")
    async def nowplaying(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild.id)
        track = state.current
        if track is None:
            await interaction.response.send_message(
                "❌ Сейчас ничего не играет.", ephemeral=True
            )
            return
        embed = discord.Embed(
            title="▶️ Сейчас играет",
            description=f"**[{track.title}]({track.webpage_url})**",
            color=0x5865F2,
        )
        embed.add_field(name="Длительность", value=track.duration_str, inline=True)
        if track.uploader:
            embed.add_field(name="Автор", value=track.uploader, inline=True)
        embed.add_field(name="Заказал", value=track.requested_by, inline=True)
        if track.thumbnail:
            embed.set_thumbnail(url=track.thumbnail)
        await interaction.response.send_message(embed=embed)

    # ── Авто-отключение, когда бот остался в канале один ──────────────────
    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        """Если в голосовом остался только бот — отключаемся."""
        if member.bot:
            return
        guild = member.guild
        vc = guild.voice_client
        if vc is None:
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
