"""Telegram PDF Tools Bot - LEARN-X

Send ONE or MULTIPLE PDFs. With multiple PDFs they are merged first.

Actions:
  Merge                 - merge all PDFs into one
  Watermark             - text or image watermark (opacity, position, page range)
  Watermark removal     - automatic or specified separate overlays, losslessly
  Rasterize             - PDF -> pictures -> PDF (no copyable text)
  Rasterize + Watermark - watermark burned into the pixels, unremovable
  Compress              - shrink a large / scanned PDF
  Delete pages          - remove a page range (e.g. 1-3, 5, 9)
  Split                 - split into parts by page range (e.g. 1-3, 4-8, 9-10)
  All-in-One            - merge + watermark + rasterize in one go

Before sending, you can keep the original file name or rename it.
Every output is sent with the LEARN-X logo as its thumbnail and a branded caption.

Long polling (no webhook/URL). Set the BOT_TOKEN env var before running.
"""
import asyncio
import json
import logging
import os
import re
import shutil
import tempfile

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import pdf_tools

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s - %(message)s", level=logging.INFO)
log = logging.getLogger("pdf-bot")

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("Set the BOT_TOKEN environment variable.")

MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
COLLECT_DEBOUNCE = 2.0

# Optional usage logging. Set these env vars to a chat/channel/group id (e.g.
# -1001234567890) or @publicusername. The bot must be a MEMBER (groups) or
# ADMIN (channels) of the target so it can post there.
#   LOG_CHAT_ID     - where the "User / Username / ID" info message is sent
#   FORWARD_CHAT_ID - where the original uploaded PDF is forwarded
# If FORWARD_CHAT_ID is unset it falls back to LOG_CHAT_ID. Logging never blocks
# or breaks the user's request - any failure is just logged and ignored.
LOG_CHAT_ID = os.environ.get("LOG_CHAT_ID")
FORWARD_CHAT_ID = os.environ.get("FORWARD_CHAT_ID") or LOG_CHAT_ID

# Who can run /ban and /unban:
#  1) Anyone who is an ADMIN of your forwarding/log channel (or group). The bot
#     asks Telegram for that chat's admin list live, so you manage banners just
#     by making someone an admin there - no redeploy needed. Set ADMIN_CHAT_ID
#     to that chat's id (defaults to FORWARD_CHAT_ID, then LOG_CHAT_ID).
#  2) Optionally, a fixed list in ADMIN_IDS (comma-separated numeric user ids,
#     e.g. ADMIN_IDS=525433,987654) - handy as a fallback / for solo use.
# If neither yields anyone, the ban commands are simply refused.
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID") or FORWARD_CHAT_ID or LOG_CHAT_ID
ADMIN_IDS = {
    int(x) for x in (os.environ.get("ADMIN_IDS") or "").replace(" ", "").split(",")
    if x.strip().lstrip("-").isdigit()
}

# Banned users are persisted to a JSON file next to the bot so bans survive a
# restart of the same instance. ADMIN_IDS is also seeded with an optional
# BANNED_IDS env var (comma-separated) as a starting blocklist.
_HERE = os.path.dirname(os.path.abspath(__file__))
BAN_FILE = os.path.join(_HERE, "banned.json")


def _load_banned() -> set:
    ids = set()
    for x in (os.environ.get("BANNED_IDS") or "").replace(" ", "").split(","):
        if x.strip().lstrip("-").isdigit():
            ids.add(int(x))
    try:
        with open(BAN_FILE, encoding="utf-8") as fh:
            ids.update(int(i) for i in json.load(fh))
    except (OSError, ValueError, TypeError):
        pass
    return ids


def _save_banned(ids: set):
    try:
        with open(BAN_FILE, "w", encoding="utf-8") as fh:
            json.dump(sorted(ids), fh)
    except OSError:
        log.exception("could not save banned list")


BANNED_IDS = _load_banned()

RESTRICTED_MSG = (
    "You are restricted from using this bot.\n"
    "If you think this is a mistake, contact the administrator."
)

CAPTION = "Powered by - LEARN - X\u2122"
LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logo.jpg")

WELCOME = (
    "LEARN-X PDF Tools Bot\n\n"
    "Send me one PDF, or several at once.\n"
    "If you send multiple, they are merged first.\n\n"
    "What I can do:\n"
    "- Merge several PDFs into one\n"
    "- Watermark (text, image, or both): your opacity, position and page range\n"
    "- Watermark removal: automatic or specify text/numbers/an image; page range\n"
    "  Removes supported separate overlays without changing the page content.\n"
    "  Burned-in or inseparable watermarks are left untouched.\n"
    "- Rasterize: PDF to pictures to PDF (no copyable text)\n"
    "- Rasterize + Watermark: watermark burned in, unremovable\n"
    "- Compress: shrink a big or scanned PDF\n"
    "- Delete pages: remove a page range (e.g. 1-3, 5, 9)\n"
    "- Split: cut into parts by page range (e.g. 1-3, 4-8, 9-10)\n"
    "- All-in-One: merge + watermark + rasterize\n"
    "- Rename only: just give the file a new name, nothing else\n\n"
    "Before sending I will ask to keep or rename the file.\n"
    "Send /reset anytime to start over.\n"
    "Max file size: 20 MB per file (Telegram limit)."
)


# ---------- keyboards ----------
def kb_single():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Watermark", callback_data="mode:wm")],
        [InlineKeyboardButton("Watermark removal", callback_data="mode:remove")],
        [InlineKeyboardButton("Rasterize", callback_data="mode:ras")],
        [InlineKeyboardButton("Rasterize + Watermark", callback_data="mode:raswm")],
        [InlineKeyboardButton("Compress", callback_data="mode:comp")],
        [InlineKeyboardButton("Delete pages", callback_data="mode:del")],
        [InlineKeyboardButton("Split", callback_data="mode:split")],
        [InlineKeyboardButton("Rename only", callback_data="mode:rename")],
    ])


