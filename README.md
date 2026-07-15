# Lunchbot for Brno daily menus

Small Python script that fetches lunch menus from configured restaurant pages and posts a formatted summary to Slack.

## What it does

- Fetches normal HTML menus.
- Extracts today's Czech weekday section from weekly menus.
- Extracts menu text from linked PDFs, when a page links to a PDF.
- Falls back to Playwright for pages that fail with a normal HTTP request.
- Groups meal fragments so the meal text and price are shown on the same row in Slack/reports.
- Adds global item indexes like `#1`, `#2`, `#3` to meal rows so people can vote by number.
- Can create formatted HTML/PDF reports.
- Can save smart fallback screenshots for failed restaurants: keyword sections, large images/PDF pages, and page slices.
- Can save rendered visible text and HTML debug dumps to help tune parsers.
- Posts the final text to Slack via a Slack Workflow Builder "From a webhook" trigger (works even without permission to create a custom Slack app).
- Has a `--dry-run` mode so you can preview output without posting.

## Setup

```bash
cd lunchbot
python -m venv .venv

# Windows PowerShell:
.\.venv\Scripts\Activate.ps1

# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env
```

On Windows PowerShell, use this instead of `cp`:

```powershell
copy .env.example .env
```

Edit `.env` and set `SLACK_WEBHOOK_URL`.

For Playwright fallback, PDF export, and screenshot support, run once after installing requirements:

```bash
python -m playwright install chromium
```

## Run locally

Preview only:

```bash
python main.py --dry-run
```

Post to Slack:

```bash
python main.py
```

Test a specific date:

```bash
python main.py --dry-run --date 2026-07-07
```

Run only one restaurant by name substring:

```bash
python main.py --dry-run --only solnici
```

## Reports

Save a formatted HTML report:

```bash
python main.py --dry-run --save-html
```

Save a formatted PDF report:

```bash
python main.py --dry-run --save-pdf
```

Save smart fallback screenshots for restaurants that need manual checks, and include those screenshots in the report:

```bash
python main.py --dry-run --save-pdf --save-screenshots
```

Save rendered visible text and HTML for failed restaurants. This helps when a screenshot shows the menu but the parser did not find it:

```bash
python main.py --dry-run --save-pdf --save-screenshots --save-debug-pages
```

The output will be created in:

```text
reports/lunch_menu_YYYY-MM-DD.html
reports/lunch_menu_YYYY-MM-DD.pdf
reports/screenshots/*.png
reports/debug/*-visible-text.txt
reports/debug/*-rendered.html
```

## Notes about iframe/weekly menus

The parser handles iframe-based menus by reading rendered iframe contents with Playwright.
When a page contains several upcoming days, extraction stops at the next weekday/date heading,
so a heading like `Úterý 7.7.2026` should return only Tuesday's menu, not Wednesday-Friday too.

## Scheduling examples

### Windows Task Scheduler

Create a task that runs daily around 10:30 or 11:00.

Program:

```text
C:\path\to\lunchbot\.venv\Scripts\python.exe
```

Arguments:

```text
C:\path\to\lunchbot\main.py
```

Start in:

```text
C:\path\to\lunchbot
```

To also generate a PDF with screenshots, use arguments like this instead:

```text
C:\path\to\lunchbot\main.py --save-pdf --save-screenshots
```

### Linux cron

```cron
30 10 * * 1-5 cd /path/to/lunchbot && /path/to/lunchbot/.venv/bin/python main.py
```

### GitHub Actions

`.github/workflows/lunch-menu.yml` runs the bot on a schedule (weekdays only, via cron's
day-of-week field) once this repo is pushed to GitHub. To enable it:

1. Push this repo to GitHub.
2. Go to the repo's **Settings → Secrets and variables → Actions** and add a repository
   secret named `SLACK_WEBHOOK_URL` with your webhook URL (a Slack Workflow Builder "From
   a webhook" trigger works without needing permission to create a custom Slack app —
   see the note in `.env.example`).
