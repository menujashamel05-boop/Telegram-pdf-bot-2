# Sinhala PDF filename patch

## Start / deploy

1. Replace your old project with the contents of this project folder. Keep your
   existing BOT_TOKEN and any existing log/admin environment variables.
2. Your supplied API_ID and API_HASH are already in the private `.env` included
   in this ZIP. For a local run, add `BOT_TOKEN=your-existing-bot-token` to that
   file, or export BOT_TOKEN in the environment.
3. For Railway/GitHub deployment, add API_ID and API_HASH to Railway Variables,
   alongside the existing BOT_TOKEN. Copy the values from the private `.env`.
   The `.env` file is deliberately excluded from Git. Redeploy after updating
   the code and requirements.
4. Install dependencies and start:

   ```bash
   python -m pip install -r requirements.txt
   python bot.py
   ```

Use Python 3.10 or newer. The handler tests supplied with this patch were run on
Python 3.12. Stop the old deployment first; run only one polling instance per bot.
No personal Telegram account login, phone number, or login code is required.
The MTProto connection authenticates with the same BOT_TOKEN. It uses an
in-memory session, so there is no extra `.session` file to deploy or share.

## What changed

- Added `unicode_upload.py`: sends PDFs with an explicit
  `DocumentAttributeFilename`, bypassing Bot API document-upload sanitization.
- Uses the same exact Unicode string for the upload name and the document
  filename attribute. Does not normalize, transliterate, strip Sinhala marks,
  or remove ZWJ/ZWNJ.
- Reads incoming original filenames from MTProto when available. If the
  original message is unavailable, keeps the filename received via the Bot API.
- Both **Keep original name** and **Rename** feed the new sender. All PDF modes
  share it; split outputs keep their existing page-range suffixes.
- Keeps the existing caption and logo thumbnail (thumbnail display remains
  Telegram-controlled).
- Checks the filename returned after sending. A missing/different name is
  reported, not silently accepted. The already-sent file is not automatically
  uploaded again.
- Retains input and offers naming buttons after failed delivery. No silent
  fallback to the original Bot API PDF upload route.
- Optional log copies use the Unicode sender too. Logging failures remain
  non-blocking.
- PDF-processing modules, watermark logic, and existing download/output size
  limits were not changed.

Unsafe path characters and control characters are still removed for filename
safety. `.pdf` is added if missing. Existing `.PDF` casing is retained.
Leading/trailing whitespace is trimmed, as in the original bot.

## Checks actually performed

- **35 tests passed**, covering Unicode code-point preservation, joining marks,
  typed rename, keep-original, received filenames, upload attributes,
  split filenames, thumbnails, startup/shutdown, auth errors, peer-resolution
  races, filename mismatches, and input retention after failed sending.
- Syntax compilation passed for all project Python files.
- The upload/authentication tests use mocked Telegram clients. The handler
  tests execute actual handler functions extracted from `bot.py`, with external
  dependencies mocked. They are not a full runtime import or live Telegram test.
- The original PDF-engine integration tests are retained, but were not rerun
  for this patch: PyMuPDF and Telegram dependencies are not installed in the
  build sandbox, which has no internet access for installing them.
- Live Telegram authentication, permissions, upload behavior, and client
  rendering have **not** been verified here. API credentials were included as
  supplied, not validated by contacting Telegram. No claim of zero live errors
  is made.

Run the new tests without third-party packages:

```bash
python -m unittest discover -s tests -p "test_unicode*.py" -v
```

After installing all requirements, run all tests:

```bash
python -m unittest discover -s tests -v
```

## Live acceptance check after deployment

1. Send `/start`, then upload a PDF named
   `රසායනික ගණනය (1+2) 2027 Revision.pdf`.
2. Select **Rename only**, then **Keep original name**. Download the result
   and compare its filename with your source.
3. Upload again, select **Rename**, and send
   `ශ්‍රී ලංකා ප්‍රශ්න පත්‍රය 2027`.
4. Confirm the downloaded filename is
   `ශ්‍රී ලංකා ප්‍රශ්න පත්‍රය 2027.pdf`.
5. Repeat with the PDF operation you normally use, including Split if needed.
   Split outputs intentionally include a page-range suffix.

If a name was damaged by another bot before you upload it, use Rename and send
the correct text. Lost characters cannot be inferred reliably from a damaged
filename. This patch changes filenames, not the PDF's internal title metadata
or text content. If the downloaded filename is exact but Telegram displays
letters differently, that remaining issue is client-side font/rendering.

## Private distribution

This ZIP contains the API credentials you supplied in `.env`. Do not publish it.
Environment variables take precedence over `.env`; remove stale API variables
from your host if you intend to use the supplied configuration locally.
