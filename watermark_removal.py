"""Conservative, lossless removal of independently-contained PDF watermarks.

This module deliberately removes page-level *content streams*, never portions of a
stream, pixels, or shared XObjects.  It is consequently safe by default, but it
cannot remove a watermark that was rasterized into a scan or mixed with document
content.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any
import math
import os

from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject, ByteStringObject, ContentStream, DictionaryObject, NameObject,
    TextStringObject,
)

try:  # Pillow is a declared dependency; keep import errors human-readable.
    from PIL import Image, ImageChops
except ImportError:  # pragma: no cover - dependency error path
    Image = ImageChops = None


_MAX_IMAGE_PIXELS = 40_000_000
_ALLOWED_TEXT_OPS = {
    b"q", b"Q", b"gs", b"BT", b"ET", b"Tf", b"TL", b"Tm", b"Td", b"TD",
    b"T*", b"Tc", b"Tw", b"Tz", b"Ts", b"Tr", b"rg", b"g", b"k", b"Tj", b"TJ",
}
# Operators allowed inside the producer's explicitly-marked image watermark.
_ALLOWED_MARKED_OPS = _ALLOWED_TEXT_OPS | {b"cm", b"Do"}


def _result(*, status: str, removed_streams: int = 0, removed_occurrences: int = 0,
            pages_changed: list[int] | None = None, matched_targets: list[str] | None = None,
            unmatched_targets: list[str] | None = None, warnings: list[str] | None = None,
            validated: bool = False, **extra: Any) -> dict[str, Any]:
    answer = {
        "status": status, "removed_streams": removed_streams,
        "removed_occurrences": removed_occurrences,
        "pages_changed": pages_changed or [], "matched_targets": matched_targets or [],
        "unmatched_targets": unmatched_targets or [], "warnings": warnings or [],
        "validated": validated,
    }
    answer.update(extra)
    return answer


def _deref(obj: Any) -> Any:
    return obj.get_object() if hasattr(obj, "get_object") else obj


def _contents(page: Any) -> list[Any]:
    contents = page.get("/Contents")
    if contents is None:
        return []
    contents = _deref(contents)
    return list(contents) if isinstance(contents, ArrayObject) else [contents]


def _numbers(values: Any, n: int) -> list[float] | None:
    if not isinstance(values, (list, tuple)) or len(values) != n:
        return None
    try:
        return [float(x) for x in values]
    except (ValueError, TypeError, OverflowError):
        return None


def _reliable_text(value: Any) -> str | None:
    """Only accept pypdf's confidently decoded Unicode strings, never bytes."""
    if isinstance(value, ByteStringObject) or not isinstance(value, TextStringObject):
        return None
    text = str(value)
    return text if text and "\ufffd" not in text and "\x00" not in text else None


def _show_text(args: Any, op: bytes) -> str | None:
    if op == b"Tj":
        return _reliable_text(args[0]) if isinstance(args, (list, tuple)) and len(args) == 1 else None
    if op != b"TJ" or not isinstance(args, (list, tuple)) or len(args) != 1:
        return None
    seq = args[0]
    if not isinstance(seq, (list, tuple)):
        return None
    pieces: list[str] = []
    for part in seq:
        if isinstance(part, (int, float)):
            continue
        text = _reliable_text(part)
        if text is None:
            return None
        pieces.append(text)
    return "".join(pieces) or None


def _extgstate(page: Any, name: Any) -> bool:
    """Return whether a named graphics state is an ordinary translucent fill."""
    try:
        gs = _deref(_deref(page["/Resources"])["/ExtGState"][name])
        ca = float(gs.get("/ca", 1))
        CA = float(gs.get("/CA", ca))
        bm = gs.get("/BM", "/Normal")
        if isinstance(bm, ArrayObject):
            return False
        # Soft masks, alpha-is-shape and transfer functions change semantics.
        forbidden = {"/SMask", "/AIS", "/TR", "/TR2", "/HT", "/OP", "/op"}
        return (0 < ca <= .2 and 0 <= CA <= .2 and str(bm) == "/Normal"
                and not any(k in gs for k in forbidden))
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def _diagonal(matrix: list[float]) -> bool:
    a, b, c, d, _, _ = matrix
    # Real rotation: both axes participate and are approximately orthogonal.
    scale_a, scale_b = math.hypot(a, b), math.hypot(c, d)
    return (scale_a > .05 and scale_b > .05 and abs(a * c + b * d) < .02 * scale_a * scale_b
            and abs(a) > .12 * scale_a and abs(b) > .12 * scale_a
            and abs(c) > .12 * scale_b and abs(d) > .12 * scale_b)


