from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Sequence

CZECH_WEEKDAYS = [
    "Pondělí",
    "Úterý",
    "Středa",
    "Čtvrtek",
    "Pátek",
    "Sobota",
    "Neděle",
]

STOP_KEYWORDS = {
    "nabídka baru",
    "rezervace",
    "kontakt",
    "otevírací doba",
    "náš instagram",
    "jsme jednou ze",
    "seznam alergenů",
    "adresa restaurace",
    "restaurace",
    "kde nás najdete",
}

NOISE_PATTERNS = [
    re.compile(r"^přeskočit na obsah$", re.I),
    re.compile(r"^menu$", re.I),
    re.compile(r"^více$", re.I),
    re.compile(r"^image", re.I),
    re.compile(r"^close$", re.I),
    re.compile(r"^vyberte stránku$", re.I),
]

PRICE_RE = re.compile(r"(?P<price>\b\d{2,4}\s*(?:Kč|Kc|CZK|,-)\b)", re.I)
CATEGORY_KEYWORDS = (
    "polévka",
    "polevka",
    "soup",
    "hlavní chody",
    "hlavni chody",
    "main courses",
    "dezert",
    "salát",
    "salat",
    "hotovka",
    "minutka",
    "ryba",
    "denní nabídka",
    "denni nabidka",
    "polední menu",
    "poledni menu",
)


@dataclass
class StructuredMenuRow:
    kind: str  # "category", "item", or "note"
    text: str = ""
    price: str = ""


def strip_accents(value: str) -> str:
    return "".join(
        char for char in unicodedata.normalize("NFKD", value)
        if not unicodedata.combining(char)
    )


def comparable(value: str) -> str:
    value = value.replace("\xa0", " ")
    value = strip_accents(value).casefold()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def normalize_whitespace(value: str) -> str:
    value = value.replace("\xa0", " ")
    value = re.sub(r"[ \t]+", " ", value)
    return value.strip()


def clean_lines(lines: Iterable[str]) -> list[str]:
    cleaned: list[str] = []
    for line in lines:
        line = normalize_whitespace(line)
        if not line:
            continue
        if any(pattern.search(line) for pattern in NOISE_PATTERNS):
            continue
        cleaned.append(line)
    return cleaned


def soup_to_lines(soup) -> list[str]:
    # BeautifulSoup inserts separators between block-ish elements reasonably well with stripped_strings.
    return clean_lines(soup.stripped_strings)


def looks_like_weekday_heading(line: str, weekday: str) -> bool:
    # Examples matched: "Úterý", "Úterý 7.7.2026", "Úterý 7. 7.", "#### Úterý".
    c_line = comparable(line)
    c_day = comparable(weekday)
    if not c_line.startswith(c_day):
        return False
    # Avoid matching normal dish text that only happens to start with a weekday word.
    return len(c_line.split()) <= 5 or re.search(r"\d{1,2}[.,] ?\d{1,2}", line)


def is_any_weekday_heading(line: str) -> bool:
    return any(looks_like_weekday_heading(line, day) for day in CZECH_WEEKDAYS)


def line_contains_target_date(line: str, target_date: date) -> bool:
    """Return True when a line contains the target date in common Czech formats.

    Examples matched: 7.7.2026, 7. 7. 2026, 07/07, 7.7.
    """
    patterns = [
        rf"\b0?{target_date.day}\s*[./]\s*0?{target_date.month}\s*[./]\s*{target_date.year}\b",
        rf"\b0?{target_date.day}\s*[./]\s*0?{target_date.month}\s*[./]?\b",
    ]
    return any(re.search(pattern, line) for pattern in patterns)


