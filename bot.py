import os
import re
import asyncio
import logging
import time
from itertools import cycle
from typing import Optional, Tuple, Dict, Any, List
from urllib.parse import urlparse, parse_qs
from decimal import Decimal, ROUND_HALF_UP
from contextlib import asynccontextmanager

from keep_alive import keep_alive

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiohttp


# ---------------- Logging ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("gp-scanner")


# ---------------- Config ----------------
STATUS_MESSAGES = [
    "SCAN NOW!",
    "DON'T FORGET TO SCAN!",
]
NOTE_TEXT = STATUS_MESSAGES[0]
MAX_AVATAR_BYTES = 8 * 1024 * 1024
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "300"))
API_RPS = float(os.getenv("API_RPS", "3"))
API_BURST = int(os.getenv("API_BURST", "6"))
ROBLOSECURITY = (os.getenv("ROBLOSECURITY", "") or "").replace("\r", "").replace("\n", "")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()

if not DISCORD_TOKEN:
    raise SystemExit("DISCORD_TOKEN not set in .env")


# ---------------- Discord init ----------------
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
_status_cycle = cycle(STATUS_MESSAGES)


# ---------------- keep-alive ----------------
keep_alive()


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
        headers: Dict[str, str] = {}
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
    if ctype == "user" and not str(name).startswith("@"):
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


def build_not_found_container(gp_id: str) -> Dict[str, Any]:
    content = (
        f"<a:Exclamation:1449272852338446457> Could not find gamepass `{gp_id}`.\n"
        f"**Gamepass ID · ** `{gp_id}`\n"
        f"[Open Gamepass](https://www.roblox.com/game-pass/{gp_id})"
    )
    return make_container([make_text_display("Gamepass Not Found"), make_separator(divider=False, spacing=1), make_text_display(content)])


# ---------------- Components V2 helpers ----------------
SEPARATOR_LINE = "------------------------------"
COMPONENTS_V2_FLAG = 1 << 15
EPHEMERAL_FLAG = 1 << 6


def make_text_display(content: str) -> Dict[str, Any]:
    return {"type": 10, "content": content}


def make_separator(*, divider: bool = True, spacing: int = 1) -> Dict[str, Any]:
    return {"type": 14, "divider": divider, "spacing": spacing}


def make_container(children: List[Dict[str, Any]], *, accent_color: Optional[int] = None) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"type": 17, "components": children}
    if accent_color is not None:
        payload["accent_color"] = accent_color
    return payload


def _component_size(component: Dict[str, Any]) -> int:
    size = 1
    ctype = component.get("type")
    if ctype in {9, 17}:  # section or container
        for child in component.get("components", []) or []:
            size += _component_size(child)
    if ctype == 9 and component.get("accessory"):
        size += _component_size(component["accessory"])
    return size


def _chunk_components(components: List[Dict[str, Any]], limit: int = 40) -> List[List[Dict[str, Any]]]:
    chunks: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    current_size = 0
    for comp in components:
        comp_size = _component_size(comp)
        if comp_size > limit:
            if current:
                chunks.append(current)
                current = []
                current_size = 0
            chunks.append([comp])
            continue
        if current_size + comp_size > limit and current:
            chunks.append(current)
            current = []
            current_size = 0
        current.append(comp)
        current_size += comp_size
    if current:
        chunks.append(current)
    return chunks


async def _post_components_payload(url: str, payload: Dict[str, Any]):
    async with http_session() as sess:
        async with sess.post(url, json=payload) as resp:
            if resp.status >= 400:
                body = await resp.text()
                raise RuntimeError(f"Components V2 post failed ({resp.status}): {body[:200]}")


async def send_components_message(
    interaction: discord.Interaction,
    components: List[Dict[str, Any]],
    *,
    ephemeral: bool = False,
):
    if not components:
        return
    flags = COMPONENTS_V2_FLAG | (EPHEMERAL_FLAG if ephemeral else 0)
    url = f"https://discord.com/api/v10/webhooks/{interaction.application_id}/{interaction.token}"
    chunks = _chunk_components(components)
    for idx, chunk in enumerate(chunks):
        payload = {"flags": flags, "components": chunk, "allowed_mentions": {"parse": []}}
        try:
            await _post_components_payload(url, payload)
        except Exception as exc:
            log.warning("Failed to send Components V2 chunk %d/%d: %s", idx + 1, len(chunks), exc)
            await interaction.followup.send(
                "<a:Exclamation:1449272852338446457> Failed to send formatted response.",
                ephemeral=True,
            )
            break