3. That's it. The workflow also has a manual trigger (**Actions → Post lunch menu to
   Slack → Run workflow**) so you can test it without waiting for the schedule.

The workflow registers two cron entries (`20 9 * * 1-5` and `20 8 * * 1-5`) so it stays
pinned to 10:20 Prague time year-round; the `check-schedule` job figures out which one is
currently correct for DST and skips the other (GitHub Actions cron is always UTC and
never shifts for daylight saving on its own). Adjust both cron entries and the
`expected_cron` values in `check-schedule` together if you want a different local time.

### Posted message + hosted report

Rather than dumping every menu item into Slack as plain text, the workflow generates the
PDF/HTML report, publishes it via GitHub Pages (`https://<user>.github.io/<repo>/`), and
posts a short summary — one line per restaurant — alongside the report URL. This only
works because the repo is **public** (GitHub Pages on a private repo needs a paid plan).
If you fork this to a private repo, drop `--report-url` from the workflow's `python
main.py` call; the summary is posted either way, just with an empty `report_url`.

`--report-url` is sent to Slack as its own `report_url` trigger variable rather than
being embedded as text, because Slack Workflow Builder inserts variables as plain text,
not mrkdwn — a `<url|label>` link written into the message text would show up
unrendered. To get a real "Denní menu" hyperlink instead of a bare auto-linked URL, add
`report_url` as a second variable on the webhook trigger in Workflow Builder, then in the
message step type "Denní menu" and use Slack's rich-text link tool to point it at that
variable.

## Notes

Some sites do not expose the daily menu as plain text. The fallback now saves multiple screenshot candidates instead of just one image: matching text sections, large images, PDF page renders, and page slices. If the menu is present in the rendered DOM, `--save-debug-pages` gives you a text/HTML dump that can be used to write a better parser. If the menu exists only inside an image, OCR can be added later, but it requires extra setup such as Tesseract on Windows.

A webhook (classic Incoming Webhook, or a Slack Workflow Builder "From a webhook" trigger) can only post text to a channel. Uploading the PDF or screenshots as a real Slack file attachment requires a Slack bot token with the `files:write` scope and the Slack file upload API, which needs a Slack app — the same permission a Workflow Builder webhook was set up specifically to avoid. If that's ever available, it would be a separate next step.


### Iframe menus

Some restaurant pages embed the daily menu in an iframe. The bot now has an iframe-aware fallback for Buddha and the screenshot/debug tools also inspect Playwright `page.frames`. If a parser cannot find menu text, run:

```powershell
python main.py --dry-run --save-pdf --save-screenshots --save-debug-pages --only buddha
```

Then check `reports/debug/*visible-text.txt`; it now includes sections named `IFRAME ...` so you can confirm whether the menu text is available as real HTML or only as an image.


## Restaurant filters

You can now use stable keys with `--only`, for example:

```powershell
python main.py --dry-run --only vlk
python main.py --dry-run --only orel
python main.py --dry-run --only buddha
```

For Dřevěný Vlk/Orel the bot uses the weekly-menu subpages, because the real Zomato daily-menu iframe is not on the homepage.

### Zomato iframe overrides for Dřevěný Orel/Vlk

The Vlk iframe from DevTools uses:

```env
ZOMATO_VLK_ENTITY_ID=16507597
```

That value is already included in the script for Vlk. Orel is intentionally not hardcoded to the same entity id, because using the same id makes Orel and Vlk return identical menus.

If you find Orel's real iframe in DevTools, add it to `.env` like this:

```env
ZOMATO_OREL_ENTITY_ID=16506896
```

or paste the full iframe URL:

```env
ZOMATO_OREL_URL=https://www.zomato.com/widgets/daily_menu.php?entity_id=16506896&width=100%25&height=1000px
```

If both restaurants really do intentionally share the same Zomato widget, you can force that by setting:

```env
ZOMATO_OREL_ENTITY_ID=16507597
```



## Added restaurant

- La Famiglia (`--only lafamiglia`) from https://lafamigliabrno.cz/denni-menu/