def looks_like_date_heading(line: str) -> bool:
    """Detect short standalone date/day headings used in weekly menus.

    This must be stricter than simply finding ``number dot number``. Menus often
    contain items like ``1. 300g ...`` and the previous implementation sometimes
    treated those as a date heading, which cut the menu after the soup.
    """
    value = normalize_whitespace(line)
    if not value or len(value) > 90:
        return False

    # Match dates like 7.7., 7. 7. 2026, 07/07/2026, but validate the
    # day/month values so meal rows such as ``1. 300g`` are not considered
    # a day boundary.
    for match in re.finditer(
        r"(?<!\d)(?P<day>\d{1,2})\s*[./]\s*(?P<month>\d{1,2})(?:\s*[./]\s*(?P<year>\d{2,4}))?",
        value,
    ):
        try:
            day = int(match.group("day"))
            month = int(match.group("month"))
        except ValueError:
            continue

        if not (1 <= day <= 31 and 1 <= month <= 12):
            continue

        # Avoid partial matches inside quantities/weights, e.g. ``1. 2l``.
        after = value[match.end(): match.end() + 3].lower()
        if after.startswith(("g", "kg", "l", "ml")):
            continue

        return True

    return False


def looks_like_day_boundary(line: str, target_date: date) -> bool:
    """Return True for another weekday/date heading that should end today's section."""
    if looks_like_stop(line):
        return True

    target_weekday = CZECH_WEEKDAYS[target_date.weekday()]

    if is_any_weekday_heading(line):
        # Do not treat a repeated heading for today as the end of today's section.
        if looks_like_weekday_heading(line, target_weekday) and (
            line_contains_target_date(line, target_date) or not looks_like_date_heading(line)
        ):
            return False
        return True

    if looks_like_date_heading(line):
        return not line_contains_target_date(line, target_date)

    return False


def looks_like_stop(line: str) -> bool:
    c_line = comparable(line)
    return any(keyword in c_line for keyword in [comparable(k) for k in STOP_KEYWORDS])


def has_food_signal(lines: Sequence[str]) -> bool:
    joined = "\n".join(lines)
    if re.search(r"\b\d{2,3}\s*(?:Kč|,-|kč)\b", joined, re.I):
        return True
    if re.search(r"\b(polévka|soup|menu|hotovka|hlavní|main courses)\b", joined, re.I):
        return True
    return len(lines) >= 3


def extract_today_weekday_section(lines: Sequence[str], target_date: date) -> list[str]:
    weekday = CZECH_WEEKDAYS[target_date.weekday()]
    candidates: list[list[str]] = []

    for index, line in enumerate(lines):
        if not looks_like_weekday_heading(line, weekday):
            continue

        section: list[str] = []
        for next_line in lines[index + 1:]:
            if looks_like_day_boundary(next_line, target_date):
                break
            # Some pages repeat today's date right under the weekday heading. That line is
            # metadata, not a menu item, so skip it instead of putting it into the report.
            if looks_like_date_heading(next_line) and line_contains_target_date(next_line, target_date):
                continue
            section.append(next_line)

        section = clean_lines(section)
        if has_food_signal(section):
            candidates.append(section)

    if not candidates:
        return []

    # Prefer the longest useful candidate. This avoids weekday navigation links.
    return max(candidates, key=lambda item: sum(len(x) for x in item))


def extract_between_keywords(lines: Sequence[str], start_keywords: Sequence[str], stop_keywords: Sequence[str]) -> list[str]:
    start_index = None
    for index, line in enumerate(lines):
        c_line = comparable(line)
        if any(comparable(keyword) in c_line for keyword in start_keywords):
            start_index = index + 1
            break

    if start_index is None:
        return []

    section: list[str] = []
    for line in lines[start_index:]:
        c_line = comparable(line)
        if any(comparable(keyword) in c_line for keyword in stop_keywords):
            break
        section.append(line)

    return clean_lines(section)


