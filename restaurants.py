from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import html
import os
import re
from pathlib import Path

import requests
from typing import Callable
from urllib.parse import urljoin

from fetchers import (
    extract_pdf_text_from_url,
    fetch_html,
    fetch_html_playwright_all_frames,
    fetch_html_playwright_frames,
    fetch_playwright_frame_texts,
    find_rendered_iframe_sources,
    find_external_links,
    find_pdf_links,
    make_soup,
)
from text_utils import (
    CZECH_WEEKDAYS,
    clean_lines,
    comparable,
    extract_between_keywords,
    extract_current_date_section,
    extract_today_weekday_section,
    normalize_whitespace,
    soup_to_lines,
)


@dataclass
class MenuResult:
    restaurant: str
    url: str
    status: str
    lines: list[str]
    error: str = ""
    note: str = ""
    screenshot_url: str = ""
    screenshot_path: str = ""
    screenshot_paths: list[str] = field(default_factory=list)


@dataclass
class Restaurant:
    name: str
    url: str
    parser: Callable[["Restaurant", date, int], MenuResult]
    allow_playwright_fallback: bool = False
    key: str = ""
    aliases: tuple[str, ...] = ()

    def matches_filter(self, value: str) -> bool:
        needle = value.casefold().strip()
        candidates = [self.name.casefold(), self.key.casefold(), *(alias.casefold() for alias in self.aliases)]
        return any(needle in candidate for candidate in candidates)

    def fetch(self, target_date: date, timeout_seconds: int) -> MenuResult:
        return self.parser(self, target_date, timeout_seconds)

    def failure(self, message: str, note: str = "", screenshot_url: str = "") -> MenuResult:
        return MenuResult(
            restaurant=self.name,
            url=self.url,
            status="failed",
            lines=[],
            error=message,
            note=note,
            screenshot_url=screenshot_url or self.url,
        )

    def success(self, lines: list[str], source_url: str | None = None, note: str = "") -> MenuResult:
        return MenuResult(
            restaurant=self.name,
            url=source_url or self.url,
            status="ok",
            lines=lines,
            note=note,
        )


def html_lines(restaurant: Restaurant, timeout_seconds: int) -> tuple[str, list[str]]:
    fetched = fetch_html(
        restaurant.url,
        timeout_seconds=timeout_seconds,
        allow_playwright_fallback=restaurant.allow_playwright_fallback,
    )
    soup = make_soup(fetched.text)
    return fetched.url, soup_to_lines(soup)


def parse_generic_html(restaurant: Restaurant, target_date: date, timeout_seconds: int) -> MenuResult:
    source_url, lines = html_lines(restaurant, timeout_seconds)

    section = extract_today_weekday_section(lines, target_date)
    note = "Matched today's weekday section."

    if not section:
        section = extract_current_date_section(lines, target_date)
        note = "Matched today's date section."

    if not section:
        return restaurant.failure(
            "Could not find today's menu section in page text.",
            note="Open the source link to check whether the site layout changed.",
            screenshot_url=source_url,
        )

    return restaurant.success(section, source_url=source_url, note=note)


def parse_daily_menu_page(restaurant: Restaurant, target_date: date, timeout_seconds: int) -> MenuResult:
    source_url, lines = html_lines(restaurant, timeout_seconds)

    section = extract_current_date_section(lines, target_date)
    note = "Matched today's date section."

    if not section:
        section = extract_today_weekday_section(lines, target_date)
        note = "Matched today's weekday section."

    if not section:
        return restaurant.failure(
            "Could not find a daily/date menu section.",
            note="This parser expects a daily heading like 'Úterý 7. 7.' or 'Menu – 7. 7.'.",
            screenshot_url=source_url,
        )

    return restaurant.success(section, source_url=source_url, note=note)


def _singha_fix_mojibake(value: str) -> str:
    """Repair common UTF-8-as-Latin-1 mojibake if the site response is decoded badly."""
    if "Ã" not in value and "Ä" not in value and "Å" not in value:
        return value
    try:
        return value.encode("latin1", errors="ignore").decode("utf-8", errors="ignore") or value
    except Exception:
        return value


def _singha_comparable(value: str) -> str:
    return comparable(_singha_fix_mojibake(value))


def _singha_price(value: str) -> str:
    """Normalize prices such as '209,- Kč', '199 Kč' or '194Kč' to '209 Kč'."""
    match = re.search(r"\b(?P<number>\d{2,4})\s*(?:,-\s*)?(?:Kč|Kc|CZK)\b|\b(?P<number2>\d{2,4})\s*,-", value, flags=re.I)
    if not match:
        return ""
    return f"{match.group('number') or match.group('number2')} Kč"


def _singha_remove_price(value: str) -> str:
    value = re.sub(r"\b\d{2,4}\s*(?:,-\s*)?(?:Kč|Kc|CZK)\b", "", value, flags=re.I)
    value = re.sub(r"\b\d{2,4}\s*,-", "", value, flags=re.I)
    return normalize_whitespace(value).strip(" -–—:")


def _singha_is_price_only(value: str) -> bool:
    return bool(re.fullmatch(r"\s*\d{2,4}\s*(?:,-\s*)?(?:Kč|Kc|CZK)?\s*", value, flags=re.I))


def _singha_is_english_description(value: str) -> bool:
    """Skip the duplicated English description; keep the Czech line for compact output."""
    if re.search(r"[ěščřžýáíéúůďťňĚŠČŘŽÝÁÍÉÚŮĎŤŇ]", value):
        return False
    return bool(re.search(
        r"\b(japanese|wide|yellow|soup|spicy|thai|with|chicken|beef|shrimp|shrimps|noodles|served|curry|mushroom|coconut|milk|vegetables|fresh|rice|stir|fried|paste|meat|served)\b",
        value,
        flags=re.I,
    ))


def _singha_is_probable_title(value: str, current_category: str) -> bool:
    """Detect Singha item titles even when the price is in a separate Elementor widget.

    In the page DOM, a title can be in one widget and the price in a neighboring
    widget/column. Inner text therefore often appears as:

    ``1) UDON SUZUKI - KUŘECÍ/CHICKEN``
    ``Japonské udon nudle ...``
    ``Japanese udon noodles ...``
    ``194 Kč``

    The old parser only accepted title+price on the same line, so it could return
    no rows even though the menu was visible.
    """
    value = normalize_whitespace(_singha_fix_mojibake(value))
    if not value or _singha_is_price_only(value) or _singha_category(value):
        return False
    c_value = _singha_comparable(value)
    if any(stop in c_value for stop in ["uvod", "poledni menu", "jidelnicek", "napojovy listek", "kontakt", "nas instagram"]):
        return False
    # Main-course titles are numbered. Check this before the English-description
    # heuristic because titles may contain words like CHICKEN/BEEF/SHRIMPS.
    if re.match(r"^\d+\s*[).]", value):
        return True
    if current_category == "Polévky":
        return bool(re.search(r"\b(tom\s+kha|tom\s+yam|pol[eé]vka|soup)\b", value, flags=re.I))
    if _singha_is_english_description(value):
        return False
    return False


