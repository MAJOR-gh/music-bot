"""Офлайн-тесты музыкального бота (без Discord и без интернета).

Запуск:  python test_bot.py

Голос Discord и скачивание подменены заглушками: проверяется логика очереди,
плеера и кнопок — повтор, пропуск, «Стоп», ⏮️, простой, предзагрузка.
"""
from __future__ import annotations

import asyncio
import inspect
import os
import subprocess
import sys
import tempfile
import time
import traceback

from music import player as player_module
from music import sources, ytdl
from music.cog import MusicCog, _field
from music.player import MusicPlayer, Prepared
from music.queue import RepeatMode
from music.track import SearchResult, Track, format_duration, short_title


# ── Заглушки Discord ──────────────────────────────────────────────────────────
class FakeSource:
    def cleanup(self) -> None:
        pass


class FakeVC:
    """Голосовое подключение: «играет» трек play_time секунд, потом зовёт after."""

    def __init__(self, play_time: float):
        self.play_time = play_time
        self.guild = None
        self.channel = None
        self.connected = True
        self.disconnected = False
        self._task: asyncio.Task | None = None
        self._after = None
        self._paused = False
        self._stopping = False

    def is_connected(self) -> bool:
        return self.connected

    def is_playing(self) -> bool:
        return self._task is not None and not self._paused and not self._stopping

    def is_paused(self) -> bool:
        return self._task is not None and self._paused and not self._stopping

    def play(self, source, *, after) -> None:
        if self._task is not None:
            raise RuntimeError("Already playing audio.")
        self._after, self._paused, self._stopping = after, False, False
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def _run(self) -> None:
        try:
            await asyncio.sleep(self.play_time)
        except asyncio.CancelledError:
            pass
        after, self._after, self._task = self._after, None, None
        if after:
            after(None)

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def stop(self) -> None:
        if self._task is not None:
            self._stopping = True
            self._task.cancel()

    async def disconnect(self, force: bool = False) -> None:
        self.stop()
        self.connected = False
        self.disconnected = True
        self.guild.voice_client = None


class FakeGuild:
    def __init__(self, gid: int, vc: FakeVC):
        self.id = gid
        self.voice_client = vc
        vc.guild = self


class FakeBot:
    def __init__(self):
        self.guilds: dict[int, FakeGuild] = {}
        self.user = None

    def get_guild(self, gid: int):
        return self.guilds.get(gid)


class TestCog(MusicCog):
    """Настоящий MusicCog, только панель в чат заменена записью в список."""

    def __init__(self, bot, idle_timeout: int = 30):
        super().__init__(bot, idle_timeout)
        self.played: list[str] = []
        self.errors: list[str] = []

    async def _on_track_change(self, guild_id: int) -> None:
        state = self.get_state(guild_id)
        if state.current is not None:
            self.played.append(state.current.title)

    async def _on_track_error(self, guild_id: int, track: Track) -> None:
        self.errors.append(track.title)


# ── Подмена подготовки трека (вместо yt-dlp) ──────────────────────────────────
PREPARE_DELAY = 0.05
prepare_calls: list[str] = []
fail_titles: set[str] = set()


async def fake_prepare(track: Track) -> Prepared | None:
    prepare_calls.append(track.title)
    await asyncio.sleep(PREPARE_DELAY)
    if track.title in fail_titles:
        return None
    return Prepared("file", path=None)


async def fake_make_source(prepared: Prepared):
    return FakeSource(), None


MusicPlayer._prepare = staticmethod(fake_prepare)
MusicPlayer._make_source = staticmethod(fake_make_source)


def T(title: str, duration: int = 180) -> Track:
    return Track(title=title, webpage_url=f"https://youtu.be/{title}", duration=duration,
                 uploader=None, thumbnail=None, requested_by="tester")


def setup(play_time: float = 0.1, idle: int = 30):
    global PREPARE_DELAY
    PREPARE_DELAY = 0.05
    prepare_calls.clear()
    fail_titles.clear()
    bot = FakeBot()
    vc = FakeVC(play_time)
    guild = FakeGuild(1, vc)
    bot.guilds[1] = guild
    cog = TestCog(bot, idle)
    return cog, guild, vc, cog.get_state(1)


async def wait_until(cond, timeout: float = 3.0, what: str = "условие") -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"не дождался: {what}")


async def shutdown(cog: TestCog, guild: FakeGuild) -> None:
    await cog._disconnect(guild.id)
    await asyncio.sleep(0.05)


