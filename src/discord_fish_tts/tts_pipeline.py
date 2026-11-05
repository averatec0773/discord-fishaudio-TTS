"""Per-guild TTS pipeline: FIFO queue, dedicated worker, skip semantics."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import queue as _queue
from dataclasses import dataclass, replace
from typing import Optional

import discord

from .audio_source import StreamingOpusAudioSource
from .config_store import ConfigStore
from .fish_client import FishConfig, stream_opus_ogg
from .ogg_demux import OggDemuxer
from .voice_manager import VoiceManager

log = logging.getLogger(__name__)

QUEUE_MAXSIZE_DEFAULT = 20
PLAY_TIMEOUT_S = 120
FRAME_QUEUE_MAXSIZE = 200
DAVE_READY_TIMEOUT_S = 5.0


async def wait_for_dave_ready(voice_client, timeout: float = DAVE_READY_TIMEOUT_S) -> bool:
    """Wait until the voice connection can DAVE-encrypt audio.

    discord.py sends frames unencrypted while a DAVE (E2EE) session exists but
    has no key yet. Discord clients drop those frames, so the bot shows the
    speaking indicator but nobody hears anything. This happens briefly right
    after joining, and permanently with an incompatible `davey` build.
    Returns True when ready (or when DAVE is not in use), False on timeout.
    """
    conn = getattr(voice_client, "_connection", None)
    if conn is None or getattr(conn, "dave_session", None) is None:
        return True
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not getattr(conn, "can_encrypt", True):
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(0.05)
    return True


async def _put_with_backpressure(q: "_queue.Queue", item: bytes) -> None:
    """Put into a sync queue from async; if full, briefly yield and retry.

    Avoids blocking the event loop on a full queue, while still applying
    backpressure to the producer so we don't OOM if Discord stalls.
    """
    while True:
        try:
            q.put_nowait(item)
            return
        except _queue.Full:
            await asyncio.sleep(0.005)


@dataclass
class TTSJob:
    text: str
    user_id: int
    guild_id: int
    voice_channel_id: int


class GuildTTSPipeline:
    def __init__(
        self,
        guild_id: int,
        fish_cfg: FishConfig,
        voice_manager: VoiceManager,
        loop: asyncio.AbstractEventLoop,
        config_store: ConfigStore,
        queue_maxsize: int = QUEUE_MAXSIZE_DEFAULT,
    ) -> None:
        self.guild_id = guild_id
        # Base FishConfig contains static fields (api_key, model, latency, base_url).
        # voice_id is taken from config_store at job time for live updates.
        self._fish_cfg = fish_cfg
        self._voice_manager = voice_manager
        self._loop = loop
        self._config_store = config_store
        self._queue: asyncio.Queue[TTSJob] = asyncio.Queue(maxsize=queue_maxsize)
        self._worker: Optional[asyncio.Task] = None
        self._current_task: Optional[asyncio.Task] = None
        self._stop_flag = False

    async def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._run(), name=f"tts-worker-{self.guild_id}")

    async def stop(self) -> None:
        self._stop_flag = True
        if self._current_task and not self._current_task.done():
            self._current_task.cancel()
        if self._worker:
            self._worker.cancel()
            with contextlib.suppress(BaseException):
                await self._worker
            self._worker = None

    async def enqueue(self, job: TTSJob) -> None:
        self._queue.put_nowait(job)  # raises asyncio.QueueFull on overflow

    async def skip_current(self) -> bool:
        t = self._current_task
        if t and not t.done():
            t.cancel()
            return True
        return False

    async def _run(self) -> None:
        while not self._stop_flag:
            try:
                job = await self._queue.get()
            except asyncio.CancelledError:
                return
            try:
                self._current_task = asyncio.create_task(
                    self._play_one(job), name=f"tts-job-{self.guild_id}"
                )
                await self._current_task
            except asyncio.CancelledError:
                # /skip path
                pass
            except Exception:
                log.exception("TTS job failed")
            finally:
                self._current_task = None
                self._queue.task_done()

    async def _play_one(self, job: TTSJob) -> None:
        # Manual-summon model: do NOT move the bot from /say. The bot must
        # already be present in the requested voice channel (placed there
        # by /join). If not, fail this job loudly so the worker logs it
        # and the user can be informed by the command layer.
        voice_client = self._voice_manager.get_voice_client_in_channel(
            job.guild_id, job.voice_channel_id
        )
        if voice_client is None:
            raise RuntimeError(
                "bot is not in the requested voice channel; /join required"
            )

        # Read the voice at job time so /voice set takes effect for queued
        # jobs: the speaker's own voice, else the default voice.
        voice_id = self._config_store.get_voice_for(job.user_id)
        if not voice_id:
            raise RuntimeError(
                "no voice configured; run /voice set <id> (or owner: /voice default <id>)"
            )
        fish_cfg = replace(self._fish_cfg, voice_id=voice_id)

        # Thread-safe sync queue: discord.py's player thread reads from it
        # via StreamingOpusAudioSource.read(). Using asyncio.Queue here would
        # require asyncio.run_coroutine_threadsafe in read(), whose orphaned
        # get() coroutines silently consume packets when read() times out.
        frame_queue: _queue.Queue[bytes | None] = _queue.Queue(
            maxsize=FRAME_QUEUE_MAXSIZE
        )
        demux = OggDemuxer()
        source = StreamingOpusAudioSource(frame_queue)

        async def producer() -> None:
            try:
                async for chunk in stream_opus_ogg(job.text, fish_cfg):
                    demux.feed(chunk)
                    for pkt in demux.packets():
                        # put() blocks if full, but in async context we need
                        # to yield. Use put_nowait with a tiny retry loop.
                        await _put_with_backpressure(frame_queue, pkt)
                for pkt in demux.flush():
                    await _put_with_backpressure(frame_queue, pkt)
            finally:
                # EOF marker. Use put_nowait; if queue is full, drop (but
                # producer already finished, so consumer will hit underrun
                # silence and we can't help it).
                try:
                    frame_queue.put_nowait(None)
                except _queue.Full:
                    pass

        producer_task = asyncio.create_task(producer(), name="tts-producer")

        play_done = asyncio.Event()

        def after(error: Optional[Exception]) -> None:
            if error is not None:
                log.warning("voice_client.play after-error: %r", error)
            self._loop.call_soon_threadsafe(play_done.set)

        if not await wait_for_dave_ready(voice_client):
            log.warning(
                "DAVE E2EE session has no encryption key after %.0fs; listeners "
                "will not hear audio. Check the installed `davey` version.",
                DAVE_READY_TIMEOUT_S,
            )

        try:
            voice_client.play(source, after=after)
            await asyncio.wait_for(play_done.wait(), timeout=PLAY_TIMEOUT_S)
        finally:
            producer_task.cancel()
            with contextlib.suppress(BaseException):
                await producer_task
            try:
                if voice_client.is_playing():
                    voice_client.stop()
            except Exception:
                pass


class PipelineRegistry:
    def __init__(
        self,
        fish_cfg: FishConfig,
        voice_manager: VoiceManager,
        loop: asyncio.AbstractEventLoop,
        config_store: ConfigStore,
    ) -> None:
        self._fish_cfg = fish_cfg
        self._voice_manager = voice_manager
        self._loop = loop
        self._config_store = config_store
        self._pipelines: dict[int, GuildTTSPipeline] = {}

    def get_or_create(self, guild_id: int) -> GuildTTSPipeline:
        p = self._pipelines.get(guild_id)
        if p is None:
            p = GuildTTSPipeline(
                guild_id, self._fish_cfg, self._voice_manager,
                self._loop, self._config_store,
            )
            self._pipelines[guild_id] = p
            asyncio.create_task(p.start())
        return p

    async def shutdown_all(self) -> None:
        for p in list(self._pipelines.values()):
            await p.stop()
        self._pipelines.clear()
