"""Discord slash commands, auto-TTS listener and DM handler.

Authorization model:
  - Owner is derived from `bot.application.owner.id` and passed in at
    construction time. Owner has full rights including whitelist management.
  - Whitelisted users (in ConfigStore) can use voice/TTS commands, pick their
    own voice and use /toggle auto-speak.
  - Everyone else is told they have no access and who to contact.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import urllib.error
import urllib.request

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .config_store import ConfigStore, normalize_voice_id
from .tts_pipeline import PipelineRegistry, TTSJob
from .validators import ValidationError, check_text
from .voice_manager import VoiceManager

log = logging.getLogger(__name__)

# Catches messages that look like a slash command typed as text
# (e.g. "/help" or "/say hello"). Lets users type things starting
# with a single non-alpha character through ("/dev/null", "//path").
_SLASH_TEXT_RE = re.compile(r'^\s*/[a-zA-Z]\w*')
_URL_RE = re.compile(r"https?://\S+")
_CUSTOM_EMOJI_RE = re.compile(r"<a?:(\w+):\d+>")
_SPOILER_RE = re.compile(r"\|\|.+?\|\|", re.S)

VOICE_LIBRARY_URL = "https://fish.audio/app/discovery"


def speakable_text(raw: str, max_len: int) -> str:
    """Turn a chat message (already clean_content) into something worth speaking."""
    text = _CUSTOM_EMOJI_RE.sub(r"\1", raw)
    text = _SPOILER_RE.sub(" spoiler ", text)
    text = _URL_RE.sub(" link ", text)
    text = " ".join(text.split())
    if len(text) > max_len:
        cut = text[:max_len]
        text = cut.rsplit(" ", 1)[0] if " " in cut[max_len // 2:] else cut
    return text.strip()


def _lookup_voice_sync(voice_id: str, api_key: str | None) -> tuple[bool | None, str | None]:
    """(exists, title). exists=None means Fish could not be reached."""
    headers = {"User-Agent": "discord-fish-tts"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(f"https://api.fish.audio/model/{voice_id}", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.load(r)
            return True, data.get("title")
    except urllib.error.HTTPError as e:
        if e.code in (400, 404, 422):
            return False, None
        return None, None
    except Exception:
        return None, None


class TTSCog(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
        voice_manager: VoiceManager,
        registry: PipelineRegistry,
        config_store: ConfigStore,
        owner_id_provider,  # () -> int | None — late-bound (owner known after on_ready)
        max_text_len: int,
        idle_timeout_s: int = 300,
        fish_api_key: str | None = None,
        owner_contact: str | None = None,  # e.g. "@handle", shown to users without access
    ) -> None:
        self.bot = bot
        self.voice_manager = voice_manager
        self.registry = registry
        self.config = config_store
        self._owner_id_provider = owner_id_provider
        self.max_text_len = max_text_len
        self._idle_timeout_s = idle_timeout_s
        self._fish_api_key = fish_api_key
        self._owner_contact = (owner_contact or "").strip() or None
        self._voice_titles: dict[str, str] = {}
        self._join_locks: dict[int, asyncio.Lock] = {}
        # guild_id → first time bot found itself alone in voice (epoch s)
        self._empty_since: dict[int, float] = {}
        if idle_timeout_s > 0:
            self._idle_check.start()

    def cog_unload(self) -> None:
        self._idle_check.cancel()

    @tasks.loop(seconds=60)
    async def _idle_check(self) -> None:
        """Disconnect from voice channels that have had no human listeners
        for `_idle_timeout_s` seconds. Prevents wasted Fish API quota and
        leftover voice connections from forgotten sessions."""
        try:
            now = time.time()
            for guild in list(self.bot.guilds):
                vc = guild.voice_client
                if vc is None:
                    self._empty_since.pop(guild.id, None)
                    continue
                # Count human (non-bot) members in the bot's channel
                channel = vc.channel
                humans = [m for m in channel.members if not m.bot]
                if humans:
                    self._empty_since.pop(guild.id, None)
                    continue
                first_empty = self._empty_since.get(guild.id)
                if first_empty is None:
                    self._empty_since[guild.id] = now
                    continue
                if now - first_empty >= self._idle_timeout_s:
                    log.info(
                        "guild %d (%s): voice channel #%s empty for %ds, leaving",
                        guild.id, guild.name, channel.name,
                        int(now - first_empty),
                    )
                    try:
                        await vc.disconnect(force=False)
                    except Exception:
                        log.exception("idle disconnect failed for guild %d", guild.id)
                    finally:
                        self._empty_since.pop(guild.id, None)
        except Exception:
            log.exception("idle check loop iteration failed")

    @_idle_check.before_loop
    async def _idle_check_before(self) -> None:
        await self.bot.wait_until_ready()

    # ---- helpers ----

    def _owner_id(self) -> int | None:
        return self._owner_id_provider()

    def _is_owner(self, user_id: int) -> bool:
        oid = self._owner_id()
        return oid is not None and user_id == oid

    def _is_allowed(self, user_id: int) -> bool:
        return self._is_owner(user_id) or self.config.is_user_whitelisted(user_id)

    def _bot_name(self) -> str:
        return self.bot.user.name if self.bot.user else "this bot"

    def _no_access_message(self) -> str:
        oid = self._owner_id()
        handle = self._owner_contact
        if oid and handle:
            contact = f"<@{oid}> ({handle})"
        else:
            contact = f"<@{oid}>" if oid else (handle or "the bot owner")
        return f"🚫 You do not have access to **{self._bot_name()}**. Contact {contact} to get access."

    def _user_voice_channel(self, member: discord.Member) -> discord.VoiceChannel | None:
        if member.voice and isinstance(member.voice.channel, discord.VoiceChannel):
            return member.voice.channel
        return None

    def _find_user_voice_anywhere(
        self, user_id: int
    ) -> tuple[int, int] | None:
        """For DM: scan all guilds the bot is in for this user's voice channel."""
        for guild in self.bot.guilds:
            member = guild.get_member(user_id)
            if member and member.voice and member.voice.channel:
                return guild.id, member.voice.channel.id
        return None

    def _channels_text(self) -> str:
        chans = self.config.list_auto_tts_channels()
        return ", ".join(f"<#{c}>" for c in chans) if chans else "*(no auto-speak channels set)*"

    async def _lookup_voice(self, voice_id: str) -> tuple[bool | None, str | None]:
        exists, title = await asyncio.to_thread(_lookup_voice_sync, voice_id, self._fish_api_key)
        if title:
            self._voice_titles[voice_id] = title
        return exists, title

    async def _voice_label(self, voice_id: str | None) -> str:
        if not voice_id:
            return "*(none)*"
        title = self._voice_titles.get(voice_id)
        if title is None:
            _, title = await self._lookup_voice(voice_id)
        return f"**{title}** (`{voice_id}`)" if title else f"`{voice_id}`"

    async def _deny_if_not_allowed(self, interaction: discord.Interaction) -> bool:
        if not self._is_allowed(interaction.user.id):
            await interaction.response.send_message(self._no_access_message(), ephemeral=True)
            return True
        return False

    async def _deny_if_not_owner(self, interaction: discord.Interaction) -> bool:
        if not self._is_owner(interaction.user.id):
            await interaction.response.send_message(
                "Only the bot owner can use this command.", ephemeral=True
            )
            return True
        return False

    # ---- /say ----

    @app_commands.command(name="say", description="Speak text in the bot's current voice channel.")
    @app_commands.describe(text="Text to speak (max 200 chars)")
    async def say(self, interaction: discord.Interaction, text: str) -> None:
        log.info("/say from %s in guild %s: %r", interaction.user, interaction.guild_id, text[:60])
        try:
            text = check_text(text, max_len=self.max_text_len)
        except ValidationError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return

        if await self._deny_if_not_allowed(interaction):
            return

        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Run /say from a server.", ephemeral=True)
            return

        if self.config.get_voice_for(interaction.user.id) is None:
            await interaction.response.send_message(
                f"No voice set. Pick one with `/voice set <link>` — browse voices at <{VOICE_LIBRARY_URL}>.",
                ephemeral=True,
            )
            return

        user_ch = self._user_voice_channel(interaction.user)
        if user_ch is None:
            log.info("/say rejected: user not in a voice channel")
            await interaction.response.send_message(
                "Join a voice channel first.", ephemeral=True,
            )
            return

        bot_ch_id = self.voice_manager.current_channel_id(interaction.guild_id)  # type: ignore[arg-type]
        if bot_ch_id is None:
            log.info("/say rejected: bot not in voice")
            await interaction.response.send_message(
                "I'm not in voice. Run `/join` to summon me.", ephemeral=True,
            )
            return
        if bot_ch_id != user_ch.id:
            log.info("/say rejected: bot in channel %s, user in %s", bot_ch_id, user_ch.id)
            bot_ch = interaction.guild.get_channel(bot_ch_id) if interaction.guild else None
            ch_name = bot_ch.name if bot_ch else str(bot_ch_id)
            guild_name = interaction.guild.name if interaction.guild else "this server"
            await interaction.response.send_message(
                f"I'm in **#{ch_name}** of **{guild_name}**. "
                f"Run `/join` from your channel to bring me over.",
                ephemeral=True,
            )
            return

        pipeline = self.registry.get_or_create(interaction.guild_id)  # type: ignore[arg-type]
        try:
            await pipeline.enqueue(TTSJob(
                text=text,
                user_id=interaction.user.id,
                guild_id=interaction.guild_id,  # type: ignore[arg-type]
                voice_channel_id=user_ch.id,
            ))
        except asyncio.QueueFull:
            await interaction.response.send_message(
                "Queue is full, try again in a moment.", ephemeral=True
            )
            return

        log.info("/say queued for channel %s", user_ch.id)
        await interaction.response.send_message("Queued.", ephemeral=True)

    # ---- /join /leave /skip ----

    @app_commands.command(
        name="join",
        description="Summon the bot to a voice channel (defaults to yours).",
    )
    @app_commands.describe(
        channel="Voice channel to summon to. Leave empty to use your current channel.",
    )
    async def join(
        self,
        interaction: discord.Interaction,
        channel: discord.VoiceChannel | None = None,
    ) -> None:
        if await self._deny_if_not_allowed(interaction):
            return
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "Run /join from a server, not in DM.", ephemeral=True,
            )
            return

        target = channel
        if target is None:
            if not isinstance(interaction.user, discord.Member):
                await interaction.response.send_message(
                    "Run /join from a server.", ephemeral=True,
                )
                return
            target = self._user_voice_channel(interaction.user)
            if target is None:
                await interaction.response.send_message(
                    "Join a voice channel first, or specify one: `/join channel:<name>`.",
                    ephemeral=True,
                )
                return
        elif target.guild.id != interaction.guild_id:
            await interaction.response.send_message(
                "That voice channel is not in this server.", ephemeral=True,
            )
            return

        # Refuse to hijack: if bot is in another channel of this guild AND
        # currently playing, force the requester to /skip first to avoid
        # silently teleporting audio away from listeners in that channel.
        current_ch_id = self.voice_manager.current_channel_id(interaction.guild_id)
        if (current_ch_id is not None and current_ch_id != target.id):
            guild = interaction.guild
            vc = guild.voice_client if guild else None
            if vc is not None and vc.is_playing():
                cur_ch = guild.get_channel(current_ch_id) if guild else None
                cur_name = cur_ch.name if cur_ch else str(current_ch_id)
                await interaction.response.send_message(
                    f"I'm currently playing TTS in **#{cur_name}**. "
                    f"Run `/skip` first if you want to move me.",
                    ephemeral=True,
                )
                return

        await self.voice_manager.ensure_in_channel(interaction.guild_id, target.id)
        await interaction.response.send_message(f"Joined {target.name}.", ephemeral=True)

    @app_commands.command(name="leave", description="Disconnect the bot from its voice channel.")
    async def leave(self, interaction: discord.Interaction) -> None:
        if await self._deny_if_not_allowed(interaction):
            return
        ok = await self.voice_manager.leave(interaction.guild_id)  # type: ignore[arg-type]
        msg = "Left the voice channel." if ok else "I'm not in a voice channel here."
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="skip", description="Skip the currently playing TTS.")
    async def skip(self, interaction: discord.Interaction) -> None:
        if await self._deny_if_not_allowed(interaction):
            return
        pipeline = self.registry.get_or_create(interaction.guild_id)  # type: ignore[arg-type]
        ok = await pipeline.skip_current()
        msg = "Skipped." if ok else "Nothing playing."
        await interaction.response.send_message(msg, ephemeral=True)

    # ---- /toggle ----

    @app_commands.command(
        name="toggle",
        description="Auto-speak everything you type in the auto-speak channel(s) while you're in voice.",
    )
    async def toggle(self, interaction: discord.Interaction) -> None:
        if await self._deny_if_not_allowed(interaction):
            return
        uid = interaction.user.id
        enabled = not self.config.is_auto_tts(uid)
        self.config.set_auto_tts(uid, enabled)
        log.info("/toggle: %s auto-speak %s", interaction.user, "on" if enabled else "off")
        if enabled:
            voice = await self._voice_label(self.config.get_voice_for(uid))
            msg = (
                f"🔊 Auto-speak **on**. Messages you type in {self._channels_text()} are spoken "
                f"while you're in a voice channel (I'll join you if I'm not in voice yet).\n"
                f"Your voice: {voice}. Run `/toggle` again to turn it off."
            )
            if self.config.get_voice_for(uid) is None:
                msg += f"\n⚠️ You have no voice yet — `/voice set <link>` (browse <{VOICE_LIBRARY_URL}>)."
            if not self.config.list_auto_tts_channels():
                msg += "\n⚠️ No auto-speak channels are set yet — the owner can add one with `/ttschannel add`."
        else:
            msg = "🔇 Auto-speak **off**."
        await interaction.response.send_message(msg, ephemeral=True)

    # ---- /voice ----

    voice_group = app_commands.Group(
        name="voice",
        description="Pick your own Fish Audio voice.",
        allowed_contexts=app_commands.AppCommandContext(
            guild=True, dm_channel=True, private_channel=True,
        ),
    )

    @voice_group.command(name="show", description="Show your voice and the default voice.")
    async def voice_show(self, interaction: discord.Interaction) -> None:
        if await self._deny_if_not_allowed(interaction):
            return
        uid = interaction.user.id
        mine = self.config.get_user_voice(uid)
        default = self.config.get_voice_id()
        lines = [
            f"Your voice: {await self._voice_label(mine) if mine else '*(not set — using the default)*'}",
            f"Default voice: {await self._voice_label(default)}",
            f"Auto-speak (`/toggle`): **{'on' if self.config.is_auto_tts(uid) else 'off'}**",
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @voice_group.command(name="set", description="Set YOUR voice (paste a fish.audio voice link or ID).")
    @app_commands.describe(voice="fish.audio voice link (…/app/m/<id>) or the voice ID")
    async def voice_set(self, interaction: discord.Interaction, voice: str) -> None:
        if await self._deny_if_not_allowed(interaction):
            return
        vid = normalize_voice_id(voice)
        if not vid:
            await interaction.response.send_message("Paste a fish.audio voice link or ID.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        exists, title = await self._lookup_voice(vid)
        if exists is False:
            await interaction.followup.send(
                f"❌ Fish Audio has no voice `{vid}`. Open a voice at <{VOICE_LIBRARY_URL}> and copy its link.",
                ephemeral=True,
            )
            return
        self.config.set_user_voice(interaction.user.id, vid)
        log.info("/voice set: %s -> %s (%s)", interaction.user, vid, title)
        note = "" if exists else " (couldn't reach Fish Audio to verify it, saved anyway)"
        await interaction.followup.send(
            f"✅ Your voice is now {f'**{title}** (`{vid}`)' if title else f'`{vid}`'}{note}.",
            ephemeral=True,
        )

    @voice_group.command(name="reset", description="Go back to the default voice.")
    async def voice_reset(self, interaction: discord.Interaction) -> None:
        if await self._deny_if_not_allowed(interaction):
            return
        self.config.set_user_voice(interaction.user.id, None)
        default = await self._voice_label(self.config.get_voice_id())
        await interaction.response.send_message(f"Reset. You'll use the default voice: {default}.", ephemeral=True)

    @voice_group.command(name="default", description="Set the default voice for everyone without their own (owner only).")
    @app_commands.describe(voice="fish.audio voice link or ID")
    async def voice_default(self, interaction: discord.Interaction, voice: str) -> None:
        if await self._deny_if_not_owner(interaction):
            return
        vid = normalize_voice_id(voice)
        if not vid:
            await interaction.response.send_message("Paste a fish.audio voice link or ID.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        exists, title = await self._lookup_voice(vid)
        if exists is False:
            await interaction.followup.send(f"❌ Fish Audio has no voice `{vid}`.", ephemeral=True)
            return
        self.config.set_voice_id(vid)
        await interaction.followup.send(
            f"✅ Default voice is now {f'**{title}** (`{vid}`)' if title else f'`{vid}`'}.", ephemeral=True,
        )

    # ---- /ttschannel ----

    ttschannel_group = app_commands.Group(
        name="ttschannel",
        description="Channels where /toggle auto-speak listens (owner only).",
        guild_only=True,
    )

    @ttschannel_group.command(name="add", description="Let /toggle auto-speak work in a text channel.")
    async def ttschannel_add(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        if await self._deny_if_not_owner(interaction):
            return
        added = self.config.add_auto_tts_channel(channel.id)
        msg = f"Added {channel.mention}." if added else f"{channel.mention} is already an auto-speak channel."
        await interaction.response.send_message(f"{msg}\nAuto-speak channels: {self._channels_text()}", ephemeral=True)

    @ttschannel_group.command(name="remove", description="Stop /toggle auto-speak in a text channel.")
    async def ttschannel_remove(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        if await self._deny_if_not_owner(interaction):
            return
        removed = self.config.remove_auto_tts_channel(channel.id)
        msg = f"Removed {channel.mention}." if removed else f"{channel.mention} wasn't an auto-speak channel."
        await interaction.response.send_message(f"{msg}\nAuto-speak channels: {self._channels_text()}", ephemeral=True)

    @ttschannel_group.command(name="list", description="List auto-speak channels.")
    async def ttschannel_list(self, interaction: discord.Interaction) -> None:
        if await self._deny_if_not_owner(interaction):
            return
        await interaction.response.send_message(f"Auto-speak channels: {self._channels_text()}", ephemeral=True)

    # ---- /whitelist ----

    whitelist_group = app_commands.Group(
        name="whitelist",
        description="Manage the user whitelist (owner only).",
        allowed_contexts=app_commands.AppCommandContext(
            guild=True, dm_channel=True, private_channel=True,
        ),
    )

    @whitelist_group.command(name="add", description="Add a user to the whitelist.")
    @app_commands.describe(user="The Discord user to add.")
    async def whitelist_add(
        self, interaction: discord.Interaction, user: discord.User
    ) -> None:
        if await self._deny_if_not_owner(interaction):
            return
        if self._is_owner(user.id):
            await interaction.response.send_message(
                f"{user.mention} is the owner — already always allowed.",
                ephemeral=True,
            )
            return
        added = self.config.add_user(user.id)
        if not added:
            await interaction.response.send_message(
                f"{user.mention} is already in the whitelist.", ephemeral=True,
            )
            return

        # Try welcome DM. Don't block the command on DM failure.
        welcome = (
            f"👋 You can use **{self._bot_name()}** now!\n\n"
            f"🗣️ Pick your own voice: browse <{VOICE_LIBRARY_URL}>, open a voice, copy its link, "
            "then run `/voice set <link>`.\n"
            "🎤 In voice: `/join` (summon me) → `/say hello`\n"
            f"⌨️ No mic? Run `/toggle` and everything you type in {self._channels_text()} "
            "is spoken while you're in voice.\n"
            "💬 Or DM me text and I'll speak it in your voice channel.\n\n"
            "Run `/help` anytime for the full command list."
        )
        dm_ok = True
        try:
            await user.send(welcome)
        except (discord.Forbidden, discord.HTTPException):
            dm_ok = False

        suffix = "" if dm_ok else " (could not DM them — they may have DMs disabled)"
        await interaction.response.send_message(
            f"Added {user.mention} to the whitelist.{suffix}",
            ephemeral=True,
        )

    @whitelist_group.command(name="remove", description="Remove a user from the whitelist.")
    @app_commands.describe(user="The Discord user to remove.")
    async def whitelist_remove(
        self, interaction: discord.Interaction, user: discord.User
    ) -> None:
        if await self._deny_if_not_owner(interaction):
            return
        removed = self.config.remove_user(user.id)
        msg = (
            f"Removed {user.mention} from the whitelist."
            if removed else f"{user.mention} is not in the whitelist."
        )
        await interaction.response.send_message(msg, ephemeral=True)

    @whitelist_group.command(name="list", description="List whitelisted users.")
    async def whitelist_list(self, interaction: discord.Interaction) -> None:
        if await self._deny_if_not_owner(interaction):
            return
        oid = self._owner_id()
        users = self.config.list_users()
        owner_line = f"Owner: <@{oid}>" if oid else "Owner: (unknown)"
        if not users:
            await interaction.response.send_message(
                f"{owner_line}\nWhitelist: (empty)", ephemeral=True,
            )
            return
        mentions = "\n".join(f"• <@{u}>" for u in users)
        await interaction.response.send_message(
            f"{owner_line}\nWhitelisted users:\n{mentions}", ephemeral=True,
        )

    # ---- /help ----

    @app_commands.command(name="help", description="Show how to use this bot.")
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def help_cmd(self, interaction: discord.Interaction) -> None:
        uid = interaction.user.id
        if not self._is_allowed(uid):
            await interaction.response.send_message(self._no_access_message(), ephemeral=True)
            return
        lines = [
            f"**{self._bot_name()}** — text-to-speech in voice channels.",
            "",
            "🗣️ **Your voice**",
            f"• Browse voices: <{VOICE_LIBRARY_URL}> — open one and copy its link",
            "• `/voice set <link>` — use that voice for everything you say",
            "• `/voice show` — your voice + auto-speak status · `/voice reset` — back to the default",
            "",
            "🎤 **In voice**",
            "• `/join [channel]` — bring me to a voice channel",
            "• `/say <text>` — I speak it (I must be in your voice channel)",
            "• `/skip` — stop the current line · `/leave` — disconnect me",
            "",
            "⌨️ **No mic?**",
            f"• `/toggle` — auto-speak everything you type in {self._channels_text()} while you're in voice",
            "",
            "💬 **DM me** any text and I'll speak it in your voice channel (I must already be there)",
        ]
        if self._is_owner(uid):
            lines += [
                "",
                "👑 **Owner**",
                "• `/voice default <link>` — default voice for people without their own",
                "• `/whitelist add|remove @user` · `/whitelist list`",
                "• `/ttschannel add|remove #channel` · `/ttschannel list` — where `/toggle` works",
            ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    # ---- message listener: auto-speak (guild) + DM speak ----

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if message.guild is not None:
            await self._handle_auto_tts(message)
            return
        await self._handle_dm(message)

    async def _handle_auto_tts(self, message: discord.Message) -> None:
        member = message.author
        if not isinstance(member, discord.Member):
            return
        if not self.config.is_auto_tts_channel(message.channel.id):
            return
        if not self.config.is_auto_tts(member.id) or not self._is_allowed(member.id):
            return
        user_ch = self._user_voice_channel(member)
        if user_ch is None:
            return  # toggle only works while the user is in voice
        if _SLASH_TEXT_RE.match(message.content):
            return
        if self.config.get_voice_for(member.id) is None:
            return
        text = speakable_text(message.clean_content, self.max_text_len)
        if not text:
            return

        guild = message.guild
        lock = self._join_locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            bot_ch_id = self.voice_manager.current_channel_id(guild.id)
            if bot_ch_id is None:
                try:
                    await self.voice_manager.ensure_in_channel(guild.id, user_ch.id)
                    log.info("auto-speak: joined #%s for %s", user_ch.name, member)
                except Exception:
                    log.exception("auto-speak: could not join #%s", user_ch.name)
                    return
            elif bot_ch_id != user_ch.id:
                return  # bot is busy in another channel; never hijack it

        try:
            await self.registry.get_or_create(guild.id).enqueue(TTSJob(
                text=text, user_id=member.id, guild_id=guild.id, voice_channel_id=user_ch.id,
            ))
            log.info("auto-speak queued for %s: %r", member, text[:60])
        except asyncio.QueueFull:
            log.warning("auto-speak: queue full, dropped message from %s", member)

    async def _handle_dm(self, message: discord.Message) -> None:
        if not self._is_allowed(message.author.id):
            await message.reply(self._no_access_message())
            return

        # UX guard: users sometimes type "/help" as text instead of using
        # the slash command popup. Don't speak it — guide them.
        if _SLASH_TEXT_RE.match(message.content):
            await message.reply(
                "💡 That looks like a slash command typed as text. "
                "Type `/` and pick from the popup that appears, instead."
            )
            return

        if self.config.get_voice_for(message.author.id) is None:
            await message.reply(
                f"No voice set. Run `/voice set <link>` first — browse voices at <{VOICE_LIBRARY_URL}>."
            )
            return

        text = message.content
        try:
            text = check_text(text, max_len=self.max_text_len)
        except ValidationError as e:
            await message.reply(str(e))
            return

        found = self._find_user_voice_anywhere(message.author.id)
        if found is None:
            await message.reply(
                "You're not in a voice channel in any server I'm in."
            )
            return
        guild_id, channel_id = found

        bot_ch_id = self.voice_manager.current_channel_id(guild_id)
        if bot_ch_id is None:
            await message.reply("I'm not in a voice channel. Use `/join` first.")
            return
        if bot_ch_id != channel_id:
            guild = self.bot.get_guild(guild_id)
            bot_ch = guild.get_channel(bot_ch_id) if guild else None
            ch_name = bot_ch.name if bot_ch else str(bot_ch_id)
            guild_name = guild.name if guild else "the server"
            await message.reply(
                f"I'm in **#{ch_name}** of **{guild_name}**. "
                "Run `/join` from your channel there to bring me over."
            )
            return

        pipeline = self.registry.get_or_create(guild_id)
        try:
            await pipeline.enqueue(TTSJob(
                text=text,
                user_id=message.author.id,
                guild_id=guild_id,
                voice_channel_id=channel_id,
            ))
        except asyncio.QueueFull:
            await message.reply("Queue is full, try again in a moment.")
            return
        await message.add_reaction("✅")