# ── Тесты ─────────────────────────────────────────────────────────────────────
async def test_plays_queue_in_order():
    cog, guild, vc, state = setup()
    assert cog.enqueue_tracks(guild, [T("a"), T("b"), T("c")]) is True
    await wait_until(lambda: cog.played == ["a", "b", "c"] and state.current is None, what="a,b,c")
    await shutdown(cog, guild)


async def test_second_enqueue_does_not_cut_current():
    cog, guild, vc, state = setup(play_time=0.3)
    cog.enqueue_tracks(guild, [T("a")])
    await wait_until(lambda: cog.played == ["a"])
    assert cog.enqueue_tracks(guild, [T("b")]) is False
    await asyncio.sleep(0.1)
    assert cog.played == ["a"], cog.played          # «a» не оборвался
    await wait_until(lambda: cog.played == ["a", "b"])
    await shutdown(cog, guild)


async def test_prefetch_next_while_playing():
    cog, guild, vc, state = setup(play_time=0.5)
    cog.enqueue_tracks(guild, [T("a"), T("b")])
    await wait_until(lambda: cog.played == ["a"])
    await wait_until(lambda: "b" in prepare_calls, timeout=0.3, what="предзагрузка b во время a")
    await wait_until(lambda: cog.played == ["a", "b"])
    assert prepare_calls.count("b") == 1, prepare_calls
    await shutdown(cog, guild)


async def test_repeat_one_replays_and_reuses_file():
    cog, guild, vc, state = setup(play_time=0.05)
    state.repeat = RepeatMode.ONE
    cog.enqueue_tracks(guild, [T("a"), T("b")])
    await wait_until(lambda: cog.played.count("a") >= 3, what="a трижды")
    assert "b" not in cog.played
    assert prepare_calls.count("a") == 1, prepare_calls   # не качаем заново
    await shutdown(cog, guild)


async def test_repeat_all_cycles():
    cog, guild, vc, state = setup(play_time=0.05)
    state.repeat = RepeatMode.ALL
    cog.enqueue_tracks(guild, [T("a"), T("b")])
    await wait_until(lambda: len(cog.played) >= 5)
    assert cog.played[:5] == ["a", "b", "a", "b", "a"], cog.played
    await shutdown(cog, guild)


async def test_skip_with_repeat_one_goes_next():
    cog, guild, vc, state = setup(play_time=5)
    state.repeat = RepeatMode.ONE
    cog.enqueue_tracks(guild, [T("a"), T("b")])
    await wait_until(lambda: cog.played == ["a"])
    assert cog.skip(guild)
    await wait_until(lambda: cog.played == ["a", "b"])
    assert [t.title for t in state.upcoming] == [], state.upcoming
    await shutdown(cog, guild)


async def test_skip_while_paused():
    cog, guild, vc, state = setup(play_time=5)
    cog.enqueue_tracks(guild, [T("a"), T("b")])
    await wait_until(lambda: cog.played == ["a"])
    vc.pause()
    assert vc.is_paused()
    assert cog.skip(guild)
    await wait_until(lambda: cog.played == ["a", "b"])
    await shutdown(cog, guild)


async def test_stop_with_repeat_one_stops_for_real():
    cog, guild, vc, state = setup(play_time=5)
    state.repeat = RepeatMode.ONE
    cog.enqueue_tracks(guild, [T("a"), T("b")])
    await wait_until(lambda: cog.played == ["a"])
    cog.stop_all(guild)
    await wait_until(lambda: state.current is None and not vc.is_playing())
    await asyncio.sleep(0.2)
    assert cog.played == ["a"], cog.played
    assert state.is_empty
    # После «Стопа» бот снова играет новые треки, и повтор работает как прежде
    cog.enqueue_tracks(guild, [T("c")])
    await wait_until(lambda: cog.played[-1] == "c")
    await shutdown(cog, guild)


async def test_restart_button_with_repeat_one_no_duplicate():
    cog, guild, vc, state = setup(play_time=5)
    state.repeat = RepeatMode.ONE
    cog.enqueue_tracks(guild, [T("a"), T("b")])
    await wait_until(lambda: cog.played == ["a"])
    assert cog.restart_current(guild)
    await wait_until(lambda: cog.played == ["a", "a"])
    assert [t.title for t in state.upcoming] == ["b"], [t.title for t in state.upcoming]
    assert prepare_calls.count("a") == 1, prepare_calls
    await shutdown(cog, guild)


