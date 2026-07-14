from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from pathlib import Path
from datetime import date, datetime
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

from restaurants import RESTAURANTS
from text_utils import format_slack_message
from report_utils import write_html_report, write_pdf_report
from fetchers import capture_menu_screenshots, save_page_debug_dump

logger = logging.getLogger("lunchbot")

# Slack Incoming Webhooks reject a "text" payload over 40,000 characters. Leave headroom
# for the truncation note itself.
SLACK_MAX_TEXT_LENGTH = 39000


def today_in_timezone(tz_name: str) -> date:
    return datetime.now(ZoneInfo(tz_name)).date()


def truncate_for_slack(message: str, max_length: int = SLACK_MAX_TEXT_LENGTH) -> str:
    if len(message) <= max_length:
        return message
    cutoff = message.rfind("\n\n", 0, max_length)
    if cutoff == -1:
        cutoff = max_length
    return message[:cutoff].rstrip() + "\n\n_Zpráva byla zkrácena, protože přesáhla limit Slacku._"


def post_to_slack(webhook_url: str, message: str, timeout_seconds: int, max_attempts: int = 2) -> None:
    message = truncate_for_slack(message)
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.post(
                webhook_url,
                json={"text": message},
                headers={"Content-Type": "application/json"},
                timeout=timeout_seconds,
            )
            response.raise_for_status()
            return
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < max_attempts:
                logger.warning("Slack post attempt %d/%d failed: %s. Retrying...", attempt, max_attempts, exc)
                time.sleep(3)
    assert last_exc is not None
    raise last_exc




def slugify_filename(value: str) -> str:
    value = value.casefold()
    value = re.sub(r"[^a-z0-9áčďéěíňóřšťúůýž]+", "-", value, flags=re.I)
    value = value.strip("-")
    return value or "restaurant"


def attach_fallback_screenshots(results, report_dir: str, timeout_seconds: int) -> None:
    """Attach several smart screenshots to failed restaurants.

    The older version saved one full-page screenshot. That was often not enough, because
    the useful menu could be far down the page or published as an image/PDF. This version
    saves several candidates: menu-keyword sections, large images, PDF pages, and page slices.
    """
    screenshots_dir = Path(report_dir) / "screenshots"
    for result in results:
        if result.status == "ok":
            continue

        screenshot_url = getattr(result, "screenshot_url", "") or result.url
        if not screenshot_url:
            continue

        prefix = slugify_filename(result.restaurant)
        try:
            logger.info("Saving smart fallback screenshots for %s...", result.restaurant)
            screenshot_paths = capture_menu_screenshots(
                screenshot_url,
                screenshots_dir,
                prefix,
                timeout_seconds=timeout_seconds,
            )
            relative_paths = [str(Path("screenshots") / path.name) for path in screenshot_paths]
            result.screenshot_paths = relative_paths
            result.screenshot_path = relative_paths[0] if relative_paths else ""
            if relative_paths:
                extra_note = f"Saved {len(relative_paths)} fallback screenshot(s)."
                result.note = f"{result.note} {extra_note}".strip()
        except Exception as exc:
            extra_note = f"Screenshot fallback failed: {exc}"
            result.note = f"{result.note} {extra_note}".strip()



def attach_debug_dumps(results, report_dir: str, timeout_seconds: int) -> None:
    debug_dir = Path(report_dir) / "debug"
    for result in results:
        if result.status == "ok":
            continue

        screenshot_url = getattr(result, "screenshot_url", "") or result.url
        if not screenshot_url:
            continue

        prefix = slugify_filename(result.restaurant)
        try:
            logger.info("Saving debug text/HTML for %s...", result.restaurant)
            paths = save_page_debug_dump(screenshot_url, debug_dir, prefix, timeout_seconds)
            if paths:
                extra_note = f"Saved debug dump(s): {', '.join(path.name for path in paths)}."
                result.note = f"{result.note} {extra_note}".strip()
        except Exception as exc:
            extra_note = f"Debug dump failed: {exc}"
            result.note = f"{result.note} {extra_note}".strip()

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch lunch menus and post them to Slack.")
    parser.add_argument("--dry-run", action="store_true", help="Print the Slack message without sending it.")
    parser.add_argument("--date", help="Date to use, in YYYY-MM-DD format. Defaults to today in TIMEZONE.")
    parser.add_argument("--only", help="Run only restaurants whose name contains this text.")
    parser.add_argument("--include-weekends", action="store_true", help="Run even on Saturday/Sunday.")
    parser.add_argument("--save-html", action="store_true", help="Save a nicely formatted HTML report into the reports folder.")
    parser.add_argument("--save-pdf", action="store_true", help="Save a nicely formatted PDF report into the reports folder. Requires Playwright Chromium.")
    parser.add_argument("--report-dir", default="reports", help="Folder where HTML/PDF reports are saved. Default: reports.")
    parser.add_argument(
        "--save-screenshots",
        action="store_true",
        help="For failed restaurants, save smart screenshots: keyword sections, large images, PDF pages, and page slices.",
    )
    parser.add_argument(
        "--save-debug-pages",
        action="store_true",
        help="For failed restaurants, save rendered visible text and HTML into reports/debug for parser tuning.",
    )
    return parser.parse_args()


def main() -> int:
    # Windows consoles often default to a legacy codepage (e.g. cp1250) that cannot
    # encode emoji used in the Slack message, which crashes --dry-run output.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    load_dotenv()
    args = parse_args()

    tz_name = os.getenv("TIMEZONE", "Europe/Prague")
    timeout_seconds = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "20"))

    if args.date:
        target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
    else:
        target_date = today_in_timezone(tz_name)

    if target_date.weekday() >= 5 and not args.include_weekends:
        logger.info("%s is weekend. Use --include-weekends to run anyway.", target_date.isoformat())
        return 0

    restaurants = RESTAURANTS
    if args.only:
        restaurants = [r for r in restaurants if r.matches_filter(args.only)]
        if not restaurants:
            available = ", ".join(r.key or r.name for r in RESTAURANTS)
            logger.warning("No restaurant matched --only %r. Available keys: %s", args.only, available)
            return 2

    results = []
    for restaurant in restaurants:
        logger.info("Fetching %s...", restaurant.name)
        try:
            result = restaurant.fetch(target_date=target_date, timeout_seconds=timeout_seconds)
        except Exception as exc:  # keep one broken restaurant from breaking the whole Slack post
            result = restaurant.failure(f"Unexpected parser error: {exc}")
        results.append(result)

    if args.save_screenshots:
        attach_fallback_screenshots(results, args.report_dir, timeout_seconds)

    if args.save_debug_pages:
        attach_debug_dumps(results, args.report_dir, timeout_seconds)

    message = format_slack_message(results, target_date)

    if args.save_html or args.save_pdf:
        try:
            html_path = write_html_report(results, target_date, args.report_dir)
            logger.info("HTML report saved: %s", html_path)
        except Exception as exc:
            logger.warning("Could not save HTML report: %s", exc)

    if args.save_pdf:
        try:
            pdf_path = write_pdf_report(results, target_date, args.report_dir)
            logger.info("PDF report saved: %s", pdf_path)
        except Exception as exc:
            logger.warning("Could not save PDF report: %s", exc)

    if args.dry_run:
        print(message)
        return 0

    webhook_url = os.getenv("SLACK_WEBHOOK_URL")
    if not webhook_url:
        logger.error("Missing SLACK_WEBHOOK_URL. Add it to .env or run with --dry-run.")
        return 2

    post_to_slack(webhook_url, message, timeout_seconds)
    logger.info("Lunch menu posted to Slack.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
