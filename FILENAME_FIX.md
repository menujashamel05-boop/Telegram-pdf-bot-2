# Sinhala / Unicode filename update

## Install

Replace your existing `bot.py` with the version in this archive, or deploy the
whole project, then restart/redeploy the bot. No new runtime dependency or
environment variable is required. Keep your existing BOT_TOKEN and settings.

## Behaviour

- **Keep original name** uses the incoming Telegram document's filename.
- **Rename** uses the text you send; `.pdf` is added only if it is missing.
- Sinhala letters, vowel signs, ZWJ (`U+200D`), ZWNJ (`U+200C`), mixed English,
  spaces, parentheses, punctuation and underscores are preserved without
  transliteration or Unicode normalization.
- Filenames are attached explicitly using Telegram's `InputFile`; internal
  working names such as `in_0.pdf` and `output.pdf` are never output names.
- Every output caption starts with the chosen filename, followed by the existing
  LEARN-X branding. This also preserves a readable/copyable name if Telegram
  changes the document label or the client renders Sinhala differently.
- Split files retain the chosen base name and append the page-range suffix,
  e.g. `පාඩම (1-3).pdf`, so the parts have distinct names.
- Directory components and control characters are removed for safety; blank
  names fall back to `document.pdf`. Very long caption names are truncated to
  Telegram's caption limit; the upload filename itself is not truncated here.

## What this does not change

This refers to the **document filename**, not a title printed inside the PDF or
its embedded Title metadata. The PDF's page content is not edited by this naming
update. Existing processing operations still work as before.

The original code already preserved Sinhala code points in ordinary filenames.
The screenshots do not establish whether the difference was caused by the
client font/renderer, Telegram server handling, or another bot. This update
hardens the upload path and adds the exact-name caption fallback; it cannot
force Telegram to preserve or render every joining character in its native
file label. It also cannot recover characters already missing in the filename
received from Telegram; in that case use **Rename** and send the intended name.

## Verification

43 offline tests passed: the original 30 tests plus 13 new filename tests.
Coverage includes Sinhala original/custom names, combining marks, ZWJ/ZWNJ,
extension handling, safety checks, logo/no-logo uploads, split names, caption
limits, and the actual python-telegram-bot HTTP multipart encoder with a mocked
HTTP transport. The latter checks that the name leaves the bot as exact UTF-8
bytes. No live Telegram account or bot was contacted.

Test environment: Python 3.13, python-telegram-bot 21.6, PyMuPDF 1.28.2,
pypdf 6.19.0, Pillow 12.3.0. The project's deployment requirements were left
unchanged. A live Telegram round-trip/client-rendering check remains necessary
with your bot after deployment.

Run the tests:

```sh
python -m unittest discover -s tests -v
```