def extract_current_date_section(lines: Sequence[str], target_date: date) -> list[str]:
    # Handles pages that have a heading such as "Úterý 7.7.2026" or "Menu – 7. 7.".
    # Important: weekly pages often list the rest of the week after today's date, so this
    # stops at the next weekday/date heading instead of reading until the end of the page.
    candidates: list[list[str]] = []
    for index, line in enumerate(lines):
        if not line_contains_target_date(line, target_date):
            continue

        section: list[str] = []
        for next_line in lines[index + 1:]:
            if looks_like_day_boundary(next_line, target_date):
                break
            # Skip repeated date/weekday headings for today.
            if (
                line_contains_target_date(next_line, target_date)
                or looks_like_weekday_heading(next_line, CZECH_WEEKDAYS[target_date.weekday()])
            ):
                continue
            section.append(next_line)

        section = clean_lines(section)
        if has_food_signal(section):
            candidates.append(section)

    if candidates:
        return max(candidates, key=lambda item: sum(len(x) for x in item))
    return []


def _looks_like_allergen_or_number(line: str) -> bool:
    """Drop lines that are usually allergen-only fragments after imperfect extraction.

    Examples: "1,3,7", ")", "T 1)", or a standalone menu index "1".
    We intentionally do not match portions such as "0,22l" or "250g".
    """
    value = normalize_whitespace(line)
    if re.fullmatch(r"\d+", value):
        return True
    if re.fullmatch(r"[\d,\s.()]+", value) and any(char in value for char in ",()"):
        return True
    if re.fullmatch(r"T\s*\d+\)?", value, flags=re.I):
        return True
    if re.fullmatch(r"\)+", value):
        return True
    return False


def _looks_like_category(line: str) -> bool:
    """Return True only for actual section labels, not food names.

    The old implementation used ``keyword in line``. That made dish names such
    as ``SALÁT S GRILOVANÝM KUŘECÍM MASEM`` look like a category because they
    contain the word ``salát``. In the PDF those dishes were then rendered bold
    as headings and were not indexed as meals.

    Keep this intentionally strict: categories are short labels such as
    ``Polévka``, ``Hlavní chody`` or ``Salát``. Full dish names should remain
    normal item fragments until their price is found.
    """
    value = normalize_whitespace(line).rstrip(":")
    if not value or PRICE_RE.search(value):
        return False

    c_value = comparable(value)
    exact_categories = {comparable(keyword) for keyword in CATEGORY_KEYWORDS}
    exact_categories.update({
        "polevky",
        "soups",
        "dezerty",
        "salaty",
        "hotovky",
        "minutky",
        "ryby",
    })

    if c_value in exact_categories:
        return True

    # Accept short label variants like ``Polévka 1`` or ``Hotovka 2``, but do
    # not accept long dish names containing these words.
    return bool(re.fullmatch(
        r"(?:polevka|soup|hotovka|minutka|ryba|salat|dezert)\s*\d?",
        c_value,
    ))


def _join_item_parts(parts: Sequence[str]) -> str:
    cleaned = []
    previous = ""
    for part in parts:
        part = normalize_whitespace(part).strip(" :")
        if not part or part == previous or _looks_like_allergen_or_number(part):
            continue
        previous = part
        cleaned.append(part)
    return " - ".join(cleaned)


def structured_menu_rows(lines: Sequence[str], max_items: int | None = None) -> list[StructuredMenuRow]:
    """Convert extracted line soup into rows where one meal and its price stay together.

    Website/PDF extraction often returns one line for portion, one line for meal, one line for
    description, and one line for the price. This groups those fragments until a price is seen.
    It is deliberately heuristic, because every restaurant formats its menu differently.
    """
    rows: list[StructuredMenuRow] = []
    current_parts: list[str] = []
    item_count = 0

    def flush_unpriced() -> None:
        nonlocal current_parts
        text = _join_item_parts(current_parts)
        if text:
            rows.append(StructuredMenuRow(kind="note", text=text))
        current_parts = []

    for original_line in clean_lines(lines):
        line = normalize_whitespace(original_line)
        if not line or _looks_like_allergen_or_number(line):
            continue

        price_match = PRICE_RE.search(line)
        if price_match:
            before_price = normalize_whitespace(line[: price_match.start()]).strip(" :-")
            price = normalize_whitespace(price_match.group("price"))
            if before_price:
                current_parts.append(before_price)
            text = _join_item_parts(current_parts)
            current_parts = []
            if text:
                rows.append(StructuredMenuRow(kind="item", text=text, price=price))
                item_count += 1
                if max_items and item_count >= max_items:
                    rows.append(StructuredMenuRow(kind="note", text="… more items in source"))
                    break
            continue

        if line.startswith("-") and not current_parts and rows and rows[-1].kind == "category":
            rows.append(StructuredMenuRow(kind="note", text=line.lstrip("- ").strip()))
            continue

        if _looks_like_category(line):
            flush_unpriced()
            rows.append(StructuredMenuRow(kind="category", text=line.rstrip(":")))
            continue

        current_parts.append(line)

    flush_unpriced()
    return rows