def _balanced(ops: list[tuple[Any, bytes]]) -> bool:
    depth = 0
    for _, op in ops:
        if op == b"q":
            depth += 1
        elif op == b"Q":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _text_candidate(ref: Any, page: Any, reader: PdfReader) -> dict[str, Any] | None:
    """Classify a text-only, self-contained stream.  Unknown syntax is unsafe."""
    try:
        ops = ContentStream(_deref(ref), reader).operations
    except Exception:
        return None
    if not ops or not _balanced(ops) or any(op not in _ALLOWED_TEXT_OPS for _, op in ops):
        return None
    in_text = False
    graphics_depth = 0
    current_gs_ok = False
    font_size: float | None = None
    matrix: list[float] | None = None
    render_mode = 0
    texts: list[str] = []
    cues: list[bool] = []
    manual_cues: list[bool] = []
    for args, op in ops:
        if op == b"q":
            graphics_depth += 1
            # Do not make assumptions about inherited graphics state. A removable
            # stream must establish its own safe transparency after its isolation q.
            current_gs_ok = False
        elif op == b"Q":
            graphics_depth -= 1
            current_gs_ok = False
        elif op == b"gs":
            if len(args) != 1:
                return None
            current_gs_ok = _extgstate(page, args[0])
        elif op == b"BT":
            if in_text:
                return None
            in_text = True
            font_size = matrix = None
            render_mode = 0
        elif op == b"ET":
            if not in_text:
                return None
            in_text = False
        elif op == b"Tf":
            if not in_text or len(args) != 2:
                return None
            try:
                font_size = abs(float(args[1]))
            except (ValueError, TypeError):
                return None
        elif op == b"Tm":
            if not in_text:
                return None
            matrix = _numbers(args, 6)
            if matrix is None:
                return None
        elif op == b"Tr":
            if not in_text or len(args) != 1:
                return None
            try:
                render_mode = int(args[0])
            except (ValueError, TypeError):
                return None
            if render_mode != 0:  # stroke, clip, invisible and combined modes are skipped
                return None
        elif op in (b"Tj", b"TJ"):
            if not in_text or graphics_depth <= 0 or font_size is None or matrix is None:
                return None
            text = _show_text(args, op)
            if text is None:
                return None
            # Coordinates/CTM cannot be wholly evaluated without interpreting all page
            # state; a very large base font plus true diagonal Tm is a conservative cue.
            if font_size * math.hypot(matrix[0], matrix[1]) < 24:
                return None
            texts.append(text)
            cues.append(current_gs_ok and _diagonal(matrix))
            manual_cues.append(current_gs_ok or _diagonal(matrix))
        elif op in (b"Td", b"TD"):
            if not in_text or _numbers(args, 2) is None:
                return None
        elif op in (b"Tc", b"Tw", b"Tz", b"Ts", b"TL"):
            if not in_text or len(args) != 1:
                return None
        elif op == b"T*" and not in_text:
            return None
    if in_text or graphics_depth != 0 or not texts:
        return None
    # This stream is text-only. All displayed strings must be the same exact text;
    # otherwise a request might accidentally erase a legitimate neighboring label.
    if len(set(texts)) != 1:
        return None
    return {"kind": "text", "text": texts[0], "occurrences": len(texts),
            "auto_cue": all(cues), "manual_cue": all(manual_cues)}


