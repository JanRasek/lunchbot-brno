from datetime import date

from restaurants import MenuResult
from text_utils import (
    compact_menu_rows_for_slack,
    extract_today_weekday_section,
    format_slack_message,
    format_slack_summary_message,
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


def test_structured_menu_rows_strips_redundant_category_from_dash_note():
    # A duplicated DOM node can repeat the category label on the dash-prefixed note that
    # follows a category row, e.g. U Třech Čertů's "Polévka" then "- Polévka: <soup>".
    rows = structured_menu_rows(["Polévka", "- Polévka: Špenátová se slaninou"])
    assert [(row.kind, row.text) for row in rows] == [
        ("category", "Polévka"),
        ("note", "Špenátová se slaninou"),
    ]


def test_structured_menu_rows_splits_inline_category_dish_line():
    # Some sources (Zomato widgets, Na Knoflíku) write the category and the first dish
    # on one line, e.g. "Polévka: Mexická fazolová ..." or "Polévka 1: Česnečka".
    rows = structured_menu_rows(["Polévka 1: Česnečka 19 Kč"])
    assert [(row.kind, row.text, row.price) for row in rows] == [
        ("category", "Polévka", ""),
        ("item", "Česnečka", "19 Kč"),
    ]


def test_structured_menu_rows_strips_leading_source_site_list_numbers():
    rows = structured_menu_rows(["1) PHAD SE-W - kuřecí nudle 194 Kč"])
    assert rows[0].text == "PHAD SE-W - kuřecí nudle"


def test_structured_menu_rows_drops_bare_list_marker_line():
    # A source site sometimes puts just "1." on its own line before the dish text.
    rows = structured_menu_rows(["1.", "Mango Lassi 55 Kč"])
    assert [(row.kind, row.text) for row in rows] == [("item", "Mango Lassi")]


def test_structured_menu_rows_strips_trailing_allergen_codes():
    rows = structured_menu_rows(["Hovězí guláš s bramboráčky(1,3,7,12) 199 Kč"])
    assert rows[0].text == "Hovězí guláš s bramboráčky"


def test_structured_menu_rows_drops_zomato_widget_header_noise():
    lines = [
        "Daily menu",
        "Tuesday, 14 July (Dnes)",
        "Polévka: Mexická fazolová s hovězím masem",
        "1. 350g Pečená vepřová žebra 199 Kč",
    ]
    rows = structured_menu_rows(lines)
    assert [(row.kind, row.text, row.price) for row in rows] == [
        ("category", "Polévka", ""),
        ("item", "Mexická fazolová s hovězím masem - 350g Pečená vepřová žebra", "199 Kč"),
    ]


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


def test_format_slack_summary_message_is_compact_with_report_link():
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

    message = format_slack_summary_message(
        [ok_result, failed_result], TUESDAY, report_url="https://example.github.io/report/latest.pdf"
    )

    assert message == "\n".join(
        [
            "🍽️ *Polední menu pro Úterý 07.07.2026*",
            "📄 Denní menu: https://example.github.io/report/latest.pdf",
            "",
            "✅ *#1* Test Restaurant",
            "⚠️ *#2* Broken Restaurant",
        ]
    )
    # The compact summary must not leak per-item menu text/prices into Slack.
    assert "Hovězí vývar" not in message


def test_format_slack_summary_message_without_report_url_omits_link_line():
    ok_result = MenuResult(restaurant="Test Restaurant", url="https://example.com", status="ok", lines=[])

    message = format_slack_summary_message([ok_result], TUESDAY)

    assert "📄" not in message
