"""Telegram PDF Tools Bot - LEARN-X

Send ONE or MULTIPLE PDFs. With multiple PDFs they are merged first.

Actions:
  Merge                 - merge all PDFs into one
  Watermark             - text or image watermark (opacity, position, page range)
  Rasterize             - PDF -> pictures -> PDF (no copyable text)
  Rasterize + Watermark - watermark burned into the pixels, unremovable
  Compress              - shrink a large / scanned PDF
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

CAPTION = "Powered by - LEARN - X\u2122"
LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logo.jpg")

WELCOME = (
    "LEARN-X PDF Tools Bot\n\n"
    "Send me one PDF, or several at once.\n"
    "If you send multiple, they are merged first.\n\n"
    "What I can do:\n"
    "- Merge several PDFs into one\n"
    "- Watermark (text or image): your opacity, position and page range\n"
    "- Rasterize: PDF to pictures to PDF (no copyable text)\n"
    "- Rasterize + Watermark: watermark burned in, unremovable\n"
    "- Compress: shrink a big or scanned PDF\n"
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
    ])


def kb_multi():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Merge only", callback_data="mode:merge")],
        [InlineKeyboardButton("All-in-One (merge+watermark+rasterize)",
                              callback_data="mode:raswm")],
        [InlineKeyboardButton("Merge + Watermark", callback_data="mode:wm")],
        [InlineKeyboardButton("Merge + Rasterize", callback_data="mode:ras")],
        [InlineKeyboardButton("Merge + Compress", callback_data="mode:comp")],
    ])


def kb_wm_type():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Text", callback_data="wt:text"),
         InlineKeyboardButton("Image", callback_data="wt:image")],
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
    if context.user_data["wm_kind"] == "text":
        pos = context.user_data.get("wm_position", "center")
        tiled = pos == "tiled"
        angle = 45 if tiled else 0
        return pdf_tools.text_watermarker(
            context.user_data["wm_text"], context.user_data["wm_opacity"],
            position=pos, angle=angle, tiled=tiled)
    with open(context.user_data["wm_image_path"], "rb") as fh:
        img_bytes = fh.read()
    return pdf_tools.image_watermarker(img_bytes, context.user_data["wm_opacity"])


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
    await update.message.reply_text(
        "Image received (it will be centered, up to 500x500).\n"
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
        if context.user_data.get("flow") == "raswm":
            return await update.message.reply_text(
                "Pick output quality (DPI):", reply_markup=kb_dpi("fdpi"))
        context.user_data["op"] = {"kind": "wm"}
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

    if data == "wt:text":
        context.user_data["wm_kind"] = "text"
        context.user_data["awaiting"] = "wm_text"
        return await q.edit_message_text("Send the watermark text:")

    if data == "wt:image":
        context.user_data["wm_kind"] = "image"
        context.user_data["awaiting"] = "wm_image"
        return await q.edit_message_text(
            "Send the image to use as a watermark (photo or file):")

    if data.startswith("pos:"):
        context.user_data["wm_position"] = data.split(":")[1]
        context.user_data["awaiting"] = "opacity"
        return await q.edit_message_text(
            "Now type the opacity from 1 to 100 (e.g. 25):")

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
        elif kind == "wm":
            build = _build_watermarker(context)
            raster = context.user_data.get("flow") == "raswm"
            dpi = context.user_data.get("wm_dpi", 150)
            pages = context.user_data.get("wm_pages")
            await loop.run_in_executor(None, lambda: pdf_tools.apply_watermark(
                in_path, out_path, build, rasterize=raster, dpi=dpi, pages=pages))
    except Exception as exc:  # noqa: BLE001
        log.exception("processing failed")
        return await context.bot.send_message(chat_id, f"Error: {exc}")
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
