"""PDF processing engine: merge, watermark (text + image), rasterize, compress.

rasterize = render each PDF page to a JPEG, then rebuild a PDF from those
images (PDF -> picture -> PDF). Uses PyMuPDF + Pillow; no external binaries.
"""
import io

import pymupdf  # PyMuPDF (the `fitz` module, new import name)
from PIL import Image, ImageDraw, ImageFont

DEFAULT_IMAGE_BOX_PT = 400  # watermark image fits in a 400x400 pt box

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
    for name in ("DejaVuSans-Bold.ttf", "DejaVuSans.ttf", "arial.ttf"):
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
    pad = fontsize
    tile = Image.new("RGBA", (tw + 2 * pad, th + 2 * pad), (0, 0, 0, 0))
    ImageDraw.Draw(tile).text((pad, pad), text, font=font,
                              fill=(120, 120, 120, int(255 * opacity)))
    if angle:
        tile = tile.rotate(angle, expand=True)
    return tile


def build_text_overlay(cw, ch, pt_to_px, text, opacity,
                       position="center", angle=0, tiled=False):
    fontsize = max(18, int(min(cw, ch) / 16))
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


def text_watermarker(text, opacity, position="center", angle=0, tiled=False):
    return lambda cw, ch, ppx: build_text_overlay(
        cw, ch, ppx, text, opacity, position, angle, tiled)


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


def compress_pdf(input_path, output_path, level="medium"):
    presets = {"light": (160, 80), "medium": (120, 65), "strong": (96, 50)}
    dpi, quality = presets.get(level, presets["medium"])
    return rasterize_pdf(input_path, output_path, dpi=dpi, jpeg_quality=quality)
