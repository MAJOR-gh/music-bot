"""Регрессия аудиоформата: python test_audio.py (FFmpeg/libopus, без сети)."""
from __future__ import annotations

import asyncio
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import discord

from music.player import DiscordOpusAudio, MusicPlayer, Prepared


def packet_duration_ms(packet: bytes) -> float:
    """Длительность из TOC, RFC 6716 §3.1 (без системной libopus)."""
    config = packet[0] >> 3
    if config < 12:
        frame = (10, 20, 40, 60)[config & 3]
    elif config < 16:
        frame = (10, 20)[config & 1]
    else:
        frame = (2.5, 5, 10, 20)[config & 3]
    code = packet[0] & 3
    frames = 1 if code == 0 else 2 if code in (1, 2) else packet[1] & 63
    return frame * frames


class HeaderTests(unittest.TestCase):
    def test_skips_container_headers(self):
        source = object.__new__(DiscordOpusAudio)
        source.cleanup = lambda: None  # Заглушка не создаёт процесс FFmpeg.
        with patch.object(discord.FFmpegOpusAudio, 'read',
                          side_effect=[b'OpusHead' + b'\0' * 11,
                                       b'OpusTags' + b'\0' * 8, b'\xfc\x00', b'']):
            self.assertEqual(source.read(), b'\xfc\x00')
            self.assertEqual(source.read(), b'')


class AudioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which('ffmpeg'):
            raise RuntimeError('Для аудиотестов нужен FFmpeg с libopus')
        cls.tmp = tempfile.TemporaryDirectory(prefix='musicbot-audio-test-')
        cls.root = Path(cls.tmp.name)
        for name, codec, channels, frame in [
            ('opus20.ogg', 'libopus', 2, 20),
            ('opus60.ogg', 'libopus', 1, 60),
            ('aac.m4a', 'aac', 1, None),
            ('pcm.wav', 'pcm_s16le', 1, None),
        ]:
            args = ['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                    'sine=frequency=1000:duration=1', '-ac', str(channels), '-c:a', codec]
            if frame:
                args += ['-frame_duration', str(frame)]
            subprocess.run(args + ['-y', str(cls.root / name)], check=True,
                           capture_output=True, timeout=30)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def check_source(self, source):
        packets = []
        try:
            while packet := source.read():
                self.assertFalse(packet.startswith((b'OpusHead', b'OpusTags')))
                self.assertEqual(packet_duration_ms(packet), 20)
                self.assertTrue(packet[0] & 4, 'выходной Opus должен быть stereo')
                packets.append(packet)
        finally:
            source.cleanup()
        # Одна секунда + padding входного AAC/Opus и задержка выходного кодера.
        self.assertTrue(50 <= len(packets) <= 53, len(packets))

    def test_file_formats(self):
        for path in sorted(self.root.iterdir()):
            with self.subTest(format=path.name):
                source, proc = asyncio.run(MusicPlayer._make_source(
                    Prepared('file', path=str(path))))
                self.assertIsNone(proc)
                self.check_source(source)

    def test_direct_http(self):
        class QuietHandler(SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0),
                                     partial(QuietHandler, directory=str(self.root)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f'http://127.0.0.1:{server.server_port}/opus60.ogg'
            source, proc = asyncio.run(MusicPlayer._make_source(Prepared('direct', url=url)))
            self.assertIsNone(proc)
            self.check_source(source)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_pipe(self):
        proc = subprocess.Popen([sys.executable, '-c',
                                 'import pathlib,sys; sys.stdout.buffer.write(pathlib.Path(sys.argv[1]).read_bytes())',
                                 str(self.root / 'opus60.ogg')], stdout=subprocess.PIPE)
        try:
            with patch('music.player.ytdl.spawn_pipe', return_value=proc):
                source, returned_proc = asyncio.run(MusicPlayer._make_source(
                    Prepared('pipe', url='unused')))
            self.assertIs(returned_proc, proc)
            self.check_source(source)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
            proc.stdout.close()

    def test_pipe_failure_cleans_up(self):
        with patch('music.player.ytdl.spawn_pipe') as spawn, \
                patch.object(MusicPlayer, '_opus_source', side_effect=RuntimeError('test')):
            spawn.return_value.poll.return_value = None
            with self.assertRaises(RuntimeError):
                asyncio.run(MusicPlayer._make_source(Prepared('pipe', url='unused')))
            spawn.return_value.kill.assert_called_once()
            spawn.return_value.stdout.close.assert_called_once()


if __name__ == '__main__':
    unittest.main(verbosity=2)
