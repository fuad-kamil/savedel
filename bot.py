"""
Telegram Business Anti-Delete Bot
Saves ALL messages (including timed/view-once media) and alerts on deletion/edit.
"""

import sys
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import os, html, logging, atexit, threading, asyncio, time
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional, Any
from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, BotCommand, MenuButtonCommands,
    BotCommandScopeChat, BotCommandScopeDefault,
)
from telegram.constants import ParseMode
from telegram.request import HTTPXRequest
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    BusinessConnectionHandler, BusinessMessagesDeletedHandler,
    CallbackQueryHandler, ContextTypes, filters,
)
import database

# ── Config ────────────────────────────────────────────────────────────────────
load_dotenv()
BOT_TOKEN          = os.getenv("BOT_TOKEN", "")
FALLBACK_OWNER     = os.getenv("OWNER_ID", "")
ADMIN_IDS_RAW      = os.getenv("ADMIN_IDS", "")
BACKUP_CHANNEL     = os.getenv("BACKUP_CHANNEL_ID", "")
CLEANUP_TEMP_MEDIA = os.getenv("CLEANUP_TEMP_MEDIA", "false").lower() in ("true", "1", "yes")
PORT               = os.getenv("PORT", "")
BASE_DIR           = os.path.dirname(os.path.abspath(__file__))
MEDIA_DIR          = os.path.join(BASE_DIR, "media")
LOCK_FILE          = os.path.join(BASE_DIR, ".bot.lock")
os.makedirs(MEDIA_DIR, exist_ok=True)

# Parse configured admin IDs from ADMIN_IDS and OWNER_ID
ADMIN_IDS: set[int] = set()
for _raw in (ADMIN_IDS_RAW, FALLBACK_OWNER):
    for _p in _raw.split(","):
        _p = _p.strip()
        if _p.lstrip("-").isdigit():
            ADMIN_IDS.add(int(_p))

# ── Cloudinary Media Backup Configuration ─────────────────────────────────────
CLOUDINARY_URL = os.getenv("CLOUDINARY_URL", "")
try:
    import cloudinary
    import cloudinary.uploader
    CLOUDINARY_AVAILABLE = bool(CLOUDINARY_URL)
except ImportError:
    CLOUDINARY_AVAILABLE = False


def upload_to_cloudinary(
    local_path: Optional[str],
    mtype: str,
    chat_id: Optional[int] = None,
    msg_id: Optional[int] = None,
) -> tuple[Optional[str], Optional[str]]:
    """
    Upload media file to Cloudinary if configured.
    Uses deterministic public_id (tg_{chat_id}_{msg_id}) with overwrite=True
    to guarantee no duplicate assets are ever created in Cloudinary.
    """
    if not CLOUDINARY_AVAILABLE or not CLOUDINARY_URL or not local_path or not os.path.exists(local_path):
        return None, None
    try:
        rtype = "video" if mtype in ("video", "voice", "audio", "video_note") else ("image" if mtype in ("photo", "sticker") else "auto")
        upload_kwargs: dict[str, Any] = {"resource_type": rtype}
        if chat_id is not None and msg_id is not None:
            upload_kwargs["public_id"] = f"tg_{abs(chat_id)}_{msg_id}"
            upload_kwargs["overwrite"] = True
            upload_kwargs["unique_filename"] = False

        res = cloudinary.uploader.upload(local_path, **upload_kwargs)
        url = res.get("secure_url")
        pid = res.get("public_id")
        logger.info(f"  [CLOUDINARY] Uploaded {mtype} pid={pid} => {url}")
        return url, pid
    except Exception as e:
        logger.warning(f"  [CLOUDINARY FAIL] {e}")
        return None, None


def extract_cloudinary_public_id(cloud_url: Optional[str]) -> Optional[str]:
    """Extract Cloudinary public_id from a secure_url."""
    if not cloud_url or "cloudinary.com" not in cloud_url:
        return None
    try:
        parts = cloud_url.split("/upload/")
        if len(parts) < 2:
            return None
        sub = parts[1]
        if sub.startswith("v") and "/" in sub:
            sub = sub.split("/", 1)[1]
        return os.path.splitext(sub)[0]
    except Exception:
        return None


def delete_from_cloudinary(
    public_id: Optional[str] = None,
    cloud_url: Optional[str] = None,
    mtype: str = "auto",
    chat_id: Optional[int] = None,
    msg_id: Optional[int] = None,
) -> bool:
    """
    Delete a media asset from Cloudinary using public_id, cloud_url, or chat+msg ID.
    Attempts deletion across multiple resource types (image, video, raw) and candidate IDs
    so no duplicate or orphaned copies are left behind in Cloudinary.
    """
    if not CLOUDINARY_AVAILABLE or not CLOUDINARY_URL:
        return False

    candidate_pids = set()
    if public_id:
        candidate_pids.add(public_id)
    url_pid = extract_cloudinary_public_id(cloud_url)
    if url_pid:
        candidate_pids.add(url_pid)
    if chat_id is not None and msg_id is not None:
        candidate_pids.add(f"tg_{abs(chat_id)}_{msg_id}")

    if not candidate_pids:
        return False

    success = False
    resource_types = ["image", "video", "raw"]
    primary_rtype = "video" if mtype in ("video", "voice", "audio", "video_note") else ("image" if mtype in ("photo", "sticker") else "raw")
    if primary_rtype in resource_types:
        resource_types.remove(primary_rtype)
        resource_types.insert(0, primary_rtype)

    for pid in candidate_pids:
        for rtype in resource_types:
            try:
                res = cloudinary.uploader.destroy(pid, resource_type=rtype)
                logger.info(f"  [CLOUDINARY DESTROY] pid={pid} rtype={rtype} res={res}")
                if res.get("result") in ("ok", "not found"):
                    if res.get("result") == "ok":
                        success = True
            except Exception as e:
                logger.warning(f"  [CLOUDINARY DESTROY FAIL] pid={pid} rtype={rtype}: {e}")

    return success


async def bg_upload_to_cloudinary(local_path: Optional[str], mtype: str, chat_id: int, msg_id: int) -> None:
    """Run Cloudinary upload in a background thread so the Telegram event loop is never blocked."""
    if not CLOUDINARY_AVAILABLE or not CLOUDINARY_URL or not local_path or not os.path.exists(local_path):
        return
    try:
        url, pid = await asyncio.to_thread(upload_to_cloudinary, local_path, mtype, chat_id, msg_id)
        if url and pid:
            database.update_message_cloud_info(chat_id, msg_id, url, pid)
            logger.info(f"  [BG CLOUDINARY OK] chat={chat_id} msg={msg_id} pid={pid}")
    except Exception as e:
        logger.warning(f"  [BG CLOUDINARY FAIL] chat={chat_id} msg={msg_id}: {e}")


def is_admin(user_id: Optional[int]) -> bool:
    """Check if a given Telegram user ID has admin rights."""
    if not user_id:
        return False
    if user_id in ADMIN_IDS:
        return True
    try:
        if FALLBACK_OWNER and int(FALLBACK_OWNER) == user_id:
            return True
    except (ValueError, TypeError):
        pass
    # Check if marked admin in users DB
    try:
        if user_id in database.get_admin_user_ids():
            return True
    except Exception:
        pass
    # Owners of an active Telegram Business connection are automatically admins
    conn = database.get_active_connection_by_user(user_id)
    return bool(conn)


def get_reply_keyboard(admin: bool = False) -> ReplyKeyboardMarkup:
    """Return persistent bottom reply keyboard so users and admins never have to type /start manually."""
    if admin:
        keyboard = [
            [KeyboardButton("🚀 Start / Menu"), KeyboardButton("👑 Admin Dashboard")],
            [KeyboardButton("💾 Saved Messages"), KeyboardButton("📊 System Status")],
        ]
    else:
        keyboard = [
            [KeyboardButton("🚀 Start / Menu"), KeyboardButton("💾 My Saved Messages")],
        ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, is_persistent=True)


_recent_alerts: dict[str, float] = {}

def is_duplicate_alert(alert_key: str, cooldown_seconds: float = 4.0) -> bool:
    """Return True if this exact alert key was triggered within the cooldown window."""
    now = time.time()
    for k in list(_recent_alerts.keys()):
        if now - _recent_alerts[k] > 60.0:
            _recent_alerts.pop(k, None)
    last_time = _recent_alerts.get(alert_key)
    if last_time and (now - last_time) < cooldown_seconds:
        return True
    _recent_alerts[alert_key] = now
    return False


def get_admin_notification_targets() -> set[int]:
    """Retrieve unique admin and active business owner IDs to receive operational notifications."""
    targets: set[int] = set()
    for aid in ADMIN_IDS:
        targets.add(int(aid))
    if FALLBACK_OWNER and str(FALLBACK_OWNER).lstrip("-").isdigit():
        targets.add(int(FALLBACK_OWNER))
    try:
        for uid in database.get_admin_user_ids():
            targets.add(int(uid))
    except Exception:
        pass
    # Business connection owners are primary bot administrators and must always receive alerts
    try:
        for conn in database.get_active_connections():
            uid = conn.get("user_chat_id") or conn.get("user_id")
            if uid:
                targets.add(int(uid))
    except Exception:
        pass
    return targets


