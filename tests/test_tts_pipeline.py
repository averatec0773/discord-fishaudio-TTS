import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from discord_fish_tts.tts_pipeline import (
    GuildTTSPipeline, PipelineRegistry, TTSJob,
)
from discord_fish_tts.config_store import ConfigStore
from discord_fish_tts.fish_client import FishConfig


@pytest.fixture
def fish_cfg():
    return FishConfig(api_key="x", voice_id="y")


@pytest.fixture
def cfg_store(tmp_path):
    s = ConfigStore(tmp_path / "config.json")
    s.set_voice_id("voice-from-config")
    return s


def make_voice_mgr():
    vm = MagicMock()
    vc = MagicMock()
    vc.is_playing = MagicMock(return_value=False)
    vc.play = MagicMock()
    vc.stop = MagicMock()
    # New manual-summon API: pipeline expects bot to already be in the channel
    vm.get_voice_client_in_channel = MagicMock(return_value=vc)
    vm._vc = vc
    return vm


@pytest.mark.asyncio
async def test_jobs_run_in_fifo_order(fish_cfg, cfg_store):
    vm = make_voice_mgr()
    loop = asyncio.get_running_loop()
    pipeline = GuildTTSPipeline(1, fish_cfg, vm, loop, cfg_store)

    played: list[str] = []

    async def fake_stream(text, cfg):
        played.append(text)
        # Yield one fake chunk so producer terminates
        yield b""

    # Simulate voice_client.play immediately calling 'after'
    def play(source, after):
        loop.call_soon(after, None)
    vm._vc.play = MagicMock(side_effect=play)

    with patch("discord_fish_tts.tts_pipeline.stream_opus_ogg", fake_stream):
        await pipeline.start()
        try:
            await pipeline.enqueue(TTSJob("first", 1, 1, 1))
            await pipeline.enqueue(TTSJob("second", 1, 1, 1))
            await pipeline.enqueue(TTSJob("third", 1, 1, 1))
            # Wait until queue drains
            await asyncio.wait_for(pipeline._queue.join(), timeout=5.0)
        finally:
            await pipeline.stop()

    assert played == ["first", "second", "third"]


@pytest.mark.asyncio
async def test_skip_cancels_current(fish_cfg, cfg_store):
    vm = make_voice_mgr()
    loop = asyncio.get_running_loop()
    pipeline = GuildTTSPipeline(1, fish_cfg, vm, loop, cfg_store)

    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow_stream(text, cfg):
        started.set()
        try:
            await asyncio.sleep(10)
            yield b""
        except asyncio.CancelledError:
            cancelled.set()
            raise

    # play() never auto-completes; we'll skip
    vm._vc.play = MagicMock()

    with patch("discord_fish_tts.tts_pipeline.stream_opus_ogg", slow_stream):
        await pipeline.start()
        try:
            await pipeline.enqueue(TTSJob("hang", 1, 1, 1))
            await asyncio.wait_for(started.wait(), timeout=2.0)
            ok = await pipeline.skip_current()
            assert ok is True
            await asyncio.wait_for(cancelled.wait(), timeout=1.0)
        finally:
            await pipeline.stop()


@pytest.mark.asyncio
async def test_skip_returns_false_when_idle(fish_cfg, cfg_store):
    vm = make_voice_mgr()
    loop = asyncio.get_running_loop()
    pipeline = GuildTTSPipeline(1, fish_cfg, vm, loop, cfg_store)
    await pipeline.start()
    try:
        ok = await pipeline.skip_current()
        assert ok is False
    finally:
        await pipeline.stop()


@pytest.mark.asyncio
async def test_pipeline_registry_lazy_create(fish_cfg, cfg_store):
    vm = make_voice_mgr()
    loop = asyncio.get_running_loop()
    reg = PipelineRegistry(fish_cfg, vm, loop, cfg_store)
    p1 = reg.get_or_create(1)
    p2 = reg.get_or_create(1)
    p3 = reg.get_or_create(2)
    assert p1 is p2
    assert p1 is not p3
    await reg.shutdown_all()


# ---- DAVE (E2EE) readiness ----

def make_dave_vc(ready_after: float | None):
    """Voice client whose DAVE session becomes able to encrypt after `ready_after`
    seconds (None = never)."""
    vc = MagicMock()
    conn = MagicMock()
    conn.dave_session = object()
    start = asyncio.get_running_loop().time()
    type(conn).can_encrypt = property(
        lambda self: ready_after is not None
        and asyncio.get_running_loop().time() - start >= ready_after
    )
    vc._connection = conn
    return vc


@pytest.mark.asyncio
async def test_wait_for_dave_ready_waits_for_key():
    from discord_fish_tts.tts_pipeline import wait_for_dave_ready
    vc = make_dave_vc(ready_after=0.2)
    t0 = asyncio.get_running_loop().time()
    assert await wait_for_dave_ready(vc, timeout=2.0) is True
    assert asyncio.get_running_loop().time() - t0 >= 0.2


@pytest.mark.asyncio
async def test_wait_for_dave_ready_times_out_without_key():
    from discord_fish_tts.tts_pipeline import wait_for_dave_ready
    assert await wait_for_dave_ready(make_dave_vc(ready_after=None), timeout=0.2) is False


@pytest.mark.asyncio
async def test_wait_for_dave_ready_without_dave_session():
    from discord_fish_tts.tts_pipeline import wait_for_dave_ready
    vc = MagicMock()
    vc._connection.dave_session = None
    assert await wait_for_dave_ready(vc, timeout=0.1) is True


@pytest.mark.asyncio
async def test_play_waits_until_dave_can_encrypt(fish_cfg, cfg_store):
    vm = make_voice_mgr()
    vc = make_dave_vc(ready_after=0.3)
    vc.is_playing = MagicMock(return_value=False)
    vm.get_voice_client_in_channel = MagicMock(return_value=vc)
    loop = asyncio.get_running_loop()
    pipeline = GuildTTSPipeline(1, fish_cfg, vm, loop, cfg_store)
    t0 = loop.time()
    played_at: list[float] = []

    async def fake_stream(text, cfg):
        yield b""

    def play(source, after):
        assert vc._connection.can_encrypt, "played before DAVE key was ready"
        played_at.append(loop.time() - t0)
        loop.call_soon(after, None)
    vc.play = MagicMock(side_effect=play)

    with patch("discord_fish_tts.tts_pipeline.stream_opus_ogg", fake_stream):
        await pipeline.start()
        try:
            await pipeline.enqueue(TTSJob("hello", 1, 1, 1))
            await asyncio.wait_for(pipeline._queue.join(), timeout=5.0)
        finally:
            await pipeline.stop()

    assert played_at and played_at[0] >= 0.3
