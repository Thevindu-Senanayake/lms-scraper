import requests
from bs4 import BeautifulSoup
import json
import re
import time
import hashlib
import os
from dotenv import load_dotenv
from datetime import datetime, timezone
import urllib3
import logging
import threading
import io
import asyncio
import discord
from discord import app_commands
from discord.ext import commands

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),  # Log to console
        logging.FileHandler("scraper.log", encoding="utf-8")  # Optional: log to file
    ]
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Try to load initial course list
try:
    with open("course_urls.json", "r", encoding="utf-8") as f:
        COURSE_URLS = json.load(f)
    MISSING_COURSE_URLS = False
except FileNotFoundError:
    COURSE_URLS = []
    MISSING_COURSE_URLS = True

load_dotenv()
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_NOTIFY_CHANNEL_ID = os.getenv("DISCORD_NOTIFY_CHANNEL_ID")
DISCORD_LOG_CHANNEL_ID = os.getenv("DISCORD_LOG_CHANNEL_ID")
# Log level for Discord channel (DEBUG, INFO, WARNING, ERROR, CRITICAL)
DISCORD_LOG_LEVEL = os.getenv("DISCORD_LOG_LEVEL", "WARNING").upper()
DISCORD_LOG_HANDLER = None
ADMIN_ROLE_NAME = os.getenv("DISCORD_ADMIN_ROLE", "course-admin")
WHITELISTED_IDS = set([s.strip() for s in os.getenv("DISCORD_WHITELISTED_IDS", "").split(",") if s.strip()])

# Load cookies
try:
    with open('cookies.json', 'r', encoding='utf-8') as f:
        session_data = json.load(f)
    COOKIES_MISSING = False
    cookies = {"MoodleSession": session_data.get("value")} if session_data and session_data.get("value") else {}
except FileNotFoundError:
    session_data = None
    COOKIES_MISSING = True
    cookies = {}


def read_cookies():
    """Thread-safe read of cookies.json. Returns a dict usable by requests."""
    with file_lock:
        try:
            with open('cookies.json', 'r', encoding='utf-8') as f:
                data = json.load(f)
            val = data.get('value') if isinstance(data, dict) else None
            return {"MoodleSession": val} if val else {}
        except FileNotFoundError:
            return {}


def is_cookie_full_shape(obj) -> bool:
    """Return True if obj looks like a full exported cookie object.

    We require at minimum: 'name', 'value', and 'domain' keys. The 'name'
    should equal 'MoodleSession' to be considered valid.
    """
    if not isinstance(obj, dict):
        return False
    if 'name' not in obj or 'value' not in obj or 'domain' not in obj:
        return False
    if obj.get('name') != 'MoodleSession':
        return False
    return True


def write_cookies(value: str):
    """Thread-safe write of cookies.json.

    The cookie must be provided as a full JSON object (JSON-object string).
    This function will overwrite `cookies.json` with that object.
    If the input is not a JSON object, a ValueError is raised.
    """
    with file_lock:
        try:
            if not isinstance(value, str):
                raise ValueError("Cookie must be provided as a full JSON object (JSON-object string)")

            # parse the JSON string
            s = value.strip()
            if s.startswith('{') and s.endswith('}'):
                try:
                    parsed = json.loads(s)
                    if isinstance(parsed, dict):
                        with open('cookies.json', 'w', encoding='utf-8') as f:
                            json.dump(parsed, f, indent=2, ensure_ascii=False)
                        return
                except json.JSONDecodeError:
                    raise ValueError("Provided cookie string is not valid JSON")
        except Exception:
            logging.exception("Failed to write cookies.json")
            # Re-raise so callers can react to validation/write failures
            raise


headers = {
    "User-Agent": "Mozilla/5.0"
}

try:
    with open("scraper_state.json", "r", encoding="utf-8") as f:
        previous_data = json.load(f)
except FileNotFoundError:
    previous_data = {}

# Lock to coordinate file access between scraper and bot commands
file_lock = threading.Lock()