def compact_menu_lines(lines: Sequence[str], max_lines: int = 16) -> list[str]:
    result: list[str] = []
    previous = ""
    for line in lines:
        line = normalize_whitespace(line)
        if not line or line == previous:
            continue
        previous = line
        result.append(line)
        if len(result) >= max_lines:
            remaining = len(lines) - len(result)
            if remaining > 0:
                result.append(f"… plus {remaining} more lines")
            break
    return result


def compact_menu_rows_for_slack(
    lines: Sequence[str],
    max_rows: int = 50,
) -> list[str]:
    """Format menu rows for Slack with per-restaurant numbering."""
    result: list[str] = []
    rows = structured_menu_rows(lines, max_items=max_rows)
    item_index = 1
    for row in rows:
        if row.kind == "category":
            result.append(f"*{row.text}*")
        elif row.kind == "item":
            result.append(f"• *#{item_index}* {row.text} — *{row.price}*")
            item_index += 1
        else:
            result.append(f"_{row.text}_")
    return result


def format_slack_message(results, target_date: date) -> str:
    weekday = CZECH_WEEKDAYS[target_date.weekday()]
    header = f"🍽️ *Polední menu pro {weekday} {target_date.strftime('%d.%m.%Y')}*"
    parts = [header]
    restaurant_index = 1

    for result in results:
        parts.append("")
        status_icon = "✅" if result.status == "ok" else "⚠️"
        title = f"{status_icon} *#{restaurant_index} {result.restaurant}*"
        if result.url:
            title += f"\n<{result.url}|Otevřít zdroj>"
        parts.append(title)

        if result.status == "ok":
            formatted_rows = compact_menu_rows_for_slack(result.lines)
            if formatted_rows:
                parts.extend(formatted_rows)
            else:
                for line in compact_menu_lines(result.lines):
                    parts.append(f"• {line}")
        else:
            parts.append(result.error or "Menu se nepodařilo načíst.")
            if result.note:
                parts.append(f"_{result.note}_")
            screenshot_path = getattr(result, "screenshot_path", "")
            if screenshot_path:
                parts.append("_Snímek je uložený ve složce reportu._")

        restaurant_index += 1

    return "\n".join(parts)


def format_slack_summary_message(results, target_date: date, report_url: str = "") -> str:
    """Build a short Slack message: one line per restaurant, plus a link to the full report.

    Unlike format_slack_message, this intentionally leaves out per-item menu text/prices
    so the channel gets a glanceable status list instead of a long wall of text; the full
    breakdown lives in the linked PDF/HTML report instead.
    """
    weekday = CZECH_WEEKDAYS[target_date.weekday()]
    parts = [f"🍽️ *Polední menu pro {weekday} {target_date.strftime('%d.%m.%Y')}*"]

    if report_url:
        parts.append(f"📄 <{report_url}|Otevřít celé menu (PDF)>")

    parts.append("")
    for index, result in enumerate(results, start=1):
        status_icon = "✅" if result.status == "ok" else "⚠️"
        parts.append(f"{status_icon} *#{index}* {result.restaurant}")

    return "\n".join(parts)