def _marked_candidate(ref: Any, page: Any, reader: PdfReader) -> dict[str, Any] | None:
    """Recognize only a complete `/Artifact /Watermark BDC ... EMC` stream."""
    try:
        ops = ContentStream(_deref(ref), reader).operations
    except Exception:
        return None
    if len(ops) < 3 or ops[0][1] != b"BDC" or ops[-1][1] != b"EMC":
        return None
    first = ops[0][0]
    if not isinstance(first, (list, tuple)) or len(first) != 2:
        return None
    try:
        tag, props = first
        props = _deref(props)
        if str(tag) != "/Artifact" or str(props.get("/Subtype")) != "/Watermark":
            return None
    except (AttributeError, TypeError):
        return None
    inner = ops[1:-1]
    if (not inner or inner[0][1] != b"q" or inner[-1][1] != b"Q"
            or any(op not in _ALLOWED_MARKED_OPS for _, op in inner) or not _balanced(inner)):
        return None
    # BDC/EMC must own all painting. Text is valid only if it is a safe standalone
    # text overlay; image Do calls are resolved separately by image mode.
    do_names = [a[0] for a, op in inner if op == b"Do" and isinstance(a, (list, tuple)) and len(a) == 1]
    if len(do_names) != sum(1 for _, op in inner if op == b"Do"):
        return None
    return {"kind": "marked", "occurrences": 1, "do_names": do_names}


def _image_stream_within_limit(obj: Any, seen: set[tuple[int, int] | int] | None = None) -> bool:
    """Check declared image (and soft-mask) dimensions before Pillow decodes it."""
    seen = seen or set()
    try:
        raw = obj
        ref_key = ((int(raw.idnum), int(raw.generation)) if hasattr(raw, "idnum") else id(raw))
        if ref_key in seen:
            return True
        seen.add(ref_key)
        image = _deref(raw)
        if str(image.get("/Subtype")) != "/Image":
            return False
        width, height = image.get("/Width"), image.get("/Height")
        if isinstance(width, bool) or isinstance(height, bool):
            return False
        width, height = int(width), int(height)
        if width < 1 or height < 1 or width * height > _MAX_IMAGE_PIXELS:
            return False
        # pypdf may decode a soft mask while constructing the composited image.
        soft_mask = image.get("/SMask")
        if soft_mask is not None and not _image_stream_within_limit(soft_mask, seen):
            return False
        return True
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        return False


def _image_from_page(page: Any, name: Any) -> Any | None:
    if Image is None:
        return None
    try:
        resources = _deref(page.get("/Resources", {}))
        xobjects = _deref(resources.get("/XObject", {}))
        image_ref = xobjects[name]
        # Do not call ImageFile.image (which decodes/allocates) until every
        # declared image dimension needed for that decode has been bounded.
        if not _image_stream_within_limit(image_ref):
            return None
        image_file = page.images[str(name)]
        im = image_file.image.copy()
        return im.convert("RGBA")
    except Exception:
        return None


def _normalized_rgba(im: Any, *, require_transparency: bool = False) -> Any | None:
    if Image is None:
        return None
    try:
        im = im.convert("RGBA")
        if im.width < 1 or im.height < 1 or im.width * im.height > _MAX_IMAGE_PIXELS:
            return None
        alpha = im.getchannel("A")
        box = alpha.getbbox()
        if box is None:  # fully transparent references are ambiguous/no-op
            return None
        im = im.crop(box)
        alpha = im.getchannel("A")
        lo, hi = alpha.getextrema()
        # The candidate must demonstrate transparency. A user reference may be
        # the original opaque logo: the parent watermarker can apply its uniform
        # opacity only when constructing the page-sized overlay.
        if require_transparency and lo == 255:
            return None
        # Normalise a uniform opacity multiplier.  Alpha shape and RGB stay exact.
        amax = max(1, hi)
        if amax != 255:
            alpha = alpha.point(lambda v: min(255, int(round(v * 255 / amax))))
            im.putalpha(alpha)
        return im
    except Exception:
        return None


def _same_reference(candidate: Any, reference: Any) -> bool:
    a, b = _normalized_rgba(candidate, require_transparency=True), _normalized_rgba(reference)
    if a is None or b is None or a.size != b.size:
        return False
    # PDF colour-space conversion can change a PNG RGB component by 1–2 levels.
    # Permit only that bounded encoding round-off (alpha remains exact), not a
    # perceptual/fuzzy similarity decision.
    extrema = ImageChops.difference(a, b).getextrema()
    return all(high <= (0 if channel == 3 else 2)
               for channel, (_low, high) in enumerate(extrema))


