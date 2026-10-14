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

import os, html, logging, atexit, threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional, Any
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.request import HTTPXRequest
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    BusinessConnectionHandler, BusinessMessagesDeletedHandler,
    ContextTypes, filters,
)
import database

# ── Config ────────────────────────────────────────────────────────────────────
load_dotenv()
BOT_TOKEN          = os.getenv("BOT_TOKEN", "")
FALLBACK_OWNER     = os.getenv("OWNER_ID", "")
BACKUP_CHANNEL     = os.getenv("BACKUP_CHANNEL_ID", "")
CLEANUP_TEMP_MEDIA = os.getenv("CLEANUP_TEMP_MEDIA", "false").lower() in ("true", "1", "yes")
PORT               = os.getenv("PORT", "")
BASE_DIR           = os.path.dirname(os.path.abspath(__file__))
MEDIA_DIR          = os.path.join(BASE_DIR, "media")
LOCK_FILE          = os.path.join(BASE_DIR, ".bot.lock")
os.makedirs(MEDIA_DIR, exist_ok=True)

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


async def send_owner(ctx: ContextTypes.DEFAULT_TYPE, oid: int,
                     mtype: str, lpath: Optional[str], fid: Optional[str],
                     header: str, body: str) -> None:
    """Send media (from disk or file_id) or plain text to owner and optional backup channel."""
    caption = f"{header}\n{body}" if body else header
    has = bool(lpath and os.path.exists(lpath))
    targets = [oid]
    if BACKUP_CHANNEL and BACKUP_CHANNEL != str(oid):
        targets.append(BACKUP_CHANNEL)

    for target in targets:
        try:
            target_chat_id = int(target) if str(target).lstrip("-").isdigit() else target
            if not (has or fid):
                await ctx.bot.send_message(chat_id=target_chat_id, text=caption, parse_mode=ParseMode.HTML)
                continue
            p = open(lpath, "rb") if has else fid
            try:
                if   mtype == "photo":      await ctx.bot.send_photo(chat_id=target_chat_id, photo=p, caption=caption, parse_mode=ParseMode.HTML)
                elif mtype == "video":      await ctx.bot.send_video(chat_id=target_chat_id, video=p, caption=caption, parse_mode=ParseMode.HTML)
                elif mtype == "voice":      await ctx.bot.send_voice(chat_id=target_chat_id, voice=p, caption=caption, parse_mode=ParseMode.HTML)
                elif mtype == "video_note":
                    await ctx.bot.send_message(chat_id=target_chat_id, text=header, parse_mode=ParseMode.HTML)
                    await ctx.bot.send_video_note(chat_id=target_chat_id, video_note=p)
                elif mtype == "audio":      await ctx.bot.send_audio(chat_id=target_chat_id, audio=p, caption=caption, parse_mode=ParseMode.HTML)
                elif mtype == "document":   await ctx.bot.send_document(chat_id=target_chat_id, document=p, caption=caption, parse_mode=ParseMode.HTML)
                elif mtype == "animation":  await ctx.bot.send_animation(chat_id=target_chat_id, animation=p, caption=caption, parse_mode=ParseMode.HTML)
                elif mtype == "sticker":
                    await ctx.bot.send_message(chat_id=target_chat_id, text=f"{header}\n[Sticker]", parse_mode=ParseMode.HTML)
                    await ctx.bot.send_sticker(chat_id=target_chat_id, sticker=p)
                else:
                    await ctx.bot.send_message(chat_id=target_chat_id, text=caption, parse_mode=ParseMode.HTML)
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
    mt, fid = extract_media(obj)
    logger.info(
        f"  [MSG] chat={chat_id} id={msg_id} "
        f"from='{s.full_name if s else '?'}' type={mt or 'text'}"
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
    )
    return lp


