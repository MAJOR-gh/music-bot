"""Точка входа: настройка бота, логирования и синхронизация слэш-команд."""
from __future__ import annotations

import logging

import discord
from discord.ext import commands

import config
from music.cog import setup_cog

# ── Логирование ───────────────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("music_bot")

# ── Intents ───────────────────────────────────────────────────────────────────
# voice_states нужен для отслеживания голосовых каналов.
intents = discord.Intents.default()
intents.voice_states = True
intents.guilds = True


class MusicBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self) -> None:
        await setup_cog(self, config.IDLE_TIMEOUT)

        if config.GUILD_ID:
            guild = discord.Object(id=config.GUILD_ID)
            # 1) Копируем команды (они зарегистрированы как глобальные) в guild.
            self.tree.copy_global_to(guild=guild)
            # 2) Чистим глобальные и синхроним пустой список — стираем глобальные
            #    команды на стороне Discord (иначе они дублируют гильдийные).
            self.tree.clear_commands(guild=None)
            await self.tree.sync()
            # 3) Пушим гильдийные команды — появляются мгновенно, без дублей.
            synced = await self.tree.sync(guild=guild)
            logger.info("Синхронизировано %d команд для guild %s", len(synced), config.GUILD_ID)
        else:
            synced = await self.tree.sync()
            logger.info("Синхронизировано %d глобальных команд (может занять до ~1 ч)", len(synced))

    async def on_ready(self) -> None:
        logger.info("✅ Бот онлайн: %s (id=%s)", self.user, self.user.id)
        await self.change_presence(
            activity=discord.Activity(type=discord.ActivityType.listening, name="/play")
        )


def main() -> None:
    if not config.DISCORD_TOKEN:
        raise SystemExit(
            "❌ Не задан DISCORD_TOKEN. Скопируй .env.example в .env и впиши токен."
        )
    bot = MusicBot()
    bot.run(config.DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