def _singha_visible_text_lines(url: str, timeout_seconds: int) -> list[str]:
    """Return Playwright visible text lines for Elementor layouts.

    BeautifulSoup over page.content() can miss the practical reading order when
    Elementor splits a menu into multiple widgets/columns. Browser-visible text is
    closer to what a human sees on the page.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is not installed. Run: pip install playwright && python -m playwright install chromium"
        ) from exc

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(locale="cs-CZ", viewport={"width": 1365, "height": 1600})
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_seconds * 1000)
            page.wait_for_timeout(1500)
            try:
                page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(700)
                page.evaluate("() => window.scrollTo(0, 0)")
                page.wait_for_timeout(300)
            except Exception:
                pass
            text = page.locator("body").inner_text(timeout=timeout_seconds * 1000)
            return clean_lines(text.splitlines())
        finally:
            browser.close()


def _singha_category(value: str) -> str:
    c_value = _singha_comparable(value)
    if c_value in {"hlavni chody", "main courses"}:
        return "Hlavní chody"
    if c_value in {"polevky", "soups", "soup"}:
        return "Polévky"
    return ""


def _parse_singha_lines(lines: list[str]) -> list[str]:
    """Parse Singha's Elementor lunch-menu page into compact meal rows.

    Handles both forms seen on the page:
    - title and price on the same line: ``1) UDON ... 194 Kč``
    - title/description/price split across separate Elementor widgets.
    """
    fixed_lines = [normalize_whitespace(_singha_fix_mojibake(line)) for line in lines]
    fixed_lines = [line for line in fixed_lines if line]

    start_index = None
    for index, line in enumerate(fixed_lines):
        if _singha_category(line) == "Hlavní chody":
            start_index = index
            break
    if start_index is None:
        for index, line in enumerate(fixed_lines):
            if _singha_price(line) and not _singha_is_price_only(line):
                start_index = index
                break
    if start_index is None:
        return []

    rows: list[str] = []
    current_category = ""
    index = start_index
    seen_items: set[str] = set()

    def is_stop(value: str) -> bool:
        c_value = _singha_comparable(value)
        return any(stop in c_value for stop in [
            "nas instagram",
            "kontakt",
            "zadne produkty",
            "jidelnicek",
            "napojovy listek",
            "facebook",
            "instagram",
        ])

    while index < len(fixed_lines):
        line = fixed_lines[index]

        if is_stop(line):
            break

        category = _singha_category(line)
        if category:
            if category != current_category:
                rows.append(category)
                current_category = category
            index += 1
            continue

        price_on_title = _singha_price(line)
        is_title_with_price = bool(price_on_title and not _singha_is_price_only(line))
        is_split_title = _singha_is_probable_title(line, current_category)

        if is_title_with_price or is_split_title:
            price = price_on_title
            meal = _singha_remove_price(line) if price_on_title else line
            description = ""
            lookahead = index + 1

            # Elementor often renders the visible reading order as:
            # title -> price -> Czech description -> English description,
            # or title -> Czech description -> English description -> price.
            # The previous parser stopped as soon as it consumed a standalone price,
            # which meant Singha rows only contained the meal name. Keep scanning
            # briefly until the next title/category so we can attach the Czech
            # description even when the price appears before it.
            max_lookahead = min(len(fixed_lines), index + 10)
            while lookahead < max_lookahead:
                candidate = fixed_lines[lookahead]

                if is_stop(candidate) or _singha_category(candidate):
                    break

                # The next probable title starts the next item.
                if _singha_is_probable_title(candidate, current_category):
                    break

                candidate_price = _singha_price(candidate)
                if candidate_price:
                    if not price:
                        price = candidate_price
                    lookahead += 1
                    continue

                if _singha_is_english_description(candidate):
                    lookahead += 1
                    continue

                if candidate and not _singha_is_price_only(candidate):
                    if not description:
                        description = candidate
                    elif len(description) < 180 and candidate not in description:
                        description = f"{description} {candidate}"

                lookahead += 1

            if meal and price:
                compact = meal
                if description:
                    compact = f"{compact} - {description}"
                compact = f"{compact} {price}"
                if compact not in seen_items:
                    seen_items.add(compact)
                    rows.append(compact)
                index = max(index + 1, lookahead)
                continue

        index += 1

    # Require at least two price rows so we do not accept navigation/header text.
    if sum(1 for row in rows if _singha_price(row)) < 2:
        return []
    return clean_lines(rows)

def parse_singha(restaurant: Restaurant, target_date: date, timeout_seconds: int) -> MenuResult:
    fetch_errors: list[str] = []
    attempts: list[tuple[str, str, list[str]]] = []

    try:
        source_url, lines = html_lines(restaurant, timeout_seconds)
        attempts.append((source_url, "normal HTML", lines))
    except Exception as exc:
        fetch_errors.append(f"normal HTML failed: {exc}")

    # Singha uses an Elementor page where the visible menu can be more reliable after
    # browser rendering. Try both rendered HTML and browser-visible text before failing.
    try:
        rendered = fetch_html_playwright_all_frames(restaurant.url, timeout_seconds)
        soup = make_soup(rendered.text)
        attempts.append((rendered.url, "rendered browser HTML", soup_to_lines(soup)))
    except Exception as exc:
        fetch_errors.append(f"rendered browser HTML failed: {exc}")

    try:
        visible_lines = _singha_visible_text_lines(restaurant.url, timeout_seconds)
        attempts.append((restaurant.url, "browser-visible text", visible_lines))
    except Exception as exc:
        fetch_errors.append(f"browser-visible text failed: {exc}")

    for source_url, source_name, lines in attempts:
        section = _parse_singha_lines(lines)
        if section:
            return restaurant.success(
                section,
                source_url=source_url,
                note=f"Extracted Singha lunch-menu rows from the {source_name}; this page does not expose weekday headings.",
            )

    extra = " ".join(fetch_errors).strip()
    return restaurant.failure(
        "Could not extract Singha lunch menu rows.",
        note=(
            "This parser expects the visible Singha page sections 'Hlavní chody' and 'Polévky' "
            "with item titles and Kč prices. " + extra
        ).strip(),
        screenshot_url=restaurant.url,
    )


def parse_linked_pdf_weekly(restaurant: Restaurant, target_date: date, timeout_seconds: int) -> MenuResult:
    fetched = fetch_html(
        restaurant.url,
        timeout_seconds=timeout_seconds,
        allow_playwright_fallback=restaurant.allow_playwright_fallback,
    )
    soup = make_soup(fetched.text)
    pdf_links = find_pdf_links(fetched.url, soup)

    if not pdf_links:
        return restaurant.failure("Could not find a linked PDF menu on the page.", screenshot_url=fetched.url)

    last_error = ""
    for pdf_url in pdf_links:
        try:
            pdf_text = extract_pdf_text_from_url(pdf_url, timeout_seconds)
            lines = clean_lines(pdf_text.splitlines())
            section = extract_today_weekday_section(lines, target_date)
            if section:
                return restaurant.success(
                    section,
                    source_url=pdf_url,
                    note="Extracted from linked PDF.",
                )
        except Exception as exc:
            last_error = str(exc)

    return restaurant.failure(
        "Found PDF link, but could not extract today's weekday section.",
        note=last_error or "The PDF may have a layout that needs custom handling.",
        screenshot_url=pdf_links[0] if pdf_links else fetched.url,
    )



def find_iframe_links(page_url: str, soup, keywords: list[str]) -> list[str]:
    """Return iframe src URLs whose URL looks relevant for menus.

    Some restaurants embed the real menu as a separate document, usually in an iframe.
    A normal top-level scrape sees only the iframe tag, not the content inside it.
    """
    found: list[str] = []
    for tag in soup.find_all("iframe", src=True):
        src = urljoin(page_url, tag.get("src", ""))
        combined = f"{src} {tag.get_text(' ', strip=True)}".casefold()
        if any(keyword.casefold() in combined for keyword in keywords):
            if src not in found:
                found.append(src)
    return found


def find_zomato_widget_urls_from_html(page_url: str, html_text: str) -> list[str]:
    """Find Zomato daily-menu widget URLs in raw HTML/rendered HTML.

    The Dřevěný Orel/Vlk pages embed the real menu in a Zomato iframe. Depending on
    how the browser/parser reads the page, the URL can appear as an iframe src, as
    escaped HTML, or inside JavaScript. This function intentionally scans raw text too.
    """
    raw = html.unescape(html_text).replace("&amp;", "&")
    patterns = [
        r"(?P<url>(?:https?:)?//[^\s\"'<>]+zomato\.com/widgets/daily_menu\.php[^\s\"'<>]*)",
        r"(?P<url>(?:https?:)?//[^\s\"'<>]+zomato\.com/[^\s\"'<>]*daily_menu[^\s\"'<>]*)",
    ]
    found: list[str] = []
    for pattern in patterns:
        for match in re.finditer(pattern, raw, flags=re.I):
            url = normalize_zomato_widget_url(match.group("url"))
            url = urljoin(page_url, url)
            if url not in found:
                found.append(url)

    # Some widget snippets store only entity_id="16507597" near the widget loader.
    # In that case, build the final iframe URL ourselves.
    for match in re.finditer(r"entity_id[\s=:\"']+(?P<id>\d{5,})", raw, flags=re.I):
        url = normalize_zomato_widget_url(f"https://www.zomato.com/widgets/daily_menu.php?entity_id={match.group('id')}")
        if url not in found:
            found.append(url)
    return found


def looks_like_zomato_daily_menu_url(url: str) -> bool:
    value = html.unescape(url).replace("&amp;", "&").casefold()
    return "zomato" in value and (
        "daily_menu.php" in value
        or "entity_id=" in value
        or "daily-menu" in value
        or "dailymenu" in value
    )


def looks_like_zomato_widget_loader_url(url: str) -> bool:
    """True for Zomato's script/widget loader, which is not the real menu document."""
    value = html.unescape(url).replace("&amp;", "&").casefold()
    return "zomato" in value and "daily_menu_widget" in value and "daily_menu.php" not in value


