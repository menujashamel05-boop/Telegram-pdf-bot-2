"""Regression tests for the lossless conservative watermark-removal engine."""
from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import unittest

from PIL import Image, ImageDraw
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (ArrayObject, DecodedStreamObject, DictionaryObject,
                           FloatObject, NameObject, NumberObject, TextStringObject)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from watermark_removal import preflight_removal_inputs, remove_watermarks  # noqa: E402


def stream(writer, data: bytes):
    obj = DecodedStreamObject()
    obj.set_data(data)
    return writer._add_object(obj)


def text_stream(text: str, *, font=36, diagonal=True, translucent=True, repeats=1) -> bytes:
    gs = b"/GS1 gs\n" if translucent else b""
    tm = b"0.707107 0.707107 -0.707107 0.707107 170 420" if diagonal else b"1 0 0 1 170 420"
    piece = b"q\n" + gs + b"BT\n0.15 g\n/F1 " + str(font).encode() + b" Tf\n" + tm + b" Tm\n(" + text.encode("latin1") + b") Tj\nET\nQ\n"
    return piece * repeats


def make_text_pdf(path: Path, pages=2, *, layers=("111111111111", "222222222222"),
                  body="body number 123", include_wm=True, mixed=False,
                  translucent=True, diagonal=True, repeats=2):
    w = PdfWriter()
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    gs = DictionaryObject({NameObject("/Type"): NameObject("/ExtGState"),
                           NameObject("/ca"): FloatObject(.1), NameObject("/CA"): FloatObject(.1)})
    fref, gref = w._add_object(font), w._add_object(gs)
    for _ in range(pages):
        page = w.add_blank_page(612, 792)
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): fref}),
            NameObject("/ExtGState"): DictionaryObject({NameObject("/GS1"): gref}),
        })
        contents = [stream(w, b"BT\n/F1 12 Tf\n72 700 Td\n(" + body.encode("latin1") + b") Tj\nET\n")]
        if include_wm:
            for layer in layers:
                data = text_stream(layer, diagonal=diagonal, translucent=translucent, repeats=repeats)
                if mixed:
                    data = data[:-2] + b"0 0 m 10 10 l S\nQ\n"
                contents.append(stream(w, data))
        page[NameObject("/Contents")] = ArrayObject(contents)
    with path.open("wb") as f:
        w.write(f)


def hashes(page):
    c = page["/Contents"]
    if not isinstance(c, ArrayObject):
        c = [c]
    return [sha256(x.get_object().get_data()).hexdigest() for x in c]


def make_image(path: Path, alpha=100, size=(80, 50), color=(200, 20, 40)):
    im = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(im).rectangle((8, 8, size[0] - 9, size[1] - 9), fill=(*color, alpha))
    im.save(path)


def make_image_pdf(path: Path, reference: Path, *, tagged=False):
    import pymupdf
    d = pymupdf.open()
    p = d.new_page(width=612, height=792)
    p.insert_text((72, 72), "Body diagram must remain", fontsize=12)
    p.insert_image(p.rect, filename=str(reference), overlay=True, keep_proportion=False)
    d.save(path)
    d.close()
    if tagged:
        # Only wrap the independently inserted page-level image stream.
        r = PdfReader(path)
        w = PdfWriter()
        w.clone_document_from_reader(r)
        page = w.pages[0]
        arr = page["/Contents"]
        final = arr[-1].get_object()
        final.set_data(b"/Artifact << /Subtype /Watermark >> BDC\n" + final.get_data() + b"EMC\n")
        with path.open("wb") as f:
            w.write(f)


class WatermarkRemovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.d = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_auto_removes_two_numeric_layers_and_preserves_body_bytes(self):
        source, output = self.d / "in.pdf", self.d / "out.pdf"
        make_text_pdf(source)
        before = PdfReader(source)
        body_hashes = [hashes(p)[0] for p in before.pages]
        report = remove_watermarks(source, output)
        self.assertEqual(report["status"], "cleaned")
        self.assertEqual((report["removed_streams"], report["removed_occurrences"]), (4, 8))
        self.assertEqual(report["pages_changed"], [1, 2])
        after = PdfReader(output)
        self.assertEqual([hashes(p)[0] for p in after.pages], body_hashes)
        self.assertTrue(all(len(hashes(p)) == 1 for p in after.pages))

    def test_auto_removes_repeated_nonnumeric_but_not_ordinary_body(self):
        source, output = self.d / "in.pdf", self.d / "out.pdf"
        make_text_pdf(source, layers=("CONFIDENTIAL",), body="CONFIDENTIAL 2026", repeats=2)
        report = remove_watermarks(source, output, mode="auto")
        self.assertEqual(report["matched_targets"], ["CONFIDENTIAL"])
        text = "\n".join(p.extract_text() for p in PdfReader(output).pages)
        self.assertIn("CONFIDENTIAL 2026", text)
        self.assertNotIn("\nCONFIDENTIAL\nCONFIDENTIAL", text)

    def test_manual_selective_one_or_both_and_page_range(self):
        source, one, both = self.d / "in.pdf", self.d / "one.pdf", self.d / "both.pdf"
        make_text_pdf(source, pages=2)
        report = remove_watermarks(source, one, mode="text", targets=["111111111111"], pages={1})
        self.assertEqual((report["removed_streams"], report["removed_occurrences"], report["pages_changed"]), (1, 2, [1]))
        r = PdfReader(one)
        self.assertEqual([len(hashes(p)) for p in r.pages], [2, 3])
        report = remove_watermarks(source, both, mode="text", targets=["111111111111", "222222222222"])
        self.assertEqual(report["removed_streams"], 4)
        self.assertEqual(report["unmatched_targets"], [])

    def test_auto_does_not_count_occurrences_on_unselected_pages(self):
        source, output = self.d / "in.pdf", self.d / "out.pdf"
        # One occurrence per page is repeated only globally. Limiting to page 1
        # must not use page 2 as evidence or alter page 1.
        make_text_pdf(source, pages=2, layers=("SELECTED",), repeats=1)
        report = remove_watermarks(source, output, mode="auto", pages={1})
        self.assertEqual(report["status"], "no_matches")
        self.assertFalse(output.exists())

    def test_manual_translucent_horizontal_overlay_is_allowed(self):
        source, output = self.d / "in.pdf", self.d / "out.pdf"
        make_text_pdf(source, pages=1, layers=("HORIZONTAL",), diagonal=False, translucent=True, repeats=1)
        report = remove_watermarks(source, output, mode="text", targets=["HORIZONTAL"])
        self.assertEqual((report["status"], report["removed_occurrences"]), ("cleaned", 1))

    def test_same_target_body_stays_and_mixed_stream_declines(self):
        source, output = self.d / "in.pdf", self.d / "out.pdf"
        make_text_pdf(source, pages=1, layers=("777",), body="normal 777", mixed=True)
        report = remove_watermarks(source, output, mode="text", targets=["777"])
        self.assertEqual(report["status"], "no_matches")
        self.assertFalse(output.exists())

    def test_no_auto_removal_if_not_low_opacity_and_diagonal(self):
        for key, translucent, diagonal in (("opaque", False, True), ("horizontal", True, False)):
            with self.subTest(key=key):
                source, output = self.d / f"{key}.pdf", self.d / f"{key}-out.pdf"
                make_text_pdf(source, pages=2, layers=("KEEP",), translucent=translucent, diagonal=diagonal)
                self.assertEqual(remove_watermarks(source, output)["status"], "no_matches")
                self.assertFalse(output.exists())

    def test_unmarked_transparent_diagram_and_opaque_scanned_like_page_are_not_auto_removed(self):
        ref, source, output = self.d / "ref.png", self.d / "image.pdf", self.d / "out.pdf"
        make_image(ref)
        make_image_pdf(source, ref, tagged=False)
        self.assertEqual(remove_watermarks(source, output, mode="auto")["status"], "no_matches")
        self.assertFalse(output.exists())
        scan, scan_pdf = self.d / "scan.png", self.d / "scan.pdf"
        make_image(scan, alpha=255, size=(612, 792))
        make_image_pdf(scan_pdf, scan, tagged=False)
        self.assertEqual(remove_watermarks(scan_pdf, output, mode="auto")["status"], "no_matches")
        self.assertFalse(output.exists())

    def test_explicit_marker_image_and_reference_image_match(self):
        ref, source, output = self.d / "ref.png", self.d / "tagged.pdf", self.d / "out.pdf"
        # A realistic full-page RGBA overlay: transparent padding is retained in
        # the reference and must not prevent comparison to the embedded image.
        make_image(ref, size=(612, 792))
        make_image_pdf(source, ref, tagged=True)
        auto = remove_watermarks(source, output, mode="auto")
        self.assertEqual((auto["status"], auto["removed_streams"]), ("cleaned", 1))
        # Same marked overlay is also removable in image mode by exact normalised pixels.
        output.unlink()
        image = remove_watermarks(source, output, mode="image", reference_image=ref)
        self.assertEqual((image["status"], image["removed_streams"]), ("cleaned", 1))
        self.assertIn("Body diagram", PdfReader(output).pages[0].extract_text())

    def test_image_reference_mismatch_is_noop(self):
        ref, other, source, output = self.d / "ref.png", self.d / "other.png", self.d / "image.pdf", self.d / "out.pdf"
        make_image(ref)
        make_image(other, alpha=70, color=(20, 200, 40))
        make_image_pdf(source, ref, tagged=True)
        self.assertEqual(remove_watermarks(source, output, mode="image", reference_image=other)["status"], "no_matches")
        self.assertFalse(output.exists())

    def test_explicit_marker_text_and_generic_artifact_rules(self):
        source, output = self.d / "in.pdf", self.d / "out.pdf"
        make_text_pdf(source, pages=1, include_wm=False)
        r = PdfReader(source); w = PdfWriter(); w.clone_document_from_reader(r)
        page = w.pages[0]
        data = b"/Artifact << /Subtype /Watermark >> BDC\n" + text_stream("ONCE", repeats=1) + b"EMC\n"
        page[NameObject("/Contents")] = ArrayObject([page["/Contents"][0], stream(w, data)])
        with source.open("wb") as f: w.write(f)
        self.assertEqual(remove_watermarks(source, output)["removed_streams"], 1)
        # Generic Artifact must never be treated as a watermark.
        make_text_pdf(source, pages=1, include_wm=False)
        r = PdfReader(source); w = PdfWriter(); w.clone_document_from_reader(r)
        p = w.pages[0]
        s = stream(w, b"/Artifact BMC\nq 1 0 0 1 0 0 cm /X Do Q\nEMC\n")
        p[NameObject("/Contents")] = ArrayObject([p["/Contents"][0], s])
        with source.open("wb") as f: w.write(f)
        output.unlink()
        self.assertEqual(remove_watermarks(source, output)["status"], "no_matches")

    def _image_object(self, writer):
        """A structural Image XObject; its bytes need not be rendered in these tests."""
        image = DecodedStreamObject()
        image.set_data(b"not-decoded")
        image.update({NameObject("/Type"): NameObject("/XObject"),
                      NameObject("/Subtype"): NameObject("/Image"),
                      NameObject("/Width"): NumberObject(1),
                      NameObject("/Height"): NumberObject(1),
                      NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
                      NameObject("/BitsPerComponent"): NumberObject(8)})
        return writer._add_object(image)

    def _marked_image_stream(self, writer):
        return stream(writer, b"/Artifact << /Subtype /Watermark >> BDC\nq /Overlay Do Q\nEMC\n")

    def test_unused_removed_image_registration_is_pruned_without_touching_shared_page_resources(self):
        source, output = self.d / "shared.pdf", self.d / "clean.pdf"
        writer = PdfWriter()
        image_ref = self._image_object(writer)
        resources_ref = writer._add_object(DictionaryObject({
            NameObject("/XObject"): DictionaryObject({NameObject("/Overlay"): image_ref})
        }))
        first = writer.add_blank_page(100, 100)
        second = writer.add_blank_page(100, 100)
        # Both pages deliberately point at the same Resources object.  Page 2
        # retains the shared image, while page 1's removed artifact is its only use.
        first[NameObject("/Resources")] = resources_ref
        second[NameObject("/Resources")] = resources_ref
        first[NameObject("/Contents")] = ArrayObject([stream(writer, b"q Q\n"), self._marked_image_stream(writer)])
        second[NameObject("/Contents")] = ArrayObject([stream(writer, b"q /Overlay Do Q\n")])
        with source.open("wb") as file:
            writer.write(file)
        report = remove_watermarks(source, output, pages={1})
        self.assertEqual(report["status"], "cleaned")
        cleaned = PdfReader(output)
        self.assertNotIn("/Overlay", cleaned.pages[0]["/Resources"]["/XObject"])
        self.assertIn("/Overlay", cleaned.pages[1]["/Resources"]["/XObject"])

    def test_removed_image_registration_is_retained_when_remaining_page_content_uses_it(self):
        source, output = self.d / "in-use.pdf", self.d / "clean.pdf"
        writer = PdfWriter()
        image_ref = self._image_object(writer)
        page = writer.add_blank_page(100, 100)
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/XObject"): DictionaryObject({NameObject("/Overlay"): image_ref})
        })
        page[NameObject("/Contents")] = ArrayObject([
            stream(writer, b"q /Overlay Do Q\n"), self._marked_image_stream(writer)
        ])
        with source.open("wb") as file:
            writer.write(file)
        self.assertEqual(remove_watermarks(source, output)["status"], "cleaned")
        after = PdfReader(output).pages[0]
        self.assertIn("/Overlay", after["/Resources"]["/XObject"])
        self.assertEqual(hashes(after), [sha256(b"q /Overlay Do Q\n").hexdigest()])

    def test_preflight_rejects_signature_after_ordinary_text_field(self):
        source, output = self.d / "signed.pdf", self.d / "out.pdf"
        writer = PdfWriter()
        writer.add_blank_page(100, 100)
        signature = writer._add_object(DictionaryObject({
            NameObject("/FT"): NameObject("/Sig"),
            NameObject("/V"): DictionaryObject({NameObject("/Type"): NameObject("/Sig")}),
        }))
        ordinary_text = writer._add_object(DictionaryObject({
            NameObject("/FT"): NameObject("/Tx"), NameObject("/V"): TextStringObject("normal value"),
        }))
        writer._root_object[NameObject("/AcroForm")] = DictionaryObject({
            NameObject("/Fields"): ArrayObject([signature, ordinary_text])
        })
        with source.open("wb") as file:
            writer.write(file)
        with self.assertRaisesRegex(ValueError, "signed PDFs"):
            preflight_removal_inputs([source])
        with self.assertRaisesRegex(ValueError, "signed PDFs"):
            remove_watermarks(source, output)

    def test_oversized_reference_is_rejected_before_image_load(self):
        # A header-only PNG advertises >40M pixels.  No pixel data is present;
        # success means dimensions were rejected before Pillow tried to load it.
        import struct
        import zlib
        reference, source, output = self.d / "huge.png", self.d / "in.pdf", self.d / "out.pdf"
        header = struct.pack(">II", 10000, 5000) + b"\x08\x06\x00\x00\x00"
        reference.write_bytes(b"\x89PNG\r\n\x1a\n" + struct.pack(">I", len(header)) + b"IHDR" + header + struct.pack(">I", zlib.crc32(b"IHDR" + header)) + b"\x00\x00\x00\x00IEND\xaeB`\x82")
        make_text_pdf(source, pages=1, include_wm=False)
        with self.assertRaisesRegex(ValueError, "reasonably sized"):
            remove_watermarks(source, output, mode="image", reference_image=reference)

    def test_input_validation_malformed_encrypted_and_noop_output(self):
        bad, out = self.d / "bad.pdf", self.d / "out.pdf"
        bad.write_bytes(b"not a pdf")
        with self.assertRaisesRegex(ValueError, "readable PDF"):
            remove_watermarks(bad, out)
        source = self.d / "enc.pdf"
        make_text_pdf(source, pages=1)
        r = PdfReader(source); w = PdfWriter(); w.clone_document_from_reader(r); w.encrypt("secret")
        with source.open("wb") as f: w.write(f)
        with self.assertRaisesRegex(ValueError, "encrypted"):
            remove_watermarks(source, out)
        clean = self.d / "clean.pdf"
        make_text_pdf(clean, pages=1, include_wm=False)
        self.assertEqual(remove_watermarks(clean, out)["status"], "no_matches")
        self.assertFalse(out.exists())
        with self.assertRaises(ValueError):
            remove_watermarks(clean, clean)
        with self.assertRaises(ValueError):
            remove_watermarks(clean, out, pages={2})


if __name__ == "__main__":
    unittest.main()