async def notify_admins(
    ctx: ContextTypes.DEFAULT_TYPE,
    text: str,
    exclude_id: Optional[Any] = None,
) -> None:
    """Deliver an operational notification to all admins and owners with format fallback."""
    exclude_int = None
    if exclude_id is not None:
        try:
            exclude_int = int(exclude_id)
        except (ValueError, TypeError):
            exclude_int = None

    targets = get_admin_notification_targets()
    for tid in targets:
        if exclude_int is not None and int(tid) == exclude_int:
            continue
        try:
            await ctx.bot.send_message(
                chat_id=tid,
                text=text,
                parse_mode=ParseMode.HTML,
            )
        except Exception as e_html:
            # Fallback to plain text if Telegram fails on HTML tags
            try:
                import re
                plain = re.sub(r"<[^>]+>", "", text)
                await ctx.bot.send_message(
                    chat_id=tid,
                    text=plain,
                )
            except Exception as e_plain:
                logger.warning(f"[ADMIN NOTIFY FAIL] to={tid}: {e_html} / {e_plain}")


# Commands visible to all regular (non-admin) users
_USER_COMMANDS = [
    BotCommand("start", "🚀 Start / Main Menu"),
    BotCommand("saved", "💾 My Saved Messages"),
]

# Full command set visible only to administrators
_ADMIN_COMMANDS = [
    BotCommand("start", "🚀 Start / Main Menu"),
    BotCommand("saved", "💾 My Saved Messages"),
    BotCommand("status", "📊 System Status"),
    BotCommand("admin", "👑 Admin Dashboard"),
]


async def post_init(application: Any) -> None:
    """Register persistent Telegram commands scoped per user role, and set chat menu button."""
    try:
        # 1. Set default commands for all users (non-admin): only /start and /saved
        await application.bot.set_my_commands(
            _USER_COMMANDS,
            scope=BotCommandScopeDefault(),
        )

        # 2. Override commands for each known admin with the full admin command set
        admin_ids = get_admin_notification_targets()
        for admin_id in admin_ids:
            try:
                await application.bot.set_my_commands(
                    _ADMIN_COMMANDS,
                    scope=BotCommandScopeChat(chat_id=admin_id),
                )
                logger.info(f"[INIT] Set admin commands for user_id={admin_id}")
            except Exception as e_scope:
                logger.warning(f"[INIT] Could not scope commands for admin {admin_id}: {e_scope}")

        # 3. Set native Telegram Chat Menu button (the blue button next to text input)
        await application.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        logger.info(f"[INIT] Registered commands (default={len(_USER_COMMANDS)}, admins={len(admin_ids)}) and Chat Menu button")
    except Exception as e:
        logger.warning(f"[INIT] Could not set bot menu commands: {e}")


async def set_admin_commands_for(bot: Any, admin_id: int) -> None:
    """Promote a specific user ID to show the full admin command set."""
    try:
        await bot.set_my_commands(
            _ADMIN_COMMANDS,
            scope=BotCommandScopeChat(chat_id=admin_id),
        )
        logger.info(f"[CMD SCOPE] Admin commands applied to user_id={admin_id}")
    except Exception as e:
        logger.warning(f"[CMD SCOPE FAIL] user_id={admin_id}: {e}")




# ── Render Health Server ───────────────────────────────────────────────────────
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"ok","service":"telegram-anti-delete-bot"}')

    def log_message(self, format, *args):
        return  # Suppress noisy health-check logs

def start_health_server():
    """Start lightweight HTTP server for Render free web service health checks."""
    port_str = os.getenv("PORT")
    if not port_str:
        return
    try:
        port = int(port_str)
        server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        logger.info(f"Health server running on port {port} (Render ready)")
    except Exception as e:
        logger.warning(f"Failed to start health server on port {port_str}: {e}")


# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(BASE_DIR, "bot.log"), encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

EXT_MAP = {
    "photo": "jpg", "video": "mp4", "voice": "ogg", "video_note": "mp4",
    "audio": "mp3", "animation": "gif", "document": "bin", "sticker": "webp",
}


_lock_file_handle = None

def acquire_lock() -> bool:
    """Prevent two bot copies running at once (causes 409 Conflict errors)."""
    global _lock_file_handle
    try:
        import msvcrt
        _lock_file_handle = open(LOCK_FILE, "a+")
        msvcrt.locking(_lock_file_handle.fileno(), msvcrt.LK_NBLCK, 1)
        _lock_file_handle.seek(0)
        _lock_file_handle.truncate()
        _lock_file_handle.write(str(os.getpid()))
        _lock_file_handle.flush()
        return True
    except (IOError, OSError):
        logger.error("Another bot instance is already running (locked by OS).")
        return False
    except ImportError:
        # Non-Windows fallback
        if os.path.exists(LOCK_FILE):
            return False
        with open(LOCK_FILE, "w") as f:
            f.write(str(os.getpid()))
        atexit.register(lambda: os.path.exists(LOCK_FILE) and os.remove(LOCK_FILE))
        return True


# ── Helpers ───────────────────────────────────────────────────────────────────
def esc(t: Optional[str]) -> str:
    return html.escape(t or "")


def extract_media(obj: Any) -> tuple:
    """Return (media_type, file_id) or (None, None)."""
    if not obj:
        return None, None
    if getattr(obj, "photo", None):
        return "photo", obj.photo[-1].file_id
    for a in ("video", "voice", "video_note", "document", "audio", "animation", "sticker"):
        v = getattr(obj, a, None)
        if v:
            return a, v.file_id
    return None, None


async def dl(ctx: ContextTypes.DEFAULT_TYPE, file_id: str,
             mtype: str, chat_id: int, msg_id: int) -> Optional[str]:
    """Download media to disk immediately. Returns local path or None."""
    path = os.path.join(MEDIA_DIR,
                        f"{chat_id}_{msg_id}_{mtype}.{EXT_MAP.get(mtype, 'bin')}")
    if os.path.exists(path):
        return path
    try:
        tg = await ctx.bot.get_file(file_id)
        await tg.download_to_drive(custom_path=path)
        logger.info(f"  [SAVED] {mtype} => {os.path.basename(path)}")
        return path
    except Exception as e:
        logger.warning(f"  [DL FAIL] {mtype}: {e}")
        return None


async def dl_media_obj(ctx: ContextTypes.DEFAULT_TYPE, obj: Any,
                       mtype: str, chat_id: int, msg_id: int) -> tuple[Optional[str], Optional[str]]:
    """
    Download media to disk. If photo has multiple resolutions, tries from highest
    down to thumbnail in case full-resolution is restricted by Telegram server.
    Returns (local_path, file_id).
    """
    if mtype == "photo" and getattr(obj, "photo", None):
        for ps in reversed(obj.photo):
            lp = await dl(ctx, ps.file_id, "photo", chat_id, msg_id)
            if lp:
                return lp, ps.file_id
        return None, (obj.photo[-1].file_id if obj.photo else None)

    _, fid = extract_media(obj)
    if fid:
        lp = await dl(ctx, fid, mtype, chat_id, msg_id)
        return lp, fid
    return None, None