def build_card(price: Optional[int], owner: Optional[str], rp_enabled: Optional[bool], gp_id: str) -> Dict[str, Any]:
    rec = robux_received_after_fee(price)
    price_txt = f"{price} Robux" if price is not None else ""
    rec_txt = f"{rec} Robux" if rec is not None else ""
    rp_label = "Enabled" if rp_enabled else ("Disabled" if rp_enabled is False else "Unknown")
    rp_dot = "<a:Exclamation:1449272852338446457>" if rp_enabled else ("<a:Red_Check:1449273074456465418>" if rp_enabled is False else "<a:PenguHmmMath:1439407116111843388> ")

    lines = []
    if owner:
        lines.append(f"*Owner:* {owner}")
        lines.append("")
    lines.append(f"**Gamepass Price · **  `{price_txt}`")
    lines.append(f"**You will receive · **  `{rec_txt}`")
    info_block = "\n".join(lines)
    region_line = f"**Regional Pricing · **  {rp_dot} **{rp_label}**"
    url = f"https://www.roblox.com/game-pass/{gp_id}"
    id_block = f"**Gamepass ID · ** `{gp_id}`\n[Open Gamepass]({url})"

    return make_container(
        [
            make_text_display("Gamepass Summary"),
            make_separator(divider=False, spacing=1),
            make_text_display(info_block),
            make_separator(divider=True, spacing=1),
            make_text_display(region_line),
            make_separator(divider=False, spacing=2),
            make_text_display(id_block),
        ]
    )


def build_summary(total_price: int, n_scanned: int, n_with_price: int) -> Dict[str, Any]:
    missing = n_scanned - n_with_price
    covered_tax = round_half_up(total_price * 0.70) if total_price else 0
    content = (
        f"**TOTAL GAMEPASS PRICE · **  `{total_price} Robux`\n"
        f"**COVERED TAX · **  `{covered_tax} Robux`\n"
        f"**ITEMS SCANNED · **  `{n_scanned}` (with price: `{n_with_price}`, missing: `{missing}`)"
    )
    return make_container(
        [
            make_text_display("<a:Butterfly_Red:1449273839052914891> Multi-Scan Summary"),
            make_separator(divider=True, spacing=1),
            make_text_display(content),
        ]
    )


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


async def scan_one(gp_id: str, *, force: bool = False) -> Tuple[Dict[str, Any], Optional[int]]:
    if force:
        _clear_gp_cache(gp_id)
    price, details = await best_price_and_details(gp_id, force=force)
    if price is None and details is None:
        return build_not_found_container(gp_id), None
    owner = extract_owner(details)
    rp = regional_pricing_enabled(details)
    container = build_card(price, owner, rp, gp_id)
    return container, price


async def build_components_for_ids(gp_ids: List[str], *, force: bool) -> List[Dict[str, Any]]:
    if not gp_ids:
        return []
    sem = asyncio.Semaphore(6)
    total_price = 0
    n_with_price = 0

    async def one(gid: str):
        nonlocal total_price, n_with_price
        async with sem:
            try:
                container, price = await scan_one(gid, force=force)
                if price is not None:
                    n_with_price += 1
                    total_price += int(price)
                return container
            except Exception:
                info_block = (
                    "*Owner:* \n\n"
                    "**Gamepass Price · **  ``\n"
                    "**You will receive · **  ``"
                )
                alert_text = f"<a:Exclamation:1449272852338446457> Failed to scan ID {gid}"
                err_container = make_container(
                    [
                        make_text_display("<a:Butterfly_Red:1449273839052914891> Gamepass Summary"),
                        make_separator(divider=False, spacing=1),
                        make_text_display(info_block),
                        make_separator(divider=True, spacing=1),
                        make_text_display(alert_text),
                        make_separator(divider=False, spacing=2),
                        make_text_display(
                            f"**Gamepass ID · ** `{gid}`\n[Open Gamepass](https://www.roblox.com/game-pass/{gid})"
                        ),
                    ]
                )
                return err_container

    tasks = [asyncio.create_task(one(g)) for g in gp_ids]
    containers = await asyncio.gather(*tasks)
    containers.append(build_summary(total_price, len(gp_ids), n_with_price))
    return containers


async def _set_next_presence():
    message = next(_status_cycle)
    await bot.change_presence(
        status=discord.Status.online,
        activity=discord.Activity(type=discord.ActivityType.watching, name=message),
    )