def _resource_xobject_hashes(resources: Any, prefix: str = "", seen: set[int] | None = None) -> dict[str, str]:
    """Hash image/form stream bytes, recursively, without altering resources."""
    seen = seen or set()
    found: dict[str, str] = {}
    try:
        xobjects = _deref(_deref(resources).get("/XObject", {}))
        for name, ref in xobjects.items():
            obj = _deref(ref)
            marker = id(obj)
            subtype = str(obj.get("/Subtype"))
            key = prefix + str(name)
            # Record every resource *registration*, including aliases of the
            # same indirect image.  Alias names matter when one registration is
            # removed while another is retained on this page.
            if subtype in {"/Image", "/Form"}:
                found[key] = sha256(obj.get_data()).hexdigest()
            # Only recursion needs a visited set; otherwise aliases would vanish
            # from the verification snapshot.
            if subtype == "/Form" and "/Resources" in obj and marker not in seen:
                seen.add(marker)
                found.update(_resource_xobject_hashes(obj["/Resources"], key + "/", seen))
    except Exception:
        # A malformed/unreadable retained XObject is not modified; writer reload
        # validation still protects page count and content streams.
        pass
    return found


def _page_snapshot(page: Any, excluded: set[int] | None = None,
                   excluded_xobjects: set[str] | None = None) -> dict[str, Any]:
    excluded = excluded or set()
    excluded_xobjects = excluded_xobjects or set()
    streams = [sha256(_deref(ref).get_data()).hexdigest()
               for i, ref in enumerate(_contents(page)) if i not in excluded]
    xobjects = _resource_xobject_hashes(page.get("/Resources", {}))
    # A registration is excluded only after _unused_removed_image_names proved
    # it is a removed standalone overlay and it is unused by retained content.
    xobjects = {name: digest for name, digest in xobjects.items()
                if name not in excluded_xobjects}
    return {"contents": streams, "xobjects": xobjects,
            "mediabox": tuple(float(v) for v in page.mediabox),
            "cropbox": tuple(float(v) for v in page.cropbox),
            "rotation": int(page.get("/Rotate", 0))}


def _validate(output: Path, expected: list[dict[str, Any]]) -> None:
    check = PdfReader(str(output))
    if len(check.pages) != len(expected):
        raise ValueError("verification failed: page count changed")
    for number, (page, want) in enumerate(zip(check.pages, expected), 1):
        got = _page_snapshot(page)
        if got != want:
            raise ValueError(f"verification failed: retained content or page geometry changed on page {number}")


def _xobject_is_image(page: Any, name: Any) -> bool:
    try:
        resources = _deref(page.get("/Resources", {}))
        xobjects = _deref(resources.get("/XObject", {}))
        return str(_deref(xobjects[name]).get("/Subtype")) == "/Image"
    except Exception:
        return False


def _image_names_used_by_removed_stream(page: Any, candidate: dict[str, Any], reader: PdfReader) -> set[str]:
    """Return registrations for images painted by a removed stream.

    A PDF may register the same image object under several names.  Once a
    removed stream identifies that object, each alias is also a registration of
    the removed image and may be pruned if page-content analysis proves that
    alias unused.  No distinct image object is included.
    """
    try:
        ops = ContentStream(_deref(candidate["ref"]), reader).operations
        painted = {
            str(args[0]) for args, op in ops
            if op == b"Do" and isinstance(args, (list, tuple)) and len(args) == 1
        }
        resources = _deref(page.get("/Resources", {}))
        xobjects = _deref(resources.get("/XObject", {}))
        target_objects = {
            id(_deref(xobjects[NameObject(name)])) for name in painted
            if _xobject_is_image(page, NameObject(name))
        }
        return {str(name) for name, ref in xobjects.items()
                if id(_deref(ref)) in target_objects
                and str(_deref(ref).get("/Subtype")) == "/Image"}
    except Exception:
        return set()


