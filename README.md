# discord-fishaudio-TTS

Standalone Python Discord bot that speaks user-provided text in voice channels via [Fish Audio](https://fish.audio) TTS Live (streaming WebSocket). Companion project to my TypeScript [openclaw-fish-audio-live](https://github.com/averatec/openclaw-fish-audio-live) plugin — same API, different deployment pattern.

## Features (v1.3)

- `/say <text>` — speak text in the bot's current voice channel (does **not** summon)
- `/join` — summon the bot to your voice channel (the only command that moves the bot)
- `/leave` — disconnect the bot
- `/skip` — stop the currently-playing TTS
- DM the bot text → speaks in your voice channel (only if the bot is already there)
- `/voice set | show | reset` — **each whitelisted user picks their own voice** (paste a fish.audio link or ID; it's verified with Fish Audio). `/voice default` (owner) sets the fallback voice
- `/toggle` — **auto-speak** for people without a mic: everything you type in an auto-speak channel is spoken in your voice while you're in a voice channel (the bot joins you if it isn't in voice yet; it never leaves another channel for you). Links, spoilers and custom emoji are cleaned up before speaking
- `/ttschannel add | remove | list` — owner-only: which text channels `/toggle` listens in
- `/whitelist add | remove | list` — owner-only user management
- `/help` — context-aware command reference with a link to the voice library; users without access are told who to contact
- Per-guild FIFO queue
- **Real streaming playback** — first audio plays before full synthesis completes
- 200-char text limit

## Setup (zero-config plugin model)

Requires Python 3.11+ and a Discord bot with Message Content + Server Members intents.

```bash
git clone https://github.com/<you>/discord-fishaudio-TTS
cd discord-fishaudio-TTS
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env.local
# Edit .env.local with TWO secrets only:
#   DISCORD_TOKEN      — https://discord.com/developers/applications → Bot → Reset Token
#   FISH_AUDIO_API_KEY — https://fish.audio Account → API

discord-fish-tts
```

That's it. On first start, the bot DMs the **Discord application owner** (whoever created the app at developers.discord.com — same person who has the token) with setup instructions. Everything else (voice ID, whitelist, …) is configured at runtime via slash commands.

Invite the bot to a server with scopes `bot applications.commands` and permissions `Connect`, `Speak`, `Send Messages`, `Use Slash Commands`.

### First-run flow

1. Bot logs in → DMs you (the app owner) a welcome with quick-start instructions.
2. `/voice default <link>` — pick a default voice from the [voice library](https://fish.audio/app/discovery) (or any reference_id). Each user can `/voice set` their own.
3. `/whitelist add @user` — grant access to friends.
4. Invite to a server → join a voice channel → `/join` → `/say hello`.
5. Optional: `/ttschannel add #no-mic` → users run `/toggle` and just type.

No file editing required after the initial token setup.

## Architecture

Fish Audio outputs Ogg/Opus 48 kHz mono 20 ms — a hand-rolled Ogg page parser extracts raw Opus packets and feeds them to a custom `discord.AudioSource` with `is_opus()=True`, so packets are passed straight into Discord's RTP without any decode/re-encode step. Wire protocol with Fish is MessagePack.

```
Fish WS (msgpack)  ─►  Ogg/Opus chunks  ─►  Ogg demuxer  ─►  Opus packets  ─►  Discord RTP
                                                              (48k mono 20ms — pass-through)
```

| Module | Responsibility |
|---|---|
| `fish_client.py` | MessagePack WebSocket client, async generator of Ogg/Opus chunks |
| `ogg_demux.py` | Streaming Ogg page → Opus packet demuxer (RFC 3533/7845) |
| `audio_source.py` | Thread-safe queue → `discord.AudioSource.read()` bridge |
| `tts_pipeline.py` | Per-guild FIFO queue, worker, skip semantics, dynamic voice_id |
| `voice_manager.py` | discord.py voice client lifecycle wrapper |
| `config_store.py` | JSON-persisted runtime config (whitelist + voice_id + flags) |
| `validators.py` | Text length validation |
| `commands.py` | Slash commands and DM handler (Cog) |
| `bot.py` | Entry point, owner detection from Discord app, .env loading |

## Authorization model

- **Owner** is auto-detected from the Discord application's owner — no env var required. Owner has all rights.
- **Whitelisted users** are added by the owner via `/whitelist add @user`. Managed at runtime, persisted to `data/config.json`.
- **Everyone else** is denied. The bot can be invited to any server (it auto-allows all guilds), but unless you're whitelisted, all commands and DMs are rejected with a message naming the owner (`OWNER_CONTACT` adds a handle).

For a friend-group deploy: run the host, get the welcome DM, set the voice, `/whitelist add` your friends. Done.

## Troubleshooting

**The bot's speaking indicator lights up but nobody hears anything.** Discord voice is end-to-end encrypted (DAVE). If the voice session never gets an encryption key, discord.py sends the audio unencrypted and Discord clients drop it. `davey` 0.2.0 causes this with discord.py 2.7.x, so `pyproject.toml` pins `davey<0.2`. If you installed before the pin, run `pip install "davey<0.2"` and restart. The bot logs `DAVE E2EE session has no encryption key` when it hits this state.

## Tests

```bash
pytest -v
```

53 tests covering Ogg demuxing (against a captured Fish output fixture), MessagePack WebSocket protocol mocking, cross-thread audio source bridging, FIFO queue with skip semantics, DAVE readiness, config persistence (per-user voices, auto-speak), chat-text cleanup, and input validation.

## Roadmap (v1.4+)

- Inline directives (`[[voice=...]] [[speed=...]]`)
- Latency telemetry display ("first audio in 320ms")
- HTTP fallback transport
- Idle auto-leave
- Stats command, audit log

## License

MIT
