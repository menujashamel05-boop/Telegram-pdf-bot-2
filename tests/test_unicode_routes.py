"""Test the actual bot handler functions with external services mocked.

AST extraction avoids importing the unrelated PDF engine/Telegram libraries.
This is a handler-level test, NOT a full dependency or live Telegram test.
"""
import ast
import asyncio
import logging
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from unicode_upload import FilenameChangedError, clean_pdf_name


ROOT = Path(__file__).resolve().parents[1]
NAME = "ශ්‍රී ලංකා රසායනික ගණනය (1+2) 2027.pdf"


def load_handlers(namespace):
    source = ast.parse((ROOT / "bot.py").read_text(encoding="utf-8"))
    names = {
        "_clean_name", "_send_result", "_execute_split", "_group_label",
        "on_file", "on_text", "on_button", "_start_unicode_uploads",
        "_stop_unicode_uploads",
    }
    nodes = [node for node in source.body
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    assert len(nodes) == len(names), "A tested handler was removed or renamed"
    tree = ast.Module(body=nodes, type_ignores=[])
    exec(compile(tree, str(ROOT / "bot.py"), "exec"), namespace)
    return namespace


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / "in_0.pdf"
        self.path.write_bytes(b"%PDF-test")
        self.uploader = SimpleNamespace(send_pdf=AsyncMock(), original_name=AsyncMock(return_value=NAME),
                                        close=AsyncMock())
        self.context = SimpleNamespace(
            user_data={"pdfs": [{"path": str(self.path), "name": NAME}],
                       "op": {"kind": "rename"}, "awaiting": "name"},
            application=SimpleNamespace(bot_data={"unicode_uploader": self.uploader}),
            job_queue=None,
            bot=SimpleNamespace(send_message=AsyncMock(), send_chat_action=AsyncMock()),
        )
        self.update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=123),
            effective_user=SimpleNamespace(id=456),
            callback_query=SimpleNamespace(data="name:keep", answer=AsyncMock(),
                                           edit_message_text=AsyncMock()),
            message=SimpleNamespace(text=NAME[:-4], message_id=50,
                                    reply_text=AsyncMock(), photo=None),
        )
        self.ns = load_handlers({
            "os": os, "shutil": shutil, "asyncio": asyncio,
            "log": logging.getLogger("unicode-route-tests"),
            "clean_pdf_name": clean_pdf_name,
            "FilenameChangedError": FilenameChangedError,
            "Update": object,
            "ContextTypes": SimpleNamespace(DEFAULT_TYPE=object),
            "ChatAction": SimpleNamespace(UPLOAD_DOCUMENT="upload_document"),
            "LOGO_PATH": str(ROOT / "logo.jpg"),
            "CAPTION": "LEARN-X",
            "MAX_DOWNLOAD_BYTES": 20 * 1024 * 1024,
            "_user_dir": Mock(return_value=str(self.root)),
            "_blocked": AsyncMock(return_value=False),
            "_execute": AsyncMock(),
            "_ask_name": AsyncMock(),
            "_log_usage": AsyncMock(),
            "_show_menu": AsyncMock(),
            "kb_name": Mock(return_value="name-buttons"),
            "BOT_TOKEN": "123:test-token",
            "start_uploader": AsyncMock(return_value=self.uploader),
            "pdf_tools": SimpleNamespace(
                split_pdf=Mock(),
                shrink_to_limit=Mock(),
            ),
        })

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_keep_original_passes_exact_unicode(self):
        await self.ns["on_button"](self.update, self.context)
        self.ns["_execute"].assert_awaited_once_with(self.update, self.context, NAME)

    async def test_rename_text_passes_exact_unicode(self):
        self.context.user_data["awaiting"] = "rename"
        await self.ns["on_text"](self.update, self.context)
        self.ns["_execute"].assert_awaited_once_with(self.update, self.context, NAME)

    async def test_incoming_filename_uses_original_mtproto_name(self):
        self.update.message.document = SimpleNamespace(
            file_name="damaged.pdf", mime_type="application/pdf", file_size=500,
            get_file=AsyncMock(return_value=SimpleNamespace(download_to_drive=AsyncMock())))
        self.context.user_data.clear()
        await self.ns["on_file"](self.update, self.context)
        self.uploader.original_name.assert_awaited_once_with(123, 50, "damaged.pdf")
        self.assertEqual(self.context.user_data["pdfs"][0]["name"], NAME)
        self.ns["_log_usage"].assert_awaited_once()
        self.assertEqual(self.ns["_log_usage"].call_args.kwargs["file_name"], NAME)

    async def test_success_uses_mtproto_then_cleans_up(self):
        await self.ns["_send_result"](self.update, self.context, str(self.path), NAME)
        self.uploader.send_pdf.assert_awaited_once_with(
            123, str(self.path), NAME, caption="LEARN-X", thumbnail=str(ROOT / "logo.jpg"))
        self.assertFalse(self.path.exists())
        self.assertEqual(self.context.user_data, {})

    async def test_failed_send_retains_input_and_enables_retry(self):
        self.context.user_data.update(awaiting=None, busy=True)
        self.uploader.send_pdf.side_effect = OSError("test send failed")
        with self.assertLogs("unicode-route-tests", level="ERROR"):
            await self.ns["_send_result"](self.update, self.context, str(self.path), NAME)
        self.assertTrue(self.path.exists())
        self.assertEqual(self.context.user_data["awaiting"], "name")
        self.assertFalse(self.context.user_data["busy"])
        self.assertEqual(self.context.bot.send_message.call_args.kwargs["reply_markup"], "name-buttons")

    async def test_name_mismatch_keeps_input_and_reports_already_sent(self):
        self.uploader.send_pdf.side_effect = FilenameChangedError()
        await self.ns["_send_result"](self.update, self.context, str(self.path), NAME)
        self.assertTrue(self.path.exists())
        self.assertIn("was sent", self.context.bot.send_message.call_args.args[1])
        self.assertEqual(self.context.user_data["awaiting"], "name")
        self.uploader.send_pdf.assert_awaited_once()

    async def test_split_names_and_routing(self):
        paths = [str(self.root / "part1.pdf"), str(self.root / "part2.pdf")]
        for path in paths:
            Path(path).write_bytes(b"%PDF-test")
        self.context.user_data.update(in_path=str(self.path), split_groups=[[1, 2], [3]])
        self.ns["pdf_tools"].split_pdf.return_value = [(paths[0], [1, 2]), (paths[1], [3])]
        await self.ns["_execute_split"](self.update, self.context, NAME)
        calls = self.uploader.send_pdf.await_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].args[2], NAME[:-4] + " (1-2).pdf")
        self.assertEqual(calls[1].args[2], NAME[:-4] + " (3).pdf")
        self.assertEqual(self.context.user_data, {})

    async def test_split_failure_retains_input(self):
        self.context.user_data.update(in_path=str(self.path), split_groups=[[1], [2]])
        self.ns["pdf_tools"].split_pdf.return_value = [(str(self.path), [1]), (str(self.path), [2])]
        self.uploader.send_pdf.side_effect = OSError("test send failed")
        with self.assertLogs("unicode-route-tests", level="ERROR"):
            await self.ns["_execute_split"](self.update, self.context, NAME)
        self.assertTrue(self.path.exists())
        self.uploader.send_pdf.assert_awaited_once()
        self.assertEqual(self.context.user_data["awaiting"], "name")

    async def test_split_name_mismatch_stops_remaining_parts(self):
        self.context.user_data.update(in_path=str(self.path), split_groups=[[1], [2]])
        self.ns["pdf_tools"].split_pdf.return_value = [(str(self.path), [1]), (str(self.path), [2])]
        self.uploader.send_pdf.side_effect = FilenameChangedError()
        await self.ns["_execute_split"](self.update, self.context, NAME)
        self.assertTrue(self.path.exists())
        self.uploader.send_pdf.assert_awaited_once()

    async def test_application_lifecycle(self):
        self.context.application.bot_data.clear()
        await self.ns["_start_unicode_uploads"](self.context.application)
        self.ns["start_uploader"].assert_awaited_once_with("123:test-token")
        self.assertIs(self.context.application.bot_data["unicode_uploader"], self.uploader)
        await self.ns["_stop_unicode_uploads"](self.context.application)
        self.uploader.close.assert_awaited_once()
        await self.ns["_stop_unicode_uploads"](self.context.application)
        self.uploader.close.assert_awaited_once()

    def test_bot_has_no_bot_api_document_upload_fallback(self):
        source = (ROOT / "bot.py").read_text(encoding="utf-8")
        self.assertNotIn(".send_document(", source)
        self.assertIn(".post_init(_start_unicode_uploads)", source)
        self.assertIn(".post_shutdown(_stop_unicode_uploads)", source)


if __name__ == "__main__":
    unittest.main()
