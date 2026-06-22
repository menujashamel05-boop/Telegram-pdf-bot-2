"""PDF processing engine: merge, watermark (text + image), rasterize, compress.

rasterize = render each PDF page to a JPEG, then rebuild a PDF from those
images (PDF -> picture -> PDF). Uses PyMuPDF + Pillow; no external binaries.

Compress now RE-COMPRESSES the embedded images (downscale + re-JPEG) instead of
blindly rasterizing, so text PDFs keep their selectable text and never get
bigger. shrink_to_limit() keeps any output under a target size (e.g. Telegram).
"""
import io
import os
import shutil

import pymupdf  # PyMuPDF (the `fitz` module, new import name)
from PIL import Image, ImageDraw, ImageFont

DEFAULT_IMAGE_BOX_PT = 350   # image watermark fits in a 350x350 pt box
DEFAULT_TEXT_FONT_PT = 40    # text watermark default size, in points (Word-style)

# A font shipped INSIDE the repo so the watermark renders at the right size on
# every server (Railway etc.). Without it, PIL silently falls back to a tiny
# bitmap font and the watermark looks microscopic.
_FONT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wm_font.ttf")

POSITIONS = {
    "top_left", "top_center", "top_right",
    "center_left", "center", "center_right",
    "bottom_left", "bottom_center", "bottom_right",
    "tiled",
}


# ---------- utilities ----------
def page_count(path: str) -> int:
    doc = pymupdf.open(path)
    n = doc.page_count
    doc.close()
    return n


