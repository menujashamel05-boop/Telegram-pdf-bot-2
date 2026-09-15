"""Offline Telegram flow and real watermark-add/remove integration tests."""
import os
os.environ.setdefault("BOT_TOKEN", "123456:offline-test-token")
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pymupdf
from PIL import Image
import bot
import pdf_tools


def make_pdf(path, count=2):
    with pymupdf.open() as doc:
        for index in range(count):
            page = doc.new_page(width=400, height=500)
            page.insert_text((30, 60), f"Question {index+1}: 1234567890 and DRAFT are body text.")
            page.draw_line((30, 100), (200, 100), color=(0, 0, 0))
        doc.save(path)


class OverlayIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.src = self.root / "original.pdf"
        make_pdf(self.src)

    def tearDown(self):
        self.tmp.cleanup()

    def assert_same_render(self, left, right):
        with pymupdf.open(left) as a, pymupdf.open(right) as b:
            self.assertEqual(len(a), len(b))
            for x, y in zip(a, b):
                self.assertEqual(x.get_pixmap().samples, y.get_pixmap().samples)
                self.assertEqual(x.get_text(), y.get_text())

    def test_added_text_watermark_auto_removes_without_content_change(self):
        marked, cleaned = self.root / "marked.pdf", self.root / "cleaned.pdf"
        pdf_tools.apply_watermark(str(self.src), str(marked),
                                  pdf_tools.text_watermarker("DRAFT", .15, tiled=True, angle=45))
        report = pdf_tools.remove_watermarks(marked, cleaned, mode="auto")
        self.assertEqual(report["status"], "cleaned")
        self.assertEqual(report["pages_changed"], [1, 2])
        self.assertTrue(report["validated"])
        self.assert_same_render(self.src, cleaned)

    def test_added_text_plus_image_layers_auto_remove(self):
        image = self.root / "reference.png"
        Image.new("RGBA", (32, 20), (80, 140, 200, 255)).save(image)
        out = self.root / "both.pdf"
        cleaned = self.root / "cleaned.pdf"
        bot._apply_both_watermarks(
            str(self.src), str(out),
            {"text": "DRAFT", "opacity": .2, "font_pt": 40, "position": "center", "pages": None},
            image.read_bytes(), {"opacity": .15, "pages": None})
        report = pdf_tools.remove_watermarks(out, cleaned)
        self.assertEqual(report["removed_streams"], 4)
        self.assert_same_render(self.src, cleaned)

    def test_existing_merge_and_page_range(self):
        marked, merged, cleaned = [self.root / n for n in ("marked.pdf", "merged.pdf", "clean.pdf")]
        pdf_tools.apply_watermark(str(self.src), str(marked),
                                  pdf_tools.text_watermarker("DRAFT", .1))
        pdf_tools.merge_pdfs([str(marked), str(marked)], str(merged))
        result = pdf_tools.remove_watermarks(merged, cleaned, pages={2, 3})
        self.assertEqual(result["pages_changed"], [2, 3])
        with pymupdf.open(merged) as before, pymupdf.open(cleaned) as after:
            for index in (0, 3):
                self.assertEqual(before[index].get_pixmap().samples,
                                 after[index].get_pixmap().samples)

    def test_rasterized_watermark_is_not_erased(self):
        marked = self.root / "burned.pdf"
        pdf_tools.apply_watermark(str(self.src), str(marked),
                                  pdf_tools.text_watermarker("DRAFT", .1),
                                  rasterize=True, dpi=72)
        out = self.root / "unsafe.pdf"
        result = pdf_tools.remove_watermarks(marked, out)
        self.assertEqual(result["status"], "no_matches")
        self.assertFalse(out.exists())


class BotFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.src = self.root / "in_0.pdf"
        make_pdf(self.src)
        self.user = SimpleNamespace(id=888888888)
        self.chat = SimpleNamespace(id=1234)
        self.context = SimpleNamespace(
            user_data={"pdfs": [{"path": str(self.src), "name": "exam.pdf"}]},
            job_queue=None,
            bot=SimpleNamespace(send_message=AsyncMock(), send_chat_action=AsyncMock(),
                                send_document=AsyncMock()))
        self.context.application = SimpleNamespace(user_data={self.user.id: self.context.user_data})
        self.dir_patch = patch.object(bot, "_user_dir", return_value=str(self.root))
        self.dir_patch.start()

    async def asyncTearDown(self):
        self.dir_patch.stop()
        self.tmp.cleanup()

    def update(self, *, data=None, text=None):
        msg = SimpleNamespace(text=text, reply_text=AsyncMock(), photo=None, document=None)
        query = None if data is None else SimpleNamespace(
            data=data, answer=AsyncMock(), edit_message_text=AsyncMock())
        return SimpleNamespace(message=msg, effective_message=msg, callback_query=query,
                               effective_user=self.user, effective_chat=self.chat)

    async def click(self, data):
        update = self.update(data=data)
        await bot.on_button(update, self.context)
        return update

    async def text(self, value):
        update = self.update(text=value)
        await bot.on_text(update, self.context)
        return update

    def fake_engine(self, source, out, **kwargs):
        shutil.copyfile(source, out)
        return {"status": "cleaned", "removed_streams": 2, "removed_occurrences": 6,
                "pages_changed": [1], "matched_targets": [], "unmatched_targets": [],
                "warnings": [], "validated": True}

    async def ready_auto(self):
        await self.click("mode:remove")
        await self.click("rm:auto")
        await self.text("all")

    def test_menus_and_strict_ranges(self):
        for keyboard in (bot.kb_single(), bot.kb_multi()):
            values = [b.callback_data for row in keyboard.inline_keyboard for b in row]
            self.assertIn("mode:remove", values)
        self.assertIsNone(bot.parse_removal_pages("all", 5))
        self.assertEqual(bot.parse_removal_pages("1, 3-5", 5), {1, 3, 4, 5})
        for value in ("0", "1,99", "", "2-", "1,,2", "1000000000-1"):
            with self.assertRaises(ValueError):
                bot.parse_removal_pages(value, 5)

    async def test_source_preflight_runs_before_removal_merge(self):
        self.context.user_data["pdfs"].append({"path": str(self.src), "name": "second.pdf"})
        with patch.object(pdf_tools, "preflight_removal_inputs", side_effect=ValueError("signed PDF")) as preflight, \
             patch.object(pdf_tools, "merge_pdfs") as merge:
            await self.click("mode:remove")
        preflight.assert_called_once_with([str(self.src), str(self.src)])
        merge.assert_not_called()
        self.assertNotIn("op", self.context.user_data)
        self.assertTrue(self.src.exists())

    async def test_auto_keep_name_uses_engine_and_never_compresses(self):
        await self.ready_auto()
        self.assertEqual(self.context.user_data["awaiting"], "name")
        with patch.object(pdf_tools, "remove_watermarks", side_effect=self.fake_engine) as engine, \
             patch.object(pdf_tools, "shrink_to_limit") as shrink, \
             patch.object(bot, "_send_result", new_callable=AsyncMock) as send:
            await self.click("name:keep")
        self.assertEqual(engine.call_args.kwargs["mode"], "auto")
        self.assertIsNone(engine.call_args.kwargs["pages"])
        self.assertEqual(send.call_args.args[-1], "exam.pdf")
        shrink.assert_not_called()

    async def test_manual_multiple_targets_pages_and_rename(self):
        await self.click("mode:remove")
        await self.click("rm:manual")
        await self.click("rmt:text")
        await self.text("123456789012\n234567890123")
        await self.text("2")
        await self.click("name:rename")
        with patch.object(pdf_tools, "remove_watermarks", side_effect=self.fake_engine) as engine, \
             patch.object(bot, "_send_result", new_callable=AsyncMock) as send:
            await self.text("clean exam")
        self.assertEqual(engine.call_args.kwargs["targets"], ["123456789012", "234567890123"])
        self.assertEqual(engine.call_args.kwargs["pages"], {2})
        self.assertEqual(send.call_args.args[-1], "clean exam.pdf")

    async def test_manual_image_upload_route(self):
        await self.click("mode:remove")
        await self.click("rm:manual")
        await self.click("rmt:image")
        async def download(path):
            Image.new("RGBA", (20, 20), (10, 20, 30, 100)).save(path, format="PNG")
        doc = SimpleNamespace(file_size=100, file_name="watermark.png", mime_type="image/png",
                              get_file=AsyncMock(return_value=SimpleNamespace(
                                  download_to_drive=AsyncMock(side_effect=download))))
        update = self.update()
        update.message.document = doc
        await bot.on_file(update, self.context)
        self.assertEqual(self.context.user_data["awaiting"], "remove_pages")
        self.assertTrue(Path(self.context.user_data["remove_reference"]).exists())
        await self.text("1")
        with patch.object(pdf_tools, "remove_watermarks", side_effect=self.fake_engine) as engine, \
             patch.object(bot, "_send_result", new_callable=AsyncMock):
            await self.click("name:keep")
        self.assertEqual(engine.call_args.kwargs["mode"], "image")
        self.assertTrue(engine.call_args.kwargs["reference_image"])

    async def test_no_match_does_not_send_success_and_can_retry(self):
        await self.ready_auto()
        with patch.object(pdf_tools, "remove_watermarks", return_value={"status": "no_matches"}), \
             patch.object(bot, "_send_result", new_callable=AsyncMock) as send:
            await self.click("name:keep")
        send.assert_not_called()
        self.assertEqual(self.context.user_data["awaiting"], "remove_method")
        self.assertTrue(self.src.exists())
        await self.click("rm:manual")
        self.assertEqual(self.context.user_data["awaiting"], "remove_target_kind")

    async def test_old_buttons_cannot_skip_selection(self):
        await self.click("mode:remove")
        await self.click("name:keep")
        await self.click("wt:text")
        self.assertEqual(self.context.user_data["awaiting"], "remove_method")
        self.assertNotIn("op", self.context.user_data)
        await self.click("rm:auto")
        await self.click("rm:manual")
        self.assertEqual(self.context.user_data["awaiting"], "remove_pages")

    async def test_oversized_result_refused_without_recompression(self):
        await self.ready_auto()
        with patch.object(pdf_tools, "remove_watermarks", side_effect=self.fake_engine), \
             patch.object(bot.os.path, "getsize", return_value=49*1024*1024), \
             patch.object(pdf_tools, "shrink_to_limit") as shrink, \
             patch.object(bot, "_send_result", new_callable=AsyncMock) as send:
            await self.click("name:keep")
        shrink.assert_not_called()
        send.assert_not_called()
        self.assertTrue(self.src.exists())

    async def test_engine_failure_retains_upload(self):
        await self.ready_auto()
        with patch.object(pdf_tools, "remove_watermarks", side_effect=ValueError("unsupported PDF")), \
             patch.object(bot, "_send_result", new_callable=AsyncMock) as send:
            await self.click("name:keep")
        send.assert_not_called()
        self.assertTrue(self.src.exists())
        self.assertEqual(self.context.user_data["awaiting"], "remove_method")

    async def test_existing_watermark_flow_still_reaches_naming(self):
        await self.click("mode:wm")
        await self.click("wt:text")
        await self.text("MY WATERMARK")
        await self.click("pos:center")
        await self.text("default")
        await self.text("20")
        await self.text("all")
        self.assertEqual(self.context.user_data["op"], {"kind": "wm"})
        self.assertEqual(self.context.user_data["awaiting"], "name")


if __name__ == "__main__":
    unittest.main()