def _resource_dependencies_are_ambiguous(page: Any) -> bool:
    """Detect constructs that can use an XObject without a direct page `Do`.

    We intentionally decline cleanup (not watermark removal) in these cases.
    Keeping an unused registration is harmless; deleting one from a resource
    graph whose indirect use cannot be proven absent is not.
    """
    try:
        resources = _deref(page.get("/Resources", {}))
        if not isinstance(resources, (DictionaryObject, dict)):
            return True
        if "/Pattern" in resources:
            return True
        xobjects = _deref(resources.get("/XObject", {}))
        if not isinstance(xobjects, (DictionaryObject, dict)):
            return True
        if any(str(_deref(ref).get("/Subtype")) == "/Form" for ref in xobjects.values()):
            return True
        fonts = _deref(resources.get("/Font", {}))
        if not isinstance(fonts, (DictionaryObject, dict)):
            return True
        if any(str(_deref(ref).get("/Subtype")) == "/Type3" for ref in fonts.values()):
            return True
        # A soft mask may contain a nested transparency group with XObject use.
        ext_states = _deref(resources.get("/ExtGState", {}))
        if not isinstance(ext_states, (DictionaryObject, dict)):
            return True
        if any("/SMask" in _deref(ref) for ref in ext_states.values()):
            return True
        return False
    except Exception:
        return True


def _unused_removed_image_names(page: Any, removed: list[dict[str, Any]], reader: PdfReader) -> set[str]:
    """Prove which removed-overlay image registrations are safe to prune.

    Page content is parsed without modifying it.  Any parser failure, resource
    indirection that might use names indirectly, or retained direct `Do` keeps
    the registration.  This is deliberately more restrictive than removal.
    """
    target_names = set().union(*(
        _image_names_used_by_removed_stream(page, candidate, reader)
        for candidate in removed
    )) if removed else set()
    if not target_names or _resource_dependencies_are_ambiguous(page):
        return set()
    removed_indices = {candidate["index"] for candidate in removed}
    used_names: set[str] = set()
    try:
        for index, ref in enumerate(_contents(page)):
            if index in removed_indices:
                continue
            for args, op in ContentStream(_deref(ref), reader).operations:
                if op == b"Do":
                    if not isinstance(args, (list, tuple)) or len(args) != 1:
                        return set()
                    used_names.add(str(args[0]))
    except Exception:
        return set()
    return target_names - used_names


def _remove_unused_image_registrations(page: Any, names: set[str]) -> None:
    """Detach per-page resource names without touching shared image objects."""
    if not names:
        return
    resources = _deref(page.get("/Resources", {}))
    xobjects = _deref(resources.get("/XObject", {}))
    if not isinstance(resources, DictionaryObject) or not isinstance(xobjects, DictionaryObject):
        return
    # Both dictionaries may be shared by other pages.  Shallow clones retain all
    # other indirect objects byte-for-byte but make just this page's registration
    # independent before names are deleted.
    cloned_resources = DictionaryObject()
    cloned_resources.update(resources)
    cloned_xobjects = DictionaryObject()
    cloned_xobjects.update(xobjects)
    for name in names:
        del cloned_xobjects[NameObject(name)]
    cloned_resources[NameObject("/XObject")] = cloned_xobjects
    page[NameObject("/Resources")] = cloned_resources


def _validate_pages(pages: Any, count: int) -> set[int]:
    if pages is None:
        return set(range(1, count + 1))
    try:
        out = set(pages)
    except TypeError as exc:
        raise ValueError("pages must be an iterable of 1-based page numbers") from exc
    if not out or any(isinstance(p, bool) or not isinstance(p, int) or p < 1 or p > count for p in out):
        raise ValueError(f"pages must contain 1-based numbers between 1 and {count}")
    return out


def _has_signature(reader: PdfReader) -> bool:
    """Return True for a signature *or an unsafe-to-inspect form structure*.

    Removal changes bytes, so when the AcroForm cannot be completely inspected
    we deliberately treat it as signed.  Field graphs can be cyclic in malformed
    PDFs; indirect-reference keys make the traversal finite.
    """
    try:
        root = _deref(reader.trailer["/Root"])
        if not isinstance(root, DictionaryObject):
            return True
        if "/Perms" in root:
            return True
        acro_raw = root.get("/AcroForm")
        if acro_raw is None:
            return False
        acro = _deref(acro_raw)
        if not isinstance(acro, DictionaryObject):
            return True
        fields_raw = acro.get("/Fields", ArrayObject())
        fields = _deref(fields_raw)
        if not isinstance(fields, (ArrayObject, list, tuple)):
            return True
        stack: list[tuple[Any, Any]] = [(field, None) for field in fields]
        seen: set[tuple[int, int] | int] = set()
        while stack:
            raw_field, inherited_ft = stack.pop()
            key = ((int(raw_field.idnum), int(raw_field.generation))
                   if hasattr(raw_field, "idnum") else id(raw_field))
            if key in seen:
                continue
            seen.add(key)
            field = _deref(raw_field)
            if not isinstance(field, DictionaryObject):
                return True
            ft = field.get("/FT", inherited_ft)
            if str(ft) == "/Sig":
                return True
            if "/V" in field:
                value = _deref(field["/V"])
                # Text and choice fields commonly have a string /V.  Only a
                # dictionary can be a signature value; malformed dictionaries
                # fail closed rather than allowing a signature to be discarded.
                if isinstance(value, DictionaryObject):
                    if str(value.get("/Type")) == "/Sig":
                        return True
                    return True
            if "/Kids" in field:
                kids = _deref(field["/Kids"])
                if not isinstance(kids, (ArrayObject, list, tuple)):
                    return True
                stack.extend((kid, ft) for kid in kids)
        return False
    except Exception:
        return True


