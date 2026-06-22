# PDF Tools Telegram Bot - LEARN-X

A Telegram bot to **merge**, **watermark**, **rasterize**, and **compress** PDFs.

## Features

- **Watermark** - text or image:
  - *Text*: enter text -> pick position (9-grid or tiled diagonal) -> choose
    **text size** (Word-style points, default 40) -> type opacity (1-100) ->
    pick page range.
  - *Image*: send an image -> centered in a 350x350 box -> type opacity (1-100).
  - Text watermark keeps the original text layer selectable.
- **Rasterize** - PDF -> pictures -> PDF (renders each page to a JPEG and
  rebuilds the PDF). No copyable text / removable layers.
- **Rasterize + Watermark** - same watermark flow, then burns it into the
  pixels so it *cannot* be removed.
- **Compress** - shrink large / scanned PDFs (light / medium / strong).
  Re-compresses the embedded images and never returns a file bigger than the
  original. Text/vector PDFs keep their selectable text.
- **Merge** - send several PDFs (album or one-by-one) and they merge in order.
- **Delete pages** - remove a page range and keep the rest in order. Type
  something like `1-3, 5, 9` to delete those 5 pages (you can't delete every
  page). Works on a single PDF or on the merged result.
- **Split** - cut one PDF into several by page range. Type
  `1-3, 4-8, 9-10` on a 10-page PDF and you get **3 separate files**
  (pages 1-3, 4-8 and 9-10). A single page like `7` is allowed as its own part.
  Each part is sent with the LEARN-X thumbnail and a `(page-range)` suffix in
  its file name.
- **All-in-One** - merge + watermark + rasterize in one action.
- **Page range** for watermarking: `all`, `1`, or `1, 2-5, 10`.

Every output: you choose to keep the original file name or rename it, the file
is sent with the **LEARN-X logo as its thumbnail** (`logo.jpg`) and the caption
**"Powered by - LEARN - X"**.

> Make sure `logo.jpg` **and** `wm_font.ttf` are uploaded to the repo alongside
> `bot.py`. `logo.jpg` is the output thumbnail; `wm_font.ttf` is the watermark
> font - without it the text watermark falls back to a tiny font and looks
> microscopic on the server.

Built with `python-telegram-bot` + `PyMuPDF` + `Pillow`. Uses **long polling**,
so no public URL or webhook is needed - it runs identically on your laptop and
on Railway.

---

## 1. Create your bot token

1. In Telegram, open **@BotFather**.
2. Send `/newbot`, choose a name and username.
3. Copy the **token** it gives you (looks like `123456789:AAH...`).

## 2. Run locally (optional, for testing)

```bash
pip install -r requirements.txt
export BOT_TOKEN="paste-your-token"      # Windows: set BOT_TOKEN=...
python bot.py
```

Open your bot in Telegram, send `/start`, then send a PDF.

## 3. Deploy on Railway via GitHub

1. Push this folder to a GitHub repo (or use **Add file -> Upload files** to
   drag the files in). Make sure these are all uploaded:
   `bot.py`, `pdf_tools.py`, `requirements.txt`, `Procfile`, `railway.json`,
   `logo.jpg`, `wm_font.ttf`.
2. Go to **railway.com -> New Project -> Deploy from GitHub repo** and pick the repo.
3. Railway auto-detects Python and installs `requirements.txt`.
4. Open the service -> **Variables** tab -> add `BOT_TOKEN = your-telegram-bot-token`.
5. Deploy. Watch the **Deploy Logs** for `Bot starting (long polling)...` - that
   means it's live.

> This is a **worker** (no web port). If Railway asks about a port / healthcheck,
> ignore it - a polling bot doesn't serve HTTP.

## 4. Use it

- `/start` - help
- `/reset` - clear and start over
- Send a PDF (or several) -> tap a mode button -> follow the prompts.

---

## Notes & limits

- **20 MB** max file *download* (a hard Telegram Bot API limit for bots). The
  bot rejects bigger incoming files gracefully. Raising this needs a self-hosted
  Local Bot API server.
- Sending back is up to **50 MB**. Big rasterized outputs are automatically
  shrunk to stay under that cap (`shrink_to_limit`).
- Watermark look: image box (`DEFAULT_IMAGE_BOX_PT`, 350) and default text size
  (`DEFAULT_TEXT_FONT_PT`, 40) are at the top of `pdf_tools.py`.
- Files are processed in the OS temp dir and deleted right after sending.

## Cost on Railway

The free trial gives a one-time $5 credit; after that the Hobby plan is $5/mo
with $5 usage included. A low-traffic bot like this typically fits inside that -
cost is dominated by idle always-on memory, not the PDF work.
