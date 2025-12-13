# Gamepass Scanner Bot (beta 0.1.2)

Discord slash-command bot that scans Roblox gamepasses, shows current price, estimated Robux received after fee, and whether regional pricing is detected.

## Features
- Slash-only commands: `/ping`, `/help`, `/scan`, `/multi`.
- Fetches gamepass details via Roblox APIs; optional `.ROBLOSECURITY` cookie for better coverage.
- Shows net Robux after the 30% marketplace fee.
- Flags regional pricing/optimization if reported by Roblox.
- Response caching with configurable TTL and a lightweight rate gate.

## Requirements
- Python 3.10+
- A Discord bot token with `applications.commands` scope
- Dependencies in `requirements.txt`: `discord.py`, `aiohttp`, `python-dotenv`, `Flask` (Flask only if you enable the optional keep-alive).

## Setup
1. (Recommended) create a virtualenv:
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\activate
   ```
2. Install dependencies:
   ```powershell
   pip install -r requirements.txt
   ```
3. Create a `.env` file in the project root:
   ```env
   DISCORD_TOKEN=your_bot_token_here
   ROBLOSECURITY=optional_cookie_for_richer_data
   CACHE_TTL_SECONDS=300
   API_RPS=3
   API_BURST=6
   ```
   - Only `DISCORD_TOKEN` is required. Roblox cookie improves lookup success but is optional.

## Running
```powershell
python bot.py
```
- Presence text uses `NOTE_TEXT` (currently `[beta 0.1.2]`).
- Slash commands sync on startup.

## Slash Commands
- `/ping` — health check.
- `/help` — shows available commands.
- `/scan link_or_id:<value> force:<true|false>` — scan a single gamepass.
- `/multi links:<values> force:<true|false>` — scan multiple IDs/links (space/comma/newline separated).
  - Use `force:true` to bypass cache if you need fresh data.

## Notes
- Embeds use a neutral gray accent and include a dashed separator between pricing and regional status.
- Responses are chunked to avoid Discord embed limits when scanning many items.
- Optional `keep_alive.py` (commented in `bot.py`) can host a tiny Flask app if you need uptime pings.

## Troubleshooting
- Missing token: ensure `.env` contains `DISCORD_TOKEN`.
- Frequent failures: lower `API_RPS`/`API_BURST` or add a valid `.ROBLOSECURITY`.
- Stale data: rerun with `force:true`.
