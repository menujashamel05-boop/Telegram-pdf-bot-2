"""PDF processing engine: watermark (text + image), rasterize, compress.

rasterize = render each PDF page to a JPEG image, then rebuild a PDF from those
images (PDF -> picture -> PDF). Uses PyMuPDF + Pillow; no external binaries.
"""
import io

import pymupdf  # PyMuPDF (the `fitz` module, new import name)
from PIL import Image, ImageDraw, ImageFont

DEFAULT_WATERMARK = "Learn_X_Edu"
DEFAULT_IMAGE_BOX_PT = 300  # watermark image fits in a 300x300 pt box (like Sejda)

# valid text positions
POSITIONS = {
    "top_left", "top_center", "top_right",
    "center_left", "center", "center_right",
    "bottom_left", "bottom_center", "bottom_right",
    "tiled",
}


# ---------- fonts ----------
def _load_font(size: int) -> ImageFont.FreeTypeFont:
    for name in ("DejaVuSans-Bold.ttf", "DejaVuSans.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


# ---------- placement ----------
def _position_xy(cw: int, ch: int, ow: int, oh: int, position: str):
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


# ---------- overlay builders (return an RGBA PIL image sized to the canvas) ----------
def _text_tile(text: str, fontsize: int, opacity: float, angle: int) -> Image.Image:
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


def build_text_overlay(cw: int, ch: int, pt_to_px: float, text: str,
                       opacity: float, position: str = "center",
                       angle: int = 0, tiled: bool = False) -> Image.Image:
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


def build_image_overlay(cw: int, ch: int, pt_to_px: float, image_bytes: bytes,
                        opacity: float, box_pt: int = DEFAULT_IMAGE_BOX_PT) -> Image.Image:
    wm = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    box = max(1, int(box_pt * pt_to_px))
    wm.thumbnail((box, box))  # fit inside the box, keep aspect ratio
    if opacity < 1:
        alpha = wm.split()[3].point(lambda v: int(v * opacity))
        wm.putalpha(alpha)
    overlay = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    overlay.alpha_composite(wm, ((cw - wm.width) // 2, (ch - wm.height) // 2))  # centered
    return overlay


# ---------- watermarker factories: return build_fn(cw, ch, pt_to_px) ----------
def text_watermarker(text: str, opacity: float, position: str = "center",
                     angle: int = 0, tiled: bool = False):
    return lambda cw, ch, ppx: build_text_overlay(
        cw, ch, ppx, text, opacity, position, angle, tiled)


def image_watermarker(image_bytes: bytes, opacity: float,
                      box_pt: int = DEFAULT_IMAGE_BOX_PT):
    return lambda cw, ch, ppx: build_image_overlay(
        cw, ch, ppx, image_bytes, opacity, box_pt)


def _blank(cw, ch, ppx):
    return Image.new("RGBA", (cw, ch), (0, 0, 0, 0))


# ---------- core ----------
def _png(overlay: Image.Image) -> bytes:
    buf = io.BytesIO()
    overlay.save(buf, format="PNG")
    return buf.getvalue()


def apply_watermark(input_path: str, output_path: str, build_fn,
                    rasterize: bool = False, dpi: int = 150,
                    jpeg_quality: int = 85) -> str:
    """Overlay a watermark on every page.
    rasterize=False -> keeps text layer, watermark sits on top as an image.
    rasterize=True  -> renders pages to JPEG (PDF->picture->PDF); watermark is
                       burned into the pixels and cannot be removed.
    """
    src = pymupdf.open(input_path)
    try:
        if not rasterize:
            for page in src:
                rect = page.rect
                scale = 2  # supersample for crisp overlay text
                cw, ch = int(rect.width * scale), int(rect.height * scale)
                overlay = build_fn(cw, ch, scale)
                page.insert_image(rect, stream=_png(overlay),
                                  overlay=True, keep_proportion=False)
            src.save(output_path, garbage=4, deflate=True)
        else:
            out = pymupdf.open()
            try:
                for page in src:
                    pix = page.get_pixmap(dpi=dpi)
                    img = Image.frombytes(
                        "RGB", (pix.width, pix.height), pix.samples).convert("RGBA")
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


def rasterize_pdf(input_path: str, output_path: str, dpi: int = 150,
                  jpeg_quality: int = 85) -> str:
    """PDF -> picture -> PDF, no watermark."""
    return apply_watermark(input_path, output_path, _blank,
                           rasterize=True, dpi=dpi, jpeg_quality=jpeg_quality)


def compress_pdf(input_path: str, output_path: str, level: str = "medium") -> str:
    presets = {"light": (160, 80), "medium": (120, 65), "strong": (96, 50)}
    dpi, quality = presets.get(level, presets["medium"])
    return rasterize_pdf(input_path, output_path, dpi=dpi, jpeg_quality=quality)