def _read_removal_input(input_path: Any) -> PdfReader:
    """Open and reject inputs that removal must never modify."""
    try:
        source = Path(input_path)
    except (TypeError, ValueError) as exc:
        raise ValueError("input PDF does not exist") from exc
    if not source.is_file():
        raise ValueError("input PDF does not exist")
    try:
        reader = PdfReader(str(source))
        if reader.is_encrypted and reader.decrypt("") == 0:
            raise ValueError("encrypted PDFs require a password and cannot be modified")
        # Force the page tree to be read before downstream merge/removal work.
        len(reader.pages)
        if _has_signature(reader):
            raise ValueError("signed PDFs are not modified because removal invalidates signatures")
        return reader
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("input is not a readable PDF") from exc


def preflight_removal_inputs(paths: Any) -> None:
    """Validate every source PDF before a removal flow merges any of them.

    A string/path is accepted as one input for convenience; an empty or invalid
    iterable is rejected.  This intentionally performs no writes.
    """
    if isinstance(paths, (str, Path)):
        paths = [paths]
    try:
        input_paths = list(paths)
    except TypeError as exc:
        raise ValueError("paths must be an iterable of input PDFs") from exc
    if not input_paths:
        raise ValueError("at least one input PDF is required")
    for input_path in input_paths:
        _read_removal_input(input_path)