def normalize_zomato_widget_url(url: str) -> str:
    """Clean and, when possible, turn a Zomato URL into the daily_menu.php iframe URL."""
    cleaned = html.unescape(url).replace("&amp;", "&").strip().rstrip(".,);]")
    if cleaned.startswith("//"):
        cleaned = "https:" + cleaned

    # If we have an entity id anywhere, construct the stable iframe URL directly.
    match = re.search(r"entity_id=(\d+)", cleaned, flags=re.I) or re.search(r"entityId[=:](\d+)", cleaned, flags=re.I)
    if match:
        return f"https://www.zomato.com/widgets/daily_menu.php?entity_id={match.group(1)}&width=100%25&height=1000px"

    return cleaned


def _normalize_zomato_price(value: str) -> str:
    value = normalize_zomato_text(value)
    # Zomato widgets sometimes render weird fragments around Kč. Keep the number and normalize the currency.
    match = re.search(r"\b(\d{2,4})\b", value)
    if match:
        return f"{match.group(1)} Kč"
    return value


def normalize_zomato_text(value: str) -> str:
    value = value.replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _zomato_date_matches_target(date_text: str, target_date: date) -> bool:
    """Return True when a Zomato date row is the target day.

    Zomato often renders examples like:
      Tuesday, 07 July (Today)
      Tuesday, 07 July (Dnes)

    The safest signal is the Today/Dnes marker. We also match English/Czech month
    names so tests with --date still work when the marker is not present.
    """
    value = normalize_zomato_text(date_text).casefold()
    if not value:
        return False
    if "today" in value or "dnes" in value:
        return True

    english_months = [
        "january", "february", "march", "april", "may", "june",
        "july", "august", "september", "october", "november", "december",
    ]
    czech_months = [
        "ledna", "února", "brezna", "března", "dubna", "května", "kvetna", "června", "cervna",
        "července", "cervence", "srpna", "září", "zari", "října", "rijna", "listopadu", "prosince",
    ]
    month_words = {english_months[target_date.month - 1], czech_months[target_date.month - 1]}
    day_patterns = {
        str(target_date.day),
        f"{target_date.day:02d}",
    }
    return any(day in value.split() or re.search(rf"\b{re.escape(day)}\b", value) for day in day_patterns) and any(month in value for month in month_words)


def _extract_zomato_item_line(item) -> str:
    name_el = item.select_one(".item-name")
    price_el = item.select_one(".item-price-down, .item-price, [class*=price]")
    desc_el = item.select_one(".item-description")

    name = normalize_zomato_text(name_el.get_text(" ", strip=True)) if name_el else ""
    desc = normalize_zomato_text(desc_el.get_text(" ", strip=True)) if desc_el else ""
    price = _normalize_zomato_price(price_el.get_text(" ", strip=True)) if price_el else ""

    if not name:
        return ""

    meal = name
    if desc:
        meal = f"{meal} - {desc}"
    if price:
        meal = f"{meal} {price}"
    return meal


def parse_zomato_daily_widget_html(html_text: str, target_date: date) -> list[str]:
    """Extract only today's rows from a Zomato daily-menu widget iframe.

    The previous version selected all `.inner-layer.item` elements globally, so it
    returned Tuesday plus the following days. This version first finds the date block
    for Today/Dnes or for target_date, then walks siblings until the next `.date` row.
    """
    soup = make_soup(html_text)

    date_nodes = soup.select(".date")
    selected_date = None
    selected_date_text = ""

    for node in date_nodes:
        text = normalize_zomato_text(node.get_text(" ", strip=True))
        if _zomato_date_matches_target(text, target_date):
            selected_date = node
            selected_date_text = text
            break

    rows: list[str] = []

    if selected_date is not None:
        seen_items: set[str] = set()
        for sibling in selected_date.next_siblings:
            # Stop as soon as the next date section begins.
            if getattr(sibling, "name", None):
                classes = sibling.get("class") or []
                if "date" in classes:
                    break
                if sibling.select_one(".date"):
                    break

                candidate_items = []
                sibling_classes = sibling.get("class") or []
                if "item" in sibling_classes:
                    candidate_items.append(sibling)
                candidate_items.extend(sibling.select(".inner-layer.item, div.item"))

                for item in candidate_items:
                    line = _extract_zomato_item_line(item)
                    if line and line not in seen_items:
                        seen_items.add(line)
                        rows.append(line)

        if rows:
            result = ["Daily menu"]
            if selected_date_text:
                result.append(selected_date_text)
            result.extend(rows)
            return clean_lines(result)

    # If there are no date nodes, only use global item parsing when this appears to be
    # a single-day widget. If multiple dates are present but no target match was found,
    # refuse to parse rather than returning the whole week.
    if not date_nodes:
        seen_items: set[str] = set()
        for item in soup.select(".inner-layer.item, div.item"):
            line = _extract_zomato_item_line(item)
            if line and line not in seen_items:
                seen_items.add(line)
                rows.append(line)
        if rows:
            return clean_lines(["Daily menu"] + rows)

    # Fallback for slightly different widget markup: use visible text from the iframe,
    # but only when it clearly looks like the small Zomato daily menu document.
    lines = soup_to_lines(soup)
    joined = "\n".join(lines).casefold()
    if "daily menu" not in joined and "denní menu" not in joined and "polední menu" not in joined:
        return []
    if "item-name" not in html_text and not re.search(r"\b\d{2,4}\s*(?:kč|kc|czk)\b", joined, flags=re.I):
        return []

    text_rows = parse_zomato_daily_widget_text("\n".join(lines), target_date)
    return clean_lines(text_rows)


def parse_zomato_daily_widget_text(visible_text: str, target_date: date) -> list[str]:
    """Extract today's Zomato daily menu from visible iframe text.

    The embedded Zomato widget often renders text like:
      Daily menu
      Tuesday, 07 July (Today)
      Polévka: ...
      1. 300g ...
      199 Kč
      Wednesday, 08 July

    This parser starts at "Daily menu", prefers the block marked "(Today)", and stops
    at the next weekday/date heading. It also joins standalone price lines to the
    previous meal line so the report is easier to read.
    """
    lines = clean_lines(visible_text.splitlines())
    if not lines:
        return []

    joined = "\n".join(lines)
    joined_lower = joined.casefold()
    if not any(keyword in joined_lower for keyword in ["daily menu", "denní menu", "denni menu", "polední menu", "poledni menu"]):
        return []

    # Require at least one price-like value so we do not accidentally parse a generic
    # restaurant link such as "Zomato - Denní menu Brno".
    if not re.search(r"\b\d{2,4}\s*(?:kč|kc|czk)\b|(?:czk|kč|kc)\s*\d{2,4}", joined_lower, flags=re.I):
        return []

    weekday_names = "Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|Pondělí|Pondeli|Úterý|Utery|Středa|Streda|Čtvrtek|Ctvrtek|Pátek|Patek|Sobota|Neděle|Nedele"
    date_heading_re = re.compile(rf"^(?:{weekday_names}),?\s+\d{{1,2}}\s+[A-Za-zÁ-ž]+(?:\s+\([^)]*\))?$", re.I)

    menu_index = 0
    for index, line in enumerate(lines):
        if any(keyword in line.casefold() for keyword in ["daily menu", "denní menu", "denni menu", "polední menu", "poledni menu"]):
            menu_index = index
            break

    # Prefer the date row marked as today. If it is not present, start right after the
    # Daily menu heading and still stop at the next date heading.
    today_index = None
    for index, line in enumerate(lines[menu_index:], start=menu_index):
        if "(today)" in line.casefold() or "(dnes)" in line.casefold() or _zomato_date_matches_target(line, target_date):
            today_index = index
            break

    start_index = menu_index if today_index is None else max(menu_index, today_index - 1)

    stop_words = [
        "book a table", "write a review", "add restaurant", "login", "log in",
        "sign up", "privacy", "cookies",
    ]

    section: list[str] = []
    seen_today_heading = False
    for line in lines[start_index:]:
        value = line.casefold().strip()
        if not value:
            continue
        if section and any(stop in value for stop in stop_words):
            break

        if date_heading_re.match(line.strip()):
            if "(today)" in value or "(dnes)" in value or not seen_today_heading:
                seen_today_heading = True
            elif seen_today_heading:
                break

        if value in {"share", "photos", "menu", "reviews", "overview", "the restaurant guide", "zomato"}:
            continue
        section.append(line)

    section = clean_lines(section)

    # Join standalone price lines to the previous row.
    compacted: list[str] = []
    price_re = re.compile(r"^\d{2,4}\s*(?:Kč|KC|kc|CZK|czk)$")
    for line in section[:60]:
        line = normalize_zomato_text(line)
        if price_re.match(line) and compacted:
            if not re.search(r"\b\d{2,4}\s*(?:Kč|KC|kc|CZK|czk)\b", compacted[-1]):
                compacted[-1] = f"{compacted[-1]} - {line}"
            else:
                compacted.append(line)
        else:
            compacted.append(line)

    return clean_lines(compacted)