async def test_skip_while_preparing_is_instant():
    global PREPARE_DELAY
    cog, guild, vc, state = setup(play_time=5)
    PREPARE_DELAY = 10           # «скачивание» на 10 секунд
    cog.enqueue_tracks(guild, [T("slow"), T("next")])
    await wait_until(lambda: state.current is not None and state.current.title == "slow")
    PREPARE_DELAY = 0.05
    started = time.monotonic()
    assert cog.skip(guild)
    await wait_until(lambda: cog.played == ["next"], timeout=2, what="следующий сразу после пропуска")
    assert time.monotonic() - started < 1.5
    await shutdown(cog, guild)


async def test_stop_while_preparing():
    global PREPARE_DELAY
    cog, guild, vc, state = setup(play_time=5)
    PREPARE_DELAY = 10
    cog.enqueue_tracks(guild, [T("slow"), T("next")])
    await wait_until(lambda: state.current is not None)
    cog.stop_all(guild)
    await wait_until(lambda: state.current is None, timeout=2)
    await asyncio.sleep(0.2)
    assert cog.played == [] and state.is_empty, (cog.played, state.upcoming)
    await shutdown(cog, guild)


async def test_failed_track_reported_and_skipped():
    cog, guild, vc, state = setup()
    fail_titles.add("broken")
    cog.enqueue_tracks(guild, [T("broken"), T("ok")])
    await wait_until(lambda: cog.played == ["ok"])
    assert cog.errors == ["broken"], cog.errors
    await shutdown(cog, guild)


async def test_idle_timeout_really_disconnects():
    cog, guild, vc, state = setup(play_time=0.05, idle=1)
    cog.enqueue_tracks(guild, [T("a")])
    await wait_until(lambda: cog.played == ["a"])
    await wait_until(lambda: vc.disconnected, timeout=3, what="выход из канала по простою")
    assert guild.voice_client is None
    assert state.player_task is None


async def test_player_survives_new_voice_client():
    """/leave + /join создают новый VoiceClient — плеер должен играть в новый."""
    cog, guild, vc, state = setup(play_time=0.05)
    cog.enqueue_tracks(guild, [T("a")])
    await wait_until(lambda: cog.played == ["a"])
    new_vc = FakeVC(0.05)
    new_vc.guild = guild
    guild.voice_client = new_vc       # старый объект больше не используется
    vc.connected = False
    cog.enqueue_tracks(guild, [T("b")])
    await wait_until(lambda: cog.played == ["a", "b"])
    await shutdown(cog, guild)


async def test_leave_cancels_player_and_cleans_up():
    cog, guild, vc, state = setup(play_time=5)
    cog.enqueue_tracks(guild, [T("a"), T("b")])
    await wait_until(lambda: cog.played == ["a"])
    task = state.player_task
    await cog._disconnect(guild.id)
    await asyncio.sleep(0.05)
    assert task.done() and vc.disconnected and state.current is None and state.is_empty


def test_discard_file_waits_until_unlocked():
    """Windows не даёт удалить файл, пока его держит FFmpeg, — удаление должно дождаться."""
    fd, path = tempfile.mkstemp(prefix="musicbot-test-")
    os.close(fd)
    holder = subprocess.Popen([sys.executable, "-c",
                               f"f = open({path!r}, 'rb'); import time; time.sleep(1.5)"])

    async def run():
        await asyncio.sleep(0.3)
        ytdl.discard_file(path)
        await asyncio.sleep(3.5)

    asyncio.run(run())
    holder.wait()
    assert not os.path.exists(path), "файл так и не удалился"


def test_helpers():
    assert format_duration(None) == "?:??"
    assert format_duration(None, live=True) == "LIVE"
    assert format_duration(65) == "1:05" and format_duration(3725) == "1:02:05"
    assert short_title("a[b]c") == "a(b)c"
    assert len(short_title("x" * 300)) == 60
    lines = [f"`{i}.` " + "y" * 150 for i in range(10)]
    value = _field(lines, total=40)
    assert len(value) <= 1024 and value.endswith(f"… и ещё {40 - value.count(chr(10))}"), value[-40:]
    t = T("q")
    assert t.link_title == "[q](https://youtu.be/q)"
    sr = SearchResult(title="Song", url=None, duration=200, uploader="Artist", source="Spotify")
    tr = sr.to_track("me")
    assert tr.match == ("Artist", "Song") and tr.webpage_url is None