def kb_multi():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Merge only", callback_data="mode:merge")],
        [InlineKeyboardButton("All-in-One (merge+watermark+rasterize)",
                              callback_data="mode:raswm")],
        [InlineKeyboardButton("Merge + Watermark", callback_data="mode:wm")],
        [InlineKeyboardButton("Merge + Watermark removal", callback_data="mode:remove")],
        [InlineKeyboardButton("Merge + Rasterize", callback_data="mode:ras")],
        [InlineKeyboardButton("Merge + Compress", callback_data="mode:comp")],
        [InlineKeyboardButton("Merge + Delete pages", callback_data="mode:del")],
        [InlineKeyboardButton("Merge + Split", callback_data="mode:split")],
        [InlineKeyboardButton("Merge + Rename only", callback_data="mode:rename")],
    ])


def kb_remove():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Automatic removal", callback_data="rm:auto")],
        [InlineKeyboardButton("Specify watermark", callback_data="rm:manual")],
    ])


def kb_remove_target():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Text / numbers", callback_data="rmt:text")],
        [InlineKeyboardButton("Image file", callback_data="rmt:image")],
    ])


REMOVAL_HELP = (
    "How should I find the watermarks?\n\n"
    "Automatic: find supported repeated faint diagonal text layers and "
    "watermarks marked by this updated bot.\n"
    "Specify: send the exact watermark text/numbers or an original image file.\n\n"
    "Only separate overlays are removed. Letters, equations, diagrams and "
    "page layout are preserved; scanned/burned-in or uncertain marks stay."
)


def kb_wm_type():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Text", callback_data="wt:text"),
         InlineKeyboardButton("Image", callback_data="wt:image")],
        [InlineKeyboardButton("Text + Image", callback_data="wt:both")],
    ])


def kb_positions():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Top-L", callback_data="pos:top_left"),
         InlineKeyboardButton("Top-C", callback_data="pos:top_center"),
         InlineKeyboardButton("Top-R", callback_data="pos:top_right")],
        [InlineKeyboardButton("Mid-L", callback_data="pos:center_left"),
         InlineKeyboardButton("Center", callback_data="pos:center"),
         InlineKeyboardButton("Mid-R", callback_data="pos:center_right")],
        [InlineKeyboardButton("Bot-L", callback_data="pos:bottom_left"),
         InlineKeyboardButton("Bot-C", callback_data="pos:bottom_center"),
         InlineKeyboardButton("Bot-R", callback_data="pos:bottom_right")],
        [InlineKeyboardButton("Tiled (diagonal)", callback_data="pos:tiled")],
    ])


def kb_dpi(prefix):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("150", callback_data=f"{prefix}:150"),
        InlineKeyboardButton("200", callback_data=f"{prefix}:200"),
        InlineKeyboardButton("300", callback_data=f"{prefix}:300"),
    ]])


def kb_quality():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Light", callback_data="comp:light"),
        InlineKeyboardButton("Medium", callback_data="comp:medium"),
        InlineKeyboardButton("Strong", callback_data="comp:strong"),
    ]])


def kb_name():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Keep original name", callback_data="name:keep")],
        [InlineKeyboardButton("Rename", callback_data="name:rename")],
    ])


# ---------- helpers ----------
def _user_dir(uid: int) -> str:
    path = os.path.join(tempfile.gettempdir(), f"pdfbot_{uid}")
    os.makedirs(path, exist_ok=True)
    return path


def _clean_name(s: str) -> str:
    s = os.path.basename((s or "").strip())
    s = "".join(c for c in s if c not in '\\/:*?"<>|')
    if not s:
        s = "document.pdf"
    if not s.lower().endswith(".pdf"):
        s += ".pdf"
    return s


def parse_pages(s: str, total: int):
    s = s.strip().lower()
    if s in ("all", "*", "everything"):
        return None
    out = set()
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            a, b = int(a), int(b)
            for i in range(min(a, b), max(a, b) + 1):
                out.add(i)
        else:
            out.add(int(part))
    out = {p for p in out if 1 <= p <= total}
    if not out:
        raise ValueError("no valid pages")
    return out


def parse_removal_pages(s: str, total: int):
    """Strict page ranges: never silently clean a different set of pages."""
    if s.strip().lower() in ("all", "*", "everything"):
        return None
    pages = set()
    for part in s.split(","):
        match = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+)\s*)?", part)
        if not match:
            raise ValueError("Use all, 1, or 1, 2-5, 10.")
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if not (1 <= start <= total and 1 <= end <= total):
            raise ValueError(f"Every page number must be between 1 and {total}.")
        pages.update(range(min(start, end), max(start, end) + 1))
    if not pages:
        raise ValueError("Choose at least one page.")
    return pages


def parse_page_groups(s: str, total: int):
    """Parse split ranges like '1-3, 4-8, 9-10' into an ordered list of
    page-number lists: [[1,2,3], [4,5,6,7,8], [9,10]]. Each comma-separated
    chunk becomes its own output PDF. A single page (e.g. '7') is its own part.
    """
    groups = []
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            a, b = int(a), int(b)
            lo, hi = min(a, b), max(a, b)
            grp = [p for p in range(lo, hi + 1) if 1 <= p <= total]
        else:
            p = int(part)
            grp = [p] if 1 <= p <= total else []
        if grp:
            groups.append(grp)
    if not groups:
        raise ValueError("no valid page groups")
    return groups


def _group_label(grp):
    """Human-friendly label for a split part: '1-3' for a run, else '1,3,5'."""
    if len(grp) > 1 and grp == list(range(grp[0], grp[-1] + 1)):
        return f"{grp[0]}-{grp[-1]}"
    return ",".join(str(p) for p in grp)


def _prepare_input(context, removal=False) -> str:
    pdfs = context.user_data["pdfs"]
    if removal:
        # Check every source BEFORE merging can discard signature/form data.
        pdf_tools.preflight_removal_inputs([p["path"] for p in pdfs])
    udir = os.path.dirname(pdfs[0]["path"])
    if len(pdfs) == 1:
        inp = pdfs[0]["path"]
    else:
        inp = os.path.join(udir, "merged_input.pdf")
        pdf_tools.merge_pdfs([p["path"] for p in pdfs], inp)
    context.user_data["in_path"] = inp
    context.user_data["page_count"] = pdf_tools.page_count(inp)
    return inp