def fetch_zomato_daily_widget_with_referer(widget_url: str, referer_url: str, timeout_seconds: int) -> str:
    """Fetch the Zomato iframe with browser-like HTTP/1.1 requests.

    Chromium/Playwright can sometimes fail on Zomato with ERR_HTTP2_PROTOCOL_ERROR.
    Python requests uses HTTP/1.1, and adding a proper Referer/Sec-Fetch context makes
    the iframe request much closer to the request a normal embedded browser frame sends.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.8",
        "Referer": referer_url,
        "Sec-Fetch-Dest": "iframe",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "cross-site",
        "Upgrade-Insecure-Requests": "1",
    }
    response = requests.get(widget_url, headers=headers, timeout=timeout_seconds)
    response.raise_for_status()
    return response.text


def parse_zomato_from_parent_iframe_dom(page_url: str, target_date: date, timeout_seconds: int) -> tuple[list[str], str]:
    """Read the Zomato menu from the iframe embedded in the official restaurant page.

    This deliberately does not navigate to the iframe URL directly. Instead it opens the
    restaurant page, waits for the iframe element, obtains its Playwright Frame object,
    and reads either structured menu items or visible text from inside that frame.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is not installed. Run: pip install playwright && python -m playwright install chromium"
        ) from exc

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                # Helps with Zomato/Chromium ERR_HTTP2_PROTOCOL_ERROR on some networks.
                "--disable-http2",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        try:
            context = browser.new_context(
                locale="cs-CZ",
                viewport={"width": 1365, "height": 1200},
                extra_http_headers={
                    "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.8",
                },
            )
            page = context.new_page()
            page.goto(page_url, wait_until="domcontentloaded", timeout=timeout_seconds * 1000)

            # Scroll through the page; the Zomato widget is usually lazy-created only
            # when the Týdenní menu section is near the viewport.
            for y in [0, 400, 800, 1200, 1600, 2200, 3000]:
                try:
                    page.evaluate("y => window.scrollTo(0, y)", y)
                    page.wait_for_timeout(500)
                except Exception:
                    pass

            # Prefer iframe elements whose src is the generated daily_menu.php document.
            # If the src is not available yet, still inspect all iframes, because the
            # frame URL/content may be populated after the element is created.
            deadline_ms = 12000
            elapsed = 0
            step_ms = 500
            last_url = ""
            while elapsed <= deadline_ms:
                iframe_handles = page.locator("iframe").element_handles()
                for handle in iframe_handles:
                    try:
                        src = handle.get_attribute("src") or ""
                    except Exception:
                        src = ""
                    last_url = src or last_url
                    if src and "zomato" not in src.casefold() and "daily_menu" not in src.casefold():
                        continue
                    try:
                        frame = handle.content_frame()
                    except Exception:
                        frame = None
                    if not frame:
                        continue

                    frame_url = frame.url or src or page_url
                    try:
                        html_text = frame.content()
                        rows = parse_zomato_daily_widget_html(html_text, target_date)
                        if rows:
                            return rows, frame_url
                    except Exception:
                        pass

                    try:
                        text = frame.locator("body").inner_text(timeout=1500)
                    except Exception:
                        try:
                            text = frame.evaluate("() => document.body ? document.body.innerText : ''") or ""
                        except Exception:
                            text = ""
                    rows = parse_zomato_daily_widget_text(text, target_date)
                    if rows:
                        return rows, frame_url

                    # Last structured JS attempt. This is robust when text spacing is odd.
                    try:
                        js_rows = frame.evaluate(
                            """
                            () => Array.from(document.querySelectorAll('.inner-layer.item, div.item')).map(item => {
                                const name = item.querySelector('.item-name')?.innerText?.trim() || '';
                                const desc = item.querySelector('.item-description')?.innerText?.trim() || '';
                                const price = item.querySelector('.item-price-down, .item-price, [class*=price]')?.innerText?.trim() || '';
                                return {name, desc, price};
                            }).filter(row => row.name)
                            """
                        )
                        compacted = []
                        for row in js_rows or []:
                            name = normalize_zomato_text(row.get("name", ""))
                            desc = normalize_zomato_text(row.get("desc", ""))
                            price = _normalize_zomato_price(row.get("price", ""))
                            if not name:
                                continue
                            line = name
                            if desc:
                                line += f" - {desc}"
                            if price:
                                line += f" - {price}"
                            compacted.append(line)
                        if compacted:
                            return clean_lines(["Daily menu"] + compacted), frame_url
                    except Exception:
                        pass

                # Also inspect page.frames; sometimes content_frame() is missing but the
                # frame is available in page.frames.
                for frame in page.frames:
                    if frame == page.main_frame:
                        continue
                    frame_url = frame.url or ""
                    last_url = frame_url or last_url
                    if frame_url and "zomato" not in frame_url.casefold() and "daily_menu" not in frame_url.casefold():
                        continue
                    try:
                        text = frame.locator("body").inner_text(timeout=1500)
                    except Exception:
                        text = ""
                    rows = parse_zomato_daily_widget_text(text, target_date)
                    if rows:
                        return rows, frame_url or page_url
                    try:
                        html_text = frame.content()
                    except Exception:
                        html_text = ""
                    rows = parse_zomato_daily_widget_html(html_text, target_date)
                    if rows:
                        return rows, frame_url or page_url

                page.wait_for_timeout(step_ms)
                elapsed += step_ms

            raise RuntimeError(f"Zomato iframe was found but no menu rows were readable. Last iframe URL/src: {last_url}")
        finally:
            browser.close()

ZOMATO_DAILY_MENU_OVERRIDES = {
    # Confirmed from the U Dřevěného orla DevTools screenshot.
    "orel": "https://www.zomato.com/widgets/daily_menu.php?entity_id=16506896&width=100%25&height=1000px",
    # Confirmed from the U Dřevěného vlka DevTools screenshot.
    "vlk": "https://www.zomato.com/widgets/daily_menu.php?entity_id=16507597&width=100%25&height=1000px",
}

KNOWN_VLK_ZOMATO_ENTITY_ID = "16507597"


def _zomato_entity_id_from_url(url: str) -> str:
    match = re.search(r"[?&]entity_id=(\d+)", url or "")
    return match.group(1) if match else ""


def has_explicit_zomato_override(restaurant: Restaurant) -> bool:
    key = (restaurant.key or "").upper()
    return bool(
        key
        and (
            os.getenv(f"ZOMATO_{key}_URL", "").strip()
            or os.getenv(f"ZOMATO_{key}_ENTITY_ID", "").strip()
        )
    )


def is_rejected_zomato_source_for_restaurant(restaurant: Restaurant, source_url: str) -> bool:
    """Avoid silently using the Vlk Zomato entity for Orel.

    We temporarily used entity_id=16507597 for both restaurants, which produced
    identical Orel/Vlk results. The Vlk screenshot confirms that 16507597 belongs
    to the Vlk page. Until Orel's real entity id is confirmed, do not accept that
    source for Orel automatically. Users can still explicitly force it via .env if
    both restaurants really do share the same source.
    """
    if restaurant.key != "orel":
        return False
    if has_explicit_zomato_override(restaurant):
        return False
    return _zomato_entity_id_from_url(source_url) == KNOWN_VLK_ZOMATO_ENTITY_ID


def get_zomato_override_for_restaurant(restaurant: Restaurant) -> str:
    """Return a hardcoded or .env-provided Zomato widget URL for a restaurant.

    You can override any restaurant explicitly in .env, for example:
      ZOMATO_OREL_ENTITY_ID=12345678
      ZOMATO_OREL_URL=https://www.zomato.com/widgets/daily_menu.php?entity_id=12345678&width=100%25&height=1000px
      ZOMATO_VLK_ENTITY_ID=16507597
    """
    key = (restaurant.key or "").upper()
    configured_url = os.getenv(f"ZOMATO_{key}_URL", "").strip() if key else ""
    if configured_url:
        return normalize_zomato_widget_url(configured_url)

    configured_entity_id = os.getenv(f"ZOMATO_{key}_ENTITY_ID", "").strip() if key else ""
    if configured_entity_id:
        return normalize_zomato_widget_url(f"https://www.zomato.com/widgets/daily_menu.php?entity_id={configured_entity_id}")

    return ZOMATO_DAILY_MENU_OVERRIDES.get(restaurant.key or "", "")