def read_course_urls():
    """Thread-safe read of course_urls.json returning a list."""
    with file_lock:
        try:
            with open("course_urls.json", "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return []


def write_course_urls(urls):
    """Thread-safe write of course_urls.json."""
    with file_lock:
        with open("course_urls.json", "w", encoding="utf-8") as f:
            json.dump(urls, f, indent=2, ensure_ascii=False)


intents = discord.Intents.default()
# We need members intent to check roles on users
intents.members = True
bot = commands.Bot(command_prefix=[], intents=intents)


class DiscordLogHandler(logging.Handler):
    """Logging handler that posts log records to a Discord channel asynchronously.

    It enqueues formatted log messages and a background coroutine on the bot loop will
    pull from the queue and send them to the configured channel. This avoids blocking
    the main thread or bot event loop directly from the logging call site.
    """
    def __init__(self, bot, level=logging.WARNING):
        super().__init__(level=level)
        self.bot = bot
        self.queue = asyncio.Queue()
        self._task = None

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            # Enqueue without blocking
            try:
                self.queue.put_nowait((record.levelno, msg))
            except Exception:
                # If queue put fails for any reason, fallback to console
                print(msg)
            # Ensure background sender is scheduled when bot loop is available
            if self._task is None and self.bot and getattr(self.bot, 'loop', None):
                try:
                    # schedule the background sender on the bot loop
                    self._task = asyncio.run_coroutine_threadsafe(self._sender(), self.bot.loop)
                except Exception:
                    # If the loop isn't running yet, ignore — sender will be started from on_ready
                    pass
        except Exception:
            self.handleError(record)

@bot.tree.command(name="get_log_level", description="Show the current Discord log-forwarding level")
async def get_log_level(interaction: discord.Interaction):
    try:
        guild_id = interaction.guild.id if interaction.guild else 'DM'
        logging.info(f"/get_log_level requested by {interaction.user} (id={interaction.user.id}) in guild={guild_id}")
    except Exception:
        pass
    if not user_is_authorized(interaction.user, interaction.guild):
        await interaction.response.send_message("You are not authorized to use this command.", ephemeral=True)
        return
    level = DISCORD_LOG_LEVEL
    await interaction.response.send_message(f"Discord log-forwarding level: {level}", ephemeral=True)

@bot.tree.command(name="set_log_level", description="Set the Discord log-forwarding level (DEBUG/INFO/WARNING/ERROR/CRITICAL)")
@app_commands.describe(level="One of: DEBUG, INFO, WARNING, ERROR, CRITICAL")
async def set_log_level(interaction: discord.Interaction, level: str):
    try:
        guild_id = interaction.guild.id if interaction.guild else 'DM'
        logging.info(f"/set_log_level requested by {interaction.user} (id={interaction.user.id}) in guild={guild_id} level={level}")
    except Exception:
        pass
    if not user_is_authorized(interaction.user, interaction.guild):
        await interaction.response.send_message("You are not authorized to use this command.", ephemeral=True)
        return
    l = level.strip().upper()
    if l not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        await interaction.response.send_message("Invalid level. Use DEBUG, INFO, WARNING, ERROR, or CRITICAL.", ephemeral=True)
        return
    try:
        global DISCORD_LOG_LEVEL, DISCORD_LOG_HANDLER
        DISCORD_LOG_LEVEL = l
        if DISCORD_LOG_HANDLER is not None:
            DISCORD_LOG_HANDLER.setLevel(getattr(logging, l))
        await interaction.response.send_message(f"Discord log-forwarding level set to {l}", ephemeral=True)
        logging.info(f"Discord log-forwarding level changed to {l} by user id={interaction.user.id}")
    except Exception:
        logging.exception("Failed to set log level via /set_log_level")
        await interaction.response.send_message("Failed to set log level. See logs.", ephemeral=True)

    async def _sender(self):
        """Background coroutine running on bot loop that sends queued logs to the channel."""
        # resolve the channel id
        try:
            if not DISCORD_LOG_CHANNEL_ID:
                return
            channel_id = int(DISCORD_LOG_CHANNEL_ID)
        except Exception:
            logging.error("DISCORD_LOG_CHANNEL_ID is invalid; DiscordLogHandler will not start")
            return

        # Attempt to get channel object
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except Exception:
                logging.exception(f"DiscordLogHandler failed to fetch channel {DISCORD_LOG_CHANNEL_ID}")
                return

        # repeatedly drain queue and send messages
        while True:
            try:
                levelno, msg = await self.queue.get()
                # Use embed colors per level for readability
                level_name = logging.getLevelName(levelno)
                # Map log levels to Discord embed colors
                color_map = {
                    logging.DEBUG: 0x99AAB5,    # grey
                    logging.INFO: 0x57F287,     # green
                    logging.WARNING: 0xFAA61A,  # orange
                    logging.ERROR: 0xED4245,    # red
                    logging.CRITICAL: 0x732FCE  # purple
                }
                color = color_map.get(levelno, 0x5865F2)

                # Discord embed description limit is 4096; keep chunks smaller to be safe
                max_desc = 3800
                if len(msg) <= max_desc:
                    embed = discord.Embed(title=f"[{level_name}]", description=msg, color=color, timestamp=datetime.now(timezone.utc))
                    embed.set_footer(text="lms-scraper logs")
                    try:
                        await channel.send(embed=embed)
                    except Exception:
                        logging.exception("Failed to send embed log message to Discord channel")
                else:
                    # Split into multiple embeds
                    for i in range(0, len(msg), max_desc):
                        chunk = msg[i:i+max_desc]
                        embed = discord.Embed(title=f"[{level_name}] (part {i//max_desc + 1})", description=chunk, color=color, timestamp=datetime.now(timezone.utc))
                        embed.set_footer(text="lms-scraper logs")
                        try:
                            await channel.send(embed=embed)
                        except Exception:
                            logging.exception("Failed to send log embed chunk to Discord channel")
                # small sleep to avoid hitting rate limits when many logs appear
                await asyncio.sleep(0.25)
            except asyncio.CancelledError:
                break
            except Exception:
                logging.exception("Unexpected error in DiscordLogHandler sender loop")
                await asyncio.sleep(5)


def setup_discord_log_handler():
    if not DISCORD_LOG_CHANNEL_ID:
        logging.info("DISCORD_LOG_CHANNEL_ID not set; Discord log forwarding disabled")
        return
    try:
        global DISCORD_LOG_HANDLER
        level = getattr(logging, DISCORD_LOG_LEVEL, logging.WARNING)
        handler = DiscordLogHandler(bot, level=level)
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        handler.setFormatter(fmt)
        logging.getLogger().addHandler(handler)
        DISCORD_LOG_HANDLER = handler
        logging.info(f"DiscordLogHandler installed for channel {DISCORD_LOG_CHANNEL_ID} at level {DISCORD_LOG_LEVEL}")
    except Exception:
        logging.exception("Failed to install DiscordLogHandler")


def is_valid_course_url(url: str) -> bool:
    """Basic validation: must be http(s) and contain `id=` with a numeric id."""
    if not isinstance(url, str):
        return False
    m = re.search(r"^https?://", url)
    if not m:
        return False
    if not re.search(r"[?&]id=\d+", url):
        return False
    return True


def user_is_authorized(user: discord.abc.User, guild: discord.Guild | None) -> bool:
    """Return True if the user is whitelisted or has the admin role in the guild."""
    try:
        if str(user.id) in WHITELISTED_IDS:
            return True
        if guild is None:
            return False
        member = guild.get_member(user.id)
        if member is None:
            # try fetch
            try:
                member = asyncio.run_coroutine_threadsafe(guild.fetch_member(user.id), bot.loop).result(timeout=5)
            except Exception:
                member = None
        if member:
            for role in member.roles:
                if role.name == ADMIN_ROLE_NAME:
                    return True
    except Exception:
        pass
    return False


@bot.event
async def on_ready():
    global COOKIES_MISSING
    try:
        await bot.tree.sync()
        logging.info(f"Discord admin bot ready as {bot.user} and commands synced")
    except Exception:
        logging.exception("Failed to sync app commands")

    # Setup discord log handler now that bot.loop is available
    try:
        setup_discord_log_handler()
    except Exception:
        logging.exception("Failed to setup Discord log handler in on_ready")

    if MISSING_COURSE_URLS:
        logging.warning("course_urls.json not found — notifying admins/whitelist via DM")
        notified_ids = set()

        # Notify whitelisted user IDs first
        for uid in WHITELISTED_IDS:
            try:
                user_id = int(uid)
            except Exception:
                continue
            if user_id in notified_ids:
                continue
            try:
                user = await bot.fetch_user(user_id)
                if user:
                    await user.send(
                        "lms-scraper: `course_urls.json` was not found in the project. "
                        "The scraper is running but has no courses configured. You can add courses using the `/add_course <url>` command or upload a `course_urls.json` file to the project root."
                    )
                    notified_ids.add(user_id)
            except Exception:
                logging.exception(f"Failed to DM whitelisted user {user_id}")

        # Notify members who have the admin role
        for guild in bot.guilds:
            try:
                role = discord.utils.get(guild.roles, name=ADMIN_ROLE_NAME)
                if not role:
                    continue
                for member in role.members:
                    if member.bot:
                        continue
                    if member.id in notified_ids:
                        continue
                    try:
                        await member.send(
                            "lms-scraper: `course_urls.json` was not found in the project. "
                            "The scraper is running but has no courses configured. You can add courses using the `/add_course <url>` command or upload a `course_urls.json` file to the project root."
                        )
                        notified_ids.add(member.id)
                    except Exception:
                        logging.exception(f"Failed to DM admin member {member.id} in guild {guild.id}")
            except Exception:
                logging.exception(f"Failed to notify admins in guild {guild.id}")
        # Check channel permissions before sending startup notice
        if DISCORD_NOTIFY_CHANNEL_ID:
            try:
                channel_id = int(DISCORD_NOTIFY_CHANNEL_ID)
                channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
                # Check send permissions for bot in that channel
                guild = getattr(channel, 'guild', None)
                can_send = True
                try:
                    if guild is not None:
                        me = guild.get_member(bot.user.id) or await guild.fetch_member(bot.user.id)
                        perms = channel.permissions_for(me)
                        can_send = perms.send_messages and perms.embed_links
                except Exception:
                    # permission check failed; we'll attempt send and catch Forbidden
                    pass

                if not can_send:
                    logging.warning(f"Bot lacks send/embed permissions in channel {channel_id}; skipping in-channel notice")
                else:
                    await channel.send(
                        "⚠️ lms-scraper started but `course_urls.json` was not found. Admins: add courses using `/add_course <url>` or use `/set_cookie` to set the MoodleSession cookie."
                    )
            except Exception:
                logging.exception("Failed to post missing-course notification to notify channel")

    if COOKIES_MISSING:
        logging.warning("cookies.json not found — notifying admins/whitelist via DM")
        notified_ids = set()
        for uid in WHITELISTED_IDS:
            try:
                user_id = int(uid)
            except Exception:
                continue
            if user_id in notified_ids:
                continue
            try:
                user = await bot.fetch_user(user_id)
                if user:
                    await user.send(
                        "lms-scraper: `cookies.json` was not found in the project. "
                        "The scraper may need a MoodleSession cookie to access course pages. You can set it using `/set_cookie <json_object>` or upload a `cookies.json` file to the project root."
                    )
                    notified_ids.add(user_id)
            except Exception:
                logging.exception(f"Failed to DM whitelisted user {user_id} about cookies")

        for guild in bot.guilds:
            try:
                role = discord.utils.get(guild.roles, name=ADMIN_ROLE_NAME)
                if not role:
                    continue
                for member in role.members:
                    if member.bot:
                        continue
                    if member.id in notified_ids:
                        continue
                    try:
                        await member.send(
                            "lms-scraper: `cookies.json` was not found in the project. "
                            "The scraper may need a MoodleSession cookie to access course pages. You can set it using `/set_cookie <json_object>` or upload a `cookies.json` file to the project root."
                        )
                        notified_ids.add(member.id)
                    except Exception:
                        logging.exception(f"Failed to DM admin member {member.id} in guild {guild.id} about cookies")
            except Exception:
                logging.exception(f"Failed to notify admins in guild {guild.id} about cookies")

    # Validate cookies.json
    try:
        if os.path.exists('cookies.json'):
            try:
                with open('cookies.json', 'r', encoding='utf-8') as f:
                    cookie_obj = json.load(f)
            except Exception:
                logging.exception("Failed to load cookies.json for shape validation")
                cookie_obj = None

            # If cookie is not the full object shape, remove it and notify
            if not is_cookie_full_shape(cookie_obj):
                try:
                    os.remove('cookies.json')
                except Exception:
                    logging.exception("Failed to remove malformed cookies.json")
                COOKIES_MISSING = True
                logging.warning("cookies.json was malformed and has been removed — notifying admins/whitelist via DM")
                notified_ids = set()
                for uid in WHITELISTED_IDS:
                    try:
                        user_id = int(uid)
                    except Exception:
                        continue
                    if user_id in notified_ids:
                        continue
                    try:
                        user = await bot.fetch_user(user_id)
                        if user:
                            await user.send(
                                "lms-scraper: The existing `cookies.json` was malformed and has been removed. Please set a valid MoodleSession cookie using `/set_cookie <json_object>` or upload a `cookies.json` file."
                            )
                            notified_ids.add(user_id)
                    except Exception:
                        logging.exception(f"Failed to DM whitelisted user {user_id} after malformed cookie deletion")

                for guild in bot.guilds:
                    try:
                        role = discord.utils.get(guild.roles, name=ADMIN_ROLE_NAME)
                        if not role:
                            continue
                        for member in role.members:
                            if member.bot:
                                continue
                            if member.id in notified_ids:
                                continue
                            try:
                                await member.send(
                                    "lms-scraper: The existing `cookies.json` was malformed and has been removed. Please set a valid MoodleSession cookie using `/set_cookie <json_object>` or upload a `cookies.json` file."
                                )
                                notified_ids.add(member.id)
                            except Exception:
                                logging.exception(f"Failed to DM admin member {member.id} after malformed cookie deletion in guild {guild.id}")
                    except Exception:
                        logging.exception(f"Failed to notify admins in guild {guild.id} after malformed cookie deletion")
            else:
                COOKIES_MISSING = False
                logging.info("cookies.json present and has required shape.")
    except Exception:
        logging.exception("Error occurred while validating/removing cookies.json")


@bot.tree.command(name="get_courses", description="Show the current course_urls.json contents")
async def slash_get_courses(interaction: discord.Interaction):
    # Log who invoked the command
    try:
        guild_id = interaction.guild.id if interaction.guild else 'DM'
        logging.info(f"/get_courses requested by {interaction.user} (id={interaction.user.id}) in guild={guild_id}")
    except Exception:
        pass
    if not user_is_authorized(interaction.user, interaction.guild):
        try:
            await interaction.response.send_message("You are not authorized to use this command.", ephemeral=True)
        except discord.NotFound:
            try:
                await interaction.user.send("You are not authorized to use this command.")
            except Exception:
                logging.exception("Failed to DM user after interaction NotFound in get_courses")
        return
    urls = read_course_urls()
    text = json.dumps(urls, indent=2, ensure_ascii=False)
    if len(text) < 1900:
        try:
            await interaction.response.send_message(f"Current course URLs:\n```json\n{text}\n```")
        except discord.NotFound:
            try:
                await interaction.user.send(f"Current course URLs:\n```json\n{text}\n```")
            except Exception:
                logging.exception("Failed to DM user after interaction NotFound in get_courses")
    else:
        fp = io.BytesIO(text.encode("utf-8"))
        try:
            await interaction.response.send_message(file=discord.File(fp, filename="course_urls.json"))
        except discord.NotFound:
            try:
                await interaction.user.send(file=discord.File(fp, filename="course_urls.json"))
            except Exception:
                logging.exception("Failed to DM user after interaction NotFound in get_courses file send")


@bot.tree.command(name="add_course", description="Add a Moodle course URL to the scraper list")
@app_commands.describe(url="Full course URL (must include id=)")
async def slash_add_course(interaction: discord.Interaction, url: str):
    try:
        guild_id = interaction.guild.id if interaction.guild else 'DM'
        logging.info(f"/add_course requested by {interaction.user} (id={interaction.user.id}) in guild={guild_id} url={url}")
    except Exception:
        pass
    if not user_is_authorized(interaction.user, interaction.guild):
        try:
            await interaction.response.send_message("You are not authorized to use this command.", ephemeral=True)
        except discord.NotFound:
            try:
                await interaction.user.send("You are not authorized to use this command.")
            except Exception:
                logging.exception("Failed to DM user after interaction NotFound in add_course")
        return
    if not is_valid_course_url(url):
        await interaction.response.send_message("Invalid course URL. It must start with http(s) and include `id=` followed by digits.", ephemeral=True)
        return
    urls = read_course_urls()
    if url in urls:
        await interaction.response.send_message("This URL is already present.", ephemeral=True)
        return
    urls.append(url)
    write_course_urls(urls)
    await interaction.response.send_message(f"Added URL. Total courses: {len(urls)}")


@bot.tree.command(name="remove_course", description="Remove a Moodle course URL from the scraper list (requires confirmation)")
@app_commands.describe(url="Full course URL to remove")
async def slash_remove_course(interaction: discord.Interaction, url: str):
    try:
        guild_id = interaction.guild.id if interaction.guild else 'DM'
        logging.info(f"/remove_course requested by {interaction.user} (id={interaction.user.id}) in guild={guild_id} url={url}")
    except Exception:
        pass
    if not user_is_authorized(interaction.user, interaction.guild):
        try:
            await interaction.response.send_message("You are not authorized to use this command.", ephemeral=True)
        except discord.NotFound:
            try:
                await interaction.user.send("You are not authorized to use this command.")
            except Exception:
                logging.exception("Failed to DM user after interaction NotFound in remove_course")
        return
    urls = read_course_urls()
    if url not in urls:
        await interaction.response.send_message("URL not found in the list.", ephemeral=True)
        return

    # Ask for confirmation via reactions
    try:
        await interaction.response.send_message(f"Please confirm deletion of:\n{url}\nReact with ✅ to confirm or ❌ to cancel within 60 seconds.")
    except discord.NotFound:
        try:
            await interaction.user.send(f"Please confirm deletion of:\n{url}\nReact with ✅ to confirm or ❌ to cancel within 60 seconds.")
        except Exception:
            logging.exception("Failed to DM user after interaction NotFound in remove_course confirmation")
    msg = await interaction.original_response()
    try:
        await msg.add_reaction("✅")
        await msg.add_reaction("❌")
    except Exception:
        logging.exception("Failed to add reactions for confirmation")

    def check(reaction, user):
        return user.id == interaction.user.id and reaction.message.id == msg.id and str(reaction.emoji) in ("✅", "❌")

    try:
        reaction, user = await bot.wait_for("reaction_add", timeout=60.0, check=check)
    except asyncio.TimeoutError:
        try:
            await msg.edit(content=f"Deletion timed out. No changes made for:\n{url}")
        except Exception:
            pass
        return

    if str(reaction.emoji) == "✅":
        urls = [u for u in urls if u != url]
        write_course_urls(urls)
        try:
            await msg.edit(content=f"Deleted URL:\n{url}\nTotal courses: {len(urls)}")
        except Exception:
            pass
    else:
        try:
            await msg.edit(content=f"Deletion cancelled for:\n{url}")
        except Exception:
            pass


@bot.tree.command(name="get_cookie", description="Show masked MoodleSession cookie value (admins/whitelist only)")
async def get_cookie(interaction: discord.Interaction):
    try:
        guild_id = interaction.guild.id if interaction.guild else 'DM'
        logging.info(f"/get_cookie requested by {interaction.user} (id={interaction.user.id}) in guild={guild_id}")
    except Exception:
        pass
    if not user_is_authorized(interaction.user, interaction.guild):
        try:
            await interaction.response.send_message("You are not authorized to use this command.", ephemeral=True)
        except discord.NotFound:
            try:
                await interaction.user.send("You are not authorized to use this command.")
            except Exception:
                logging.exception("Failed to DM user after interaction NotFound in get_cookie auth check")
        return
    data = read_cookies()
    val = data.get('MoodleSession')
    # Acknowledge the interaction early to avoid 'The application did not respond'
    try:
        await interaction.response.defer(ephemeral=True)
    except Exception:
        # If defer fails, we'll still try followup or DM below
        pass

    if not val:
        try:
            await interaction.followup.send("No MoodleSession cookie is set.", ephemeral=True)
            return
        except discord.NotFound:
            try:
                await interaction.user.send("No MoodleSession cookie is set.")
            except Exception:
                logging.exception("Failed to DM user after interaction NotFound in get_cookie")
        except Exception:
            logging.exception("Failed to send followup in get_cookie; will attempt DM")
            try:
                await interaction.user.send("No MoodleSession cookie is set.")
            except Exception:
                logging.exception("Failed to DM user after followup failure in get_cookie")
        return

    masked = val[:4] + '...' + val[-4:]
    try:
        await interaction.followup.send(f"MoodleSession: `{masked}` (masked)", ephemeral=True)
    except discord.NotFound:
        # Interaction token expired / unknown; fallback to DM the user
        try:
            await interaction.user.send(f"MoodleSession: `{masked}` (masked)")
            logging.info(f"/get_cookie fallback DM sent to user id={interaction.user.id}")
        except Exception:
            logging.exception("Failed to DM user after interaction NotFound in get_cookie")
    except Exception:
        logging.exception("Failed to send followup in get_cookie; attempting DM")
        try:
            await interaction.user.send(f"MoodleSession: `{masked}` (masked)")
        except Exception:
            logging.exception("Failed to DM user after followup failure in get_cookie")


@bot.tree.command(name="set_cookie", description="Set MoodleSession cookie value (admins/whitelist only)")
@app_commands.describe(cookie_value="The full MoodleSession cookie string")
async def set_cookie(interaction: discord.Interaction, cookie_value: str):
    try:
        guild_id = interaction.guild.id if interaction.guild else 'DM'
        logging.info(f"/set_cookie requested by {interaction.user} (id={interaction.user.id}) in guild={guild_id}")
    except Exception:
        pass
    if not user_is_authorized(interaction.user, interaction.guild):
        await interaction.response.send_message("You are not authorized to use this command.", ephemeral=True)
        return
    global COOKIES_MISSING, cookies
    try:
        try:
            write_cookies(cookie_value)
        except ValueError as ve:
            try:
                await interaction.response.send_message(f"Invalid cookie format: {ve}", ephemeral=True)
            except discord.NotFound:
                try:
                    await interaction.user.send(f"Invalid cookie format: {ve}")
                except Exception:
                    logging.exception("Failed to DM user after ValueError in set_cookie")
            return

        # Reload cookies into memory
        try:
            new_cookies = read_cookies()
            cookies = new_cookies
            COOKIES_MISSING = False
            logging.info("cookies.json updated and reloaded into memory via /set_cookie")
        except Exception:
            logging.exception("Failed to reload cookies after write")

        try:
            await interaction.response.send_message("MoodleSession cookie updated and reloaded.", ephemeral=True)
        except discord.NotFound:
            # Interaction token may be unknown/expired; DM the user as a fallback confirmation
            try:
                await interaction.user.send("MoodleSession cookie updated and reloaded. (confirmation fallback)")
            except Exception:
                logging.exception("Failed to DM user after interaction response NotFound in set_cookie")
    except Exception:
        logging.exception("Failed to write cookies.json via set_cookie command")
        try:
            await interaction.response.send_message("Failed to update cookie. See logs.", ephemeral=True)
        except Exception:
            try:
                await interaction.user.send("Failed to update cookie. Check bot logs for details.")
            except Exception:
                logging.exception("Also failed to DM user after set_cookie failure")


def start_discord_bot():
    if not DISCORD_BOT_TOKEN:
        logging.info("DISCORD_BOT_TOKEN not set; Discord admin bot will not start.")
        return
    def _run():
        try:
            bot.run(DISCORD_BOT_TOKEN)
        except Exception as e:
            logging.exception(f"Discord bot stopped: {e}")
    thread = threading.Thread(target=_run, name="discord-bot-thread", daemon=True)
    thread.start()

def classify(title, desc):
    text = f"{title} {desc}".lower()
    if re.search(r'\bpost[- ]?lecture\b', text):
        return "post_lecture"
    elif re.search(r'\bpre[- ]?lecture\b', text):
        return "pre_lecture"
    elif re.search(r'\blecture\b', text):
        return "lecture"
    elif re.search(r'\btutorial\b', text):
        return "tutorial"
    return "others"

def parse_activities(activity_elements, enable_classification=True):
    categorized = {
        "pre_lecture": [], "lecture": [], "post_lecture": [],
        "tutorial": [], "others": [], "notices": []
    }

    for act in activity_elements:
        instancename = act.select_one(".instancename")
        link = act.select_one("a.aalink")
        desc_p = act.select_one("div.description p")
        desc_text = desc_p.get_text(strip=True) if desc_p else ""

        if instancename and link:
            for span in instancename.select("span.accesshide"):
                span.decompose()
            title = instancename.get_text(strip=True)
            url = link["href"]
            category = classify(title, desc_text) if enable_classification else "others"
            categorized[category].append({"title": title, "url": url})
        else:
            notice_div = act.select_one(".description-inner")
            if notice_div:
                notice_texts = [el.get_text(strip=True) for el in notice_div.select("h6 span")]
                if notice_texts:
                    full_notice = " ".join(notice_texts)
                    categorized["notices"].append({"notice": full_notice})

    return {k: v for k, v in categorized.items() if v}

def hash_data(data):
    return hashlib.md5(json.dumps(data, sort_keys=True).encode()).hexdigest()

def send_discord_notification(course_title, section, item):
    # Convert the item to a discord.Embed and send via the bot to the configured channel
    is_resource = isinstance(item, dict) and "url" in item
    is_notice = isinstance(item, dict) and "notice" in item

    title_text = item.get("title") if is_resource else ("New Notice" if is_notice else "Update")
    url = item.get("url") if is_resource else None
    description = item.get("notice") if is_notice else (f"[Open resource]({url})" if url else "")

    embed = discord.Embed(title=title_text, description=description, color=0x5865F2, timestamp=datetime.now(timezone.utc))
    if url:
        embed.url = url
    embed.add_field(name="Course", value=course_title, inline=True)
    embed.add_field(name="Section", value=section, inline=True)
    embed.add_field(name="Type", value=("Notice" if is_notice else ("Resource" if is_resource else "Other")), inline=True)
    embed.set_footer(text="lms-scraper")

    if not DISCORD_NOTIFY_CHANNEL_ID:
        logging.warning("DISCORD_NOTIFY_CHANNEL_ID not set; skipping bot-based notification")
        return

    try:
        channel_id = int(DISCORD_NOTIFY_CHANNEL_ID)
    except Exception:
        logging.error("DISCORD_NOTIFY_CHANNEL_ID is not a valid integer channel id")
        return

    if not bot.is_ready():
        logging.warning("Discord bot not ready yet; cannot send notification")
        return

    async def _send():
        channel = bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await bot.fetch_channel(channel_id)
            except Exception as exc:
                logging.exception(f"Failed to fetch notify channel {channel_id}: {exc}")
                return
        try:
            await channel.send(content="@here 📢 New course update", embed=embed)
        except Exception:
            logging.exception("Failed to send embed notification via bot")

    try:
        asyncio.run_coroutine_threadsafe(_send(), bot.loop)
    except Exception:
        logging.exception("Failed to schedule embed send on bot loop")

def scrape_course(url):
    req_cookies = read_cookies()
    response = requests.get(url, cookies=req_cookies, headers=headers, verify=False)
    soup = BeautifulSoup(response.text, "html.parser")
    title = soup.find("h1").get_text(strip=True)
    course_data = {}

    general_activities = soup.select("ul.general-section-activities > li.activity")
    if general_activities:
        course_data["General Activities"] = parse_activities(general_activities, False)

    sections = soup.select("li.section.main")
    for section in sections:
        section_title_el = section.select_one(".sectionname")
        if not section_title_el:
            continue
        section_title = section_title_el.get_text(strip=True)
        activity_elements = section.select("li.activity")
        is_week_section = section_title.lower().startswith("week")
        parsed = parse_activities(activity_elements, enable_classification=is_week_section)
        if parsed:
            course_data[section_title] = parsed

    return title, course_data

start_discord_bot()

while True:
    for url in read_course_urls():
        try:
            course_id = url.split("id=")[-1]
            title, data = scrape_course(url)
            data_hash = hash_data(data)

            prev_hash = previous_data.get(course_id, {}).get("hash")
            if prev_hash != data_hash:
                logging.info(f"[+] Change detected in {title}")
                # Compare and send changes
                old_data = previous_data.get(course_id, {}).get("data", {})
                for section, entries in data.items():
                    for key in entries:
                        new_items = [i for i in entries[key] if i not in old_data.get(section, {}).get(key, [])]
                        for item in new_items:
                            send_discord_notification(title, section, item)

                previous_data[course_id] = {
                    "title": title,
                    "hash": data_hash,
                    "data": data
                }

        except Exception as e:
            logging.error(f"[!] Error fetching {url}: {e}")

    with open("scraper_state.json", "w", encoding="utf-8") as f:
        json.dump(previous_data, f, indent=2, ensure_ascii=False)

    time.sleep(120)