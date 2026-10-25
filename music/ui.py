"""Кнопки живой панели-плеера под эмбедом «сейчас играет».

Сами действия (пауза, пропуск, обновление панели) живут в MusicCog — здесь только
разметка и проброс кликов в cog, чтобы не дублировать логику.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from .queue import RepeatMode

if TYPE_CHECKING:
    from .cog import MusicCog


class PlayerView(discord.ui.View):
    """Кнопки под эмбедом «сейчас играет». Привязаны к конкретному серверу."""

    def __init__(self, cog: MusicCog, guild_id: int):
        super().__init__(timeout=None)  # живёт пока висит панель
        self.cog = cog
        self.guild_id = guild_id
        self._sync_repeat_button()
        self._sync_autoplay_button()

    def _vc(self, interaction: discord.Interaction) -> discord.VoiceClient | None:
        guild = interaction.guild
        return guild.voice_client if guild else None

    def _sync_repeat_button(self) -> None:
        """Привести вид кнопки 🔁 в соответствие текущему режиму повтора."""
        mode = self.cog.get_state(self.guild_id).repeat
        self.repeat_btn.emoji = mode.emoji
        self.repeat_btn.label = f"Повтор: {mode.label}"
        self.repeat_btn.style = (
            discord.ButtonStyle.secondary if mode is RepeatMode.OFF else discord.ButtonStyle.success
        )

    def _sync_autoplay_button(self) -> None:
        on = self.cog.get_state(self.guild_id).autoplay
        self.autoplay_btn.label = f"Автоплей: {'вкл' if on else 'выкл'}"
        self.autoplay_btn.style = discord.ButtonStyle.success if on else discord.ButtonStyle.secondary

    @discord.ui.button(emoji="⏪", label="Назад", style=discord.ButtonStyle.secondary)
    async def back_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Предыдущий трек: панель перевыложится сама, когда он заиграет.
        if self._vc(interaction) is not None and self.cog.go_back(interaction.guild):
            await interaction.response.defer()
        else:
            await interaction.response.send_message("❌ Раньше ничего не играло.", ephemeral=True)

    @discord.ui.button(emoji="⏮️", label="В начало", style=discord.ButtonStyle.secondary)
    async def restart_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Перезапуск с начала: cog кладёт трек в начало очереди и обрывает
        # текущий. Панель обновится сама через on_track_change.
        if self.cog.restart_current(interaction.guild):
            await interaction.response.defer()
        else:
            await interaction.response.send_message("❌ Сейчас ничего не играет.", ephemeral=True)

    @discord.ui.button(emoji="⏸️", label="Пауза", style=discord.ButtonStyle.secondary)
    async def pause_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = self._vc(interaction)
        if vc and vc.is_playing():
            vc.pause()
            await self.cog.refresh_panel_inplace(interaction)
        else:
            await interaction.response.send_message("❌ Сейчас ничего не играет.", ephemeral=True)

    @discord.ui.button(emoji="▶️", label="Продолжить", style=discord.ButtonStyle.success)
    async def resume_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = self._vc(interaction)
        if vc and vc.is_paused():
            vc.resume()
            await self.cog.refresh_panel_inplace(interaction)
        else:
            await interaction.response.send_message("❌ Нечего возобновлять.", ephemeral=True)

    @discord.ui.button(emoji="⏭️", label="Пропустить", style=discord.ButtonStyle.primary)
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Плеер возьмёт следующий трек и сам перевыложит панель (on_track_change).
        if self.cog.skip(interaction.guild):
            await interaction.response.defer()
        else:
            await interaction.response.send_message("❌ Нечего пропускать.", ephemeral=True)

    @discord.ui.button(emoji="🔁", label="Повтор: выкл", style=discord.ButtonStyle.secondary, row=1)
    async def repeat_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Переключаем режим и перерисовываем панель на месте (вид кнопки и эмбед
        # обновятся через refresh_panel_inplace → новый PlayerView).
        self.cog.get_state(self.guild_id).cycle_repeat()
        await self.cog.refresh_panel_inplace(interaction)

    @discord.ui.button(emoji="🔀", label="Перемешать", style=discord.ButtonStyle.secondary, row=1)
    async def shuffle_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = self.cog.get_state(self.guild_id)
        if len(state) < 2:
            await interaction.response.send_message(
                "❌ Перемешивать нечего — в очереди меньше двух треков.", ephemeral=True)
            return
        state.shuffle()
        self.cog.queue_changed(interaction.guild)
        await self.cog.refresh_panel_inplace(interaction)

    @discord.ui.button(emoji="📻", label="Автоплей: выкл", style=discord.ButtonStyle.secondary, row=1)
    async def autoplay_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = self.cog.get_state(self.guild_id)
        self.cog.set_autoplay(interaction.guild, not state.autoplay)
        await self.cog.refresh_panel_inplace(interaction)

    @discord.ui.button(emoji="⏹️", label="Стоп", style=discord.ButtonStyle.danger, row=1)
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self._vc(interaction) is None:
            await interaction.response.send_message("❌ Я не в голосовом канале.", ephemeral=True)
            return
        self.cog.stop_all(interaction.guild)
        state = self.cog.get_state(self.guild_id)
        if state.panel_message is not None and state.panel_message.id == interaction.message.id:
            state.panel_message = None   # это сообщение превращаем в «Остановлено», не удаляем
        await interaction.response.edit_message(
            content="⏹️ Остановлено, очередь очищена.", embed=None, view=None
        )