@tasks.loop(minutes=1)
async def rotate_presence():
    await _set_next_presence()


@rotate_presence.before_loop
async def before_rotate():
    await bot.wait_until_ready()


def _has_admin_access(interaction: discord.Interaction) -> bool:
    if not interaction.guild:
        return False
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and perms.administrator)


# ---------------- Commands ----------------
def build_help_components() -> List[Dict[str, Any]]:
    commands_text = (
        "`/ping`\n"
        "`/scan link_or_id:<value> force:<true|false>`\n"
        "`/multi links:<values> force:<true|false>`\n"
        "`/changeprofile image:<attachment> (admin only)`\n"
        "`/help`"
    )
    footer = "Tip: paste multiple links/IDs with spaces, commas, or newlines."
    container = make_container(
        [
            make_text_display("<a:Butterfly_Red:1449273839052914891> Commands"),
            make_separator(divider=False, spacing=1),
            make_text_display(commands_text),
            make_separator(divider=False, spacing=1),
            make_text_display(footer),
        ]
    )
    return [container]


@bot.event
async def on_ready():
    log.info("Logged in as %s (id: %s)", bot.user, bot.user.id)
    await _set_next_presence()
    if not rotate_presence.is_running():
        rotate_presence.start()
    try:
        synced = await bot.tree.sync()
        log.info("Slash commands synced: %d", len(synced))
    except Exception as e:
        log.warning("Slash sync failed: %s", e)


@bot.tree.command(name="help", description="Show help")
async def help_slash(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True, ephemeral=True)
    await send_components_message(interaction, build_help_components(), ephemeral=True)


@bot.tree.command(name="ping", description="Ping")
async def ping_slash(interaction: discord.Interaction):
    await interaction.response.send_message("<a:Butterfly_Red:1449273839052914891> pong")


@bot.tree.command(name="changeprofile", description="Update the bot avatar (server admin only)")
@app_commands.describe(image="Image attachment to use as the bot's avatar")
@app_commands.guild_only()
async def changeprofile_slash(interaction: discord.Interaction, image: discord.Attachment):
    if not _has_admin_access(interaction):
        await interaction.response.send_message(
            "<a:Exclamation:1449272852338446457> Only server administrators can run this command.",
            ephemeral=True,
        )
        return
    if not image.content_type or not image.content_type.startswith("image/"):
        await interaction.response.send_message(
            "<a:Exclamation:1449272852338446457> Please upload a valid image.",
            ephemeral=True,
        )
        return
    if image.size and image.size > MAX_AVATAR_BYTES:
        await interaction.response.send_message(
            f"<a:Exclamation:1449272852338446457> Image must be {MAX_AVATAR_BYTES // (1024 * 1024)} MB or smaller.",
            ephemeral=True,
        )
        return
    try:
        data = await image.read()
        if not bot.user:
            raise RuntimeError("Bot user not ready")
        await bot.user.edit(avatar=data)
    except Exception as exc:
        log.warning("Avatar update failed: %s", exc)
        await interaction.response.send_message(
            "<a:Exclamation:1449272852338446457> Failed to update avatar. Try a different image.",
            ephemeral=True,
        )
        return
    await interaction.response.send_message(
        "<a:Red_Check:1449273074456465418> Bot avatar updated successfully!",
        ephemeral=True,
    )


@bot.tree.command(name="scan", description="Scan a single gamepass")
@app_commands.describe(link_or_id="Gamepass link or numeric ID", force="Bypass cache and refresh")
async def scan_slash(interaction: discord.Interaction, link_or_id: str, force: bool = False):
    await interaction.response.defer(thinking=True)
    gp_id = extract_gamepass_id(link_or_id)
    if not gp_id:
        return await interaction.followup.send("<a:Exclamation:1449272852338446457> Please provide a valid game-pass link or numeric ID.")
    container, _ = await scan_one(gp_id, force=force)
    await send_components_message(interaction, [container])


@bot.tree.command(name="multi", description="Scan multiple gamepasses")
@app_commands.describe(links="Links/IDs (space/comma/newline separated)", force="Bypass cache and refresh")
async def multi_slash(interaction: discord.Interaction, links: str, force: bool = False):
    await interaction.response.defer(thinking=True)
    gp_ids = extract_many_ids(links)
    if not gp_ids:
        return await interaction.followup.send("<a:Exclamation:1449272852338446457> Provide at least one valid game-pass link or numeric ID.")
    components = await build_components_for_ids(gp_ids, force=force)
    await send_components_message(interaction, components)


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)