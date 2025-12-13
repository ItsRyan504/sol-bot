import os
import re
import asyncio
import logging
import time
from typing import Optional, Tuple, Dict, Any, List
from urllib.parse import urlparse, parse_qs
from decimal import Decimal, ROUND_HALF_UP
from contextlib import asynccontextmanager

#from keep_alive import keep_alive

try:
	from dotenv import load_dotenv
	load_dotenv()
except Exception:
	pass

import discord
from discord.ext import commands
from discord import app_commands
import aiohttp


# ---------------- Config ----------------
NOTE_TEXT = "gamepass scanner"
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "300"))
API_RPS = float(os.getenv("API_RPS", "3"))
API_BURST = int(os.getenv("API_BURST", "6"))
ROBLOSECURITY = (os.getenv("ROBLOSECURITY", "") or "").replace("\r", "").replace("\n", "")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()

if not DISCORD_TOKEN:
	raise SystemExit("DISCORD_TOKEN not set in .env")


# ---------------- Logging ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("gp-scanner")


# ---------------- Discord init ----------------
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


# ---------------- keep-alive ----------------
#keep_alive()


# ---------------- cache ----------------
_cache: Dict[str, Tuple[float, Any]] = {}


def _getc(key: str, *, bypass: bool = False) -> Optional[Any]:
	if bypass:
		return None
	tup = _cache.get(key)
	if not tup:
		return None
	ts, val = tup
	if CACHE_TTL_SECONDS <= 0 or time.time() - ts > CACHE_TTL_SECONDS:
		_cache.pop(key, None)
		return None
	return val


def _setc(key: str, val: Any):
	_cache[key] = (time.time(), val)


def _clear_gp_cache(gp_id: str):
	for k in list(_cache.keys()):
		if gp_id in k:
			_cache.pop(k, None)


# ---------------- aiohttp session ----------------
_http_session: Optional[aiohttp.ClientSession] = None


@asynccontextmanager
async def http_session():
	global _http_session
	if _http_session is None or _http_session.closed:
		_http_session = aiohttp.ClientSession(
			timeout=aiohttp.ClientTimeout(total=10),
			raise_for_status=False,
		)
	try:
		yield _http_session
	finally:
		pass


# ---------------- rate gate ----------------
_api_tokens = API_BURST
_api_last = time.monotonic()
_api_lock = asyncio.Lock()


async def _api_rate_gate():
	global _api_tokens, _api_last
	async with _api_lock:
		now = time.monotonic()
		_api_tokens = min(API_BURST, _api_tokens + (now - _api_last) * API_RPS)
		_api_last = now
		if _api_tokens >= 1:
			_api_tokens -= 1
			return
		need = 1 - _api_tokens
		sleep_s = need / API_RPS
	await asyncio.sleep(max(0.01, sleep_s))


# ---------------- utils ----------------
PRICE_PATTERNS = [
	r"\b(\d[\d,\.]*)\s*robux\b",
	r"\brobux\s*(\d[\d,\.]*)\b",
]


def extract_gamepass_id(text: str) -> Optional[str]:
	if not text:
		return None
	s = text.strip()
	if "configure?id=" in s.lower():
		try:
			q = parse_qs(urlparse(s).query)
			gid = (q.get("id") or [None])[0]
			if gid and gid.isdigit():
				return gid
		except Exception:
			pass
	m = re.search(r"game[-_]pass/(\d+)", s, flags=re.IGNORECASE)
	if m:
		return m.group(1)
	m2 = re.search(r"\b(\d{6,20})\b", s)
	if m2:
		return m2.group(1)
	return None