def remove_watermarks(input_path, output_path, *, mode="auto", targets=None,
                      reference_image=None, pages=None) -> dict:
    """Safely remove recognized standalone watermarks and atomically verify output.

    `auto` handles repeated low-opacity diagonal text streams and explicitly tagged
    watermark artifacts. `text` requires exact target strings. `image` requires an
    exact (or alpha-normalised) reference image and a standalone image-only stream.
    """
    if mode not in {"auto", "text", "image"}:
        raise ValueError("mode must be 'auto', 'text', or 'image'")
    source, destination = Path(input_path), Path(output_path)
    try:
        if source.resolve() == destination.resolve():
            raise ValueError("output_path must be different from input_path")
    except OSError:
        raise ValueError("input_path and output_path must be valid filesystem paths")
    if not source.is_file():
        raise ValueError("input PDF does not exist")
    if mode == "text":
        if not isinstance(targets, (list, tuple, set)) or not targets or any(not isinstance(x, str) or not x for x in targets):
            raise ValueError("text mode requires a non-empty list of exact target strings")
        target_list = list(dict.fromkeys(targets))
    else:
        target_list = []
    if mode == "image":
        if not reference_image or not Path(reference_image).is_file():
            raise ValueError("image mode requires an existing reference_image")
        if Image is None:
            raise ValueError("image mode requires Pillow")
        try:
            with Image.open(reference_image) as src:
                # Image.open reads headers only.  Check them before load/copy/
                # convert, each of which could allocate the full bitmap.
                width, height = src.size
                if (width < 1 or height < 1 or width * height > _MAX_IMAGE_PIXELS):
                    raise ValueError("reference_image must be a reasonably sized readable image")
                src.load()
                reference = src.convert("RGBA")
            if _normalized_rgba(reference) is None:
                raise ValueError("reference_image must be a reasonably sized readable image")
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("reference_image could not be read") from exc
    else:
        reference = None
    reader = _read_removal_input(source)
    selected = _validate_pages(pages, len(reader.pages))

    candidates: list[dict[str, Any]] = []
    for pnum, page in enumerate(reader.pages, 1):
        if pnum not in selected:
            continue
        for index, ref in enumerate(_contents(page)):
            marked = _marked_candidate(ref, page, reader)
            text = _text_candidate(ref, page, reader)
            candidates.append({"page": pnum, "index": index, "ref": ref,
                               "marked": marked, "text": text})

    remove: list[dict[str, Any]] = []
    warnings: list[str] = []
    if mode == "auto":
        repeated = Counter(c["text"]["text"] for c in candidates
                           if c["text"] and c["text"]["auto_cue"] for _ in range(c["text"]["occurrences"]))
        for c in candidates:
            if c["marked"] is not None:
                remove.append(c)
            elif c["text"] and c["text"]["auto_cue"] and repeated[c["text"]["text"]] >= 2:
                remove.append(c)
    elif mode == "text":
        requested = set(target_list)
        for c in candidates:
            text = c["text"]
            if text and text["text"] in requested and text["manual_cue"]:
                remove.append(c)
    else:  # image
        for c in candidates:
            marked = c["marked"]
            # An image-only independently balanced stream is required.  A tagged
            # artifact is accepted; untagged streams must be exactly q/cm/Do/Q.
            try:
                ops = ContentStream(_deref(c["ref"]), reader).operations
            except Exception:
                continue
            inner = ops[1:-1] if marked else ops
            do_names = [a[0] for a, op in inner if op == b"Do" and len(a) == 1]
            allowed = _ALLOWED_MARKED_OPS if marked else {b"q", b"Q", b"cm", b"Do"}
            if (len(do_names) != 1 or any(op not in allowed for _, op in inner)
                    or not _balanced(inner)):
                continue
            image = _image_from_page(reader.pages[c["page"] - 1], do_names[0])
            if image is not None and _same_reference(image, reference):
                remove.append(c)

    # Do not remove an artifact that also happened to parse as a nonmatching text in
    # manual text mode; manual mode is intentionally target-only.
    remove_by_page: dict[int, set[int]] = defaultdict(set)
    for c in remove:
        remove_by_page[c["page"]].add(c["index"])
    if not remove_by_page:
        unmatched = target_list if mode == "text" else []
        return _result(status="no_matches", unmatched_targets=unmatched,
                       warnings=["No safely removable standalone watermark matched; rasterized, mixed, ambiguous, or unsupported content was retained."])

    removed_image_names = {
        pnum: _unused_removed_image_names(reader.pages[pnum - 1],
                                          [c for c in remove if c["page"] == pnum], reader)
        for pnum in remove_by_page
    }
    expected = [_page_snapshot(page, remove_by_page.get(pnum, set()),
                               removed_image_names.get(pnum, set()))
                for pnum, page in enumerate(reader.pages, 1)]
    writer = PdfWriter()
    temporary: Path | None = None
    try:
        writer.clone_document_from_reader(reader)
        for pnum, indices in remove_by_page.items():
            page = writer.pages[pnum - 1]
            kept = ArrayObject([ref for i, ref in enumerate(_contents(page)) if i not in indices])
            page[NameObject("/Contents")] = kept
            _remove_unused_image_registrations(page, removed_image_names.get(pnum, set()))
        destination.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(prefix=".watermark-", suffix=".pdf", dir=destination.parent, delete=False) as tmp:
            temporary = Path(tmp.name)
            writer.write(tmp)
        _validate(temporary, expected)
        os.replace(temporary, destination)
        temporary = None  # os.replace succeeded; do not unlink the delivered file.
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"could not safely write cleaned PDF: {exc}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

    occurrences = sum((c["text"] or c["marked"] or {}).get("occurrences", 1) for c in remove)
    changed = sorted(remove_by_page)
    if mode == "text":
        matched = [x for x in target_list if any(c["text"] and c["text"]["text"] == x for c in remove)]
        unmatched = [x for x in target_list if x not in matched]
    elif mode == "auto":
        matched = sorted({c["text"]["text"] for c in remove if c["text"]})
        unmatched = []
    else:
        matched = []
        unmatched = []
    return _result(status="cleaned", removed_streams=len(remove), removed_occurrences=occurrences,
                   pages_changed=changed, matched_targets=matched, unmatched_targets=unmatched,
                   warnings=warnings, validated=True)
