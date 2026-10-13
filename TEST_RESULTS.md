# Watermark-removal validation

## Offline regression suite

Command: `python -B -m unittest discover -s tests -v`

**30 tests passed** using the versions pinned in `requirements.txt`:

- python-telegram-bot 21.6
- PyMuPDF 1.27.2.3
- Pillow 10.4.0
- pypdf 6.18.0

Coverage includes automatic repeated text overlays, overlapping numeric layers, exact manual text targeting, manual image references, page ranges, shared resources, ordinary text/diagram preservation, merge, keep-name/rename menus, stale-button protection, no-match/error handling, signed/encrypted input checks, image limits, and the no-lossy-compression guard. Watermark-add/remove integration tests also compare rendered pages with the originals.

## Supplied 17-page PDF

- Automatic: removed 34 standalone overlay streams / 102 watermark occurrences across 17 pages.
- Specified text, one layer on page 1: removed 3 occurrences; the other number layer remained.
- Specified text, both layers: removed all 102 occurrences.
- Two copies merged: removed 68 overlay streams / 204 occurrences across 34 pages.
- All output preservation checks passed; the uploaded source file remained unchanged.

The supplied PDF and its identifying watermark numbers are not included in this project archive.

## Scope

These are local/offline tests, not a live Telegram deployment. Supported independent overlays are removed without rewriting retained content. Burned-in, inseparable, ambiguous, or unsupported marks are not guaranteed removable and are retained rather than risk damage.


## Unicode filename update

43 offline tests passed (30 existing + 13 filename regressions). The UTF-8
filename and caption were verified through the real Telegram library multipart
encoder using a mocked HTTP transport. No live Telegram test was performed.
See FILENAME_FIX.md for environment versions and server/client limitations.
