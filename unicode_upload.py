"""Send PDFs through MTProto without Bot API filename sanitization.

No Unicode normalization, transliteration, or removal of combining marks or
joiners is performed. Telegram credentials are loaded only at startup.
"""
import asyncio
import logging
import os
from collections import OrderedDict

log = logging.getLogger(__name__)


def clean_pdf_name(value):
    """Keep Unicode code points intact; remove only unsafe filename characters."""
    name = os.path.basename((value or "").strip())
    name = "".join(
        char for char in name
        if char not in '\\/:*?"<>|' and ord(char) >= 32 and ord(char) != 127
    )
    if not name or name in {".", ".."}:
        return "document.pdf"
    return name if name.lower().endswith(".pdf") else name + ".pdf"


class FilenameChangedError(RuntimeError):
    """Telegram returned a different name after accepting the upload."""


class UnicodeUploader:
    CACHE_LIMIT = 1024

    def __init__(self, client, filename_attribute):
        self.client = client
        self.filename_attribute = filename_attribute
        self._peers = OrderedDict()
        self._names = OrderedDict()

    def _remember(self, cache, key, value):
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > self.CACHE_LIMIT:
            cache.popitem(last=False)

    def _document_name(self, message):
        document = getattr(message, "document", None)
        for attribute in getattr(document, "attributes", ()) or ():
            if isinstance(attribute, self.filename_attribute):
                return attribute.file_name
        return None

    async def remember_message(self, event):
        """Cache access hashes and original names, including Sinhala joiners."""
        try:
            peer = await event.get_input_chat()
            if peer is not None:
                self._remember(self._peers, event.chat_id, peer)
            name = self._document_name(event.message)
            if name:
                self._remember(self._names, (event.chat_id, event.message.id), name)
        except Exception:
            # An unrelated event must never kill the Telegram update receiver.
            log.warning("Could not cache an incoming Telegram document", exc_info=True)

    async def peer_for(self, chat_id):
        # Telegram's two update streams can arrive in either order. Wait briefly
        # for the MTProto update rather than inventing an access hash of zero.
        for attempt in range(21):
            if chat_id in self._peers:
                return self._peers[chat_id]
            try:
                peer = await self.client.get_input_entity(chat_id)
                self._remember(self._peers, chat_id, peer)
                return peer
            except (ValueError, TypeError):
                if attempt == 20:
                    raise RuntimeError(
                        "Cannot resolve this Telegram chat. Send /start and "
                        "upload the PDF again, then retry."
                    ) from None
                await asyncio.sleep(0.25)

    async def original_name(self, chat_id, message_id, fallback):
        key = (chat_id, message_id)
        if key in self._names:
            return self._names[key]
        try:
            peer = await self.peer_for(chat_id)
            if key in self._names:
                return self._names[key]
            message = await self.client.get_messages(peer, ids=message_id)
            name = self._document_name(message)
            if name:
                self._remember(self._names, key, name)
                return name
        except Exception:
            log.warning("Original MTProto filename unavailable; using received filename",
                        exc_info=True)
        # Cannot reconstruct marks missing from a file BEFORE it reached this bot.
        return fallback or "document.pdf"

    async def send_pdf(self, chat_id, path, filename, caption="", thumbnail=None):
        peer = await self.peer_for(chat_id)
        # A new upload plus explicit attributes avoids cached document metadata
        # and avoids exposing the ASCII temporary filename as the download name.
        uploaded = await self.client.upload_file(str(path), file_name=filename)
        message = await self.client.send_file(
            peer,
            uploaded,
            attributes=[self.filename_attribute(filename)],
            mime_type="application/pdf",
            force_document=True,
            caption=caption,
            parse_mode=None,
            thumb=thumbnail if thumbnail and os.path.isfile(thumbnail) else None,
        )
        if self._document_name(message) != filename:
            # The file was already sent. Do not send a second copy or silently
            # fall back to the Bot API path that caused the original problem.
            raise FilenameChangedError(
                "The PDF was sent, but Telegram did not confirm the exact filename."
            )
        return message

    async def close(self):
        await self.client.disconnect()


async def start_uploader(bot_token):
    """Authenticate the SAME bot, not a personal Telegram account."""
    from telethon import TelegramClient, events
    from telethon.sessions import MemorySession
    from telethon.tl.types import DocumentAttributeFilename

    raw_id = os.environ.get("API_ID", "").strip()
    api_hash = os.environ.get("API_HASH", "").strip()
    if not raw_id.isdecimal() or int(raw_id) <= 0 or not api_hash:
        raise RuntimeError("Set API_ID and API_HASH in .env or deployment variables.")
    # MemorySession avoids distributing/persisting a second login secret and
    # cannot accidentally reuse a session belonging to another bot/account.
    client = TelegramClient(MemorySession(), int(raw_id), api_hash)
    uploader = UnicodeUploader(client, DocumentAttributeFilename)
    client.add_event_handler(uploader.remember_message, events.NewMessage(incoming=True))
    try:
        await asyncio.wait_for(client.start(bot_token=bot_token), timeout=60)
        me = await client.get_me()
        if not me.bot or me.id != int(bot_token.split(":", 1)[0]):
            raise RuntimeError("MTProto login does not match BOT_TOKEN.")
    except BaseException:
        await client.disconnect()
        raise
    return uploader