def build_zomato_success(restaurant: Restaurant, lines: list[str], source_url: str, note: str) -> MenuResult | None:
    if is_rejected_zomato_source_for_restaurant(restaurant, source_url):
        return None
    return restaurant.success(lines, source_url=source_url, note=note)

def parse_zomato_iframe_or_external(restaurant: Restaurant, target_date: date, timeout_seconds: int) -> MenuResult:
    """Parse restaurants whose official page embeds a Zomato daily-menu iframe.

    Dřevěný Orel/Vlk use a Zomato widget. The important detail is that the public
    restaurant page may contain only the loader URL /widgets/daily_menu_widget.
    That loader is not the menu HTML and can fail with HTTP2 errors when opened
    directly. The real menu is the generated iframe URL /widgets/daily_menu.php?entity_id=...
    or the iframe content exposed through the rendered page.
    """
    fetched = fetch_html(
        restaurant.url,
        timeout_seconds=timeout_seconds,
        allow_playwright_fallback=True,
    )
    soup = make_soup(fetched.text)
    last_error = ""

    def add_unique(items: list[str], value: str) -> None:
        value = normalize_zomato_widget_url(value)
        if value and value not in items:
            items.append(value)

    iframe_links: list[str] = []
    override_url = get_zomato_override_for_restaurant(restaurant)
    if override_url:
        add_unique(iframe_links, override_url)

    for url in find_iframe_links(fetched.url, soup, keywords=["zomato", "daily_menu", "daily-menu", "dailymenu", "entity_id"]):
        add_unique(iframe_links, url)
    for url in find_zomato_widget_urls_from_html(fetched.url, fetched.text):
        add_unique(iframe_links, url)

    # 0) First try the discovered/hardcoded iframe URLs with plain requests + Referer.
    # This avoids Playwright/Chromium HTTP2 problems on Zomato.
    for iframe_url in list(dict.fromkeys(iframe_links)):
        if looks_like_zomato_widget_loader_url(iframe_url):
            continue
        if not looks_like_zomato_daily_menu_url(iframe_url):
            continue
        try:
            iframe_html = fetch_zomato_daily_widget_with_referer(iframe_url, restaurant.url, timeout_seconds)
            lines = parse_zomato_daily_widget_html(iframe_html, target_date)
            if lines:
                result = build_zomato_success(
                    restaurant,
                    lines,
                    iframe_url,
                    "Extracted from Zomato daily-menu iframe using HTTP/1.1 + Referer.",
                )
                if result:
                    return result
                last_error = f"Skipped likely wrong shared Zomato source for {restaurant.name}: {iframe_url}"
        except Exception as exc:
            last_error = str(exc)

    # 0b) Then read the iframe DOM from the parent restaurant page. This is the path
    # that matches what you can see in DevTools: the actual menu is inside the iframe.
    try:
        lines, frame_url = parse_zomato_from_parent_iframe_dom(restaurant.url, target_date, timeout_seconds)
        if lines:
            source_url = frame_url or restaurant.url
            result = build_zomato_success(
                restaurant,
                lines,
                source_url,
                "Extracted from the Zomato iframe DOM embedded in the restaurant page.",
            )
            if result:
                return result
            last_error = f"Skipped likely wrong shared Zomato source for {restaurant.name}: {source_url}"
    except Exception as exc:
        last_error = str(exc)

    # 1) Most reliable for these pages: render the official page and read visible text
    # from embedded iframes. This matches what you see in DevTools: the Zomato iframe
    # contains "Daily menu" followed by the actual meals. It avoids opening the
    # Zomato URL directly, which can fail with ERR_HTTP2_PROTOCOL_ERROR.
    try:
        for frame_result in fetch_playwright_frame_texts(restaurant.url, timeout_seconds, wait_seconds=8.0):
            lines = parse_zomato_daily_widget_text(frame_result.text, target_date)
            if lines:
                source_url = frame_result.url or restaurant.url
                result = build_zomato_success(
                    restaurant,
                    lines,
                    source_url,
                    "Extracted from visible text inside the rendered Zomato iframe.",
                )
                if result:
                    return result
                last_error = f"Skipped likely wrong shared Zomato source for {restaurant.name}: {source_url}"
    except Exception as exc:
        last_error = str(exc)

    # 2) Also render the official page and collect the actual iframe src values that
    # the widget creates. If we discover a stable daily_menu.php?entity_id=... URL,
    # try that as a secondary path.
    try:
        for url in find_rendered_iframe_sources(restaurant.url, timeout_seconds, wait_seconds=7.0):
            add_unique(iframe_links, urljoin(restaurant.url, url))
    except Exception as exc:
        last_error = str(exc)

    # 3) Try each discovered final iframe URL. Skip the loader URL; it is not parseable menu HTML.
    for iframe_url in list(dict.fromkeys(iframe_links)):
        if looks_like_zomato_widget_loader_url(iframe_url):
            continue
        if not looks_like_zomato_daily_menu_url(iframe_url):
            continue
        try:
            iframe_html = fetch_zomato_daily_widget_with_referer(iframe_url, restaurant.url, timeout_seconds)
            lines = parse_zomato_daily_widget_html(iframe_html, target_date)
            if lines:
                result = build_zomato_success(
                    restaurant,
                    lines,
                    iframe_url,
                    "Extracted from Zomato daily-menu iframe URL using HTTP/1.1 + Referer.",
                )
                if result:
                    return result
                last_error = f"Skipped likely wrong shared Zomato source for {restaurant.name}: {iframe_url}"
        except Exception as exc:
            last_error = str(exc)

    # 4) If iframe HTML content is already available from Playwright frames, parse that.
    # Parse every frame that looks like a Zomato daily menu by content, even when the
    # frame URL is about:blank or a loader URL. The visible DevTools DOM shows the
    # useful classes (.item-name/.item-price-down) inside the iframe itself.
    try:
        for frame_result in fetch_html_playwright_frames(restaurant.url, timeout_seconds):
            frame_url = normalize_zomato_widget_url(frame_result.url)
            lines = parse_zomato_daily_widget_html(frame_result.text, target_date)
            if lines:
                source_url = frame_url or restaurant.url
                result = build_zomato_success(
                    restaurant,
                    lines,
                    source_url,
                    "Extracted from rendered iframe HTML content.",
                )
                if result:
                    return result
                last_error = f"Skipped likely wrong shared Zomato source for {restaurant.name}: {source_url}"

            if looks_like_zomato_widget_loader_url(frame_url) or not looks_like_zomato_daily_menu_url(frame_url):
                continue

            # If we saw the right frame URL but could not read the content, fetch the URL directly.
            try:
                iframe_html = fetch_zomato_daily_widget_with_referer(frame_url, restaurant.url, timeout_seconds)
                lines = parse_zomato_daily_widget_html(iframe_html, target_date)
                if lines:
                    result = build_zomato_success(
                        restaurant,
                        lines,
                        frame_url,
                        "Extracted from rendered Zomato frame URL using HTTP/1.1 + Referer.",
                    )
                    if result:
                        return result
                    last_error = f"Skipped likely wrong shared Zomato source for {restaurant.name}: {frame_url}"
            except Exception as exc:
                last_error = str(exc)
    except Exception as exc:
        last_error = str(exc)

    # 5) Fallback: inspect merged rendered HTML for URLs/entity IDs.
    try:
        rendered = fetch_html_playwright_all_frames(restaurant.url, timeout_seconds)
        for iframe_url in find_zomato_widget_urls_from_html(rendered.url, rendered.text):
            iframe_url = normalize_zomato_widget_url(iframe_url)
            if looks_like_zomato_widget_loader_url(iframe_url):
                continue
            if not looks_like_zomato_daily_menu_url(iframe_url):
                continue
            iframe_html = fetch_zomato_daily_widget_with_referer(iframe_url, restaurant.url, timeout_seconds)
            lines = parse_zomato_daily_widget_html(iframe_html, target_date)
            if lines:
                result = build_zomato_success(
                    restaurant,
                    lines,
                    iframe_url,
                    "Extracted from Zomato iframe URL found after rendering using HTTP/1.1 + Referer.",
                )
                if result:
                    return result
                last_error = f"Skipped likely wrong shared Zomato source for {restaurant.name}: {iframe_url}"
    except Exception as exc:
        last_error = str(exc)

    links = find_external_links(fetched.url, soup, keywords=["zomato"])
    if iframe_links or links:
        useful_sources = [u for u in iframe_links if not looks_like_zomato_widget_loader_url(u)] or links or iframe_links
        source = useful_sources[0]
        return restaurant.failure(
            "Found a Zomato menu source, but the generated daily-menu iframe could not be parsed automatically.",
            note=(
                f"Source found: {source}. Last iframe error: {last_error}. "
                "If you can see an iframe src like daily_menu.php?entity_id=... in DevTools, "
                "paste that full URL and we can hardcode it as a stable fallback."
            ).strip(),
            screenshot_url=source,
        )

    return restaurant.failure(
        "Could not find a parseable Zomato iframe or external menu source.",
        note=last_error,
        screenshot_url=fetched.url,
    )