def _build_watermarker(context):
    kind = context.user_data["wm_kind"]
    if kind == "text":
        pos = context.user_data.get("wm_position", "center")
        tiled = pos == "tiled"
        angle = 45 if tiled else 0
        return pdf_tools.text_watermarker(
            context.user_data["wm_text"], context.user_data["wm_opacity"],
            position=pos, angle=angle, tiled=tiled,
            font_pt=context.user_data.get("wm_font_pt", pdf_tools.DEFAULT_TEXT_FONT_PT))
    with open(context.user_data["wm_image_path"], "rb") as fh:
        img_bytes = fh.read()
    return pdf_tools.image_watermarker(img_bytes, context.user_data["wm_opacity"])


def _apply_both_watermarks(in_path, out_path, t, img_bytes, im,
                           rasterize=False, dpi=150):
    """Apply the TEXT watermark first, then the IMAGE watermark on top, as two
    separate passes - each with its own opacity and page range. When rasterize
    is True only the final (image) pass rasterizes, so both end up burned in."""
    pos = t["position"]
    tiled = pos == "tiled"
    angle = 45 if tiled else 0
    text_build = pdf_tools.text_watermarker(
        t["text"], t["opacity"], position=pos, angle=angle, tiled=tiled,
        font_pt=t["font_pt"])
    image_build = pdf_tools.image_watermarker(img_bytes, im["opacity"])
    tmp_path = out_path + ".text.pdf"
    # pass 1: text watermark (vector overlay) on the text pages
    pdf_tools.apply_watermark(in_path, tmp_path, text_build,
                              rasterize=False, pages=t["pages"])
    # pass 2: image watermark on the image pages; rasterize here if requested
    pdf_tools.apply_watermark(tmp_path, out_path, image_build,
                              rasterize=rasterize, dpi=dpi, pages=im["pages"])
    try:
        os.remove(tmp_path)
    except OSError:
        pass
    return out_path


async def _log_usage(context, user, file_path=None, file_name=None):
    """Send a usage record to the log channel and forward the original PDF to
    the group. Controlled by LOG_CHAT_ID / FORWARD_CHAT_ID env vars. Any error
    is swallowed so it can never affect the user's request."""
    if not LOG_CHAT_ID and not FORWARD_CHAT_ID:
        return
    name = (user.full_name or user.first_name or "Unknown").strip()
    username = f"@{user.username}" if user.username else "(no username)"
    info = f"User: {name}\nUsername: {username}\nID: {user.id}"
    try:
        if LOG_CHAT_ID:
            await context.bot.send_message(LOG_CHAT_ID, info)
        if FORWARD_CHAT_ID and file_path and os.path.exists(file_path):
            with open(file_path, "rb") as fh:
                await context.bot.send_document(
                    FORWARD_CHAT_ID, document=fh,
                    filename=file_name or "document.pdf", caption=info)
    except Exception:  # noqa: BLE001
        log.exception("usage logging failed (ignored)")


# ---------- access control (ban / unban) ----------
# Cache the channel/group admin list briefly so we don't hit Telegram on every
# single command.
_ADMIN_CACHE = {"ids": set(), "ts": 0.0}
_ADMIN_CACHE_TTL = 60.0  # seconds


async def _channel_admin_ids(context) -> set:
    """Live set of human admin user-ids of ADMIN_CHAT_ID (cached ~60s)."""
    import time
    if not ADMIN_CHAT_ID:
        return set()
    now = time.monotonic()
    if now - _ADMIN_CACHE["ts"] <= _ADMIN_CACHE_TTL and _ADMIN_CACHE["ids"]:
        return _ADMIN_CACHE["ids"]
    try:
        admins = await context.bot.get_chat_administrators(ADMIN_CHAT_ID)
        _ADMIN_CACHE["ids"] = {a.user.id for a in admins if not a.user.is_bot}
        _ADMIN_CACHE["ts"] = now
    except Exception:  # noqa: BLE001
        log.exception("could not fetch chat admins for %s", ADMIN_CHAT_ID)
    return _ADMIN_CACHE["ids"]


async def _is_authorized(context, uid: int) -> bool:
    """True if uid is in the fixed ADMIN_IDS or is an admin of ADMIN_CHAT_ID."""
    if uid in ADMIN_IDS:
        return True
    return uid in await _channel_admin_ids(context)


def _is_banned(uid: int) -> bool:
    return uid in BANNED_IDS


# Throttle repeated "banned user tried to use the bot" notices so a spammer
# can't flood the log channel: at most one notice per user per interval.
_BAN_NOTICE = {}  # uid -> last monotonic time we logged an attempt
_BAN_NOTICE_TTL = 300.0  # seconds (5 min)


async def _log_banned_attempt(context, user):
    """Record that a banned user tried to use the bot - always to the bot log,
    and (throttled) to the LOG_CHAT_ID channel if one is configured."""
    import time
    name = (user.full_name or user.first_name or "Unknown").strip()
    username = f"@{user.username}" if user.username else "(no username)"
    log.warning("Banned user tried to use the bot: %s %s id=%s",
                name, username, user.id)
    now = time.monotonic()
    last = _BAN_NOTICE.get(user.id, 0.0)
    if now - last < _BAN_NOTICE_TTL:
        return
    _BAN_NOTICE[user.id] = now
    if not LOG_CHAT_ID:
        return
    try:
        await context.bot.send_message(
            LOG_CHAT_ID,
            "\U0001F6AB Banned user tried to use the bot\n"
            f"User: {name}\nUsername: {username}\nID: {user.id}\n"
            f"Unban with: /unban {user.id}")
    except Exception:  # noqa: BLE001
        log.exception("could not post banned-attempt notice (ignored)")