def round_half_up(n: float) -> int:
	return int(Decimal(n).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def robux_received_after_fee(price: Optional[int]) -> Optional[int]:
	if price is None:
		return None
	fee = round_half_up(price * 0.30)
	return max(0, int(price) - fee)


# ---------------- HTTP helpers ----------------
RETRY_STATUSES = {429, 500, 502, 503, 504}


async def _http_get_json(url: str, cookie: Optional[str] = None, *, force: bool = False) -> Optional[Dict[str, Any]]:
	cache_key = f"httpjson::{bool(cookie)}::{url}"
	hit = _getc(cache_key, bypass=force)
	if hit is not None:
		return hit
	attempts = 3
	cur_cookie = cookie
	for i in range(attempts):
		await _api_rate_gate()
		headers = {}
		if cur_cookie:
			headers["Cookie"] = f".ROBLOSECURITY={cur_cookie}"
		try:
			async with http_session() as sess:
				async with sess.get(url, headers=headers) as resp:
					if resp.status == 200:
						data = await resp.json(content_type=None)
						_setc(cache_key, data)
						return data
					if resp.status == 401:
						return None
					if resp.status in RETRY_STATUSES:
						await asyncio.sleep(0.25 * (2 ** i))
						continue
		except Exception as e:
			log.warning("HTTP error %r for %s (cookie=%s)", e, url, bool(cur_cookie))
			await asyncio.sleep(0.25 * (2 ** i))
			continue
	return None


# ---------------- Roblox fetching ----------------
async def api_get_details(gp_id: str, cookie: Optional[str], *, force: bool = False) -> Optional[Dict[str, Any]]:
	key = f"details::{cookie is not None}::{gp_id}"
	hit = _getc(key, bypass=force)
	if hit is not None:
		return hit
	url = f"https://apis.roblox.com/game-passes/v1/game-passes/{gp_id}/details"
	data = await _http_get_json(url, cookie, force=force)
	if data is not None:
		_setc(key, data)
	return data


async def get_price_via_api(gp_id: str, cookie: Optional[str]) -> Tuple[Optional[int], Optional[Dict[str, Any]]]:
	data = await api_get_details(gp_id, cookie)
	if not data:
		return None, None
	price_info = (data.get("priceInformation") or {})
	price = price_info.get("defaultPriceInRobux")
	if price is None:
		price = data.get("price")
	try:
		return int(round(float(price))), data
	except Exception:
		return None, data


async def get_price_any(gp_id: str, *, force: bool = False) -> Tuple[Optional[int], Optional[Dict[str, Any]]]:
	key = f"price_any::{gp_id}"
	hit = _getc(key, bypass=force)
	if hit is not None:
		return hit

	# try authenticated first
	price, details = await get_price_via_api(gp_id, ROBLOSECURITY or None)
	if price is None:
		price, details = await get_price_via_api(gp_id, None)
	_setc(key, (price, details))
	return price, details


async def best_price_and_details(gp_id: str, *, force: bool = False) -> Tuple[Optional[int], Optional[Dict[str, Any]]]:
	price, details = await get_price_any(gp_id, force=force)
	return price, details


def extract_owner(details: Optional[Dict[str, Any]]) -> Optional[str]:
	if not details:
		return None
	creator = (details.get("creator") or {})
	name = creator.get("name")
	if not name:
		return None
	ctype = (creator.get("type") or creator.get("creatorType") or "").lower()
	if ctype == "user" and not str(name).startswith("@"):  # prefix users with @ for clarity
		return f"@{name}"
	return str(name)


def regional_pricing_enabled(details: Optional[Dict[str, Any]]) -> Optional[bool]:
	if not details:
		return None
	pi = details.get("priceInformation") or {}
	enabled = [str(x).lower() for x in (pi.get("enabledFeatures") or [])]
	if pi.get("isInActivePriceOptimizationExperiment"):
		return True
	if any(("regional" in x) or ("price" in x) for x in enabled):
		return True
	return False


# ---------------- Embeds ----------------
BLACK = discord.Color(0x000000)


def build_card(price: Optional[int], owner: Optional[str], rp_enabled: Optional[bool], gp_id: str) -> discord.Embed:
	rec = robux_received_after_fee(price)
	price_txt = f"{price} Robux" if price is not None else "—"
	rec_txt = f"{rec} Robux" if rec is not None else "—"
	rp_label = "Enabled" if rp_enabled else ("Disabled" if rp_enabled is False else "Unknown")
	rp_dot = "<a:Exclamation:1449272852338446457>" if rp_enabled else ("<a:Red_Check:1449273074456465418>" if rp_enabled is False else "<a:PenguHmmMath:1439407116111843388> ")

	e = discord.Embed(title="Gamepass Summary", color=BLACK)
	owner_line = f"*Owner:* {owner}\n\n" if owner else ""
	e.description = (
		owner_line
		+ f"**Gamepass Price** · `{price_txt}`\n"
		+ f"**You will receive** · `{rec_txt}`\n"
		+ f"**Regional Pricing** · {rp_dot} **{rp_label}**"
	)
	url = f"https://www.roblox.com/game-pass/{gp_id}"
	e.add_field(name="Gamepass ID", value=f"`{gp_id}`", inline=True)
	e.add_field(name="URL", value=f"[Open Gamepass]({url})", inline=True)
	return e


def build_summary(total_price: int, n_scanned: int, n_with_price: int) -> discord.Embed:
	missing = n_scanned - n_with_price
	e = discord.Embed(title="Multi-Scan Summary", color=BLACK)
	e.description = (
		f"**Total Gamepass Price** · `{total_price} Robux`\n"
		f"**Items scanned** · `{n_scanned}` (with price: `{n_with_price}`, missing: `{missing}`)"
	)
	return e


# ---------------- Scan helpers ----------------
def extract_many_ids(text: str) -> List[str]:
	if not text:
		return []
	parts = re.split(r"[,\s]+", text.strip())
	ids: List[str] = []
	for p in parts:
		gid = extract_gamepass_id(p)
		if gid:
			ids.append(gid)
	seen: set[str] = set()
	uniq: List[str] = []
	for gid in ids:
		if gid not in seen:
			uniq.append(gid)
			seen.add(gid)
	return uniq


async def scan_one(gp_id: str, *, force: bool = False) -> Tuple[discord.Embed, Optional[int]]:
	if force:
		_clear_gp_cache(gp_id)
	price, details = await best_price_and_details(gp_id, force=force)
	owner = extract_owner(details)
	rp = regional_pricing_enabled(details)
	embed = build_card(price, owner, rp, gp_id)
	return embed, price


async def build_embeds_for_ids(gp_ids: List[str], *, force: bool) -> List[discord.Embed]:
	if not gp_ids:
		return []
	sem = asyncio.Semaphore(6)
	total_price = 0
	n_with_price = 0

	async def one(gid: str):
		nonlocal total_price, n_with_price
		async with sem:
			try:
				embed, price = await scan_one(gid, force=force)
				if price is not None:
					n_with_price += 1
					total_price += int(price)
				return embed
			except Exception:
				e = discord.Embed(title="<a:Butterfly_Red:1449273839052914891> Gamepass Summary", color=BLACK)
				e.description = (
					"*Owner:* —\n\n"
					"**Gamepass Price** · `—`\n"
					"**You will receive** · `—`\n\n"
					f"`Failed to scan ID {gid}`"
				)
				e.add_field(name="Gamepass ID", value=f"`{gid}`", inline=True)
				e.add_field(name="URL", value=f"[Open Gamepass](https://www.roblox.com/game-pass/{gid})", inline=True)
				return e

	tasks = [asyncio.create_task(one(g)) for g in gp_ids]
	embeds = await asyncio.gather(*tasks)
	embeds.append(build_summary(total_price, len(gp_ids), n_with_price))
	return embeds


async def send_embeds_chunked(send_func, embeds: List[discord.Embed]):
	CHUNK = 10
	for i in range(0, len(embeds), CHUNK):
		await send_func(embeds=embeds[i : i + CHUNK])


# ---------------- Commands ----------------
def build_help_embed() -> discord.Embed:
	e = discord.Embed(title="<a:Butterfly_Red:1449273839052914891> Commands", color=BLACK)
	e.add_field(
		name="Slash",
		value="`/ping`\n`/scan link_or_id:<value> force:<true|false>`\n`/multi links:<values> force:<true|false>`\n`/help`",
		inline=False,
	)
	e.set_footer(text="Tip: paste multiple links/IDs with spaces, commas, or newlines.")
	return e


@bot.event
async def on_ready():
	log.info("Logged in as %s (id: %s)", bot.user, bot.user.id)
	await bot.change_presence(
		status=discord.Status.online,
		activity=discord.Activity(type=discord.ActivityType.watching, name=NOTE_TEXT),
	)
	try:
		synced = await bot.tree.sync()
		log.info("Slash commands synced: %d", len(synced))
	except Exception as e:
		log.warning("Slash sync failed: %s", e)


@bot.tree.command(name="help", description="Show help")
async def help_slash(interaction: discord.Interaction):
	await interaction.response.send_message(embed=build_help_embed())


@bot.tree.command(name="ping", description="Ping")
async def ping_slash(interaction: discord.Interaction):
	await interaction.response.send_message("<a:Butterfly_Red:1449273839052914891> pong")


@bot.tree.command(name="scan", description="Scan a single gamepass")
@app_commands.describe(link_or_id="Gamepass link or numeric ID", force="Bypass cache and refresh")
async def scan_slash(interaction: discord.Interaction, link_or_id: str, force: bool = False):
	await interaction.response.defer(thinking=True)
	gp_id = extract_gamepass_id(link_or_id)
	if not gp_id:
		return await interaction.followup.send("<a:Exclamation:1449272852338446457> Please provide a valid game-pass link or numeric ID.")
	embed, _ = await scan_one(gp_id, force=force)
	await interaction.followup.send(embed=embed)


@bot.tree.command(name="multi", description="Scan multiple gamepasses")
@app_commands.describe(links="Links/IDs (space/comma/newline separated)", force="Bypass cache and refresh")
async def multi_slash(interaction: discord.Interaction, links: str, force: bool = False):
	await interaction.response.defer(thinking=True)
	gp_ids = extract_many_ids(links)
	if not gp_ids:
		return await interaction.followup.send("<a:Exclamation:1449272852338446457> Provide at least one valid game-pass link or numeric ID.")
	embeds = await build_embeds_for_ids(gp_ids, force=force)

	async def _send(**kw):
		await interaction.followup.send(**kw)

	await send_embeds_chunked(_send, embeds)


if __name__ == "__main__":
	bot.run(DISCORD_TOKEN)