def parse_external_only(restaurant: Restaurant, target_date: date, timeout_seconds: int) -> MenuResult:
    fetched = fetch_html(
        restaurant.url,
        timeout_seconds=timeout_seconds,
        allow_playwright_fallback=restaurant.allow_playwright_fallback,
    )
    soup = make_soup(fetched.text)
    links = find_external_links(fetched.url, soup, keywords=["zomato"])
    if links:
        return restaurant.failure(
            "The official page does not contain the actual menu text; it links to an external menu source.",
            note=f"External source found: {links[0]}",
            screenshot_url=links[0],
        )
    return restaurant.failure("The official page does not contain parseable menu text.", screenshot_url=fetched.url)


def parse_buddha(restaurant: Restaurant, target_date: date, timeout_seconds: int) -> MenuResult:
    fetched = fetch_html(
        restaurant.url,
        timeout_seconds=timeout_seconds,
        allow_playwright_fallback=restaurant.allow_playwright_fallback,
    )
    soup = make_soup(fetched.text)
    lines = soup_to_lines(soup)

    # Try normal text first, because they may later publish the daily menu as HTML.
    section = extract_current_date_section(lines, target_date) or extract_today_weekday_section(lines, target_date)
    if section:
        return restaurant.success(section, source_url=fetched.url, note="Matched today's date/weekday section in top-level HTML.")

    # Important fallback: the menu can be inside an iframe. A requests/BeautifulSoup fetch only sees
    # the parent document; it does not automatically include iframe documents. Render the page and
    # merge text/HTML from page.frames before trying the same extraction again.
    rendered_error = ""
    try:
        rendered = fetch_html_playwright_all_frames(restaurant.url, timeout_seconds)
        rendered_soup = make_soup(rendered.text)
        rendered_lines = soup_to_lines(rendered_soup)
        section = extract_current_date_section(rendered_lines, target_date) or extract_today_weekday_section(rendered_lines, target_date)
        if section:
            return restaurant.success(
                section,
                source_url=rendered.url,
                note="Matched today's date/weekday section from rendered page/iframe content.",
            )
        # Continue with the rendered soup, because iframe documents may contain PDF links too.
        soup = rendered_soup
        fetched = rendered
    except Exception as exc:
        rendered_error = str(exc)

    # Then try linked PDFs, if they ever add one.
    pdf_links = find_pdf_links(fetched.url, soup)
    for pdf_url in pdf_links:
        try:
            pdf_text = extract_pdf_text_from_url(pdf_url, timeout_seconds)
            pdf_lines = clean_lines(pdf_text.splitlines())
            section = extract_current_date_section(pdf_lines, target_date) or extract_today_weekday_section(pdf_lines, target_date)
            if section:
                return restaurant.success(section, source_url=pdf_url, note="Extracted from linked PDF.")
        except Exception:
            # Screenshot fallback below is more useful than failing the whole parser.
            pass

    note = "Screenshot fallback can capture the visual menu. OCR can be added later if you want text extraction from an image."
    if rendered_error:
        note += f" Rendered iframe fallback failed: {rendered_error}"

    return restaurant.failure(
        "Could not find today's Buddha menu in top-level HTML, iframe text, or linked PDFs.",
        note=note,
        screenshot_url=fetched.url,
    )



def _repair_czech_mojibake(value: str) -> str:
    """Repair common UTF-8-as-Latin1 mojibake seen on brnorestaurace.cz.

    Example: ``NA KNOFLÃKU`` -> ``NA KNOFLÍKU``.
    If the text is already correct, return it unchanged.
    """
    if not value:
        return value

    # Only try the repair when the text contains typical mojibake markers.
    if not any(marker in value for marker in ("Ã", "Â", "Ä", "Å", "Ă")):
        return value

    candidates = [value]
    for encoding in ("latin1", "cp1252"):
        try:
            candidates.append(value.encode(encoding).decode("utf-8"))
        except Exception:
            pass

    good_words = [
        "Úterý", "Utery", "Týdenní", "Tydenni", "Polévka", "Polevka",
        "Knoﬂ", "Knofl", "KNOFLÍKU", "KNOFLIKU", "jítra", "ryže",
        "čerstvou", "kuřecím", "brambor", "Kč",
    ]
    bad_markers = ["Ã", "Â", "Ä", "Å", "Ă", "�"]

    def score(text: str) -> int:
        return sum(5 for word in good_words if word.casefold() in text.casefold()) - sum(text.count(marker) * 3 for marker in bad_markers)

    return max(candidates, key=score)


def _knofliku_text(value: str) -> str:
    return normalize_whitespace(_repair_czech_mojibake(value).replace("Kc", "Kč"))


def _parse_knofliku_rows_from_html(html_text: str, target_date: date) -> list[str]:
    """Extract today's menu from Na Knoflíku's weekly menu table.

    The page shown in DevTools uses ``table.dmenu``. Each day starts with a weekday
    row such as ``Úterý`` and each food row has a price cell like ``142 Kč``. Older
    versions accidentally parsed a layout/marketing table; this version only accepts
    rows that contain real prices inside the target weekday block.
    """
    html_text = _repair_czech_mojibake(html_text)
    soup = make_soup(html_text)

    target_weekday_cmp = comparable(CZECH_WEEKDAYS[target_date.weekday()])
    weekday_cmps = {comparable(day) for day in CZECH_WEEKDAYS}
    price_re = re.compile(r"\b\d{1,4}\s*(?:Kč|Kc|CZK)\b", re.I)

    def row_weekday(text: str) -> str:
        cmp_value = comparable(_knofliku_text(text))
        # The weekday header should be short: "Úterý" or occasionally "Úterý 7.7.".
        if len(cmp_value.split()) > 4:
            return ""
        for weekday_cmp in weekday_cmps:
            if cmp_value == weekday_cmp or cmp_value.startswith(weekday_cmp + " "):
                return weekday_cmp
        return ""

    def clean_price(value: str) -> str:
        value = _knofliku_text(value)
        match = price_re.search(value)
        return match.group(0).replace("Kc", "Kč") if match else ""

    def price_count(rows: list[str]) -> int:
        return sum(1 for row in rows if price_re.search(row))

    def parse_table(table) -> list[str]:
        in_target_day = False
        found_target_header = False
        rows: list[str] = []
        next_number = 1

        for tr in table.find_all("tr"):
            cells = tr.find_all(["td", "th"], recursive=False)
            if not cells:
                cells = tr.find_all(["td", "th"])

            raw_row_text = _knofliku_text(tr.get_text(" ", strip=True))
            if not raw_row_text:
                continue

            current_weekday = row_weekday(raw_row_text)
            if current_weekday:
                if found_target_header and current_weekday != target_weekday_cmp:
                    break
                in_target_day = current_weekday == target_weekday_cmp
                found_target_header = in_target_day
                next_number = 1
                continue

            if not in_target_day:
                continue

            # Only accept actual menu rows with a price. This prevents layout/header/
            # marketing text from being treated as a successful meal result.
            if not price_re.search(raw_row_text):
                continue

            cell_texts = [_knofliku_text(cell.get_text(" ", strip=True)) for cell in cells]
            # Keep empty cells, because the first cell can be blank for numbered meals.
            if len(cell_texts) < 2:
                continue

            price = ""
            price_index = None
            for i in range(len(cell_texts) - 1, -1, -1):
                found_price = clean_price(cell_texts[i])
                if found_price:
                    price = found_price
                    price_index = i
                    break
            if not price:
                continue

            # Remove the price from the price cell and build the non-price content.
            content_cells: list[str] = []
            for i, text in enumerate(cell_texts):
                if i == price_index:
                    text = price_re.sub("", text).strip(" :-")
                content_cells.append(_knofliku_text(text))

            label = content_cells[0].strip() if content_cells else ""
            description_parts = [text for text in content_cells[1:] if text]

            # If the first cell is empty, infer numbering for normal meal rows. Soups
            # normally have labels like "Polévka 1:" and do not advance the meal number.
            if label:
                label = re.sub(r"^(\d+)\s*[:.)-]?\s*$", r"\1.", label)
                label = re.sub(r"^(Pol[eé]vka\s*\d*)\s*[:.)-]?\s*$", r"\1:", label, flags=re.I)
            else:
                label = f"{next_number}."

            description = _knofliku_text(" ".join(description_parts))
            if not description:
                continue

            if label[:1].isdigit():
                next_number += 1

            line = f"{label} {description} — {price}".strip()
            rows.append(line)

        return rows

    # Strong preference: the actual menu table from the screenshot.
    candidate_tables = []
    for selector in ("#col1 table.dmenu", "table.dmenu", ".content-tydenni-menu table.dmenu", ".content-tydenni-menu table"):
        for table in soup.select(selector):
            if table not in candidate_tables:
                candidate_tables.append(table)

    # Last resort: any table that contains weekday headings and multiple prices.
    for table in soup.find_all("table"):
        table_text = _knofliku_text(table.get_text(" ", strip=True))
        if table not in candidate_tables and any(day in comparable(table_text) for day in weekday_cmps) and len(price_re.findall(table_text)) >= 3:
            candidate_tables.append(table)

    best_rows: list[str] = []
    for table in candidate_tables:
        rows = parse_table(table)
        if price_count(rows) > price_count(best_rows):
            best_rows = rows

    if price_count(best_rows) >= 2:
        return best_rows

    # Sidebar fallback: the right box headed "Dnešní menu". Use only if it has prices.
    page_lines = [_knofliku_text(line) for line in soup_to_lines(soup)]
    sidebar = extract_between_keywords(page_lines, ["Dnešní menu", "Dnesni menu"], ["Kontakty", "Kontakt", "Emailová adresa"])
    cleaned: list[str] = []
    for line in sidebar:
        line = _knofliku_text(line)
        if not line or comparable(line) == "dnesni menu":
            continue
        match = price_re.search(line)
        if not match:
            continue
        price = match.group(0).replace("Kc", "Kč")
        meal = _knofliku_text(price_re.sub("", line).strip(" :-"))
        if meal:
            cleaned.append(f"{meal} — {price}")

    if price_count(cleaned) >= 2:
        return cleaned

    return []