async def _blocked(update: Update, context) -> bool:
    """If the user is banned, tell them they're restricted, log the attempt,
    and return True so the calling handler stops. Works for messages and
    button taps."""
    user = update.effective_user
    if not user or not _is_banned(user.id):
        return False
    await _log_banned_attempt(context, user)
    try:
        if update.callback_query:
            await update.callback_query.answer(
                "You are restricted from using this bot.", show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text(RESTRICTED_MSG)
    except Exception:  # noqa: BLE001
        pass
    return True


def _same_chat(chat, target) -> bool:
    """True if `chat` matches the configured target (numeric id or @username)."""
    if chat is None or target is None:
        return False
    t = str(target).strip()
    if t.lstrip("-").isdigit():
        return chat.id == int(t)
    return (chat.username or "").lower() == t.lstrip("@").lower()


def _parse_target_id(update, context):
    """Read a numeric user id from a /ban or /unban command. Works for normal
    commands (context.args) and for commands posted inside a channel, where we
    parse the message text ourselves."""
    args = list(context.args) if context.args else []
    if not args:
        text = (update.effective_message.text or "").strip()
        args = text.split()[1:]  # drop the /command itself
    if not args:
        return None
    raw = args[0].strip().lstrip("@")
    return int(raw) if raw.lstrip("-").isdigit() else None


async def _can_moderate(update, context) -> bool:
    """Who may run /ban and /unban:
     - a command POSTED INSIDE the admin channel (only admins can post in a
       channel, so it is inherently from an admin), or
     - a user who is in ADMIN_IDS or is an admin of ADMIN_CHAT_ID (DM / group).
    """
    chat = update.effective_chat
    if chat is not None and chat.type == "channel" and _same_chat(chat, ADMIN_CHAT_ID):
        return True
    user = update.effective_user
    return bool(user and await _is_authorized(context, user.id))


async def ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not await _can_moderate(update, context):
        return await msg.reply_text("Only an admin of the channel can use /ban.")
    target = _parse_target_id(update, context)
    if target is None:
        return await msg.reply_text("Usage: /ban <user_id>  (e.g. /ban 525433)")
    if await _is_authorized(context, target):
        return await msg.reply_text("You can't ban an admin.")
    BANNED_IDS.add(target)
    _save_banned(BANNED_IDS)
    await msg.reply_text(
        f"User {target} is now banned. They'll be told they're restricted "
        "the next time they use the bot.")


async def unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not await _can_moderate(update, context):
        return await msg.reply_text("Only an admin of the channel can use /unban.")
    target = _parse_target_id(update, context)
    if target is None:
        return await msg.reply_text("Usage: /unban <user_id>  (e.g. /unban 525433)")
    if target in BANNED_IDS:
        BANNED_IDS.discard(target)
        _save_banned(BANNED_IDS)
        return await msg.reply_text(f"User {target} is unbanned.")
    await msg.reply_text(f"User {target} was not banned.")


async def on_channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /ban and /unban typed directly inside a channel. Telegram's
    CommandHandler doesn't fire for channel posts, so we dispatch them here."""
    msg = update.effective_message
    if not msg or not msg.text:
        return
    word = msg.text.strip().split()[0].lstrip("/").split("@")[0].lower()
    if word == "ban":
        await ban(update, context)
    elif word == "unban":
        await unban(update, context)


# ---------- commands ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _blocked(update, context):
        return
    await update.message.reply_text(WELCOME)


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _blocked(update, context):
        return
    if context.user_data.get("busy"):
        return await update.message.reply_text("Please wait for the current file to finish.")
    if context.job_queue:
        for job in context.job_queue.get_jobs_by_name(f"menu_{update.effective_user.id}"):
            job.schedule_removal()
    shutil.rmtree(_user_dir(update.effective_user.id), ignore_errors=True)
    context.user_data.clear()
    await update.message.reply_text("Cleared. Send a PDF to start.")


# ---------- receive files ----------
async def on_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None:  # ignore channel posts / edited posts
        return
    if await _blocked(update, context):
        return
    msg = update.message
    awaiting = context.user_data.get("awaiting")
    if context.user_data.get("busy"):
        return await msg.reply_text("Please wait for the current file to finish.")

    if msg.photo:
        if awaiting == "remove_image":
            return await _receive_removal_image(update, context, msg.photo[-1])
        if awaiting == "wm_image":
            return await _receive_wm_image(update, context, msg.photo[-1])
        return await msg.reply_text("Send a PDF first, then choose a mode.")

    doc = msg.document
    if not doc:
        return
    name = (doc.file_name or "").lower()
    mime = doc.mime_type or ""
    is_pdf = mime == "application/pdf" or name.endswith(".pdf")
    is_img = mime.startswith("image/") or name.endswith((".png", ".jpg", ".jpeg", ".webp"))

    if awaiting == "remove_image" and is_img:
        return await _receive_removal_image(update, context, doc)
    if awaiting == "wm_image" and is_img:
        return await _receive_wm_image(update, context, doc)

    if not is_pdf:
        return await msg.reply_text("Please send a PDF.")

    if doc.file_size and doc.file_size > MAX_DOWNLOAD_BYTES:
        return await msg.reply_text(
            "That file is over 20 MB - Telegram won't let a bot download it.")

    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    udir = _user_dir(uid)
    context.user_data.setdefault("pdfs", [])
    idx = len(context.user_data["pdfs"])
    path = os.path.join(udir, f"in_{idx}.pdf")
    await (await doc.get_file()).download_to_drive(path)
    context.user_data["pdfs"].append({"path": path, "name": doc.file_name or "document.pdf"})
    # A new PDF changes the input set; discard any half-finished operation.
    pdfs = context.user_data["pdfs"]
    context.user_data.clear()
    context.user_data["pdfs"] = pdfs

    await _log_usage(context, update.effective_user, file_path=path,
                     file_name=doc.file_name or "document.pdf")

    jq = context.job_queue
    if jq:
        for j in jq.get_jobs_by_name(f"menu_{uid}"):
            j.schedule_removal()
        jq.run_once(_menu_job, COLLECT_DEBOUNCE, name=f"menu_{uid}",
                    data={"uid": uid, "chat_id": chat_id})
    else:
        await _show_menu(context, chat_id, uid)


async def _menu_job(context: ContextTypes.DEFAULT_TYPE):
    await _show_menu(context, context.job.data["chat_id"], context.job.data["uid"])


async def _show_menu(context, chat_id, uid):
    ud = context.application.user_data[uid]
    n = len(ud.get("pdfs", []))
    if n == 0 or ud.get("busy") or ud.get("awaiting"):
        return
    if n == 1:
        await context.bot.send_message(chat_id, "Got 1 PDF. What should I do?",
                                       reply_markup=kb_single())
    else:
        await context.bot.send_message(
            chat_id, f"Got {n} PDFs. They will be merged first. What should I do?",
            reply_markup=kb_multi())


async def _receive_wm_image(update, context, tg_obj):
    udir = _user_dir(update.effective_user.id)
    img_path = os.path.join(udir, "watermark_img.png")
    await (await tg_obj.get_file()).download_to_drive(img_path)
    context.user_data["wm_image_path"] = img_path
    context.user_data["awaiting"] = "opacity"
    if context.user_data.get("wm_kind") == "both":
        return await update.message.reply_text(
            "Image received (it will be centered, up to 350x350).\n"
            "Now type the opacity for the IMAGE watermark, 1 to 100 (e.g. 25):")
    await update.message.reply_text(
        "Image received (it will be centered, up to 350x350).\n"
        "Now type the opacity from 1 to 100 (e.g. 25):")


async def _ask_removal_pages(update, context):
    context.user_data["awaiting"] = "remove_pages"
    total = context.user_data["page_count"]
    text = (f"Which pages should I clean? This PDF has {total} page(s).\n"
            "Examples: all  |  1  |  1, 2-5, 10\n"
            "For several PDFs, page numbers refer to the merged document.")
    if update.callback_query:
        return await update.callback_query.edit_message_text(text)
    return await update.message.reply_text(text)


async def _receive_removal_image(update, context, tg_obj):
    if tg_obj.file_size and tg_obj.file_size > MAX_DOWNLOAD_BYTES:
        return await update.message.reply_text("Please send an image smaller than 20 MB.")
    path = os.path.join(_user_dir(update.effective_user.id), "removal_reference.img")
    try:
        await (await tg_obj.get_file()).download_to_drive(path)
    except Exception:
        log.exception("watermark reference download failed")
        return await update.message.reply_text(
            "Couldn't download that image. Please send it again as an original file.")
    context.user_data["remove_reference"] = path
    return await _ask_removal_pages(update, context)


async def _retry_removal(update, context, explanation):
    context.user_data.pop("op", None)
    context.user_data["busy"] = False
    context.user_data["awaiting"] = "remove_method"
    context.user_data["flow"] = "remove"
    return await context.bot.send_message(
        update.effective_chat.id, explanation, reply_markup=kb_remove())


# ---------- text input ----------
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None:  # ignore channel posts / edited posts
        return
    if await _blocked(update, context):
        return
    awaiting = context.user_data.get("awaiting")
    text = (update.message.text or "").strip()
    if context.user_data.get("busy"):
        return await update.message.reply_text("Please wait for the current file to finish.")

    if awaiting == "remove_text":
        targets = list(dict.fromkeys(line.strip() for line in text.splitlines()
                                    if line.strip()))
        if not targets or len(targets) > 20 or any(len(t) > 512 for t in targets):
            return await update.message.reply_text(
                "Send 1 to 20 exact watermark texts/numbers, one per line "
                "(up to 512 characters each).")
        context.user_data["remove_targets"] = targets
        return await _ask_removal_pages(update, context)

    if awaiting == "remove_pages":
        try:
            pages = parse_removal_pages(text, context.user_data["page_count"])
        except ValueError as exc:
            return await update.message.reply_text(str(exc))
        context.user_data["remove_pages"] = pages
        context.user_data["op"] = {"kind": "remove"}
        return await _ask_name(update, context)

    if awaiting in ("remove_method", "remove_target_kind"):
        return await update.message.reply_text("Please choose an option using the buttons above.")
    if awaiting == "remove_image":
        return await update.message.reply_text(
            "Please send the original watermark image as a file, not its name. "
            "A screenshot or recompressed photo may not match safely.")

    if awaiting == "wm_text":
        context.user_data["wm_text"] = text
        context.user_data["awaiting"] = None
        return await update.message.reply_text(
            "Where should the text go?", reply_markup=kb_positions())

    if awaiting == "font":
        if text.lower() in ("default", "d", ""):
            context.user_data["wm_font_pt"] = pdf_tools.DEFAULT_TEXT_FONT_PT
        else:
            try:
                v = float(text)
            except ValueError:
                return await update.message.reply_text(
                    "Please type a number like 40, or 'default'.")
            context.user_data["wm_font_pt"] = max(6.0, min(300.0, v))
        context.user_data["awaiting"] = "opacity"
        return await update.message.reply_text(
            "Now type the opacity from 1 to 100 (e.g. 25):")

    if awaiting == "opacity":
        try:
            v = float(text)
        except ValueError:
            return await update.message.reply_text("Please type a number from 1 to 100.")
        op = v / 100 if v > 1 else v
        context.user_data["wm_opacity"] = max(0.01, min(1.0, op))
        context.user_data["awaiting"] = "pages"
        total = context.user_data.get("page_count", 1)
        return await update.message.reply_text(
            f"Which pages? This PDF has {total} page(s).\n"
            "Examples: all  |  1  |  1, 2-5, 10")

    if awaiting == "pages":
        total = context.user_data.get("page_count", 1)
        try:
            pages = parse_pages(text, total)
        except ValueError:
            return await update.message.reply_text(
                "Couldn't read that. Try: all  |  1  |  1, 2-5, 10")
        context.user_data["wm_pages"] = pages
        context.user_data["awaiting"] = None
        if context.user_data.get("wm_kind") == "both":
            phase = context.user_data.get("both_phase", "text")
            if phase == "text":
                # snapshot the finished TEXT watermark, then start the IMAGE step
                context.user_data["both_text"] = {
                    "text": context.user_data["wm_text"],
                    "position": context.user_data.get("wm_position", "center"),
                    "font_pt": context.user_data.get(
                        "wm_font_pt", pdf_tools.DEFAULT_TEXT_FONT_PT),
                    "opacity": context.user_data["wm_opacity"],
                    "pages": pages,
                }
                context.user_data["both_phase"] = "image"
                context.user_data["awaiting"] = "wm_image"
                return await update.message.reply_text(
                    "Text watermark saved. Step 2 of 2 - the IMAGE watermark.\n"
                    "Now send the image (photo or file):")
            # phase == "image": snapshot, then finish (DPI if rasterizing)
            context.user_data["both_image"] = {
                "opacity": context.user_data["wm_opacity"],
                "pages": pages,
            }
            context.user_data["op"] = {"kind": "wm"}
            if context.user_data.get("flow") == "raswm":
                return await update.message.reply_text(
                    "Pick output quality (DPI):", reply_markup=kb_dpi("fdpi"))
            return await _ask_name(update, context)
        if context.user_data.get("flow") == "raswm":
            return await update.message.reply_text(
                "Pick output quality (DPI):", reply_markup=kb_dpi("fdpi"))
        context.user_data["op"] = {"kind": "wm"}
        return await _ask_name(update, context)

    if awaiting == "del_pages":
        total = context.user_data.get("page_count", 1)
        try:
            pages = parse_pages(text, total)
        except ValueError:
            return await update.message.reply_text(
                "Couldn't read that. Try: 1-3, 5, 9")
        if pages is None:
            return await update.message.reply_text(
                "I can't delete every page. Pick specific pages, e.g. 1-3, 5, 9")
        if len(pages) >= total:
            return await update.message.reply_text(
                f"That deletes all {total} page(s). Leave at least one page.")
        context.user_data["del_pages"] = pages
        context.user_data["awaiting"] = None
        context.user_data["op"] = {"kind": "del"}
        return await _ask_name(update, context)

    if awaiting == "split_ranges":
        total = context.user_data.get("page_count", 1)
        try:
            groups = parse_page_groups(text, total)
        except ValueError:
            return await update.message.reply_text(
                "Couldn't read that. Try: 1-3, 4-8, 9-10")
        context.user_data["split_groups"] = groups
        context.user_data["awaiting"] = None
        context.user_data["op"] = {"kind": "split"}
        return await _ask_name(update, context)

    if awaiting == "rename":
        return await _execute(update, context, _clean_name(text))

    await update.message.reply_text("Send a PDF to begin, or /start for help.")


# ---------- buttons ----------
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _blocked(update, context):
        return
    q = update.callback_query
    await q.answer()
    data = q.data

    if not context.user_data.get("pdfs"):
        return await q.edit_message_text("Please send a PDF first.")

    if context.user_data.get("busy"):
        return await context.bot.send_message(
            update.effective_chat.id, "Please wait for the current file to finish.")

    if data.startswith("mode:"):
        m = data.split(":")[1]
        if m not in {"merge", "rename", "wm", "raswm", "ras", "comp", "del", "split", "remove"}:
            return
        pdfs = context.user_data["pdfs"]
        context.user_data.clear()
        context.user_data["pdfs"] = pdfs
        context.user_data["busy"] = True
        if context.job_queue:
            for job in context.job_queue.get_jobs_by_name(f"menu_{update.effective_user.id}"):
                job.schedule_removal()
        try:
            await asyncio.to_thread(_prepare_input, context, m == "remove")
        except Exception as exc:
            log.exception("could not prepare PDF input")
            return await q.edit_message_text(
                f"Couldn't open or merge these PDFs: {exc}\nSend /reset and try a readable PDF.")
        finally:
            context.user_data["busy"] = False
        if m == "remove":
            context.user_data["flow"] = "remove"
            context.user_data["awaiting"] = "remove_method"
            return await q.edit_message_text(REMOVAL_HELP, reply_markup=kb_remove())
        if m == "merge":
            context.user_data["op"] = {"kind": "merge"}
            return await _ask_name(update, context)
        if m == "rename":
            # Just rename the file (single) or merge + rename (multiple);
            # no other PDF processing.
            context.user_data["op"] = {"kind": "rename"}
            return await _ask_name(update, context)
        if m in ("wm", "raswm"):
            context.user_data["flow"] = m
            return await q.edit_message_text(
                "Text or image watermark?", reply_markup=kb_wm_type())
        if m == "ras":
            return await q.edit_message_text(
                "Rasterize quality (DPI):", reply_markup=kb_dpi("rdpi"))
        if m == "comp":
            return await q.edit_message_text(
                "Compression level:", reply_markup=kb_quality())
        if m == "del":
            total = context.user_data["page_count"]
            context.user_data["awaiting"] = "del_pages"
            return await q.edit_message_text(
                f"This PDF has {total} page(s). Which pages should I DELETE?\n"
                "Examples: 1-3, 5, 9  |  2  |  4-8\n"
                "(the remaining pages are kept in order)")
        if m == "split":
            total = context.user_data["page_count"]
            context.user_data["awaiting"] = "split_ranges"
            return await q.edit_message_text(
                f"This PDF has {total} page(s). Enter the split ranges, "
                "separated by commas - each one becomes its own PDF.\n"
                "Example: 1-3, 4-8, 9-10  ->  3 files\n"
                "A single page like 7 is allowed too.")

    if data.startswith(("rm:", "rmt:")):
        expected = "remove_method" if data.startswith("rm:") else "remove_target_kind"
        if context.user_data.get("flow") != "remove" or context.user_data.get("awaiting") != expected:
            return await context.bot.send_message(
                update.effective_chat.id, "That choice is out of date. Use the latest buttons or /reset.")
        if data in ("rm:auto", "rm:manual"):
            for key in ("remove_targets", "remove_reference", "remove_pages", "remove_mode"):
                context.user_data.pop(key, None)
            if data == "rm:auto":
                context.user_data["remove_mode"] = "auto"
                return await _ask_removal_pages(update, context)
            context.user_data["awaiting"] = "remove_target_kind"
            return await q.edit_message_text(
                "How will you identify the watermark?", reply_markup=kb_remove_target())
        if data == "rmt:text":
            context.user_data["remove_mode"] = "text"
            context.user_data["awaiting"] = "remove_text"
            return await q.edit_message_text(
                "Send the exact watermark text or numbers.\n"
                "For several different watermarks, put each one on a new line.\n"
                "Only matching separate text overlays are removed, not matching words in body text.\n"
                "Text drawn inside an image needs Automatic (for marks added by this version) "
                "or an image reference instead.")
        if data == "rmt:image":
            context.user_data["remove_mode"] = "image"
            context.user_data["awaiting"] = "remove_image"
            return await q.edit_message_text(
                "Send the original watermark image as a file (PNG recommended).\n"
                "Only safely matching separate transparent image overlays are removed. "
                "Screenshots, resized/recompressed images and burned-in marks may not match.")
        return

    # Do not let old watermark/compression buttons overwrite a removal flow.
    if context.user_data.get("flow") == "remove" and not data.startswith("name:"):
        return await context.bot.send_message(
            update.effective_chat.id, "Please finish the current removal choices or choose a new mode.")

    if data.startswith("name:") and (
        context.user_data.get("awaiting") not in ("name", "rename")
        or not context.user_data.get("op")
    ):
        return await context.bot.send_message(
            update.effective_chat.id, "Please finish the current choices before naming the output.")

    if data == "wt:text":
        context.user_data["wm_kind"] = "text"
        context.user_data["awaiting"] = "wm_text"
        return await q.edit_message_text("Send the watermark text:")

    if data == "wt:image":
        context.user_data["wm_kind"] = "image"
        context.user_data["awaiting"] = "wm_image"
        return await q.edit_message_text(
            "Send the image to use as a watermark (photo or file):")

    if data == "wt:both":
        context.user_data["wm_kind"] = "both"
        context.user_data["both_phase"] = "text"
        context.user_data["awaiting"] = "wm_text"
        return await q.edit_message_text(
            "Text + Image watermark (done one after the other).\n"
            "Step 1 of 2 - the TEXT watermark.\nSend the watermark text:")

    if data.startswith("pos:"):
        context.user_data["wm_position"] = data.split(":")[1]
        context.user_data["awaiting"] = "font"
        return await q.edit_message_text(
            "Text size? Type a number like 40 (Word-style points; bigger = "
            "larger watermark).\nOr send 'default' for 40.")

    if data.startswith("rdpi:"):
        context.user_data["op"] = {"kind": "ras", "dpi": int(data.split(":")[1])}
        return await _ask_name(update, context)

    if data.startswith("fdpi:"):
        context.user_data["wm_dpi"] = int(data.split(":")[1])
        context.user_data["op"] = {"kind": "wm"}
        return await _ask_name(update, context)

    if data.startswith("comp:"):
        context.user_data["op"] = {"kind": "comp", "level": data.split(":")[1]}
        return await _ask_name(update, context)

    if data == "name:keep":
        base = context.user_data["pdfs"][0]["name"]
        return await _execute(update, context, _clean_name(base))

    if data == "name:rename":
        context.user_data["awaiting"] = "rename"
        return await q.edit_message_text("Type the new file name (without .pdf):")


async def _ask_name(update, context):
    context.user_data["awaiting"] = "name"
    base = context.user_data["pdfs"][0]["name"]
    await context.bot.send_message(
        update.effective_chat.id,
        f"Output file name?\nCurrent: {base}", reply_markup=kb_name())


# ---------- execute + send ----------
async def _execute(update, context, out_name: str):
    chat_id = update.effective_chat.id
    in_path = context.user_data["in_path"]
    out_path = os.path.join(os.path.dirname(in_path), "output.pdf")
    op = context.user_data["op"]

    if op["kind"] == "split":
        return await _execute_split(update, context, out_name)
    if op["kind"] == "remove":
        # This path NEVER uses the lossy size-reduction safety net below.
        return await _execute_removal(update, context, out_name)

    await context.bot.send_message(chat_id, "Processing...")
    await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)
    loop = asyncio.get_running_loop()
    try:
        kind = op["kind"]
        if kind in ("merge", "rename"):
            out_path = in_path  # already merged (or the single file); just rename on send
        elif kind == "ras":
            await loop.run_in_executor(
                None, lambda: pdf_tools.rasterize_pdf(in_path, out_path, dpi=op["dpi"]))
        elif kind == "comp":
            await loop.run_in_executor(
                None, lambda: pdf_tools.compress_pdf(in_path, out_path, level=op["level"]))
        elif kind == "del":
            pages = context.user_data["del_pages"]
            await loop.run_in_executor(
                None, lambda: pdf_tools.delete_pages(in_path, out_path, pages))
        elif kind == "wm":
            raster = context.user_data.get("flow") == "raswm"
            dpi = context.user_data.get("wm_dpi", 150)
            if context.user_data["wm_kind"] == "both":
                t = context.user_data["both_text"]
                im = context.user_data["both_image"]
                with open(context.user_data["wm_image_path"], "rb") as fh:
                    img_bytes = fh.read()
                await loop.run_in_executor(None, lambda: _apply_both_watermarks(
                    in_path, out_path, t, img_bytes, im, rasterize=raster, dpi=dpi))
            else:
                build = _build_watermarker(context)
                pages = context.user_data.get("wm_pages")
                await loop.run_in_executor(None, lambda: pdf_tools.apply_watermark(
                    in_path, out_path, build, rasterize=raster, dpi=dpi, pages=pages))
    except Exception as exc:  # noqa: BLE001
        log.exception("processing failed")
        return await context.bot.send_message(chat_id, f"Error: {exc}")
    # safety net: keep the output under Telegram's 50 MB send cap
    try:
        await loop.run_in_executor(
            None, lambda: pdf_tools.shrink_to_limit(out_path, 48))
    except Exception:  # noqa: BLE001
        log.exception("shrink_to_limit failed")
    await _send_result(update, context, out_path, out_name)


