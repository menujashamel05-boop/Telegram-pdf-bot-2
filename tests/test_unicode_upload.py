"""Dependency-free tests of Unicode handling and mocked MTProto boundaries.

These tests never connect to Telegram and do not claim live API verification.
"""
import asyncio
import os
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from unicode_upload import (
    FilenameChangedError, UnicodeUploader, clean_pdf_name, start_uploader,
)


@dataclass
class FilenameAttribute:
    file_name: str


def document_message(name):
    return SimpleNamespace(
        document=SimpleNamespace(attributes=[FilenameAttribute(name)]))


NAMES = [
    "රසායනික ගණනය (1+2) 2027 Revision.pdf",
    "රසායනික_ගණනය_Full_Short_Note_Final_2027.pdf",
    "ශ්‍රී ලංකා ප්‍රශ්න පත්‍රය.pdf",
    "ක්\u200dර ක්\u200cර.pdf",  # ZWJ and ZWNJ must BOTH survive.
    "සිංහල අකුරු කි කී කු කූ කෙ කේ කෛ කො කෝ කෞ.pdf",
    "සිංහල English 2027 (1+2)_Revision.PDF",
    "සිංහල e\u0301.pdf",  # Do not silently NFC-normalize.
    "தமிழ் हिंदी සිංහල.pdf",
    "ශ්‍රී 🔬 Notes.pdf",
    "exam 2027.pdf",
]


class FilenameTests(unittest.TestCase):
    def test_existing_names_preserve_every_codepoint(self):
        for name in NAMES:
            with self.subTest(name=name):
                self.assertEqual(clean_pdf_name(name), name)
                self.assertEqual(clean_pdf_name(name).encode("utf-8"), name.encode("utf-8"))

    def test_rename_without_extension(self):
        for name in NAMES:
            with self.subTest(name=name):
                self.assertEqual(clean_pdf_name(name[:-4]), name[:-4] + ".pdf")

    def test_no_duplicate_extension(self):
        self.assertEqual(clean_pdf_name("ශ්‍රී.PDF"), "ශ්‍රී.PDF")

    def test_empty_fallback(self):
        for value in (None, "", " ", ".", "..", "\x00\n"):
            self.assertEqual(clean_pdf_name(value), "document.pdf")

    def test_only_unsafe_characters_removed(self):
        self.assertEqual(clean_pdf_name("ශ්‍රී:*?\"<>|\x00\x1f\x7f"),
                         "ශ්‍රී.pdf")

    def test_path_is_not_used_as_output_filename(self):
        self.assertEqual(clean_pdf_name("../../ශ්‍රී.pdf"), "ශ්‍රී.pdf")

    def test_split_suffix_preserves_sinhala(self):
        self.assertEqual(clean_pdf_name("ශ්‍රී ප්‍රශ්න (1-3)"),
                         "ශ්‍රී ප්‍රශ්න (1-3).pdf")


class UploadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "output.pdf"
        self.path.write_bytes(b"%PDF-1.4\n%%EOF\n")
        self.peer = object()
        self.uploaded = object()
        self.client = SimpleNamespace(
            get_input_entity=AsyncMock(return_value=self.peer),
            upload_file=AsyncMock(return_value=self.uploaded),
            send_file=AsyncMock(),
            get_messages=AsyncMock(),
            disconnect=AsyncMock(),
        )
        self.uploader = UnicodeUploader(self.client, FilenameAttribute)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_upload_and_attribute_use_exact_filename(self):
        for name in NAMES:
            with self.subTest(name=name):
                self.client.send_file.return_value = document_message(name)
                before = self.path.read_bytes()
                result = await self.uploader.send_pdf(
                    123, self.path, name, caption="LEARN-X")
                self.assertIs(result, self.client.send_file.return_value)
                self.client.upload_file.assert_awaited_with(str(self.path), file_name=name)
                args, kwargs = self.client.send_file.call_args
                self.assertEqual(args, (self.peer, self.uploaded))
                self.assertEqual(kwargs["attributes"], [FilenameAttribute(name)])
                self.assertIsNone(kwargs["parse_mode"])
                self.assertTrue(kwargs["force_document"])
                self.assertEqual(kwargs["mime_type"], "application/pdf")
                self.assertEqual(kwargs["caption"], "LEARN-X")
                self.assertEqual(self.path.read_bytes(), before)

    async def test_thumbnail_is_optional(self):
        name = NAMES[0]
        self.client.send_file.return_value = document_message(name)
        await self.uploader.send_pdf(123, self.path, name, thumbnail="missing.jpg")
        self.assertIsNone(self.client.send_file.call_args.kwargs["thumb"])
        logo = Path(self.tmp.name) / "logo.jpg"
        logo.write_bytes(b"test-thumbnail")
        await self.uploader.send_pdf(123, self.path, name, thumbnail=str(logo))
        self.assertEqual(self.client.send_file.call_args.kwargs["thumb"], str(logo))

    async def test_changed_filename_is_not_silently_accepted(self):
        self.client.send_file.return_value = document_message("broken.pdf")
        with self.assertRaises(FilenameChangedError):
            await self.uploader.send_pdf(123, self.path, NAMES[0])
        self.client.send_file.assert_awaited_once()
        self.assertTrue(self.path.exists())

    async def test_missing_filename_is_not_silently_accepted(self):
        self.client.send_file.return_value = SimpleNamespace(document=None)
        with self.assertRaises(FilenameChangedError):
            await self.uploader.send_pdf(123, self.path, NAMES[0])

    async def test_upload_failure_does_not_fall_back_or_delete(self):
        self.client.upload_file.side_effect = OSError("test upload failure")
        with self.assertRaises(OSError):
            await self.uploader.send_pdf(123, self.path, NAMES[0])
        self.client.send_file.assert_not_awaited()
        self.assertTrue(self.path.exists())

    async def test_event_caches_original_name_and_peer(self):
        message = document_message(NAMES[2])
        message.id = 50
        event = SimpleNamespace(chat_id=123, message=message,
                                get_input_chat=AsyncMock(return_value=self.peer))
        await self.uploader.remember_message(event)
        self.assertEqual(await self.uploader.original_name(123, 50, "damaged.pdf"), NAMES[2])
        self.assertIs(await self.uploader.peer_for(123), self.peer)
        self.client.get_messages.assert_not_awaited()
        self.client.get_input_entity.assert_not_awaited()

    async def test_original_name_can_be_fetched_after_restart(self):
        self.client.get_messages.return_value = document_message(NAMES[3])
        self.assertEqual(await self.uploader.original_name(123, 50, "fallback.pdf"), NAMES[3])
        self.client.get_messages.assert_awaited_once_with(self.peer, ids=50)

    async def test_unavailable_original_uses_received_name(self):
        self.client.get_messages.return_value = None
        self.assertEqual(await self.uploader.original_name(123, 50, NAMES[0]), NAMES[0])

    async def test_original_lookup_error_is_nonfatal(self):
        self.client.get_messages.side_effect = OSError("test failure")
        with self.assertLogs("unicode_upload", level="WARNING"):
            self.assertEqual(await self.uploader.original_name(123, 50, NAMES[0]), NAMES[0])

    async def test_peer_update_race_retries(self):
        self.client.get_input_entity.side_effect = [ValueError("not cached"), self.peer]
        with patch("unicode_upload.asyncio.sleep", new_callable=AsyncMock) as sleep:
            self.assertIs(await self.uploader.peer_for(123), self.peer)
        sleep.assert_awaited_once_with(0.25)

    async def test_unresolved_peer_fails_without_fabricated_hash(self):
        self.client.get_input_entity.side_effect = ValueError("not cached")
        with patch("unicode_upload.asyncio.sleep", new_callable=AsyncMock):
            with self.assertRaisesRegex(RuntimeError, "Send /start"):
                await self.uploader.send_pdf(123, self.path, NAMES[0])
        self.client.upload_file.assert_not_awaited()

    async def test_caches_are_bounded(self):
        self.uploader.CACHE_LIMIT = 2
        for key in range(4):
            self.uploader._remember(self.uploader._names, key, str(key))
        self.assertEqual(list(self.uploader._names), [2, 3])

    async def test_close_disconnects(self):
        await self.uploader.close()
        self.client.disconnect.assert_awaited_once()


class StartupTests(unittest.IsolatedAsyncioTestCase):
    def modules(self, *, bot=True, user_id=123):
        client = SimpleNamespace(
            start=AsyncMock(),
            get_me=AsyncMock(return_value=SimpleNamespace(bot=bot, id=user_id)),
            disconnect=AsyncMock(),
            add_event_handler=Mock(),
        )
        constructor = Mock(return_value=client)
        modules = {
            "telethon": SimpleNamespace(
                TelegramClient=constructor,
                events=SimpleNamespace(NewMessage=Mock(return_value="incoming"))),
            "telethon.sessions": SimpleNamespace(MemorySession=Mock(return_value="memory")),
            "telethon.tl.types": SimpleNamespace(DocumentAttributeFilename=FilenameAttribute),
        }
        return client, constructor, modules

    async def test_authenticates_same_bot(self):
        client, constructor, modules = self.modules()
        with patch.dict("sys.modules", modules), patch.dict(
                os.environ, {"API_ID": "12345", "API_HASH": "test-only-hash"}):
            uploader = await start_uploader("123:test-token")
        self.assertIs(uploader.client, client)
        constructor.assert_called_once_with("memory", 12345, "test-only-hash")
        client.start.assert_awaited_once_with(bot_token="123:test-token")
        client.add_event_handler.assert_called_once()
        client.disconnect.assert_not_awaited()

    async def test_invalid_config_rejected_before_connect(self):
        client, constructor, modules = self.modules()
        for api_id, api_hash in (("", ""), ("wrong", "hash"), ("0", "hash"), ("123", "")):
            with patch.dict("sys.modules", modules), patch.dict(
                    os.environ, {"API_ID": api_id, "API_HASH": api_hash}):
                with self.assertRaisesRegex(RuntimeError, "API_ID"):
                    await start_uploader("123:test-token")
        constructor.assert_not_called()

    async def test_wrong_account_disconnects(self):
        for is_bot, user_id in ((False, 123), (True, 999)):
            client, _, modules = self.modules(bot=is_bot, user_id=user_id)
            with patch.dict("sys.modules", modules), patch.dict(
                    os.environ, {"API_ID": "12345", "API_HASH": "test-only-hash"}):
                with self.assertRaisesRegex(RuntimeError, "does not match"):
                    await start_uploader("123:test-token")
            client.disconnect.assert_awaited_once()

    async def test_authentication_failure_disconnects(self):
        client, _, modules = self.modules()
        client.start.side_effect = OSError("test auth error")
        with patch.dict("sys.modules", modules), patch.dict(
                os.environ, {"API_ID": "12345", "API_HASH": "test-only-hash"}):
            with self.assertRaises(OSError):
                await start_uploader("123:test-token")
        client.disconnect.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