def parse_knofliku(restaurant: Restaurant, target_date: date, timeout_seconds: int) -> MenuResult:
    """Parse Na Knoflíku / brnorestaurace.cz weekly menu table."""
    fetch_errors: list[str] = []
    attempts: list[tuple[str, FetchResult]] = []

    # Requests can occasionally receive a different/broken version of this site, while a
    # real browser shows the correct weekly table. Try both and parse whichever contains it.
    try:
        fetched = fetch_html(restaurant.url, timeout_seconds=timeout_seconds, allow_playwright_fallback=False)
        attempts.append(("normal HTTP", fetched))
    except Exception as exc:
        fetch_errors.append(f"normal HTTP failed: {exc}")

    try:
        rendered = fetch_html_playwright_all_frames(restaurant.url, timeout_seconds)
        attempts.append(("rendered browser page", rendered))
    except Exception as exc:
        fetch_errors.append(f"rendered browser page failed: {exc}")

    for source_name, fetched in attempts:
        rows = _parse_knofliku_rows_from_html(fetched.text, target_date)
        if rows:
            return restaurant.success(
                rows,
                source_url=fetched.url,
                note=f"Extracted today's Na Knoflíku rows from the {source_name} weekly-menu table.",
            )

    # Do not fall back to the generic parser here. This site has large layout tables
    # and marketing text; a generic weekday match can look "successful" while not
    # containing any meals. Failing loudly is safer than posting unrelated text.

    extra = " ".join(fetch_errors).strip()
    return restaurant.failure(
        "Could not find today's Na Knoflíku menu table.",
        note=(
            "This parser expects the page structure visible in DevTools: table.dmenu with "
            "weekday header rows such as Úterý followed by meal rows. " + extra
        ).strip(),
        screenshot_url=restaurant.url,
    )


def _lafamiglia_clean_section(section: list[str]) -> list[str]:
    """Normalize La Famiglia daily section.

    The site exposes the weekly lunch menu as plain text with a date heading,
    a weekday heading, then soup + category/title/description/price fragments.
    Keep the fragments in their natural order so structured_menu_rows can join
    category/title/description/price into one row.
    """
    cleaned: list[str] = []
    previous = ""
    for line in clean_lines(section):
        line = normalize_whitespace(line)
        if not line or line == previous:
            continue
        c_line = comparable(line)
        if c_line in {
            "la famiglia",
            "poledni menu",
            "poledni menu la famiglia",
            "nase",
        }:
            continue
        if "doplňkové nabídky" in line.casefold() or "doplnkove nabidky" in c_line:
            break
        if "autenticke italske recepty" in c_line:
            break
        previous = line
        cleaned.append(line)

    # The first line after the weekday is the soup. Add a clear category so the
    # report stays readable even though the soup usually has no separate price.
    if cleaned and "polevka" not in comparable(cleaned[0]) and "soup" not in comparable(cleaned[0]):
        cleaned.insert(0, "Polévka")
    return cleaned




def _ucertu_clean_section(section: list[str]) -> list[str]:
    """Clean U Třech Čertů daily-menu rows.

    The page contains a full a-la-carte menu before the lunch menu and a weekday
    navigation list inside the lunch section. This parser keeps only the selected
    weekday section from the daily menu and removes allergen-only rows.

    The soup is rendered as two separate DOM lines ("Polévka" then "- <name>" on the
    next line), not "Polévka - <name>" on one line. Header insertion has to track a
    small state machine: once the soup name has been consumed, the next real content
    line is the first main course, and "Hlavní chody" must be inserted right before
    it (inserting it only once a Kč price is seen, as the old code did, put the
    header after the first dish's name instead of before it, and left that dish's
    price stranded with nothing to attach to).
    """
    cleaned: list[str] = []
    previous = ""
    soup_added = False
    soup_name_added = False
    main_added = False

    for line in clean_lines(section):
        line = normalize_whitespace(html.unescape(line))
        if not line or line == previous:
            continue

        # Remove allergen-only rows such as "1,3,7,10".
        if re.fullmatch(r"[\d,\s]+", line):
            continue

        # Remove layout/menu leftovers.
        c_line = comparable(line)
        if c_line in {"denni menu", "pondeli", "utery", "streda", "ctvrtek", "patek", "sobota", "nedele"}:
            continue

        # Meal rows sometimes start with just ')' due to the site's markup.
        line = re.sub(r"^\)\s*", "", line).strip()
        if not line:
            continue
        c_line = comparable(line)

        if c_line.startswith("polevka") and not soup_added:
            cleaned.append("Polévka")
            soup_added = True
            # Some pages still write "Polévka - Name" on one line; use it directly.
            same_line_name = re.sub(r"^pol[eé]vka\s*\d?\s*[-:]\s*", "", line, flags=re.I).strip()
            if same_line_name and comparable(same_line_name) != "polevka":
                cleaned.append(same_line_name)
                soup_name_added = True
            previous = line
            continue

        if soup_added and not soup_name_added:
            # The soup name is its own DOM line, e.g. "- Gulášová z mletým masem a bramborem".
            soup_name = re.sub(r"^[-\s]+", "", line).strip()
            if soup_name:
                cleaned.append(soup_name)
                soup_name_added = True
                previous = line
                continue

        if not main_added and (soup_name_added or re.search(r"\b\d{2,4}\s*Kč\b", line, flags=re.I)):
            cleaned.append("Hlavní chody")
            main_added = True

        previous = line
        cleaned.append(line)

    return cleaned


def parse_ucertu_dvorakova(restaurant: Restaurant, target_date: date, timeout_seconds: int) -> MenuResult:
    """Parse the daily menu on U Třech Čertů Dvořákova.

    Generic weekday extraction can hit the weekday navigation or the full menu
    before the actual daily-menu section. This parser starts at the "Denní menu"
    block and then chooses the real weekday section that contains Kč prices.
    """
    source_url, lines = html_lines(restaurant, timeout_seconds)
    target_weekday = CZECH_WEEKDAYS[target_date.weekday()]
    start_index = 0

    for index, line in enumerate(lines):
        if comparable(line) == "denni menu":
            start_index = index + 1
            break

    candidates: list[list[str]] = []
    search_lines = lines[start_index:]
    for index, line in enumerate(search_lines):
        if not comparable(line).startswith(comparable(target_weekday)):
            continue

        section: list[str] = []
        for next_line in search_lines[index + 1:]:
            if comparable(next_line) == "rezervace":
                break
            if any(comparable(next_line).startswith(comparable(day)) for day in CZECH_WEEKDAYS):
                break
            section.append(next_line)

        cleaned = _ucertu_clean_section(section)
        if cleaned and any(re.search(r"\b\d{2,4}\s*Kč\b", item, flags=re.I) for item in cleaned):
            candidates.append(cleaned)

    if candidates:
        return restaurant.success(max(candidates, key=lambda item: sum(len(x) for x in item)), source_url=source_url)

    # Fallback to the generic logic, but keep the failure specific.
    section = extract_today_weekday_section(lines[start_index:] or lines, target_date)
    cleaned = _ucertu_clean_section(section)
    if cleaned and any(re.search(r"\b\d{2,4}\s*Kč\b", item, flags=re.I) for item in cleaned):
        return restaurant.success(cleaned, source_url=source_url)

    return restaurant.failure(
        "Could not find today's U Třech Čertů daily-menu section.",
        note="The page still has a daily-menu block, but the weekday section could not be parsed.",
        screenshot_url=source_url,
    )