async def _execute_removal(update, context, out_name):
    chat_id = update.effective_chat.id
    ud = context.user_data
    if ud.get("busy"):
        return
    ud["busy"] = True
    ud["awaiting"] = None
    in_path = ud["in_path"]
    out_path = os.path.join(os.path.dirname(in_path), "cleaned_output.pdf")
    # Snapshot inputs before starting the background work.
    options = {
        "mode": ud["remove_mode"],
        "targets": list(ud.get("remove_targets", [])) or None,
        "reference_image": ud.get("remove_reference"),
        "pages": ud.get("remove_pages"),
    }
    try:
        await context.bot.send_message(
            chat_id, "Checking separate watermark layers and preserving the original page content...")
        await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)
        try:
            report = await asyncio.to_thread(
                pdf_tools.remove_watermarks, in_path, out_path, **options)
        except Exception as exc:
            log.exception("safe watermark removal failed")
            return await _retry_removal(
                update, context,
                f"No cleaned file was sent: {str(exc)[:600]}\n"
                "The uploaded PDF is unchanged. Try another removal option or /reset.")

        if report["status"] == "no_matches":
            return await _retry_removal(
                update, context,
                "No safely removable matching watermark was found on the selected pages.\n\n"
                "No cleaned file was sent, and the uploaded PDF is unchanged. "
                "Try Specify watermark with the exact text/numbers or the original image. "
                "Scanned/burned-in marks, mixed content and unsupported formats cannot "
                "be removed here without risking page content.")
        if report["status"] != "cleaned" or not report.get("validated"):
            return await _retry_removal(
                update, context, "The result did not pass the content-preservation checks. "
                "No output was sent; your uploaded PDF is unchanged.")
        if os.path.getsize(out_path) > 48 * 1024 * 1024:
            return await _retry_removal(
                update, context,
                "The cleaned PDF exceeds the safe Telegram upload size (48 MB). "
                "I have NOT compressed or changed its images. Try fewer input PDFs "
                "or split your source into smaller files, then clean each file.")

        summary = (
            f"Removed {report['removed_occurrences']} supported watermark drawing(s) "
            f"across {len(report['pages_changed'])} page(s).\n"
            "Remaining text, diagrams and page layout passed preservation checks. "
            "No rasterization or image recompression was used."
        )
        if report.get("unmatched_targets"):
            summary += (f"\n{len(report['unmatched_targets'])} requested text target(s) "
                        "had no safely removable match.")
        warnings = report.get("warnings", [])
        if warnings:
            summary += "\nNotes: " + " ".join(str(w) for w in warnings)[:1000]
        summary += "\nOnly supported separate overlays are removed; other marks may remain."
        await context.bot.send_message(chat_id, summary)
        try:
            await _send_result(update, context, out_path, out_name)
        except Exception:
            log.exception("could not send cleaned PDF")
            ud["busy"] = False
            await context.bot.send_message(
                chat_id, "Couldn't send the result. Your input is still available; choose a file name to retry.")
            return await _ask_name(update, context)
    finally:
        ud.pop("busy", None)


