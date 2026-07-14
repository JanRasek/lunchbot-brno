from datetime import date

from restaurants import MenuResult
from text_utils import (
    compact_menu_rows_for_slack,
    extract_today_weekday_section,
    format_slack_message,
    looks_like_date_heading,
    looks_like_weekday_heading,
    structured_menu_rows,
)

# 2026-07-07 is a Tuesday (Úterý), used throughout as a stable "today".
TUESDAY = date(2026, 7, 7)


def test_looks_like_weekday_heading_matches_plain_weekday():
    assert looks_like_weekday_heading("Úterý", "Úterý")


def test_looks_like_weekday_heading_matches_weekday_with_date():
    assert looks_like_weekday_heading("Úterý 7.7.2026", "Úterý")


def test_looks_like_weekday_heading_rejects_other_weekday():
    assert not looks_like_weekday_heading("Středa", "Úterý")


def test_looks_like_date_heading_accepts_real_date():
    assert looks_like_date_heading("Úterý 7.7.2026")


def test_looks_like_date_heading_rejects_quantity_that_looks_like_a_date():
    # "1. 300g" is a dish quantity, not a day boundary; the parser must not
    # treat it as a date heading or menus get cut off after the soup.
    assert not looks_like_date_heading("1. 300g hovězí")


def test_looks_like_date_heading_rejects_volume_that_looks_like_a_date():
    assert not looks_like_date_heading("1. 2l")


def test_extract_today_weekday_section_stops_at_next_weekday():
    lines = [
        "Pondělí",
        "Polévka",
        "Hovězí vývar 30 Kč",
        "Úterý",
        "Polévka",
        "Kuřecí polévka 35 Kč",
        "Hlavní chody",
        "Svíčková 150 Kč",
        "Středa",
        "Polévka",
        "Gulášová 30 Kč",
    ]

    section = extract_today_weekday_section(lines, TUESDAY)

    assert section == [
        "Polévka",
        "Kuřecí polévka 35 Kč",
        "Hlavní chody",
        "Svíčková 150 Kč",
    ]


def test_structured_menu_rows_groups_category_item_and_price():
    lines = [
        "Polévka",
        "Hovězí vývar",
        "35 Kč",
        "Hlavní chody",
        "Svíčková na smetaně 150 Kč",
        "1,3,7",
    ]

    rows = structured_menu_rows(lines)

    assert [(row.kind, row.text, row.price) for row in rows] == [
        ("category", "Polévka", ""),
        ("item", "Hovězí vývar", "35 Kč"),
        ("category", "Hlavní chody", ""),
        ("item", "Svíčková na smetaně", "150 Kč"),
    ]


def test_structured_menu_rows_drops_allergen_numbers():
    rows = structured_menu_rows(["Svíčková 150 Kč", "1,3,7"])
    assert all(row.text != "1,3,7" for row in rows)


def test_compact_menu_rows_for_slack_numbers_items_sequentially():
    lines = [
        "Polévka",
        "Hovězí vývar",
        "35 Kč",
        "Hlavní chody",
        "Svíčková na smetaně 150 Kč",
    ]

    formatted = compact_menu_rows_for_slack(lines)

    assert formatted == [
        "*Polévka*",
        "• *#1* Hovězí vývar — *35 Kč*",
        "*Hlavní chody*",
        "• *#2* Svíčková na smetaně — *150 Kč*",
    ]


def test_format_slack_message_marks_ok_and_failed_restaurants():
    ok_result = MenuResult(
        restaurant="Test Restaurant",
        url="https://example.com",
        status="ok",
        lines=["Polévka", "Hovězí vývar 35 Kč"],
    )
    failed_result = MenuResult(
        restaurant="Broken Restaurant",
        url="https://example.com/broken",
        status="failed",
        lines=[],
        error="Could not find today's menu section in page text.",
    )

    message = format_slack_message([ok_result, failed_result], TUESDAY)

    assert "Úterý 07.07.2026" in message
    assert "✅ *#1 Test Restaurant*" in message
    assert "⚠️ *#2 Broken Restaurant*" in message
    assert "Could not find today's menu section in page text." in message