async def send_owner(ctx: ContextTypes.DEFAULT_TYPE, oid: int,
                     mtype: str, lpath: Optional[str], fid: Optional[str],
                     header: str, body: str,
                     reply_markup: Optional[InlineKeyboardMarkup] = None,
                     cloud_url: Optional[str] = None) -> None:
    """Send media (from disk, Cloudinary, or file_id) or plain text to owner and optional backup channel."""
    caption = f"{header}\n{body}" if body else header
    has = bool(lpath and os.path.exists(lpath))
    targets = [oid]
    if BACKUP_CHANNEL and BACKUP_CHANNEL != str(oid):
        targets.append(BACKUP_CHANNEL)

    for target in targets:
        try:
            target_chat_id = int(target) if str(target).lstrip("-").isdigit() else target
            if not (has or fid or cloud_url):
                await ctx.bot.send_message(
                    chat_id=target_chat_id, text=caption,
                    parse_mode=ParseMode.HTML, reply_markup=reply_markup
                )
                continue
            p = open(lpath, "rb") if has else (cloud_url or fid)
            try:
                if   mtype == "photo":      await ctx.bot.send_photo(chat_id=target_chat_id, photo=p, caption=caption, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
                elif mtype == "video":      await ctx.bot.send_video(chat_id=target_chat_id, video=p, caption=caption, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
                elif mtype == "voice":      await ctx.bot.send_voice(chat_id=target_chat_id, voice=p, caption=caption, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
                elif mtype == "video_note":
                    await ctx.bot.send_message(chat_id=target_chat_id, text=header, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
                    await ctx.bot.send_video_note(chat_id=target_chat_id, video_note=p)
                elif mtype == "audio":      await ctx.bot.send_audio(chat_id=target_chat_id, audio=p, caption=caption, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
                elif mtype == "document":   await ctx.bot.send_document(chat_id=target_chat_id, document=p, caption=caption, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
                elif mtype == "animation":  await ctx.bot.send_animation(chat_id=target_chat_id, animation=p, caption=caption, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
                elif mtype == "sticker":
                    await ctx.bot.send_message(chat_id=target_chat_id, text=f"{header}\n[Sticker]", parse_mode=ParseMode.HTML, reply_markup=reply_markup)
                    await ctx.bot.send_sticker(chat_id=target_chat_id, sticker=p)
                else:
                    await ctx.bot.send_message(chat_id=target_chat_id, text=caption, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
            finally:
                if hasattr(p, "close"):
                    p.close()
        except Exception as e:
            logger.error(f"  [SEND FAIL to {target}] {e}")

    # On cloud deployments with low disk space, clean up local temp file after sending
    if CLEANUP_TEMP_MEDIA and has and lpath:
        try:
            os.remove(lpath)
            logger.info(f"  [CLEANUP] removed local temp media: {os.path.basename(lpath)}")
        except Exception as ce:
            logger.warning(f"  [CLEANUP FAIL] {ce}")



async def cache_msg(ctx: ContextTypes.DEFAULT_TYPE, obj: Any,
                    conn_id: str, chat_id: int, msg_id: int,
                    date_iso: str) -> Optional[str]:
    """Save any message-like object to DB + download media. Returns local_path."""
    s = getattr(obj, "from_user", None)
    if s and getattr(s, "id", None):
        database.record_user(s.id, s.full_name or "Unknown", s.username)

    existing = database.get_message(chat_id, msg_id)
    if existing and existing.get("cloud_url") and existing.get("cloudinary_public_id"):
        logger.info(f"  [MSG CACHED] chat={chat_id} id={msg_id} already saved in DB & Cloudinary")
        return existing.get("local_path")

    mt, fid = extract_media(obj)
    is_timed = bool(
        getattr(obj, "ttl_period", None) or
        (getattr(obj, "has_media_spoiler", False) and getattr(obj, "voice", None))
    )
    logger.info(
        f"  [MSG] chat={chat_id} id={msg_id} "
        f"from='{s.full_name if s else '?'}' type={mt or 'text'}{' [TIMED]' if is_timed else ''}"
    )
    lp = await dl(ctx, fid, mt, chat_id, msg_id) if (mt and fid) else None
    database.save_message(
        business_connection_id=conn_id,
        chat_id=chat_id, message_id=msg_id,
        sender_id=s.id if s else None,
        sender_name=s.full_name if s else "Unknown",
        sender_username=s.username if s else None,
        text=getattr(obj, "text", None),
        media_type=mt, media_file_id=fid,
        caption=getattr(obj, "caption", None),
        created_at=date_iso, local_path=lp,
        is_timed=is_timed,
    )
    if lp and mt:
        asyncio.create_task(bg_upload_to_cloudinary(lp, mt, chat_id, msg_id))
    return lp



async def get_owner(ctx: ContextTypes.DEFAULT_TYPE, conn_id: str) -> Optional[int]:
    """Retrieve owner user_chat_id from DB, or fetch directly from Telegram API if not cached."""
    if not conn_id:
        try:
            return int(FALLBACK_OWNER) if FALLBACK_OWNER else None
        except (ValueError, TypeError):
            return None

    info = database.get_connection(conn_id)
    if info and info.get("user_chat_id"):
        return info["user_chat_id"]

    try:
        conn = await ctx.bot.get_business_connection(conn_id)
        if conn and conn.user_chat_id:
            database.save_or_update_connection(
                conn_id, conn.user.id, conn.user_chat_id, conn.is_enabled
            )
            logger.info(f"[CONN AUTO-RECOVER] id={conn_id} owner={conn.user_chat_id}")
            return conn.user_chat_id
    except Exception as e:
        logger.warning(f"Could not fetch business connection {conn_id}: {e}")

    try:
        return int(FALLBACK_OWNER) if FALLBACK_OWNER else None
    except (ValueError, TypeError):
        return None


# ── Handlers ──────────────────────────────────────────────────────────────────
async def on_connection(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    c = update.business_connection
    if not c:
        return
    logger.info(f"[CONN] id={c.id} user={c.user.id} enabled={c.is_enabled}")
    database.save_or_update_connection(c.id, c.user.id, c.user_chat_id, c.is_enabled)
    text = (
        "<b>Bot connected!</b>\n\n"
        "I am now watching ALL your chats including timed/view-once media.\n\n"
        "<b>Timed media trick:</b> Swipe-reply a timed message BEFORE opening it "
        "and I will capture it instantly!"
        if c.is_enabled else
        "<b>Bot disconnected</b> from your Telegram Business account."
    )
    try:
        await ctx.bot.send_message(chat_id=c.user_chat_id, text=text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"[CONN NOTIFY] {e}")

    conn_alert = (
        f"🔔 <b>Admin Notification: Business Connection Changed</b>\n\n"
        f"👤 <b>User ID:</b> <code>{c.user.id}</code>\n"
        f"💬 <b>Chat ID:</b> <code>{c.user_chat_id}</code>\n"
        f"⚙️ <b>Change Type:</b> 🔗 <code>{'Business Connected' if c.is_enabled else 'Business Disconnected'}</code>\n"
        f"🕒 <b>Connection ID:</b> <code>{c.id}</code>"
    )
    await notify_admins(ctx, conn_alert, exclude_id=c.user_chat_id)


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Called for every business message (sent or received).
    Also detects when the user replies to a timed/view-once message
    and immediately downloads + delivers that timed media.
    """
    msg = update.business_message
    if not msg:
        return

    cid   = msg.chat.id
    mid   = msg.message_id
    conn  = msg.business_connection_id
    date  = msg.date.isoformat()
    logger.info(f"[BUSINESS MSG] chat={cid} id={mid}")

    # 1. Save the message itself (text, photo, video, voice, etc.)
    await cache_msg(ctx, msg, conn, cid, mid, date)

    # 2. Timed / View-Once interception
    seen_rmids: set[int] = set()
    for replied in (msg.reply_to_message, msg.external_reply):
        if not replied:
            continue
        rtype, rfid = extract_media(replied)
        rmid = getattr(replied, "message_id", None)
        if not (rtype and rfid and rmid):
            continue
        if rmid in seen_rmids:
            continue
        seen_rmids.add(rmid)
        if is_duplicate_alert(f"timed_{cid}_{rmid}"):
            continue

        rs    = getattr(replied, "from_user", None)
        rname = rs.full_name if rs else "Unknown"
        rcap  = getattr(replied, "caption", None) or getattr(replied, "text", None)
        rdate = (
            replied.date.isoformat()
            if hasattr(replied, "date") and replied.date
            else date
        )

        existing = database.get_message(cid, rmid)
        rpath = existing.get("local_path") if existing else None
        rcurl = existing.get("cloud_url") if existing else None
        rcpid = existing.get("cloudinary_public_id") if existing else None

        # Download timed media if not already saved to disk
        if not (rpath and os.path.exists(rpath)):
            logger.info(f"  [TIMED] capturing {rtype} msg_id={rmid} from {rname}")
            rpath, effective_fid = await dl_media_obj(ctx, replied, rtype, cid, rmid)
            rfid = effective_fid or rfid

        # Save to database immediately as timed media
        database.save_message(
            business_connection_id=conn,
            chat_id=cid, message_id=rmid,
            sender_id=rs.id if rs else (existing.get("sender_id") if existing else None),
            sender_name=rname if rname != "Unknown" else ((existing.get("sender_name") or "Unknown") if existing else "Unknown"),
            sender_username=rs.username if rs else (existing.get("sender_username") if existing else None),
            text=None, media_type=rtype, media_file_id=rfid,
            caption=rcap or (existing.get("caption") if existing else None),
            created_at=rdate, local_path=rpath,
            is_timed=True,
            cloud_url=rcurl,
            cloudinary_public_id=rcpid,
        )

        # Non-blocking background upload to Cloudinary
        if rpath and os.path.exists(rpath) and not rcurl:
            asyncio.create_task(bg_upload_to_cloudinary(rpath, rtype, cid, rmid))

        oid = await get_owner(ctx, conn)
        if not oid:
            logger.warning(f"[TIMED] No owner resolved for conn={conn}")
            continue

        cname = msg.chat.title or msg.chat.first_name or str(cid)
        rsid = rs.id if rs else "Unknown"
        runame = f"@{rs.username}" if (rs and rs.username) else "None"

        header = (
            f"🔔 <b>Admin Notification: Timed Media Saved</b>\n\n"
            f"👤 <b>From:</b> {esc(rname)} ({esc(runame)})\n"
            f"🆔 <b>Sender ID:</b> <code>{rsid}</code>\n"
            f"📬 <b>To / Chat:</b> {esc(cname)} (Chat ID: <code>{cid}</code>)\n"
            f"🔢 <b>Message ID:</b> <code>{rmid}</code>\n"
            f"⚙️ <b>Change Type:</b> ⏱️ <code>Timed / View-Once Media Saved</code>\n"
            f"📦 <b>Type:</b> <code>{rtype}</code>\n"
            f"<i>Saved before it expires!</i>"
        )
        body = f"\nCaption: <blockquote>{esc(rcap)}</blockquote>" if rcap else ""

        delivered = False

        # Strategy 1: Deliver downloaded file or existing cloud URL
        if rpath and os.path.exists(rpath):
            try:
                await send_owner(ctx, oid, rtype, rpath, rfid, header, body)
                delivered = True
                logger.info(f"  [TIMED SUCCESS] Delivered to owner={oid} via local file")
            except Exception as e_send:
                logger.warning(f"  [TIMED SEND FAIL] {e_send}")
        elif rcurl:
            try:
                await send_owner(ctx, oid, rtype, None, rfid, header, body, cloud_url=rcurl)
                delivered = True
                logger.info(f"  [TIMED SUCCESS] Delivered to owner={oid} via cloud_url")
            except Exception as e_cloud:
                logger.warning(f"  [TIMED CLOUD FAIL] {e_cloud}")

        # Strategy 2: Copy message directly from chat to owner
        if not delivered:
            try:
                await ctx.bot.copy_message(
                    chat_id=oid,
                    from_chat_id=cid,
                    message_id=rmid,
                    caption=f"{header}{body}",
                    parse_mode=ParseMode.HTML,
                )
                delivered = True
                logger.info(f"  [TIMED SUCCESS] Delivered to owner={oid} via copy_message")
            except Exception as e_copy:
                logger.warning(f"  [TIMED COPY FAIL] {e_copy}")

        # Strategy 3: Forward original message directly to owner
        if not delivered:
            try:
                await ctx.bot.forward_message(
                    chat_id=oid,
                    from_chat_id=cid,
                    message_id=rmid,
                )
                delivered = True
                logger.info(f"  [TIMED SUCCESS] Delivered to owner={oid} via forward_message")
            except Exception as e_fwd:
                logger.warning(f"  [TIMED FORWARD FAIL] {e_fwd}")

        # Strategy 4: Deliver directly using Telegram file_id
        if not delivered and rfid:
            try:
                await send_owner(ctx, oid, rtype, None, rfid, header, body)
                delivered = True
                logger.info(f"  [TIMED SUCCESS] Delivered to owner={oid} via Telegram file_id")
            except Exception as e_fid:
                logger.warning(f"  [TIMED FID FAIL] {e_fid}")

        # Strategy 5: Notice alert if Telegram server prevents all automated delivery
        if not delivered:
            await ctx.bot.send_message(
                chat_id=oid,
                parse_mode=ParseMode.HTML,
                text=(
                    f"⏱️ <b>Timed Media Alert!</b>\n\n"
                    f"👤 <b>Sender:</b> {esc(rname)} ({esc(runame)})\n"
                    f"🆔 <b>Sender ID:</b> <code>{rsid}</code>\n"
                    f"💬 <b>Chat:</b> {esc(cname)}\n"
                    f"🏷️ <b>Chat ID:</b> <code>{cid}</code>\n"
                    f"🔢 <b>Message ID:</b> <code>{rmid}</code>\n"
                    f"📦 <b>Type:</b> <code>{rtype}</code>{body}\n\n"
                    f"<i>Note: If media is view-once encrypted, view it in chat before expiration.</i>"
                ),
            )

        # Notify other admins about timed media intercept
        await notify_admins(ctx, f"{header}\n{body or ''}", exclude_id=oid)

async def on_edit(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.edited_business_message or update.edited_message
    if not msg:
        return
    cid = msg.chat.id
    mid = msg.message_id
    conn = getattr(msg, "business_connection_id", None) or "direct"
    new = msg.text or msg.caption or "[media]"
    content_hash = hash(new)
    if is_duplicate_alert(f"edit_{cid}_{mid}_{content_hash}", cooldown_seconds=2.0):
        return

    old_d = database.record_message_edit(cid, mid, new)
    oid = await get_owner(ctx, conn) if conn and conn != "direct" else None
    sn = msg.from_user.full_name if msg.from_user else "Unknown"
    sid = msg.from_user.id if msg.from_user else "Unknown"
    un = f"@{msg.from_user.username}" if (msg.from_user and msg.from_user.username) else "None"
    cn = msg.chat.title or msg.chat.first_name or str(cid)
    old = (old_d.get("text") or old_d.get("caption") or "[not cached]") if old_d else "[not cached]"

    admin_edit_alert = (
        f"🔔 <b>Admin Notification: Message Edited</b>\n\n"
        f"👤 <b>From:</b> {esc(sn)} ({esc(un)})\n"
        f"🆔 <b>Sender ID:</b> <code>{sid}</code>\n"
        f"📬 <b>To / Chat:</b> {esc(cn)} (Chat ID: <code>{cid}</code>)\n"
        f"🔢 <b>Message ID:</b> <code>{mid}</code>\n"
        f"⚙️ <b>Change Type:</b> ✏️ <code>Message Content Edited</code>\n\n"
        f"<b>Before:</b>\n<blockquote>{esc(old)}</blockquote>\n\n"
        f"<b>After:</b>\n<blockquote>{esc(new)}</blockquote>"
    )

    if oid:
        try:
            await ctx.bot.send_message(chat_id=oid, parse_mode=ParseMode.HTML, text=admin_edit_alert)
        except Exception as e:
            logger.error(f"[EDIT NOTIFY to owner={oid}] {e}")

    await notify_admins(ctx, admin_edit_alert, exclude_id=oid)


async def on_deleted(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    di = update.deleted_business_messages
    if not di:
        return
    cid    = di.chat.id
    conn   = di.business_connection_id
    ids    = list(di.message_ids)
    cname  = di.chat.title or di.chat.first_name or str(cid)
    logger.info(f"[DELETED] chat={cid} ids={ids}")

    recovered = database.mark_messages_deleted(cid, ids)
    oid = await get_owner(ctx, conn)
    if not oid:
        logger.warning(f"[DELETED] no owner for conn={conn}")
        return

    if not recovered:
        untracked_alert = (
            f"🔔 <b>Admin Notification: Deletion Detected</b>\n\n"
            f"📬 <b>To / Chat:</b> {esc(cname)} (Chat ID: <code>{cid}</code>)\n"
            f"🔢 <b>Message IDs:</b> <code>{ids}</code>\n"
            f"⚙️ <b>Change Type:</b> 🗑️ <code>Untracked Message Deleted</code>\n"
            f"<i>{len(ids)} message(s) deleted before the bot started.</i>"
        )
        try:
            await ctx.bot.send_message(chat_id=oid, parse_mode=ParseMode.HTML, text=untracked_alert)
        except Exception as e:
            logger.error(f"[DELETE NOTICE] {e}")
        await notify_admins(ctx, untracked_alert, exclude_id=oid)
        return

    for item in recovered:
        mid = item.get("message_id")
        if is_duplicate_alert(f"del_{cid}_{mid}"):
            continue

        sn   = item.get("sender_name") or "Unknown"
        sid  = item.get("sender_id") or "Unknown"
        un   = f"@{item['sender_username']}" if item.get("sender_username") else "None"
        st   = (item.get("created_at") or "")[:19].replace("T", " ")
        tb   = item.get("text") or item.get("caption")
        mt   = item.get("media_type")
        fid  = item.get("media_file_id")
        lp   = item.get("local_path")
        is_timed_msg = bool(item.get("is_timed"))
        del_change_type = "⏱️ Timed Media Expired / Deleted" if is_timed_msg else "🗑️ Deleted Message Recovered"
        timed_badge = " ⏱️ <i>[View-Once Expired]</i>" if is_timed_msg else ""
        hdr  = (
            f"🔔 <b>Admin Notification: {del_change_type}</b>\n\n"
            f"👤 <b>From:</b> {esc(sn)} ({esc(un)})\n"
            f"🆔 <b>Sender ID:</b> <code>{sid}</code>\n"
            f"📬 <b>To / Chat:</b> {esc(cname)} (Chat ID: <code>{cid}</code>)\n"
            f"🔢 <b>Message ID:</b> <code>{mid}</code>\n"
            f"⚙️ <b>Change Type:</b> <code>{del_change_type}</code>\n"
            f"📦 <b>Type:</b> <code>{mt or 'text'}</code>{timed_badge}\n"
            f"📅 <b>Sent At:</b> <code>{st} UTC</code>"
        )
        body = f"\n\n📝 <b>Content:</b>\n<blockquote>{esc(tb)}</blockquote>" if tb else ""
        curl = item.get("cloud_url")

        try:
            if mt:
                await send_owner(ctx, oid, mt, lp, fid, hdr, body, cloud_url=curl)
            else:
                await ctx.bot.send_message(
                    chat_id=oid, parse_mode=ParseMode.HTML,
                    text=f"{hdr}{body or chr(10) + '[Empty message]'}",
                )
        except Exception as e:
            logger.error(f"[RECOVERED SEND] {e}")

        # Alert other admins about the deletion event
        await notify_admins(ctx, f"{hdr}{body or ''}", exclude_id=oid)


# ── Admin Dashboard Builders ──────────────────────────────────────────────────
def make_admin_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👥 Users Directory", callback_data="adm_users_1"),
            InlineKeyboardButton("🚫 Blocked Users",  callback_data="adm_blocked"),
        ],
        [
            InlineKeyboardButton("📊 System Stats",    callback_data="adm_stats"),
            InlineKeyboardButton("🗑️ Recent Deletes",  callback_data="adm_deletions"),
        ],
        [
            InlineKeyboardButton("💾 Saved Messages",  callback_data="adm_saved"),
            InlineKeyboardButton("⏱️ Timed Media",     callback_data="adm_savedfilter_timed"),
        ],
        [
            InlineKeyboardButton("🔄 Refresh",         callback_data="adm_refresh"),
            InlineKeyboardButton("❌ Close Control Panel", callback_data="adm_close"),
        ]
    ])


def make_admin_dashboard_text(user: Any) -> str:
    s = database.get_stats()
    name = user.first_name if user else "Admin"
    return (
        f"👑 <b>Admin Control Panel</b>\n"
        f"Welcome back, <b>{esc(name)}</b>!\n\n"
        f"👥 <b>Total Tracked Users:</b> <code>{s['total_users']}</code>\n"
        f"🚫 <b>Blocked Users:</b> <code>{s['total_blocked']}</code>\n"
        f"💾 <b>Cached Messages:</b> <code>{s['total_messages']}</code>\n"
        f"⏱️ <b>Timed Media Saved:</b> <code>{s.get('total_timed', 0)}</code>\n"
        f"🗑️ <b>Recovered Deletions:</b> <code>{s['total_deleted']}</code>\n"
        f"✏️ <b>Recorded Edits:</b> <code>{s['total_edits']}</code>\n"
        f"🔗 <b>Active Business Connections:</b> <code>{s['active_connections']}</code>\n\n"
        f"<i>Select an option below to manage users, view archives, or configure the bot:</i>"
    )


def render_users_view(page: int = 1) -> tuple[str, InlineKeyboardMarkup]:
    users, total_count, total_pages = database.get_users_paginated(page=page, per_page=5)
    page = min(max(1, page), total_pages)

    lines = [f"👥 <b>User Directory</b> (Page {page}/{total_pages} - Total: {total_count})\n"]
    if not users:
        lines.append("<i>No users tracked in database yet.</i>")

    action_buttons = []
    for i, u in enumerate(users, 1):
        uid = u["user_id"]
        uname = f"@{u['username']}" if u.get("username") else "None"
        fname = esc(u.get("full_name") or "Unknown")
        status = "🚫 Blocked" if u.get("is_blocked") else "✅ Active"
        msg_count = u.get("total_messages", 0)
        lines.append(
            f"<b>{i}. {fname}</b> ({uname})\n"
            f"   🆔 ID: <code>{uid}</code> | 💬 {msg_count} msgs | {status}"
        )
        if u.get("is_blocked"):
            action_buttons.append(InlineKeyboardButton(f"🔓 Unblock {fname[:10]}", callback_data=f"unblock_usr_{uid}"))
        else:
            action_buttons.append(InlineKeyboardButton(f"🚫 Block {fname[:10]}", callback_data=f"block_usr_{uid}"))

    keyboard = []
    for j in range(0, len(action_buttons), 2):
        keyboard.append(action_buttons[j:j+2])

    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton("◀️ Prev", callback_data=f"adm_users_{page - 1}"))
    nav_row.append(InlineKeyboardButton(f"📄 {page}/{total_pages}", callback_data="adm_noop"))
    if page < total_pages:
        nav_row.append(InlineKeyboardButton("Next ▶️", callback_data=f"adm_users_{page + 1}"))

    if nav_row:
        keyboard.append(nav_row)
    keyboard.append([InlineKeyboardButton("🔙 Back to Main Menu", callback_data="adm_menu")])

    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


def render_blocked_view() -> tuple[str, InlineKeyboardMarkup]:
    blocked = database.get_blocked_users(limit=15)
    lines = [f"🚫 <b>Blocked Users Directory ({len(blocked)})</b>\n"]
    keyboard = []
    if not blocked:
        lines.append("<i>No users are currently blocked. Everything is clear!</i>")
    else:
        for i, u in enumerate(blocked, 1):
            uid = u["user_id"]
            fname = esc(u.get("full_name") or "User")
            uname = f"@{u['username']}" if u.get("username") else ""
            lines.append(f"<b>{i}. {fname}</b> {uname}\n   🆔 ID: <code>{uid}</code>")
            keyboard.append([InlineKeyboardButton(f"🔓 Unblock {fname[:14]} ({uid})", callback_data=f"unblock_usr_{uid}")])

    keyboard.append([InlineKeyboardButton("🔙 Back to Main Menu", callback_data="adm_menu")])
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


# ── Rich Media Helpers & Renderers ────────────────────────────────────────────
MEDIA_ICONS = {
    "photo": "📸 Photo",
    "video": "🎥 Video",
    "voice": "🎙️ Voice Note",
    "video_note": "🔘 Video Note",
    "audio": "🎵 Audio / Music",
    "document": "📁 File / Document",
    "animation": "🎬 GIF / Animation",
    "sticker": "🏷️ Sticker",
}


def format_media_badge(mtype: Optional[str]) -> str:
    if not mtype:
        return "💬 Text"
    return MEDIA_ICONS.get(mtype, f"📦 {mtype.capitalize()}")


def render_deletions_view(filter_type: str = "all") -> tuple[str, InlineKeyboardMarkup]:
    items = database.get_recent_deletions(limit=6, filter_type=filter_type)
    if filter_type == "media":
        type_label = "📸 Media Only"
    elif filter_type == "timed":
        type_label = "⏱️ Timed Media Only"
    elif filter_type == "text":
        type_label = "💬 Text Only"
    else:
        type_label = "All Messages"

    lines = [f"🗑️ <b>Recent Deletions Archive</b> ({type_label})\n"]
    if not items:
        lines.append(f"<i>No deleted {filter_type} messages recorded yet.</i>")

    media_buttons = []
    for i, it in enumerate(items, 1):
        sn = esc(it.get("sender_name") or "Unknown")
        sid = it.get("sender_id") or "N/A"
        cid = it.get("chat_id")
        mid = it.get("message_id")
        db_id = it.get("id")
        mtype = it.get("media_type")
        is_timed = bool(it.get("is_timed"))
        mbadge = format_media_badge(mtype)
        if is_timed:
            mbadge = f"⏱️ Timed {mbadge} (View-Once)"

        cap = it.get("caption") or it.get("text") or ""
        if cap and len(cap) > 32:
            cap = cap[:29] + "..."

        t = (it.get("deleted_at") or "")[:19].replace("T", " ")

        if mtype:
            desc = f"<b>{mbadge}</b>" + (f': <i>"{esc(cap)}"</i>' if cap else " <i>(no caption)</i>")
            btn_label = f"📥 View #{i} (⏱️ {mtype})" if is_timed else f"📥 View #{i} ({mtype})"
            media_buttons.append(InlineKeyboardButton(btn_label, callback_data=f"adm_getdel_{db_id}"))
        else:
            desc = f'💬 <b>Text:</b> <i>"{esc(cap or "[Empty]")}"</i>'

        lines.append(
            f"<b>{i}. {sn}</b> (ID: <code>{sid}</code>)\n"
            f"   {desc}\n"
            f"   Chat: <code>{cid}</code> | Msg: <code>{mid}</code> | 🕒 {t} UTC\n"
        )

    keyboard = []
    for j in range(0, len(media_buttons), 2):
        keyboard.append(media_buttons[j:j+2])

    filter_row = [
        InlineKeyboardButton("• All •" if filter_type == "all" else "All", callback_data="adm_delfilter_all"),
        InlineKeyboardButton("• 📸 Media •" if filter_type == "media" else "📸 Media", callback_data="adm_delfilter_media"),
        InlineKeyboardButton("• ⏱️ Timed •" if filter_type == "timed" else "⏱️ Timed", callback_data="adm_delfilter_timed"),
        InlineKeyboardButton("• 💬 Text •" if filter_type == "text" else "💬 Text", callback_data="adm_delfilter_text"),
    ]
    keyboard.append(filter_row)
    keyboard.append([InlineKeyboardButton("🔙 Back to Main Menu", callback_data="adm_menu")])

    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


def render_saved_view(filter_type: str = "all") -> tuple[str, InlineKeyboardMarkup]:
    items = database.get_recent_messages(limit=6, filter_type=filter_type)
    if filter_type == "media":
        type_label = "📸 Media Only"
    elif filter_type == "timed":
        type_label = "⏱️ Timed Media Only"
    elif filter_type == "text":
        type_label = "💬 Text Only"
    else:
        type_label = "All Messages"

    lines = [f"💾 <b>Cached Messages Archive</b> ({type_label})\n"]
    if not items:
        lines.append(f"<i>No cached {filter_type} messages recorded yet.</i>")

    media_buttons = []
    for i, it in enumerate(items, 1):
        sn = esc(it.get("sender_name") or "Unknown")
        sid = it.get("sender_id") or "N/A"
        cid = it.get("chat_id")
        mid = it.get("message_id")
        db_id = it.get("id")
        mtype = it.get("media_type")
        is_timed = bool(it.get("is_timed"))
        mbadge = format_media_badge(mtype)
        if is_timed:
            mbadge = f"⏱️ Timed {mbadge} (View-Once)"

        cap = it.get("caption") or it.get("text") or ""
        if cap and len(cap) > 32:
            cap = cap[:29] + "..."

        t = (it.get("created_at") or "")[:19].replace("T", " ")

        if mtype:
            desc = f"<b>{mbadge}</b>" + (f': <i>"{esc(cap)}"</i>' if cap else " <i>(no caption)</i>")
            btn_label = f"📥 View #{i} (⏱️ {mtype})" if is_timed else f"📥 View #{i} ({mtype})"
            media_buttons.append(InlineKeyboardButton(btn_label, callback_data=f"adm_getsaved_{db_id}"))
        else:
            desc = f'💬 <b>Text:</b> <i>"{esc(cap or "[Empty]")}"</i>'

        lines.append(
            f"<b>{i}. {sn}</b> (ID: <code>{sid}</code>)\n"
            f"   {desc}\n"
            f"   Chat: <code>{cid}</code> | Msg: <code>{mid}</code> | 🕒 {t} UTC\n"
        )

    keyboard = []
    for j in range(0, len(media_buttons), 2):
        keyboard.append(media_buttons[j:j+2])

    filter_row = [
        InlineKeyboardButton("• All •" if filter_type == "all" else "All", callback_data="adm_savedfilter_all"),
        InlineKeyboardButton("• 📸 Media •" if filter_type == "media" else "📸 Media", callback_data="adm_savedfilter_media"),
        InlineKeyboardButton("• ⏱️ Timed •" if filter_type == "timed" else "⏱️ Timed", callback_data="adm_savedfilter_timed"),
        InlineKeyboardButton("• 💬 Text •" if filter_type == "text" else "💬 Text", callback_data="adm_savedfilter_text"),
    ]
    keyboard.append(filter_row)
    keyboard.append([InlineKeyboardButton("🔙 Back to Main Menu", callback_data="adm_menu")])

    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


def render_user_vault_view(user_id: int, page: int = 1, filter_type: str = "all") -> tuple[str, InlineKeyboardMarkup]:
    """Render conversation messages for a user (both their messages and receiver replies)."""
    items, total_count, total_pages = database.get_user_conversation_messages(
        user_id=user_id, page=page, per_page=5, filter_type=filter_type
    )
    page = min(max(1, page), total_pages)

    if filter_type == "media":
        type_label = "📸 Media Only"
    elif filter_type == "timed":
        type_label = "⏱️ Timed Media Only"
    elif filter_type == "text":
        type_label = "💬 Text Only"
    else:
        type_label = "All Messages"

    lines = [
        f"💾 <b>My Saved Messages</b> ({type_label})\n"
        f"<i>Your messages & receiver replies (Page {page}/{total_pages} - Total: {total_count})</i>\n"
    ]
    if not items:
        lines.append("<i>No conversation messages found in this category.</i>")

    media_buttons = []
    for i, it in enumerate(items, 1):
        sid = it.get("sender_id")
        is_me = (sid == user_id)
        sn = "You" if is_me else esc(it.get("sender_name") or "Receiver")
        prefix = "📤 <b>You:</b>" if is_me else f"📥 <b>{sn}:</b>"

        mtype = it.get("media_type")
        is_timed = bool(it.get("is_timed"))
        db_id = it.get("id")

        cap = it.get("caption") or it.get("text") or ""
        if cap and len(cap) > 36:
            cap = cap[:33] + "..."

        t = (it.get("created_at") or "")[:19].replace("T", " ")

        if mtype:
            mbadge = format_media_badge(mtype)
            if is_timed:
                mbadge = f"⏱️ Timed {mbadge}"
            content_desc = f"{mbadge}" + (f': <i>"{esc(cap)}"</i>' if cap else "")
            media_buttons.append(InlineKeyboardButton(f"📥 View #{i} ({mtype})", callback_data=f"usr_getmedia_{db_id}"))
        else:
            content_desc = f'<i>"{esc(cap or "[Empty]")}"</i>'

        lines.append(f"<b>{i}.</b> {prefix} {content_desc}\n   🕒 <code>{t} UTC</code>\n")

    keyboard = []
    for j in range(0, len(media_buttons), 2):
        keyboard.append(media_buttons[j:j+2])

    filter_row = [
        InlineKeyboardButton("• All •" if filter_type == "all" else "All", callback_data=f"usr_savedfilter_all_{page}"),
        InlineKeyboardButton("• 📸 Media •" if filter_type == "media" else "📸 Media", callback_data=f"usr_savedfilter_media_{page}"),
        InlineKeyboardButton("• ⏱️ Timed •" if filter_type == "timed" else "⏱️ Timed", callback_data=f"usr_savedfilter_timed_{page}"),
        InlineKeyboardButton("• 💬 Text •" if filter_type == "text" else "💬 Text", callback_data=f"usr_savedfilter_text_{page}"),
    ]
    keyboard.append(filter_row)

    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton("◀️ Prev", callback_data=f"usr_saved_{page - 1}_{filter_type}"))
    nav_row.append(InlineKeyboardButton(f"📄 {page}/{total_pages}", callback_data="adm_noop"))
    if page < total_pages:
        nav_row.append(InlineKeyboardButton("Next ▶️", callback_data=f"usr_saved_{page + 1}_{filter_type}"))

    if nav_row:
        keyboard.append(nav_row)

    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


# ── Commands ──────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    if not u:
        return
    admin_flag = is_admin(u.id)
    database.record_user(u.id, u.full_name or "Unknown", u.username, is_admin=admin_flag)

    if not admin_flag:
        fname = esc(u.first_name or u.full_name or "there")
        welcome_text = (
            f"👋 Welcome, <b>{fname}</b>!\n\n"
            f"Glad to have you here. Have a great day!"
        )
        await update.message.reply_text(
            welcome_text,
            parse_mode=ParseMode.HTML,
            reply_markup=get_reply_keyboard(admin=False),
        )
        return

    c = database.get_active_connection_by_user(u.id)
    s = database.get_stats()
    b = await ctx.bot.get_me()

    text = (
        f"🛡️ <b>Anti-Delete Business Guard</b>\n\n"
        f"<b>Account Status:</b> {'✅ Connected' if c else '⚠️ Not connected'}\n"
        f"💾 <b>Cached Messages:</b> <code>{s['total_messages']}</code>\n"
        f"🗑️ <b>Recovered Deleted:</b> <code>{s['total_deleted']}</code>\n"
        f"👥 <b>Tracked Users:</b> <code>{s['total_users']}</code>\n\n"
        f"<b>Quick Setup:</b>\n"
        f"1. Settings > Telegram Business > Chatbots > @{b.username}\n"
        f"2. @BotFather > Bot Settings > Secretary Mode > Turn On\n\n"
        f"<b>Timed Media Interception:</b>\n"
        f"Swipe-reply to any timed message BEFORE opening it to capture details!"
    )

    kb = [
        [
            InlineKeyboardButton("📊 Status", callback_data="cmd_status_btn"),
            InlineKeyboardButton("💾 Saved Messages", callback_data="cmd_saved_btn"),
        ],
        [InlineKeyboardButton("👑 Open Admin Dashboard", callback_data="adm_menu")]
    ]

    await update.message.reply_text(
        "👋 Welcome back, Admin! Quick action buttons are docked below. Tap <b>🚀 Start / Menu</b> anytime.",
        parse_mode=ParseMode.HTML,
        reply_markup=get_reply_keyboard(admin=True),
    )
    await update.message.reply_text(parse_mode=ParseMode.HTML, text=text, reply_markup=InlineKeyboardMarkup(kb))
    # Ensure this admin's chat always shows the full admin command set
    await set_admin_commands_for(ctx.bot, u.id)


async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    if not u or not is_admin(u.id):
        return  # Silently ignore — non-admins cannot access admin panel
    text = make_admin_dashboard_text(u)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=make_admin_menu_keyboard())


async def cmd_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    if not u or not is_admin(u.id):
        await update.message.reply_text("⛔ <b>Access Denied:</b> Admin only.", parse_mode=ParseMode.HTML)
        return
    text, kb = render_users_view(1)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def cmd_block(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    if not u or not is_admin(u.id):
        await update.message.reply_text("⛔ <b>Access Denied:</b> Admin only.", parse_mode=ParseMode.HTML)
        return
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text("Usage: <code>/block &lt;user_id&gt;</code>\nExample: <code>/block 123456789</code>", parse_mode=ParseMode.HTML)
        return
    target_id = int(ctx.args[0])
    database.block_user(target_id)
    await update.message.reply_text(
        f"🚫 User <code>{target_id}</code> is now <b>blocked</b> from using the bot.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_unblock(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    if not u or not is_admin(u.id):
        await update.message.reply_text("⛔ <b>Access Denied:</b> Admin only.", parse_mode=ParseMode.HTML)
        return
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text("Usage: <code>/unblock &lt;user_id&gt;</code>\nExample: <code>/unblock 123456789</code>", parse_mode=ParseMode.HTML)
        return
    target_id = int(ctx.args[0])
    database.unblock_user(target_id)
    await update.message.reply_text(
        f"✅ User <code>{target_id}</code> is now <b>unblocked</b>.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_purge(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    if not u or not is_admin(u.id):
        await update.message.reply_text("⛔ <b>Access Denied:</b> Admin only.", parse_mode=ParseMode.HTML)
        return
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text("Usage: <code>/purge &lt;db_id&gt;</code>\nExample: <code>/purge 12</code>", parse_mode=ParseMode.HTML)
        return
    db_id = int(ctx.args[0])
    item = database.get_message_by_db_id(db_id)
    if not item:
        await update.message.reply_text("❌ Record not found in database.", parse_mode=ParseMode.HTML)
        return

    linked = database.get_all_linked_media_records(db_id)
    if not linked and item:
        linked = [item]

    c_res = False
    for rec in linked:
        mtype = rec.get("media_type") or "media"
        pid   = rec.get("cloudinary_public_id")
        curl  = rec.get("cloud_url")
        lpath = rec.get("local_path")
        cid   = rec.get("chat_id")
        mid   = rec.get("message_id")
        if await asyncio.to_thread(delete_from_cloudinary, pid, curl, mtype, chat_id=cid, msg_id=mid):
            c_res = True
        if lpath and os.path.exists(lpath):
            try:
                os.remove(lpath)
            except Exception:
                pass

    database.purge_media_record(db_id)

    status_msg = "Cloudinary cloud storage and local database" if c_res else "Local bot database"
    await update.message.reply_text(
        f"🗑️ <b>Media Purged Successfully!</b>\n\n"
        f"Record ID: <code>{db_id}</code>\n"
        f"Type: <code>{item.get('media_type') or 'media'}</code>\n"
        f"Purged from: <b>{status_msg}</b>",
        parse_mode=ParseMode.HTML
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    if not u or not is_admin(u.id):
        # Silently ignore — not a command non-admins should see
        return
    c = database.get_active_connection_by_user(u.id) if u else None
    s = database.get_stats()
    await update.message.reply_text(parse_mode=ParseMode.HTML, text=(
        f"📊 <b>Bot Status</b>\n\n"
        f"Connection: {'Active' if c else 'Not connected'}\n"
        f"Cached messages: <code>{s['total_messages']}</code>\n"
        f"Deleted recovered: <code>{s['total_deleted']}</code>\n"
        f"Edits logged: <code>{s['total_edits']}</code>\n"
        f"Total users: <code>{s['total_users']}</code>\n"
        f"Blocked users: <code>{s['total_blocked']}</code>"
    ))


async def cmd_recent(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    if not u or not is_admin(u.id):
        return  # Silently ignore — non-admins should not see this
    text, kb = render_deletions_view("all")
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def cmd_saved(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    if not u:
        return
    if is_admin(u.id):
        text, kb = render_saved_view("all")
    else:
        text, kb = render_user_vault_view(u.id, page=1, filter_type="all")
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


# ── Interactive Callback Handler ──────────────────────────────────────────────
async def on_callback_query(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()

    u = query.from_user
    data = query.data or ""

    if data == "adm_noop":
        return

    # Regular user vault callbacks (available to all users)
    if data.startswith("usr_saved_"):
        parts = data.split("_")
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
        ftype = parts[3] if len(parts) > 3 else "all"
        text, kb = render_user_vault_view(u.id, page=page, filter_type=ftype)
        try:
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        except Exception:
            await query.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    if data.startswith("usr_savedfilter_"):
        parts = data.split("_")
        ftype = parts[2] if len(parts) > 2 else "all"
        page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 1
        text, kb = render_user_vault_view(u.id, page=page, filter_type=ftype)
        try:
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        except Exception:
            await query.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    if data.startswith("usr_getmedia_"):
        target_str = data.replace("usr_getmedia_", "")
        if target_str.isdigit():
            db_id = int(target_str)
            item = database.get_message_by_db_id(db_id)
            if not item:
                await query.answer("Message not found in database.", show_alert=True)
                return

            # Privacy Check: User can only access their own conversation or admin can access all
            if not (item.get("chat_id") == u.id or item.get("sender_id") == u.id or is_admin(u.id)):
                await query.answer("⛔ Access denied: You do not own this media.", show_alert=True)
                return

            mtype = item.get("media_type")
            lpath = item.get("local_path")
            fid   = item.get("media_file_id")
            curl  = item.get("cloud_url")
            if not mtype or not (fid or curl or (lpath and os.path.exists(lpath))):
                await query.answer("No downloadable media file available for this message.", show_alert=True)
                return

            await query.answer(f"Sending {mtype}... 🚀")
            is_me = (item.get("sender_id") == u.id)
            sn  = "You" if is_me else esc(item.get("sender_name") or "Receiver")
            t   = (item.get("created_at") or "")[:19].replace("T", " ")
            cap = item.get("caption") or item.get("text") or ""
            mbadge = format_media_badge(mtype)

            hdr = (
                f"💾 <b>Saved Media</b>\n\n"
                f"👤 <b>From:</b> {sn}\n"
                f"📦 <b>Type:</b> {mbadge}\n"
                f"🕒 <b>Recorded At:</b> <code>{t} UTC</code>"
            )
            body = f"\nCaption: <blockquote>{esc(cap)}</blockquote>" if cap else ""
            await send_owner(ctx, u.id, mtype, lpath, fid, hdr, body, cloud_url=curl)
            return

    # User start shortcut buttons
    if data in ("cmd_status_btn", "cmd_saved_btn"):
        if not is_admin(u.id):
            await query.answer("⛔ Access denied: Admin only.", show_alert=True)
            return
        if data == "cmd_status_btn":
            s = database.get_stats()
            await query.message.reply_text(
                f"📊 <b>Live Status</b>\n\n"
                f"Messages Cached: <code>{s['total_messages']}</code>\n"
                f"Deleted Recovered: <code>{s['total_deleted']}</code>\n"
                f"Edits Logged: <code>{s['total_edits']}</code>\n"
                f"Tracked Users: <code>{s['total_users']}</code>",
                parse_mode=ParseMode.HTML
            )
            return
        elif data == "cmd_saved_btn":
            text, kb = render_saved_view("all")
            await query.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
            return

    # Admin verification for all administrative actions
    if not is_admin(u.id):
        await query.answer("⛔ Access denied: You are not an admin.", show_alert=True)
        return

    if data in ("adm_menu", "adm_refresh"):
        text = make_admin_dashboard_text(u)
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=make_admin_menu_keyboard())

    elif data.startswith("adm_users_"):
        try:
            page = int(data.split("_")[-1])
        except ValueError:
            page = 1
        text, kb = render_users_view(page)
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)

    elif data == "adm_blocked":
        text, kb = render_blocked_view()
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)

    elif data.startswith("block_usr_"):
        target_str = data.replace("block_usr_", "")
        if target_str.isdigit():
            target_id = int(target_str)
            database.block_user(target_id)
            await query.answer(f"User {target_id} blocked! 🚫", show_alert=True)
            msg_text = query.message.text or query.message.caption or ""
            if "User Directory" in msg_text:
                text, kb = render_users_view(1)
                try:
                    await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
                except Exception:
                    pass
            else:
                try:
                    await query.edit_message_reply_markup(
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton(f"✅ User Blocked ({target_id})", callback_data="adm_noop")
                        ]])
                    )
                except Exception:
                    pass

    elif data.startswith("unblock_usr_"):
        target_str = data.replace("unblock_usr_", "")
        if target_str.isdigit():
            target_id = int(target_str)
            database.unblock_user(target_id)
            await query.answer(f"User {target_id} unblocked! ✅", show_alert=True)
            text, kb = render_blocked_view()
            try:
                await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
            except Exception:
                pass

    elif data == "adm_stats":
        s = database.get_stats()
        text = (
            f"📊 <b>Detailed System Statistics</b>\n\n"
            f"💬 <b>Total Messages Cached:</b> <code>{s['total_messages']}</code>\n"
            f"⏱️ <b>Timed Media Captured:</b> <code>{s.get('total_timed', 0)}</code>\n"
            f"🗑️ <b>Deleted Messages Recovered:</b> <code>{s['total_deleted']}</code>\n"
            f"✏️ <b>Message Edits Logged:</b> <code>{s['total_edits']}</code>\n"
            f"👥 <b>Total Tracked Users:</b> <code>{s['total_users']}</code>\n"
            f"🚫 <b>Blocked Users:</b> <code>{s['total_blocked']}</code>\n"
            f"🔗 <b>Active Business Connections:</b> <code>{s['active_connections']}</code>\n"
            f"🧹 <b>Auto-Cleanup Temp Media:</b> <code>{CLEANUP_TEMP_MEDIA}</code>\n"
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Main Menu", callback_data="adm_menu")]])
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)

    elif data.startswith("adm_delfilter_") or data == "adm_deletions":
        ftype = data.replace("adm_delfilter_", "") if data.startswith("adm_delfilter_") else "all"
        text, kb = render_deletions_view(ftype)
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)

    elif data.startswith("adm_savedfilter_") or data == "adm_saved":
        ftype = data.replace("adm_savedfilter_", "") if data.startswith("adm_savedfilter_") else "all"
        text, kb = render_saved_view(ftype)
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)

    elif data.startswith("adm_getdel_") or data.startswith("adm_getsaved_"):
        is_del = data.startswith("adm_getdel_")
        prefix = "adm_getdel_" if is_del else "adm_getsaved_"
        db_id_str = data.replace(prefix, "")
        if db_id_str.isdigit():
            db_id = int(db_id_str)
            item = database.get_message_by_db_id(db_id)
            if not item:
                await query.answer("Message not found in database.", show_alert=True)
                return
            mtype = item.get("media_type")
            lpath = item.get("local_path")
            fid   = item.get("media_file_id")
            curl  = item.get("cloud_url")
            if not mtype or not (fid or curl or (lpath and os.path.exists(lpath))):
                await query.answer("No downloadable media file available for this message.", show_alert=True)
                return

            await query.answer(f"Sending {mtype} to your chat... 🚀")
            sn  = esc(item.get("sender_name") or "Unknown")
            sid = item.get("sender_id") or "N/A"
            cid = item.get("chat_id")
            mid = item.get("message_id")
            t   = (item.get("deleted_at" if is_del else "created_at") or "")[:19].replace("T", " ")
            cap = item.get("caption") or item.get("text") or ""
            mbadge = format_media_badge(mtype)

            title = "🗑️ <b>Deleted Media Retrieval</b>" if is_del else "💾 <b>Saved Media Retrieval</b>"
            hdr = (
                f"{title}\n\n"
                f"👤 <b>Sender:</b> {sn} (ID: <code>{sid}</code>)\n"
                f"📦 <b>Media Type:</b> {mbadge}\n"
                f"💬 <b>Chat:</b> <code>{cid}</code> | Msg: <code>{mid}</code>\n"
                f"🕒 <b>Recorded At:</b> <code>{t} UTC</code>"
            )
            body = f"\nCaption: <blockquote>{esc(cap)}</blockquote>" if cap else ""
            purge_markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("🗑️ Delete from Cloudinary & Bot", callback_data=f"adm_purge_{db_id}")
            ]])
            await send_owner(ctx, u.id, mtype, lpath, fid, hdr, body, reply_markup=purge_markup, cloud_url=curl)

    elif data.startswith("adm_purge_"):
        target_str = data.replace("adm_purge_", "")
        if target_str.isdigit():
            db_id = int(target_str)
            item = database.get_message_by_db_id(db_id)
            if not item:
                await query.answer("Record not found in database.", show_alert=True)
                return

            linked = database.get_all_linked_media_records(db_id)
            if not linked and item:
                linked = [item]

            c_res = False
            for rec in linked:
                mtype = rec.get("media_type") or "media"
                pid   = rec.get("cloudinary_public_id")
                curl  = rec.get("cloud_url")
                lpath = rec.get("local_path")
                cid   = rec.get("chat_id")
                mid   = rec.get("message_id")
                if await asyncio.to_thread(delete_from_cloudinary, pid, curl, mtype, chat_id=cid, msg_id=mid):
                    c_res = True
                if lpath and os.path.exists(lpath):
                    try:
                        os.remove(lpath)
                    except Exception:
                        pass

            database.purge_media_record(db_id)

            where_str = "Cloudinary & Bot" if c_res else "Bot storage"
            admin_purge_alert = (
                f"🔔 <b>Admin Notification: Media Purged</b>\n\n"
                f"👤 <b>By Admin:</b> {esc(u.full_name or 'Admin')} (ID: <code>{u.id}</code>)\n"
                f"📬 <b>Target Chat ID:</b> <code>{item.get('chat_id')}</code> | Msg: <code>{item.get('message_id')}</code>\n"
                f"⚙️ <b>Change Type:</b> 🗑️ <code>Media Permanently Deleted ({where_str})</code>"
            )
            await notify_admins(ctx, admin_purge_alert, exclude_id=u.id)

            await query.answer(f"Media permanently deleted from {where_str}! 🗑️", show_alert=True)
            try:
                await query.edit_message_reply_markup(
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("✅ Media Purged (Deleted)", callback_data="adm_noop")
                    ]])
                )
            except Exception:
                pass

    elif data == "adm_close":
        try:
            await query.message.delete()
        except Exception:
            await query.edit_message_text("<i>Admin panel closed.</i>", parse_mode=ParseMode.HTML)


async def on_direct_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle any message, media, or forward sent directly to the bot in private chat."""
    msg = update.message
    if not msg or not msg.chat or msg.chat.type != "private":
        return

    s = msg.from_user
    if s and database.is_user_blocked(s.id):
        await msg.reply_text("⛔ <b>Access Denied:</b> You are blocked from using this bot.", parse_mode=ParseMode.HTML)
        return

    # Quick Action Keyboard Button Interception (Persistent Start / Menu / Actions)
    txt = (msg.text or "").strip()
    if txt in ("🚀 Start / Menu", "Start", "start", "/start", "Menu", "menu", "🚀 Start"):
        await cmd_start(update, ctx)
        return
    if txt in ("💾 My Saved Messages", "💾 Saved Messages", "/saved", "saved"):
        await cmd_saved(update, ctx)
        return
    if txt in ("👑 Admin Dashboard", "/admin", "admin"):
        await cmd_admin(update, ctx)
        return
    if txt in ("📊 System Status", "/status", "status"):
        await cmd_status(update, ctx)
        return

    is_user_adm = is_admin(s.id if s else 0)
    if s and getattr(s, "id", None):
        database.record_user(s.id, s.full_name or "Unknown", s.username, is_admin=is_user_adm)

    cid = msg.chat.id
    mid = msg.message_id
    if is_duplicate_alert(f"direct_{cid}_{mid}"):
        return
    date = msg.date.isoformat()
    mt, fid = extract_media(msg)
    sname = s.full_name if s else "Unknown"
    logger.info(f"[DIRECT PM] from='{sname}' chat={cid} type={mt or 'text'}")

    lp = await dl(ctx, fid, mt, cid, mid) if (mt and fid) else None
    database.save_message(
        business_connection_id="direct",
        chat_id=cid, message_id=mid,
        sender_id=s.id if s else None,
        sender_name=sname,
        sender_username=s.username if s else None,
        text=msg.text,
        media_type=mt, media_file_id=fid,
        caption=msg.caption,
        created_at=date, local_path=lp,
    )
    if lp and mt:
        asyncio.create_task(bg_upload_to_cloudinary(lp, mt, cid, mid))

    # Send simple notification to admin telling to whom it is sent, from whom, and what kind of change/save was made
    bot_info = await ctx.bot.get_me()
    bot_handle = f"@{bot_info.username}" if (bot_info and bot_info.username) else "SavedelBot"
    change_type = f"📸 Direct Media Saved ({mt})" if mt else "💾 Direct Message Saved"
    snippet = msg.text or msg.caption or (f"[{mt} file]" if mt else "[No content]")
    if len(snippet) > 160:
        snippet = snippet[:157] + "..."

    admin_note = (
        f"🔔 <b>Admin Notification: User Activity</b>\n\n"
        f"👤 <b>From:</b> {esc(sname)} ({esc(f'@{s.username}' if (s and s.username) else 'None')})\n"
        f"🆔 <b>Sender ID:</b> <code>{s.id if s else 'Unknown'}</code>\n"
        f"📬 <b>To:</b> {bot_handle} (Private Chat: <code>{cid}</code>)\n"
        f"⚙️ <b>Change Type:</b> <code>{change_type}</code>\n"
        f"📝 <b>Content:</b> <blockquote>{esc(snippet)}</blockquote>\n"
        f"🕒 <b>Recorded At:</b> <code>{date[:19].replace('T', ' ')} UTC</code>"
    )
    await notify_admins(ctx, admin_note)

    reply_kb = get_reply_keyboard(admin=is_user_adm)
    if mt and lp:
        fname = os.path.basename(lp)
        await msg.reply_text(
            f"✅ <b>File Saved!</b>\n\n"
            f"Type: <code>{mt}</code>\n"
            f"Saved: <code>{fname}</code>\n"
            f"Cached in database and cloud storage.\n"
            f"View all your messages & replies anytime with /saved or tap <b>💾 My Saved Messages</b> below.",
            parse_mode=ParseMode.HTML,
            reply_markup=reply_kb,
        )
    elif mt and not lp:
        await msg.reply_text(
            f"⚠️ <b>Logged</b>, but media download failed (could be restricted/too large).",
            parse_mode=ParseMode.HTML,
            reply_markup=reply_kb,
        )
    elif msg.text:
        await msg.reply_text(
            "✅ <b>Message Saved!</b>\n\n"
            "View all your messages & replies anytime with /saved or tap <b>💾 My Saved Messages</b> below.",
            parse_mode=ParseMode.HTML,
            reply_markup=reply_kb,
        )


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    logger.warning(f"[ERR] {ctx.error}")


def build_app():
    req = HTTPXRequest(connect_timeout=40, read_timeout=60,
                       write_timeout=40, pool_timeout=30)
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .request(req)
        .get_updates_request(HTTPXRequest(connect_timeout=40, read_timeout=60,
                                          write_timeout=40, pool_timeout=30))
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("status",  cmd_status))
    app.add_handler(CommandHandler("recent",  cmd_recent))
    app.add_handler(CommandHandler("saved",   cmd_saved))
    app.add_handler(CommandHandler("admin",   cmd_admin))
    app.add_handler(CommandHandler("users",   cmd_users))
    app.add_handler(CommandHandler("block",   cmd_block))
    app.add_handler(CommandHandler("unblock", cmd_unblock))
    app.add_handler(CommandHandler("purge",   cmd_purge))
    app.add_handler(CommandHandler("delete_media", cmd_purge))
    app.add_handler(CallbackQueryHandler(on_callback_query))
    app.add_error_handler(on_error)

    # 1. Telegram Business 1-on-1 private chats between user and other contacts
    app.add_handler(BusinessConnectionHandler(on_connection))
    app.add_handler(MessageHandler(filters.UpdateType.BUSINESS_MESSAGE, on_message))
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_BUSINESS_MESSAGE, on_edit))
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE, on_edit))
    app.add_handler(BusinessMessagesDeletedHandler(on_deleted))

    # 2. Direct 1-on-1 private chats with the bot itself (including forwarded media)
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, on_direct_message))

    return app



# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    if not BOT_TOKEN or "YOUR" in BOT_TOKEN:
        print("ERROR: Set BOT_TOKEN in .env first!")
        return

    if not acquire_lock():
        print("ERROR: Another instance is running. Stop it or delete .bot.lock")
        return

    database.init_db()
    logger.info("Bot initializing...")
    start_health_server()

    print("\n" + "=" * 50)
    print("  Anti-Delete Business Bot  RUNNING")
    print("  Ctrl+C to stop")
    print("=" * 50 + "\n")
    logger.info("Polling started.")


    import time
    retry_delay = 5
    while True:
        try:
            app = build_app()
            app.run_polling(
                drop_pending_updates=True,
                allowed_updates=Update.ALL_TYPES,
                bootstrap_retries=10,
            )
            break  # clean exit (Ctrl+C)
        except KeyboardInterrupt:
            logger.info("Bot stopped by user.")
            break
        except Exception as e:
            logger.error(f"[CRASH] {e}")
            logger.info(f"Restarting with fresh app in {retry_delay}s... (Ctrl+C to stop)")
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)


if __name__ == "__main__":
    main()
