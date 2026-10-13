"""Offline regression tests for Sinhala names, flows and HTTP multipart uploads."""
import os
os.environ.setdefault("BOT_TOKEN", "123456:offline-test-token")
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pymupdf
from telegram import Bot, InputFile
from telegram.request import HTTPXRequest

import bot

# Explicit joiner + vowel signs; these must not be deleted or normalized.
SINHALA = "රසායනික ගණනය ක්\u200dරියා (1+2) 2027 Revision.pdf"


class FilenameTests(unittest.TestCase):
    def test_original_sinhala_exact(self):
        self.assertEqual(bot._clean_name(SINHALA), SINHALA)

    def test_rename_sinhala_adds_extension_only(self):
        self.assertEqual(bot._clean_name(SINHALA[:-4]), SINHALA)

    def test_preserves_joiners_marks_and_spacing(self):
        name = "ක්\u200dර  ක්\u200cර ො Sinhala_English 📚.pdf"
        self.assertEqual(bot._clean_name(name), name)

    def test_keeps_extension_case_and_punctuation(self):
        name = 'පාඩම: "Revision" [2027] #1.PDF'
        self.assertEqual(bot._clean_name(name), name)

    def test_strips_paths_without_dropping_filename(self):
        for prefix in ("/tmp/", "C:\\notes\\"):
            self.assertEqual(bot._clean_name(prefix + SINHALA), SINHALA)

    def test_controls_and_empty_names(self):
        self.assertEqual(bot._clean_name("\0\r\n"), "document.pdf")
        self.assertEqual(bot._clean_name("."), "document.pdf")
        self.assertEqual(bot._clean_name("../.."), "document.pdf")
        self.assertEqual(bot._clean_name("exam\x7f.pdf"), "exam.pdf")

    def test_caption_keeps_exact_name(self):
        self.assertEqual(bot._output_caption(SINHALA), SINHALA + "\n\n" + bot.CAPTION)

    def test_caption_limits_utf16_length(self):
        text = bot._output_caption("📚" * 1100 + ".pdf")
        self.assertLessEqual(len(text.encode("utf-16-le")) // 2, 1024)
        self.assertTrue(text.endswith(bot.CAPTION))


class FilenameFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.src = self.root / "in_0.pdf"
        with pymupdf.open() as doc:
            doc.new_page()
            doc.save(self.src)
        self.context = SimpleNamespace(
            user_data={"pdfs": [{"path": str(self.src), "name": SINHALA}],
                       "in_path": str(self.src), "op": {"kind": "rename"},
                       "awaiting": "name"},
            bot=SimpleNamespace(send_document=AsyncMock(), send_message=AsyncMock(),
                                send_chat_action=AsyncMock()))
        self.update = SimpleNamespace(
            effective_user=SimpleNamespace(id=888888888),
            effective_chat=SimpleNamespace(id=1234),
            message=SimpleNamespace(text=None, reply_text=AsyncMock()),
            callback_query=SimpleNamespace(data="name:keep", answer=AsyncMock(),
                                           edit_message_text=AsyncMock()))

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_keep_button_passes_exact_original_name(self):
        with patch.object(bot, "_execute", new_callable=AsyncMock) as execute:
            await bot.on_button(self.update, self.context)
        self.assertEqual(execute.call_args.args[-1], SINHALA)

    async def test_custom_rename_passes_exact_name(self):
        self.context.user_data["awaiting"] = "rename"
        self.update.message.text = SINHALA[:-4]
        with patch.object(bot, "_execute", new_callable=AsyncMock) as execute:
            await bot.on_text(self.update, self.context)
        self.assertEqual(execute.call_args.args[-1], SINHALA)

    async def test_upload_name_and_caption_with_and_without_logo(self):
        for logo in (str(self.root / "missing.jpg"), bot.LOGO_PATH):
            with self.subTest(logo=logo), patch.object(bot, "LOGO_PATH", logo):
                await bot._upload_pdf(self.context, 1234, str(self.src), SINHALA)
            kwargs = self.context.bot.send_document.call_args.kwargs
            self.assertIsInstance(kwargs["document"], InputFile)
            self.assertEqual(kwargs["document"].filename, SINHALA)
            self.assertEqual(kwargs["filename"], SINHALA)
            self.assertEqual(kwargs["caption"], SINHALA + "\n\n" + bot.CAPTION)
            self.assertIsNone(kwargs["parse_mode"])
            self.assertEqual("thumbnail" in kwargs, logo == bot.LOGO_PATH)

    async def test_split_preserves_name_with_distinct_part_suffix(self):
        self.context.user_data["split_groups"] = [{1}]
        with patch.object(bot.pdf_tools, "split_pdf", return_value=[(str(self.src), {1})]), \
             patch.object(bot.pdf_tools, "shrink_to_limit"), \
             patch.object(bot, "_upload_pdf", new_callable=AsyncMock) as upload, \
             patch.object(bot, "_user_dir", return_value=str(self.root)):
            await bot._execute_split(self.update, self.context, SINHALA)
        self.assertEqual(upload.call_args.args[-1], SINHALA[:-4] + " (1).pdf")

    async def test_real_telegram_library_multipart_keeps_utf8_name(self):
        captured = []

        async def handle(request):
            if request.url.path.endswith("/getMe"):
                result = {"id": 1, "is_bot": True, "first_name": "Offline", "username": "offline_bot"}
            else:
                payload = await request.aread()
                captured.append(payload)
                result = {"message_id": 1, "date": 0,
                          "chat": {"id": 1234, "type": "private"},
                          "document": {"file_id": "offline", "file_unique_id": "offline",
                                       "file_name": SINHALA}}
            return httpx.Response(200, json={"ok": True, "result": result})

        request = HTTPXRequest(httpx_kwargs={"transport": httpx.MockTransport(handle)})
        async with Bot("123456:offline-test-token", request=request) as tg:
            context = SimpleNamespace(bot=tg)
            with patch.object(bot, "LOGO_PATH", str(self.root / "missing.jpg")):
                await bot._upload_pdf(context, 1234, str(self.src), SINHALA)
        self.assertEqual(len(captured), 1)
        self.assertIn(('filename="' + SINHALA + '"').encode("utf-8"), captured[0])
        self.assertIn((SINHALA + "\n\n" + bot.CAPTION).encode("utf-8"), captured[0])
        self.assertIn(b"%PDF-", captured[0])


if __name__ == "__main__":
    unittest.main()
