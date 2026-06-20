"""Telegram PDF Tools Bot

Modes:
  ðŸ’§ Watermark            -> Text or Image
       Text  : enter text  -> choose position (9-grid or tiled) -> type opacity
       Image : send image  -> centered (300x300 box) -> type opacity
  ðŸ–¼ Rasterize             -> PDF -> picture -> PDF (no copyable text)
  ðŸ”’ Rasterize+Watermark   -> same watermark flow, then burned into the pixels
  ðŸ—œ Compress              -> shrink large / scanned PDFs

Long-polling (no webhook/URL). Set BOT_TOKEN env var before running.
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

MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024  # Telegram bot download limit

WELCOME = (
    "ðŸ‘‹ *PDF Tools Bot*\n\n"
    "Send me a PDF, then choose:\n"
    "â€¢ ðŸ’§ *Watermark* â€“ text or image, your opacity & placement\n"
    "â€¢ ðŸ–¼ *Rasterize* â€“ PDF â†’ pictures â†’ PDF (no copyable text)\n"
    "â€¢ ðŸ”’ *Rasterize + Watermark* â€“ watermark burned in, unremovable\n"
    "â€¢ ðŸ—œ *Compress* â€“ shrink a big / scanned PDF\n\n"
    "_Max file size: 20 MB (Telegram limit)._"
)


# ---------- keyboards ----------
def kb_modes():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("ðŸ’§ Watermark", callback_data="mode:wm")],
        [InlineKeyboardButton("ðŸ–¼ Rasterize", callback_data="mode:ras")],
        [InlineKeyboardButton("ðŸ”’ Rasterize + Watermark", callback_data="mode:raswm")],
        [InlineKeyboardButton("ðŸ—œ Compress", callback_data="mode:comp")],
    ])


def kb_wm_type():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("ðŸ”¤ Text", callback_data="wt:text"),
         InlineKeyboardButton("ðŸ–¼ Image", callback_data="wt:image")],
    ])


def kb_positions():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("â†–", callback_data="pos:top_left"),
         InlineKeyboardButton("â¬†", callback_data="pos:top_center"),
         InlineKeyboardButton("â†—", callback_data="pos:top_right")],
        [InlineKeyboardButton("â¬…", callback_data="pos:center_left"),
         InlineKeyboardButton("â¸ Center", callback_data="pos:center"),
         InlineKeyboardButton("âž¡", callback_data="pos:center_right")],
        [InlineKeyboardButton("â†™", callback_data="pos:bottom_left"),
         InlineKeyboardButton("â¬‡", callback_data="pos:bottom_center"),
         InlineKeyboardButton("â†˜", callback_data="pos:bottom_right")],
        [InlineKeyboardButton("ðŸ” Tiled (diagonal)", callback_data="pos:tiled")],
    ])


def kb_dpi(prefix):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("150", callback_data=f"{prefix}:150"),
        InlineKeyboardButton("200", callback_data=f"{prefix}:200"),
        InlineKeyboardButton("300", callback_data=f"{prefix}:300"),
    ]])


def kb_quality():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("ðŸŸ¢ Light", callback_data="comp:light"),
        InlineKeyboardButton("ðŸŸ¡ Medium", callback_data="comp:medium"),
        InlineKeyboardButton("ðŸ”´ Strong", callback_data="comp:strong"),
    ]])


# ---------- helpers ----------
def _user_dir(uid: int) -> str:
    path = os.path.join(tempfile.gettempdir(), f"pdfbot_{uid}")
    os.makedirs(path, exist_ok=True)
    return path


def _has_pdf(context) -> bool:
    p = context.user_data.get("in_path")
    return bool(p and os.path.exists(p))


# ---------- commands ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_markdown(WELCOME)


# ---------- receive PDF / images ----------
async def on_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    awaiting = context.user_data.get("awaiting")

    # a photo (only meaningful when we asked for a watermark image)
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

    if is_pdf:
        if doc.file_size and doc.file_size > MAX_DOWNLOAD_BYTES:
            return await msg.reply_text(
                "âš ï¸ Over 20 MB â€” Telegram won't let a bot download it.")
        udir = _user_dir(update.effective_user.id)
        in_path = os.path.join(udir, "input.pdf")
        await (await doc.get_file()).download_to_drive(in_path)
        context.user_data.clear()
        context.user_data["in_path"] = in_path
        context.user_data["orig_name"] = doc.file_name or "document.pdf"
        return await msg.reply_text("Got it âœ… What should I do?", reply_markup=kb_modes())

    await msg.reply_text("Please send a PDF.")


async def _receive_wm_image(update, context, tg_obj):
    udir = _user_dir(update.effective_user.id)
    img_path = os.path.join(udir, "watermark_img.png")
    await (await tg_obj.get_file()).download_to_drive(img_path)
    context.user_data["wm_image_path"] = img_path
    context.user_data["awaiting"] = "opacity"
    await update.message.reply_text(
        "Image received ðŸ–¼ (it will be centered).\n"
        "Now *type the opacity* from 1 to 100 (e.g. `25`):",
        parse_mode="Markdown")


# ---------- text input (watermark text + opacity) ----------
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
        op = max(0.01, min(1.0, op))
        context.user_data["wm_opacity"] = op
        context.user_data["awaiting"] = None
        # rasterize+watermark needs a DPI; plain watermark finishes now
        if context.user_data.get("flow") == "raswm":
            return await update.message.reply_text(
                "Pick output quality (DPI):", reply_markup=kb_dpi("fdpi"))
        return await _finish_watermark(update, context)

    # no active step
    await update.message.reply_text("Send a PDF to begin, or /start for help.")


# ---------- buttons ----------
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if not _has_pdf(context):
        return await q.edit_message_text("Please send a PDF first.")

    if data in ("mode:wm", "mode:raswm"):
        context.user_data["flow"] = "raswm" if data == "mode:raswm" else "wm"
        return await q.edit_message_text(
            "Text or image watermark?", reply_markup=kb_wm_type())

    if data == "mode:ras":
        return await q.edit_message_text(
            "Rasterize quality (DPI):", reply_markup=kb_dpi("rdpi"))

    if data == "mode:comp":
        return await q.edit_message_text(
            "Compression level:", reply_markup=kb_quality())

    if data == "wt:text":
        context.user_data["wm_kind"] = "text"
        context.user_data["awaiting"] = "wm_text"
        return await q.edit_message_text("Send the *watermark text*:", parse_mode="Markdown")

    if data == "wt:image":
        context.user_data["wm_kind"] = "image"
        context.user_data["awaiting"] = "wm_image"
        return await q.edit_message_text(
            "Send the *image* to use as a watermark (photo or file):",
            parse_mode="Markdown")

    if data.startswith("pos:"):
        context.user_data["wm_position"] = data.split(":")[1]
        context.user_data["awaiting"] = "opacity"
        return await q.edit_message_text(
            "Now *type the opacity* from 1 to 100 (e.g. `25`):",
            parse_mode="Markdown")

    if data.startswith("rdpi:"):  # plain rasterize
        return await _run(update, context, mode="ras", dpi=int(data.split(":")[1]))

    if data.startswith("fdpi:"):  # rasterize + watermark final dpi
        return await _finish_watermark(update, context, dpi=int(data.split(":")[1]))

    if data.startswith("comp:"):
        return await _run(update, context, mode="comp", level=data.split(":")[1])


# ---------- run watermark ----------
async def _finish_watermark(update, context, dpi: int = 150):
    chat_id = update.effective_chat.id
    in_path = context.user_data["in_path"]
    out_path = os.path.join(os.path.dirname(in_path), "output.pdf")
    op = context.user_data["wm_opacity"]
    kind = context.user_data["wm_kind"]
    raster = context.user_data.get("flow") == "raswm"

    if kind == "text":
        pos = context.user_data.get("wm_position", "center")
        tiled = pos == "tiled"
        angle = 45 if tiled else 0
        build = pdf_tools.text_watermarker(
            context.user_data["wm_text"], op, position=pos, angle=angle, tiled=tiled)
        label = f"ðŸ’§ text Â· {pos} Â· {int(op*100)}%"
    else:
        with open(context.user_data["wm_image_path"], "rb") as fh:
            img_bytes = fh.read()
        build = pdf_tools.image_watermarker(img_bytes, op)
        label = f"ðŸ’§ image Â· center Â· {int(op*100)}%"
    if raster:
        label += f" Â· burned-in {dpi}DPI"

    await context.bot.send_message(chat_id, "â³ Processing...")
    await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, lambda: pdf_tools.apply_watermark(
            in_path, out_path, build, rasterize=raster, dpi=dpi))
    except Exception as exc:  # noqa: BLE001
        log.exception("watermark failed")
        return await context.bot.send_message(chat_id, f"âŒ Error: {exc}")
    await _send_result(update, context, out_path, label)


# ---------- run rasterize / compress ----------
async def _run(update, context, mode: str, dpi: int = 150, level: str = "medium"):
    chat_id = update.effective_chat.id
    in_path = context.user_data["in_path"]
    out_path = os.path.join(os.path.dirname(in_path), "output.pdf")

    await context.bot.send_message(chat_id, "â³ Processing...")
    await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)
    loop = asyncio.get_running_loop()
    try:
        if mode == "ras":
            await loop.run_in_executor(
                None, lambda: pdf_tools.rasterize_pdf(in_path, out_path, dpi=dpi))
            label = f"ðŸ–¼ Rasterized Â· {dpi} DPI"
        else:
            await loop.run_in_executor(
                None, lambda: pdf_tools.compress_pdf(in_path, out_path, level=level))
            label = f"ðŸ—œ Compressed Â· {level}"
    except Exception as exc:  # noqa: BLE001
        log.exception("processing failed")
        return await context.bot.send_message(chat_id, f"âŒ Error: {exc}")
    await _send_result(update, context, out_path, label)


async def _send_result(update, context, out_path: str, label: str):
    chat_id = update.effective_chat.id
    name = "processed_" + context.user_data.get("orig_name", "document.pdf")
    size_mb = os.path.getsize(out_path) / (1024 * 1024)
    with open(out_path, "rb") as fh:
        await context.bot.send_document(
            chat_id, document=fh, filename=name,
            caption=f"{label}\nðŸ“¦ {size_mb:.1f} MB")
    shutil.rmtree(os.path.dirname(out_path), ignore_errors=True)
    context.user_data.clear()


def main():
    app: Application = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, on_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_button))
    log.info("Bot starting (long polling)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