def get_owner(conn_id: str) -> Optional[int]:
    info = database.get_connection(conn_id)
    if info:
        return info["user_chat_id"]
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
    #
    #  HOW IT WORKS:
    #  - Someone sends you a view-once photo/video (the timer icon in Telegram).
    #  - YOU swipe-right on that message and reply (even just type "." and send).
    #  - Telegram puts the original timed message in msg.reply_to_message
    #    WITH the real file_id — the business bot can access it!
    #  - The bot downloads the file immediately before the timer can destroy it
    #    and forwards it to you in your private bot chat.
    #
    for replied in (msg.reply_to_message, msg.external_reply):
        if not replied:
            continue
        rtype, rfid = extract_media(replied)
        rmid = getattr(replied, "message_id", None)
        if not (rtype and rfid and rmid):
            continue

        # Skip if already saved to disk
        existing = database.get_message(cid, rmid)
        if existing and existing.get("local_path") and os.path.exists(
                existing["local_path"]):
            logger.info(f"  [SKIP] timed msg {rmid} already on disk")
            continue

        rs    = getattr(replied, "from_user", None)
        rname = rs.full_name if rs else "Unknown"
        rcap  = getattr(replied, "caption", None) or getattr(replied, "text", None)
        rdate = (
            replied.date.isoformat()
            if hasattr(replied, "date") and replied.date
            else date
        )

        logger.info(f"  [TIMED] capturing {rtype} msg_id={rmid} from {rname}")
        rpath = await dl(ctx, rfid, rtype, cid, rmid)

        database.save_message(
            business_connection_id=conn,
            chat_id=cid, message_id=rmid,
            sender_id=rs.id if rs else None,
            sender_name=rname,
            sender_username=rs.username if rs else None,
            text=None, media_type=rtype, media_file_id=rfid,
            caption=rcap, created_at=rdate, local_path=rpath,
        )

        oid = get_owner(conn)
        if not oid:
            continue

        cname = msg.chat.title or msg.chat.first_name or str(cid)
        header = (
            f"<b>Timed / View-Once Media Captured!</b>\n\n"
            f"From: {esc(rname)}\n"
            f"Chat: {esc(cname)}\n"
            f"<i>Saved before it expires!</i>"
        )
        body = f"\nCaption: <blockquote>{esc(rcap)}</blockquote>" if rcap else ""
        await send_owner(ctx, oid, rtype, rpath, rfid, header, body)