def parse_lafamiglia(restaurant: Restaurant, target_date: date, timeout_seconds: int) -> MenuResult:
    source_url, lines = html_lines(restaurant, timeout_seconds)

    section = extract_current_date_section(lines, target_date)
    note = "Matched today's date section."

    if not section:
        section = extract_today_weekday_section(lines, target_date)
        note = "Matched today's weekday section."

    if not section:
        return restaurant.failure(
            "Could not find today's La Famiglia menu section.",
            note="This parser expects the date/weekday sections on the Polední menu page.",
            screenshot_url=source_url,
        )

    cleaned = _lafamiglia_clean_section(section)
    if not cleaned or not any(re.search(r"\b\d{2,4}\s*Kč\b", line, flags=re.I) for line in cleaned):
        return restaurant.failure(
            "Could not extract La Famiglia meal rows.",
            note="The page was found, but no Kč-priced menu rows were detected.",
            screenshot_url=source_url,
        )

    return restaurant.success(cleaned, source_url=source_url, note=note)




def _static_image_result(restaurant: Restaurant, image_filename: str) -> MenuResult:
    image_path = (Path(__file__).resolve().parent / "assets" / image_filename).resolve()
    result = restaurant.success([], source_url=restaurant.url or None, note="")
    image_uri = image_path.as_uri()
    result.screenshot_path = image_uri
    result.screenshot_paths = [image_uri]
    return result


def parse_parodie_static_image(restaurant: Restaurant, target_date: date, timeout_seconds: int) -> MenuResult:
    return _static_image_result(restaurant, "parodie.png")


def parse_diandi_static_image(restaurant: Restaurant, target_date: date, timeout_seconds: int) -> MenuResult:
    return _static_image_result(restaurant, "diandi.png")

def _teatr_clean_section(section: list[str]) -> list[str]:
    """Normalize Restaurant Teátr daily menu section.

    The Teátr page exposes the whole week as text. Each day contains a soup,
    allergen-only rows like "A: 1 3 7", and numbered meals followed by prices.
    We remove allergen rows and add simple category labels so the PDF/Slack
    formatter can keep soup and main courses readable.
    """
    cleaned: list[str] = []
    previous = ""
    soup_category_added = False
    main_category_added = False

    stop_markers = (
        "nebo si vyberte",
        "obedova nabidka plati",
        "obědová nabídka platí",
        "seznam alergenu",
        "seznam alergenů",
        "adresa",
        "telefon",
        "e-mail",
    )

    for line in clean_lines(section):
        line = normalize_whitespace(html.unescape(line))
        if not line or line == previous:
            continue

        c_line = comparable(line)
        if any(marker in c_line for marker in stop_markers):
            break

        # Remove allergen-only rows such as "A: 1 3 7 12".
        if re.fullmatch(r"A:\s*[\d\s]+", line, flags=re.I):
            continue

        # Skip day/date metadata if extraction included it.
        if any(comparable(day) == c_line for day in CZECH_WEEKDAYS):
            continue
        if re.fullmatch(r"\d{1,2}\.\s*\d{1,2}\.\s*\d{4}", line):
            continue

        # First useful non-price/non-numbered line is the soup.
        if not soup_category_added and not re.match(r"^\d+\.", line) and not re.search(r"\b\d{2,4}\s*Kč\b", line, flags=re.I):
            cleaned.append("Polévka")
            soup_category_added = True

        if re.match(r"^\d+\.\s+", line) and not main_category_added:
            cleaned.append("Hlavní chody")
            main_category_added = True

        previous = line
        cleaned.append(line)

    return cleaned


def parse_teatr(restaurant: Restaurant, target_date: date, timeout_seconds: int) -> MenuResult:
    """Parse Restaurant Teátr daily menu page."""
    source_url, lines = html_lines(restaurant, timeout_seconds)

    section = extract_current_date_section(lines, target_date)
    note = "Matched today's date section."

    if not section:
        section = extract_today_weekday_section(lines, target_date)
        note = "Matched today's weekday section."

    if not section:
        return restaurant.failure(
            "Could not find today's Restaurant Teátr menu section.",
            note="This parser expects weekly day/date sections on the Denní menu page.",
            screenshot_url=source_url,
        )

    cleaned = _teatr_clean_section(section)
    if not cleaned or not any(re.search(r"\b\d{2,4}\s*Kč\b", line, flags=re.I) for line in cleaned):
        return restaurant.failure(
            "Could not extract Restaurant Teátr meal rows.",
            note="The page was found, but no Kč-priced menu rows were detected.",
            screenshot_url=source_url,
        )

    return restaurant.success(cleaned, source_url=source_url, note=note)


RESTAURANTS: list[Restaurant] = [
    # Disabled 2026-07-15: zlatalod.com's TLS cert (CN=zlatalod.com) has no SAN for
    # www.zlatalod.com, and the site force-redirects bare zlatalod.com -> www, so every
    # client (including this script) fails hostname verification. Re-enable once fixed.
    # Restaurant("Zlatá Loď", "https://www.zlatalod.com/menu/", parse_daily_menu_page, key="zlata", aliases=("lod", "zlatalod")),
    Restaurant("Indian Restaurant Buddha", "https://www.indian-restaurant-buddha.cz/", parse_buddha, key="buddha"),
    # Disabled 2026-07-15: nasolnici.cz intermittently connect-timeouts a plain
    # requests.get() from GitHub Actions runners specifically (never reproduces from a
    # residential network). Adding allow_playwright_fallback=True did not reliably fix it
    # either (still failed in 1 of 2 follow-up runs) — needs more investigation before
    # re-enabling.
    # Restaurant("Na Solnici", "https://www.nasolnici.cz/", parse_generic_html, allow_playwright_fallback=True, key="solnici"),
    Restaurant("Na Knoflíku", "http://www.brnorestaurace.cz/tydenni-menu/", parse_knofliku, allow_playwright_fallback=True, key="knofliku", aliases=("knoflik", "knoflíku", "brnorestaurace")),
    Restaurant("La Famiglia", "https://lafamigliabrno.cz/denni-menu/", parse_lafamiglia, key="lafamiglia", aliases=("la famiglia", "famiglia")),
    Restaurant("Restaurant Teátr", "https://www.restaurant-teatr.cz/denni-menu.php", parse_teatr, key="teatr", aliases=("teátr", "teatr", "restaurant teatr")),
    Restaurant("Parodie", "", parse_parodie_static_image, key="parodie", aliases=("parody",)),
    Restaurant("Diandi", "", parse_diandi_static_image, key="diandi"),
    Restaurant("U Třech Čertů Dvořákova", "https://ucertu.cz/dvorakova/", parse_ucertu_dvorakova, key="certu", aliases=("čertů", "dvorakova", "dvořákova")),
    Restaurant("Charlie's Square", "https://www.charliessquare.cz/", parse_daily_menu_page, key="charlies", aliases=("charlie",)),
    Restaurant("Singha Thai", "https://www.singhathairestaurant.cz/poledni-menu/", parse_singha, allow_playwright_fallback=True, key="singha", aliases=("single", "singha thai")),
    # Important: use the weekly-menu subpages, not the homepages. The Zomato iframe lives here.
    Restaurant("U Dřevěného orla", "https://www.drevenyorel.cz/cz/page/tydenni-menu.html", parse_zomato_iframe_or_external, allow_playwright_fallback=True, key="orel", aliases=("orla", "drevenyorel", "dřevěný orel")),
    Restaurant("Suzie's", "https://suzies.cz/poledni-menu/", parse_generic_html, key="suzies", aliases=("suzie",)),
    Restaurant("U Dřevěného vlka", "https://www.drevenyvlk.cz/cz/page/tydenni-menu.html", parse_zomato_iframe_or_external, allow_playwright_fallback=True, key="vlk", aliases=("vlka", "drevenyvlk", "dřevěný vlk")),
]