def test_spotify_parsing():
    assert sources.parse_spotify_link("https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC?si=x") == (
        "track", "4uLU6hMCjMI75M1A2tKUQC")
    assert sources.parse_spotify_link("https://open.spotify.com/intl-de/album/1DFixLWuPkv3KT3TnV35m3") == (
        "album", "1DFixLWuPkv3KT3TnV35m3")
    assert sources.parse_spotify_link("spotify:playlist:37i9dQZF1DXcBWIGoYBM5M") == (
        "playlist", "37i9dQZF1DXcBWIGoYBM5M")
    assert sources.parse_spotify_link("просто текст") is None
    html = ('<script id="__NEXT_DATA__" type="application/json">'
            '{"props":{"pageProps":{"state":{"data":{"entity":{"name":"Mix","type":"playlist",'
            '"trackList":[{"title":"One","subtitle":"Band","duration":200000},'
            '{"title":"Two","subtitle":"Other","duration":61000}]}}}}}}</script>')
    name, tracks = sources.parse_spotify_embed(html)
    assert name == "Mix" and [(t.title, t.artist, t.duration) for t in tracks] == [
        ("One", "Band", 200), ("Two", "Other", 61)]


def test_match_score_prefers_right_song():
    good = SearchResult(title="Bohemian Rhapsody (Remastered 2011)", url="u1", duration=355,
                        uploader="Queen")
    bad = SearchResult(title="Bohemian Rhapsody (Piano Cover)", url="u2", duration=290,
                       uploader="Some Pianist")
    assert sources.match_score(good, "Queen", "Bohemian Rhapsody", 354) > \
        sources.match_score(bad, "Queen", "Bohemian Rhapsody", 354)


def test_split_artist_title():
    assert sources.split_artist_title("Mick Gordon - Doom 2016: Menu theme (HQ, file rip)") == (
        "Mick Gordon", "Doom 2016: Menu theme")
    assert sources.split_artist_title("Song Name [Official Video]", "Band - Topic") == (
        "Band", "Song Name")


def test_bot_check_detected():
    ytdl.last_bot_check = None
    ytdl.note_error("ERROR: [youtube] x: Sign in to confirm you’re not a bot. Use --cookies")
    assert ytdl.last_bot_check is not None
    ytdl.last_bot_check = None
    ytdl.note_error("ERROR: HTTP Error 404")
    assert ytdl.last_bot_check is None


def test_soundcloud_picks_matching_track():
    async def fake_extract(target, *, flat=False, timeout=60):
        return {"entries": [
            {"url": "sc/wrong", "title": "Doom Eternal OST - BFG Division", "duration": 500,
             "uploader": "someone"},
            {"url": "sc/right", "title": "Doom 2016 - Menu Theme", "duration": 241,
             "uploader": "Mick Gordon"},
        ]}

    async def fake_extract_junk(target, *, flat=False, timeout=60):
        return {"entries": [{"url": "sc/junk", "title": "Totally different", "duration": 30,
                             "uploader": "x"}]}

    real = ytdl.extract
    try:
        ytdl.extract = fake_extract
        url = asyncio.run(sources.soundcloud_alternative("Mick Gordon", "Doom 2016: Menu theme", 240))
        assert url == "sc/right", url
        ytdl.extract = fake_extract_junk
        url = asyncio.run(sources.soundcloud_alternative("Mick Gordon", "Doom 2016: Menu theme", 240))
        assert url is None, url        # лучше не сыграть, чем включить другую песню
    finally:
        ytdl.extract = real


ASYNC_TESTS = [
    test_plays_queue_in_order,
    test_second_enqueue_does_not_cut_current,
    test_prefetch_next_while_playing,
    test_repeat_one_replays_and_reuses_file,
    test_repeat_all_cycles,
    test_skip_with_repeat_one_goes_next,
    test_skip_while_paused,
    test_stop_with_repeat_one_stops_for_real,
    test_restart_button_with_repeat_one_no_duplicate,
    test_skip_while_preparing_is_instant,
    test_stop_while_preparing,
    test_failed_track_reported_and_skipped,
    test_idle_timeout_really_disconnects,
    test_player_survives_new_voice_client,
    test_leave_cancels_player_and_cleans_up,
]
SYNC_TESTS = [test_helpers, test_spotify_parsing, test_match_score_prefers_right_song,
              test_split_artist_title, test_bot_check_detected, test_soundcloud_picks_matching_track,
              test_discard_file_waits_until_unlocked]


def main() -> int:
    player_module.RECONNECT_WAIT = 2
    failed = 0
    for test in SYNC_TESTS + ASYNC_TESTS:
        try:
            if inspect.iscoroutinefunction(test):
                asyncio.run(asyncio.wait_for(test(), 20))
            else:
                test()
            print(f"✅ {test.__name__}")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"❌ {test.__name__}")
            traceback.print_exc()
    total = len(SYNC_TESTS) + len(ASYNC_TESTS)
    print(f"\n{total - failed}/{total} тестов прошло")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
