"""Bot entry point.

Required env (secrets only):
    DISCORD_TOKEN          — Discord bot token
    FISH_AUDIO_API_KEY     — Fish Audio API key

Optional env:
    DATA_DIR=data          — where config.json lives
    LOG_LEVEL=INFO

Everything else (voice_id, whitelist, etc.) is managed at runtime via
slash commands. Owner identity is derived from Discord application
metadata (`bot.application.owner.id`) — no env var required.

Backward compatibility: legacy v1.1 env vars (FISH_VOICE_ID,
ALLOWED_GUILDS, BOT_OWNER, WHITELIST_PATH) are still read on first
startup and migrated into the new `data/config.json`. After migration
they're ignored.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Optional

import discord
from discord.ext import commands
from dotenv import load_dotenv

from .commands import TTSCog
from .config_store import ConfigStore
from .fish_client import FishConfig
from .tts_pipeline import PipelineRegistry
from .voice_manager import VoiceManager

log = logging.getLogger(__name__)


def _required(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise SystemExit(f"environment variable {name} is required")
    return val


def _int_list_env(name: str) -> list[int]:
    """Comma/space separated Discord IDs, e.g. AUTO_TTS_CHANNELS=123,456."""
    out: list[int] = []
    for part in os.environ.get(name, "").replace(",", " ").split():
        try:
            out.append(int(part))
        except ValueError:
            log.warning("ignoring non-numeric id %r in %s", part, name)
    return out


def _migrate_legacy_env(config: ConfigStore, data_dir: Path) -> None:
    """v1.1 → v1.2 migration. Run once on every startup; idempotent.

    Migrations:
      - FISH_VOICE_ID env → config.voice_id
      - data/whitelist.json (legacy v1.1 file) → config.whitelist
    """
    legacy_voice = os.environ.get("FISH_VOICE_ID", "").strip()
    if legacy_voice and config.get_voice_id() is None:
        config.set_voice_id(legacy_voice)
        log.info("migrated FISH_VOICE_ID env var into config.json")

    legacy_wl = data_dir / "whitelist.json"
    if legacy_wl.exists() and not config.list_users():
        try:
            import json
            data = json.loads(legacy_wl.read_text())
            users = data.get("allowed_users", [])
            for u in users:
                config.add_user(int(u))
            if users:
                log.info("migrated %d users from legacy whitelist.json", len(users))
        except Exception:
            log.exception("failed to migrate legacy whitelist.json (ignoring)")


async def amain() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    discord_token = _required("DISCORD_TOKEN")
    fish_api_key = _required("FISH_AUDIO_API_KEY")
    data_dir = Path(os.environ.get("DATA_DIR", "data"))
    config_path = data_dir / "config.json"

    config = ConfigStore(config_path, default_auto_channels=_int_list_env("AUTO_TTS_CHANNELS"))
    _migrate_legacy_env(config, data_dir)

    # Static FishConfig parts; voice_id is dynamic per-job, read from config.
    fish_cfg = FishConfig(
        api_key=fish_api_key,
        voice_id="",  # placeholder; replaced at job time
        model=os.environ.get("FISH_MODEL", "s2-pro"),
        latency=os.environ.get("FISH_LATENCY", "low"),
    )
    max_text_len = int(os.environ.get("MAX_TEXT_LEN", "200"))
    # Auto-leave voice when alone for N minutes. 0 disables.
    idle_timeout_s = int(os.environ.get("IDLE_TIMEOUT_MINUTES", "5")) * 60

    intents = discord.Intents.default()
    intents.message_content = True
    intents.voice_states = True
    intents.members = True

    bot = commands.Bot(command_prefix="!", intents=intents)
    voice_mgr = VoiceManager(bot)
    registry = PipelineRegistry(fish_cfg, voice_mgr, asyncio.get_running_loop(), config)

    # Owner is filled in at on_ready when bot.application is available.
    state: dict[str, Optional[int]] = {"owner_id": None}

    def owner_id_provider() -> Optional[int]:
        return state["owner_id"]

    @bot.event
    async def on_ready() -> None:
        # Discord populates bot.application after login. Get the app owner.
        app = await bot.application_info()
        # `app.owner` may be a Team or User. For Team-owned apps, use the
        # team's owner; for User-owned apps, use the user directly.
        if app.team is not None and app.team.owner_id is not None:
            owner_id = int(app.team.owner_id)
        else:
            owner_id = int(app.owner.id)
        state["owner_id"] = owner_id
        log.info(
            "logged in as %s (id=%s); owner=%s; voice=%s; whitelist=%d users",
            bot.user, getattr(bot.user, "id", "?"),
            owner_id, config.get_voice_id() or "(unset)",
            len(config.list_users()),
        )
        synced = await bot.tree.sync()
        log.info("synced %d slash commands", len(synced))

        # First-run DM to owner.
        if config.is_first_run():
            try:
                user = bot.get_user(owner_id) or await bot.fetch_user(owner_id)
                if user is not None:
                    voice_hint = (
                        f"✅ Default voice already set: `{config.get_voice_id()}`."
                        if config.get_voice_id()
                        else "⚠️  **No default voice yet.** Run `/voice default <link>`, or `/voice set <link>` for yourself.\n"
                             "   (Browse voices at https://fish.audio/app/discovery)"
                    )
                    await user.send(
                        "👋 Welcome — you're the owner of this Fish Audio TTS bot.\n\n"
                        f"{voice_hint}\n\n"
                        "**Setup checklist:**\n"
                        "1. `/voice default <link>` — default voice; everyone can `/voice set` their own\n"
                        "2. `/whitelist add @friend` — grant access\n"
                        "3. Invite me to a server → join voice → `/join` → `/say hello`\n\n"
                        "Run `/help` anytime."
                    )
                    config.mark_first_run_done()
                    log.info("first-run welcome DM sent to owner")
            except discord.Forbidden:
                log.warning(
                    "could not DM owner (Forbidden — DMs may be closed). "
                    "Run /help in a server instead."
                )
            except Exception:
                log.exception("first-run DM failed; will retry next startup")

    await bot.add_cog(TTSCog(
        bot=bot,
        voice_manager=voice_mgr,
        registry=registry,
        config_store=config,
        owner_id_provider=owner_id_provider,
        max_text_len=max_text_len,
        idle_timeout_s=idle_timeout_s,
        fish_api_key=fish_api_key,
        owner_contact=os.environ.get("OWNER_CONTACT"),
    ))

    try:
        await bot.start(discord_token)
    finally:
        await registry.shutdown_all()
        await bot.close()


def cli() -> None:
    for candidate in (Path.cwd() / ".env.local", Path(__file__).resolve().parents[2] / ".env.local"):
        if candidate.exists():
            load_dotenv(candidate)
            break
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    cli()