def merge_pdfs(paths, output_path: str) -> str:
    """Merge several PDFs into one, in the given order."""
    out = pymupdf.open()
    try:
        for p in paths:
            d = pymupdf.open(p)
            out.insert_pdf(d)
            d.close()
        out.save(output_path, garbage=4, deflate=True)
    finally:
        out.close()
    return output_path


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    # bundled font first (reliable everywhere), then system fonts as fallback
    for name in (_FONT_PATH, "DejaVuSans-Bold.ttf", "DejaVuSans.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _position_xy(cw, ch, ow, oh, position):
    m = int(min(cw, ch) * 0.04)
    if "left" in position:
        x = m
    elif "right" in position:
        x = cw - ow - m
    else:
        x = (cw - ow) // 2
    if "top" in position:
        y = m
    elif "bottom" in position:
        y = ch - oh - m
    else:
        y = (ch - oh) // 2
    return x, y


# ---------- overlay builders ----------
def _text_tile(text, fontsize, opacity, angle):
    font = _load_font(fontsize)
    probe = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    bbox = probe.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad = max(4, fontsize // 4)
    tile = Image.new("RGBA", (tw + 2 * pad, th + 2 * pad), (0, 0, 0, 0))
    ImageDraw.Draw(tile).text((pad - bbox[0], pad - bbox[1]), text, font=font,
                              fill=(120, 120, 120, int(255 * opacity)))
    if angle:
        tile = tile.rotate(angle, expand=True)
    return tile


def build_text_overlay(cw, ch, pt_to_px, text, opacity,
                       position="center", angle=0, tiled=False,
                       font_pt=DEFAULT_TEXT_FONT_PT):
    """font_pt is a real point size (like a Word font size). pt_to_px converts
    points to the pixel resolution of this overlay, so the size is identical on
    screen-overlay and rasterized output."""
    fontsize = max(8, int(round(font_pt * pt_to_px)))
    tile = _text_tile(text, fontsize, opacity, angle)
    overlay = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    if tiled:
        sx, sy = max(1, int(tile.width * 1.1)), max(1, int(tile.height * 1.6))
        for y in range(-tile.height, ch + tile.height, sy):
            for x in range(-tile.width, cw + tile.width, sx):
                overlay.alpha_composite(tile, (x, y))
    else:
        x, y = _position_xy(cw, ch, tile.width, tile.height, position)
        overlay.alpha_composite(tile, (x, y))
    return overlay


def build_image_overlay(cw, ch, pt_to_px, image_bytes, opacity,
                        box_pt=DEFAULT_IMAGE_BOX_PT):
    wm = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    box = max(1, int(box_pt * pt_to_px))
    wm.thumbnail((box, box))
    if opacity < 1:
        alpha = wm.split()[3].point(lambda v: int(v * opacity))
        wm.putalpha(alpha)
    overlay = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    overlay.alpha_composite(wm, ((cw - wm.width) // 2, (ch - wm.height) // 2))
    return overlay


def text_watermarker(text, opacity, position="center", angle=0, tiled=False,
                     font_pt=DEFAULT_TEXT_FONT_PT):
    return lambda cw, ch, ppx: build_text_overlay(
        cw, ch, ppx, text, opacity, position, angle, tiled, font_pt)


def image_watermarker(image_bytes, opacity, box_pt=DEFAULT_IMAGE_BOX_PT):
    return lambda cw, ch, ppx: build_image_overlay(
        cw, ch, ppx, image_bytes, opacity, box_pt)


def _blank(cw, ch, ppx):
    return Image.new("RGBA", (cw, ch), (0, 0, 0, 0))


def _png(overlay):
    buf = io.BytesIO()
    overlay.save(buf, format="PNG")
    return buf.getvalue()


# ---------- core ----------
def apply_watermark(input_path, output_path, build_fn, rasterize=False,
                    dpi=150, jpeg_quality=85, pages=None):
    """Overlay a watermark. `pages` is a set of 1-based page numbers to mark
    (None = all pages). rasterize=True burns it into the pixels."""
    src = pymupdf.open(input_path)
    try:
        if not rasterize:
            for i, page in enumerate(src):
                if pages is not None and (i + 1) not in pages:
                    continue
                rect = page.rect
                scale = 2
                cw, ch = int(rect.width * scale), int(rect.height * scale)
                overlay = build_fn(cw, ch, scale)
                page.insert_image(rect, stream=_png(overlay),
                                  overlay=True, keep_proportion=False)
            src.save(output_path, garbage=4, deflate=True)
        else:
            out = pymupdf.open()
            try:
                for i, page in enumerate(src):
                    pix = page.get_pixmap(dpi=dpi)
                    img = Image.frombytes(
                        "RGB", (pix.width, pix.height), pix.samples).convert("RGBA")
                    if pages is None or (i + 1) in pages:
                        overlay = build_fn(pix.width, pix.height, dpi / 72)
                        img.alpha_composite(overlay)
                    img = img.convert("RGB")
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=jpeg_quality, optimize=True)
                    newpage = out.new_page(width=pix.width, height=pix.height)
                    newpage.insert_image(newpage.rect, stream=buf.getvalue())
                out.save(output_path, garbage=4, deflate=True)
            finally:
                out.close()
    finally:
        src.close()
    return output_path


def rasterize_pdf(input_path, output_path, dpi=150, jpeg_quality=85):
    """PDF -> picture -> PDF, no watermark."""
    return apply_watermark(input_path, output_path, _blank,
                           rasterize=True, dpi=dpi, jpeg_quality=jpeg_quality)


# ---------- compression ----------
def _recompress_images(doc, max_edge, quality):
    """Downscale + re-encode every embedded image as JPEG. Only replaces an
    image when the new version is actually smaller."""
    seen = set()
    for page in doc:
        for img in page.get_images(full=True):
            xref = img[0]
            if xref in seen:
                continue
            seen.add(xref)
            try:
                info = doc.extract_image(xref)
                raw = info["image"]
                pil = Image.open(io.BytesIO(raw))
                pil.load()
            except Exception:
                continue
            if pil.mode in ("RGBA", "LA", "P"):
                base = pil.convert("RGBA")
                bg = Image.new("RGB", base.size, (255, 255, 255))
                bg.paste(base, mask=base.split()[-1])
                pil = bg
            elif pil.mode != "RGB":
                pil = pil.convert("RGB")
            w, h = pil.size
            sc = min(1.0, max_edge / max(w, h))
            if sc < 1.0:
                pil = pil.resize((max(1, int(w * sc)), max(1, int(h * sc))),
                                 Image.LANCZOS)
            buf = io.BytesIO()
            pil.save(buf, format="JPEG", quality=quality, optimize=True)
            nd = buf.getvalue()
            if len(nd) < len(raw):
                try:
                    page.replace_image(xref, stream=nd)
                except Exception:
                    pass


def compress_pdf(input_path, output_path, level="medium"):
    """Shrink a PDF by re-compressing its embedded images and cleaning the
    structure. Text/vector PDFs keep their selectable text. Guarantees the
    output is never larger than the input."""
    presets = {"light": (1800, 78), "medium": (1300, 62), "strong": (1000, 48)}
    max_edge, quality = presets.get(level, presets["medium"])
    doc = pymupdf.open(input_path)
    try:
        _recompress_images(doc, max_edge, quality)
        doc.save(output_path, garbage=4, deflate=True, clean=True,
                 deflate_images=True, deflate_fonts=True)
    finally:
        doc.close()
    # never hand back a bigger file than we got
    if os.path.getsize(output_path) >= os.path.getsize(input_path):
        shutil.copyfile(input_path, output_path)
    return output_path


def shrink_to_limit(path, limit_mb=48):
    """Make sure a finished PDF fits under limit_mb by progressively
    re-compressing its images. Used as a safety net for big rasterized files so
    they can still be sent over Telegram (50 MB send cap)."""
    limit = int(limit_mb * 1024 * 1024)
    if os.path.getsize(path) <= limit:
        return path
    for max_edge, quality in [(1700, 72), (1400, 62), (1150, 54), (950, 46),
                              (800, 40), (650, 34), (520, 28)]:
        doc = pymupdf.open(path)
        try:
            _recompress_images(doc, max_edge, quality)
            tmp = path + ".tmp"
            doc.save(tmp, garbage=4, deflate=True, clean=True)
        finally:
            doc.close()
        os.replace(tmp, path)
        if os.path.getsize(path) <= limit:
            break
    return path
