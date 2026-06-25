"""Telegram PDF Tools Bot - LEARN-X

Send ONE or MULTIPLE PDFs. With multiple PDFs they are merged first.

Actions:
  Merge                 - merge all PDFs into one
  Watermark             - text or image watermark (opacity, position, page range)
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
import logging
import os
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

CAPTION = "Powered by - LEARN - X\u2122"
LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logo.jpg")

WELCOME = (
    "LEARN-X PDF Tools Bot\n\n"
    "Send me one PDF, or several at once.\n"
    "If you send multiple, they are merged first.\n\n"
    "What I can do:\n"
    "- Merge several PDFs into one\n"
    "- Watermark (text, image, or both): your opacity, position and page range\n"
    "- Rasterize: PDF to pictures to PDF (no copyable text)\n"
    "- Rasterize + Watermark: watermark burned in, unremovable\n"
    "- Compress: shrink a big or scanned PDF\n"
    "- Delete pages: remove a page range (e.g. 1-3, 5, 9)\n"
    "- Split: cut into parts by page range (e.g. 1-3, 4-8, 9-10)\n"
    "- All-in-One: merge + watermark + rasterize\n\n"
    "Before sending I will ask to keep or rename the file.\n"
    "Send /reset anytime to start over.\n"
    "Max file size: 20 MB per file (Telegram limit)."
)


# ---------- keyboards ----------
def kb_single():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Watermark", callback_data="mode:wm")],
        [InlineKeyboardButton("Rasterize", callback_data="mode:ras")],
        [InlineKeyboardButton("Rasterize + Watermark", callback_data="mode:raswm")],
        [InlineKeyboardButton("Compress", callback_data="mode:comp")],
        [InlineKeyboardButton("Delete pages", callback_data="mode:del")],
        [InlineKeyboardButton("Split", callback_data="mode:split")],
    ])


def kb_multi():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Merge only", callback_data="mode:merge")],
        [InlineKeyboardButton("All-in-One (merge+watermark+rasterize)",
                              callback_data="mode:raswm")],
        [InlineKeyboardButton("Merge + Watermark", callback_data="mode:wm")],
        [InlineKeyboardButton("Merge + Rasterize", callback_data="mode:ras")],
        [InlineKeyboardButton("Merge + Compress", callback_data="mode:comp")],
        [InlineKeyboardButton("Merge + Delete pages", callback_data="mode:del")],
        [InlineKeyboardButton("Merge + Split", callback_data="mode:split")],
    ])


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


def _prepare_input(context) -> str:
    pdfs = context.user_data["pdfs"]
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


# ---------- commands ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME)


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    shutil.rmtree(_user_dir(update.effective_user.id), ignore_errors=True)
    context.user_data.clear()
    await update.message.reply_text("Cleared. Send a PDF to start.")


# ---------- receive files ----------
async def on_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    awaiting = context.user_data.get("awaiting")

    if msg.photo:
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
    if n == 0:
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


# ---------- text input ----------
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    awaiting = context.user_data.get("awaiting")
    text = (update.message.text or "").strip()

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
    q = update.callback_query
    await q.answer()
    data = q.data

    if not context.user_data.get("pdfs"):
        return await q.edit_message_text("Please send a PDF first.")

    if data.startswith("mode:"):
        _prepare_input(context)
        m = data.split(":")[1]
        if m == "merge":
            context.user_data["op"] = {"kind": "merge"}
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

    await context.bot.send_message(chat_id, "Processing...")
    await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)
    loop = asyncio.get_running_loop()
    try:
        kind = op["kind"]
        if kind == "merge":
            out_path = in_path  # already merged (or the single file)
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
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, on_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_button))
    log.info("Bot starting (long polling)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