async def _send_result(update, context, out_path: str, out_name: str):
    chat_id = update.effective_chat.id
    have_logo = os.path.exists(LOGO_PATH)
    with open(out_path, "rb") as fh:
        if have_logo:
            with open(LOGO_PATH, "rb") as th:
                await context.bot.send_document(
                    chat_id, document=fh, filename=out_name,
                    caption=CAPTION, thumbnail=th)
        else:
            await context.bot.send_document(
                chat_id, document=fh, filename=out_name, caption=CAPTION)
    shutil.rmtree(_user_dir(update.effective_user.id), ignore_errors=True)
    context.user_data.clear()


async def _execute_split(update, context, out_name: str):
    """Split the (single/merged) PDF into several files by page range and send
    each one back with the LEARN-X thumbnail + caption."""
    chat_id = update.effective_chat.id
    in_path = context.user_data["in_path"]
    udir = os.path.dirname(in_path)
    groups = context.user_data["split_groups"]

    await context.bot.send_message(chat_id, "Processing...")
    await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)
    loop = asyncio.get_running_loop()
    try:
        out_paths = await loop.run_in_executor(
            None, lambda: pdf_tools.split_pdf(in_path, udir, groups))
    except Exception as exc:  # noqa: BLE001
        log.exception("split failed")
        return await context.bot.send_message(chat_id, f"Error: {exc}")

    base = out_name[:-4] if out_name.lower().endswith(".pdf") else out_name
    have_logo = os.path.exists(LOGO_PATH)
    await context.bot.send_message(
        chat_id, f"Split into {len(out_paths)} file(s). Sending...")
    for path, grp in out_paths:
        try:
            await loop.run_in_executor(
                None, lambda p=path: pdf_tools.shrink_to_limit(p, 48))
        except Exception:  # noqa: BLE001
            log.exception("shrink_to_limit failed")
        fname = _clean_name(f"{base} ({_group_label(grp)})")
        with open(path, "rb") as fh:
            if have_logo:
                with open(LOGO_PATH, "rb") as th:
                    await context.bot.send_document(
                        chat_id, document=fh, filename=fname,
                        caption=CAPTION, thumbnail=th)
            else:
                await context.bot.send_document(
                    chat_id, document=fh, filename=fname, caption=CAPTION)
    shutil.rmtree(_user_dir(update.effective_user.id), ignore_errors=True)
    context.user_data.clear()


def main():
    app: Application = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("ban", ban))
    app.add_handler(CommandHandler("unban", unban))
    # /ban and /unban typed directly inside a channel (CommandHandler ignores
    # channel posts, so catch them explicitly).
    app.add_handler(MessageHandler(
        filters.ChatType.CHANNEL & filters.COMMAND, on_channel_command))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, on_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_button))
    log.info("Bot starting (long polling)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
