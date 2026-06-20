# PDF Tools Telegram Bot

A Telegram bot to **watermark**, **rasterize**, and **compress** PDFs.

- ðŸ’§ **Watermark** â€“ **text or image**:
  - *Text*: enter text â†’ pick position (9-grid or tiled diagonal) â†’ type opacity (1â€“100)
  - *Image*: send an image â†’ centered in a 300Ã—300 box â†’ type opacity (1â€“100)
  - Text watermark keeps the original text layer selectable.
- ðŸ–¼ **Rasterize** â€“ PDF â†’ pictures â†’ PDF (renders each page to a JPEG, rebuilds the PDF). No copyable text / removable layers.
- ðŸ”’ **Rasterize + Watermark** â€“ same watermark flow, then burns it into the pixels so it *cannot* be removed.
- ðŸ—œ **Compress** â€“ shrink large / scanned PDFs (light / medium / strong).

Every output: you choose to keep the original file name or rename it, the file is
sent with the **LEARN-X logo as its thumbnail** (`logo.jpg`) and the caption
**"Powered by - LEARN - X"**.

> Make sure `logo.jpg` is uploaded to the repo alongside `bot.py` â€” it is the
> thumbnail shown on every output file.

Built with `python-telegram-bot` + `PyMuPDF` + `Pillow`. Uses **long polling**, so no
public URL or webhook is needed â€” it runs identically on your laptop and on Railway.

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

1. **Push this folder to a new GitHub repo:**
   ```bash
   git init
   git add .
   git commit -m "PDF tools telegram bot"
   git branch -M main
   git remote add origin https://github.com/<you>/<repo>.git
   git push -u origin main
   ```
   > `.gitignore` keeps your token / test files out of the repo. Never commit `.env`.

2. Go to **railway.com â†’ New Project â†’ Deploy from GitHub repo** and pick your repo.

3. Railway auto-detects Python and installs `requirements.txt`.

4. Open the service â†’ **Variables** tab â†’ add:
   ```
   BOT_TOKEN = your-telegram-bot-token
   ```

5. **Settings â†’ Start Command** should be `python bot.py` (already set in `railway.json`).

6. Deploy. Watch the **Deploy Logs** for `Bot starting (long polling)...` â€” that means it's live.

> This is a **worker** (no web port). If Railway asks about a port / healthcheck,
> ignore it â€” a polling bot doesn't serve HTTP.

## 4. Use it

- `/start` â€“ help
- Send a PDF â†’ tap a mode button â†’ follow the prompts (text/image, position, opacity)

---

## Notes & limits

- **20 MB** max file download (Telegram Bot API limit). Sending back is up to 50 MB.
- Rasterizing big PDFs at 300 DPI is CPU/RAM heavy; the bot offers 150/200/300 DPI.
- Watermark look (opacity, angle, tiled vs centered) is configurable in `pdf_tools.py`.
- Files are processed in the OS temp dir and deleted right after sending.

## Cost on Railway

The free trial gives a one-time $5 credit; after that the Hobby plan is $5/mo
with $5 usage included. A low-traffic bot like this typically fits inside that.
To save credits, the bot only uses CPU while actually processing a file.