async def on_edit(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.edited_business_message
    if not msg:
        return
    cid, mid, conn = msg.chat.id, msg.message_id, msg.business_connection_id
    new   = msg.text or msg.caption or "[media]"
    old_d = database.record_message_edit(cid, mid, new)
    oid   = get_owner(conn)
    if not oid:
        return
    sn  = msg.from_user.full_name if msg.from_user else "Unknown"
    un  = f"@{msg.from_user.username}" if (msg.from_user and msg.from_user.username) else ""
    cn  = msg.chat.title or msg.chat.first_name or str(cid)
    old = (old_d.get("text") or old_d.get("caption") or "[not cached]") if old_d else "[not cached]"
    try:
        await ctx.bot.send_message(chat_id=oid, parse_mode=ParseMode.HTML, text=(
            f"<b>Message Edited!</b>\n\n"
            f"From: {esc(sn)} {esc(un)}\nChat: {esc(cn)}\n\n"
            f"Before:\n<blockquote>{esc(old)}</blockquote>\n"
            f"After:\n<blockquote>{esc(new)}</blockquote>"
        ))
    except Exception as e:
        logger.error(f"[EDIT NOTIFY] {e}")


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
    oid = get_owner(conn)
    if not oid:
        logger.warning(f"[DELETED] no owner for conn={conn}")
        return

    if not recovered:
        try:
            await ctx.bot.send_message(chat_id=oid, parse_mode=ParseMode.HTML, text=(
                f"<b>Deletion Detected</b>\n\nChat: {esc(cname)}\n"
                f"<i>{len(ids)} message(s) deleted — not cached "
                f"(they were sent before the bot started).</i>"
            ))
        except Exception as e:
            logger.error(f"[DELETE NOTICE] {e}")
        return

    for item in recovered:
        sn   = item.get("sender_name") or "Unknown"
        un   = f"@{item['sender_username']}" if item.get("sender_username") else ""
        st   = (item.get("created_at") or "")[:19].replace("T", " ")
        tb   = item.get("text") or item.get("caption")
        mt   = item.get("media_type")
        fid  = item.get("media_file_id")
        lp   = item.get("local_path")
        hdr  = (
            f"<b>Deleted Message Recovered!</b>\n\n"
            f"From: {esc(sn)} {esc(un)}\n"
            f"Chat: {esc(cname)}\n"
            f"Sent: <code>{st} UTC</code>"
        )
        body = f"\nContent:\n<blockquote>{esc(tb)}</blockquote>" if tb else ""
        try:
            if mt:
                await send_owner(ctx, oid, mt, lp, fid, hdr, body)
            else:
                await ctx.bot.send_message(
                    chat_id=oid, parse_mode=ParseMode.HTML,
                    text=f"{hdr}{body or chr(10) + '[Empty message]'}"
                )
        except Exception as e:
            logger.error(f"[RECOVERED SEND] {e}")


# ── Commands ──────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    c = database.get_active_connection_by_user(u.id) if u else None
    s = database.get_stats()
    b = await ctx.bot.get_me()
    await update.message.reply_text(parse_mode=ParseMode.HTML, text=(
        f"<b>Anti-Delete Business Guard</b>\n\n"
        f"Status: {'Connected' if c else 'Not connected'} | "
        f"Cached: <code>{s['total_messages']}</code> | "
        f"Recovered: <code>{s['total_deleted']}</code>\n\n"
        f"<b>Setup:</b>\n"
        f"1. Telegram Premium required\n"
        f"2. Settings > Telegram Business > Chatbots > @{b.username}\n"
        f"3. @BotFather > Bot Settings > Secretary Mode > Turn On\n\n"
        f"<b>Timed media trick:</b>\n"
        f"Swipe-reply a timed message BEFORE opening it "
        f"and I capture + send it to you instantly here!"
    ))


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    c = database.get_active_connection_by_user(u.id) if u else None
    s = database.get_stats()
    await update.message.reply_text(parse_mode=ParseMode.HTML, text=(
        f"<b>Status</b>\n"
        f"Connection: {'Active' if c else 'Not connected'}\n"
        f"Cached messages: <code>{s['total_messages']}</code>\n"
        f"Deleted recovered: <code>{s['total_deleted']}</code>\n"
        f"Edits logged: <code>{s['total_edits']}</code>"
    ))


async def cmd_recent(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    items = database.get_recent_deletions(limit=5)
    if not items:
        await update.message.reply_text("No deleted messages recorded yet.")
        return
    parts = ["<b>Recently Deleted:</b>\n"]
    for i, it in enumerate(items, 1):
        sn = esc(it.get("sender_name") or "Unknown")
        co = esc(it.get("text") or it.get("caption") or f"[{it.get('media_type', '?')}]")
        t  = (it.get("deleted_at") or "")[:19].replace("T", " ")
        parts.append(f"<b>{i}. {sn}:</b> <i>{co}</i>\n{t} UTC\n")
    await update.message.reply_text("\n".join(parts), parse_mode=ParseMode.HTML)


async def cmd_saved(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    items = database.get_recent_messages(limit=8)
    if not items:
        await update.message.reply_text("No cached messages found yet.")
        return
    parts = ["<b>Saved Messages Archive:</b>\n"]
    for i, it in enumerate(items, 1):
        sn = esc(it.get("sender_name") or "Unknown")
        mt = it.get("media_type")
        txt = it.get("text") or it.get("caption") or (f"[{mt}]" if mt else "[Empty]")
        if len(txt) > 40:
            txt = txt[:37] + "..."
        saved_status = " 💾" if it.get("local_path") else ""
        t = (it.get("created_at") or "")[:19].replace("T", " ")
        parts.append(f"<b>{i}. {sn}:</b> {esc(txt)}{saved_status}\n<code>{t} UTC</code>\n")
    await update.message.reply_text("\n".join(parts), parse_mode=ParseMode.HTML)


async def on_direct_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle any message, media, or forward sent directly to the bot in private chat."""
    msg = update.message
    if not msg or not msg.chat or msg.chat.type != "private":
        return

    cid = msg.chat.id
    mid = msg.message_id
    date = msg.date.isoformat()
    mt, fid = extract_media(msg)
    s = msg.from_user
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

    if mt and lp:
        fname = os.path.basename(lp)
        await msg.reply_text(
            f"✅ <b>File Saved!</b>\n\n"
            f"Type: <code>{mt}</code>\n"
            f"Saved: <code>{fname}</code>\n"
            f"Cached in database and stored on disk.",
            parse_mode=ParseMode.HTML,
        )
    elif mt and not lp:
        await msg.reply_text(
            f"⚠️ <b>Logged</b>, but media download failed (could be restricted/too large).",
            parse_mode=ParseMode.HTML,
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
        .build()
    )

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("recent", cmd_recent))
    app.add_handler(CommandHandler("saved",  cmd_saved))
    app.add_error_handler(on_error)

    # 1. Telegram Business 1-on-1 private chats between user and other contacts
    app.add_handler(BusinessConnectionHandler(on_connection))
    app.add_handler(MessageHandler(filters.UpdateType.BUSINESS_MESSAGE, on_message))
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_BUSINESS_MESSAGE, on_edit))
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
