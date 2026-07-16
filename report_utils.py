from __future__ import annotations

from datetime import date
from html import escape
from pathlib import Path
from typing import Sequence

from text_utils import CZECH_WEEKDAYS, normalize_whitespace, structured_menu_rows


def _report_filename(target_date: date, suffix: str) -> str:
    return f"lunch_menu_{target_date.isoformat()}{suffix}"


def _clean_for_report(lines: Sequence[str]) -> list[str]:
    cleaned: list[str] = []
    previous = ""
    for line in lines:
        line = normalize_whitespace(line)
        if not line or line == previous:
            continue
        previous = line
        cleaned.append(line)
    return cleaned


def _menu_rows(lines: Sequence[str]):
    return structured_menu_rows(_clean_for_report(lines))


def _count_menu_items(lines: Sequence[str]) -> int:
    return sum(1 for row in _menu_rows(lines) if row.kind == "item")


def _render_menu_rows(lines: Sequence[str]) -> str:
    rows = _menu_rows(lines)
    if not rows:
        return ""

    parts: list[str] = ['<div class="menu-table">']
    item_index = 1
    previous_kind = ""
    seen_category = False
    for row in rows:
        if row.kind == "category":
            if seen_category:
                # A plain rule between groups (e.g. soup -> mains), separate from the
                # category label itself, so the label keeps reading as a small title
                # rather than every group looking like an equally heavy boxed band.
                parts.append('<hr class="category-divider">')
            seen_category = True
            parts.append(
                '<div class="category-row">'
                f'<span>{escape(row.text)}</span>'
                '</div>'
            )
        elif row.kind == "item":
            parts.append(
                '<div class="food-row">'
                f'<span class="food-index">{item_index}</span>'
                f'<span class="food-name">{escape(row.text)}</span>'
                f'<span class="food-price">{escape(row.price)}</span>'
                '</div>'
            )
            item_index += 1
        elif row.kind == "note" and previous_kind == "category":
            # An unpriced dish right after a category heading (e.g. a soup that's
            # included in the meal price, with no Kč of its own) should still read as
            # a menu item, not as a smaller italic aside — otherwise restaurants whose
            # soup has no listed price look inconsistent with ones where it does.
            parts.append(
                '<div class="food-row food-row--unpriced">'
                f'<span class="food-name">{escape(row.text)}</span>'
                '</div>'
            )
        else:
            parts.append(f'<div class="note-row">{escape(row.text)}</div>')
        previous_kind = row.kind
    parts.append("</div>")
    return "\n".join(parts)


def _render_images(result) -> str:
    image_paths = list(getattr(result, "screenshot_paths", []) or [])
    single_path = getattr(result, "screenshot_path", "") or ""
    if single_path and single_path not in image_paths:
        image_paths.insert(0, single_path)

    if not image_paths:
        return ""

    is_ok = getattr(result, "status", "") == "ok"
    images: list[str] = []
    for index, image_path in enumerate(image_paths, start=1):
        safe_src = escape(str(image_path).replace("\\", "/"))
        caption = "" if is_ok else f'<figcaption>Snímek {index}</figcaption>'
        images.append(
            f"""
            <figure class="image-frame">
                <img src="{safe_src}" alt="Obrázek {index} pro {escape(result.restaurant)}">
                {caption}
            </figure>
            """
        )

    class_name = "static-images" if is_ok else "fallback-images"
    return f'<div class="{class_name}">{"".join(images)}</div>'


def _restaurant_summary(results) -> str:
    chips: list[str] = []
    for restaurant_index, result in enumerate(results, start=1):
        is_ok = result.status == "ok"
        item_count = _count_menu_items(result.lines) if is_ok and result.lines else 0
        has_image = bool(getattr(result, "screenshot_paths", None) or getattr(result, "screenshot_path", ""))
        meta = f"{item_count} jídel" if item_count else ("" if has_image else "kontrola")
        chips.append(
            f"""
            <div class="toc-chip">
                <span class="toc-index">{restaurant_index}</span>
                <span class="toc-name">{escape(result.restaurant)}</span>
                {f'<span class="toc-meta">{escape(meta)}</span>' if meta else ''}
            </div>
            """
        )
    return "\n".join(chips)


def build_html_report(results, target_date: date) -> str:
    weekday = CZECH_WEEKDAYS[target_date.weekday()]
    pretty_date = f"{weekday} {target_date.strftime('%d.%m.%Y')}"
    total_restaurants = len(results)
    total_options = sum(_count_menu_items(result.lines) for result in results if result.status == "ok")

    cards: list[str] = []
    for restaurant_index, result in enumerate(results, start=1):
        is_ok = result.status == "ok"
        card_state = "is-ok" if is_ok else "is-warning"
        item_count = _count_menu_items(result.lines) if is_ok and result.lines else 0
        has_image = bool(getattr(result, "screenshot_paths", None) or getattr(result, "screenshot_path", ""))
        meta = f"{item_count} jídel" if item_count else ("" if has_image else "ke kontrole")

        source_link = ""
        if result.url:
            source_link = f'<a href="{escape(result.url)}">zdroj</a>'

        if is_ok:
            body = _render_menu_rows(result.lines)
        else:
            body = f'<div class="error-box">{escape(result.error or "Menu se nepodařilo načíst.")}</div>'

        # Parser/debug notes are intentionally hidden for successful restaurants.
        note = f'<div class="warning-note">{escape(result.note)}</div>' if (not is_ok and result.note) else ""
        images = _render_images(result)

        cards.append(
            f"""
            <section class="restaurant-card {card_state}">
                <header class="restaurant-header">
                    <div class="restaurant-number">{restaurant_index}</div>
                    <div class="restaurant-heading">
                        <h2>{escape(result.restaurant)}</h2>
                        <div class="restaurant-subline">
                            {f'<span>{escape(meta)}</span>' if meta else ''}
                            {source_link}
                        </div>
                    </div>
                </header>
                <div class="restaurant-body">
                    {body}
                    {note}
                    {images}
                </div>
            </section>
            """
        )

    cards_html = "\n".join(cards)
    summary_html = _restaurant_summary(results)

    return f"""<!doctype html>
<html lang="cs">
<head>
    <meta charset="utf-8">
    <title>Polední menu - {target_date.isoformat()}</title>
    <style>
        @page {{ size: A4; margin: 10mm; }}
        * {{ box-sizing: border-box; }}
        body {{
            margin: 0;
            font-family: Arial, Helvetica, sans-serif;
            color: #182033;
            background: #f4efe7;
            line-height: 1.33;
        }}
        .page {{
            max-width: 960px;
            margin: 0 auto;
            padding: 14px;
        }}
        .topbar {{
            display: grid;
            grid-template-columns: 1fr auto;
            gap: 16px;
            align-items: center;
            padding: 18px 20px;
            margin-bottom: 12px;
            border-radius: 22px;
            color: #fff;
            background:
                radial-gradient(circle at 95% -20%, rgba(250, 204, 21, .38) 0, rgba(250, 204, 21, 0) 34%),
                linear-gradient(135deg, #0f172a 0%, #1e293b 46%, #064e3b 100%);
            box-shadow: 0 14px 30px rgba(15, 23, 42, .18);
        }}
        .topbar h1 {{
            margin: 0 0 5px 0;
            font-size: 31px;
            letter-spacing: .02em;
            line-height: 1.05;
        }}
        .topbar .date {{
            margin: 0;
            color: rgba(255,255,255,.84);
            font-size: 15px;
            font-weight: 700;
        }}
        .stats {{
            display: flex;
            gap: 8px;
            align-items: stretch;
        }}
        .stat {{
            min-width: 92px;
            padding: 10px 12px;
            border-radius: 16px;
            background: rgba(255,255,255,.12);
            border: 1px solid rgba(255,255,255,.18);
            text-align: center;
        }}
        .stat b {{ display: block; font-size: 23px; line-height: 1; }}
        .stat span {{ display: block; margin-top: 4px; font-size: 10px; text-transform: uppercase; letter-spacing: .12em; color: rgba(255,255,255,.72); }}
        .toc {{
            padding: 12px;
            margin-bottom: 12px;
            border-radius: 20px;
            background: rgba(255,255,255,.82);
            border: 1px solid rgba(148, 163, 184, .25);
            box-shadow: 0 8px 24px rgba(15, 23, 42, .055);
        }}
        .toc-title {{
            margin: 0 0 9px 2px;
            color: #475569;
            font-size: 10px;
            font-weight: 900;
            letter-spacing: .15em;
            text-transform: uppercase;
        }}
        .toc-grid {{
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 7px;
        }}
        .toc-chip {{
            display: grid;
            grid-template-columns: auto 1fr;
            grid-template-areas: "idx name" "idx meta";
            column-gap: 8px;
            align-items: center;
            padding: 8px;
            border-radius: 14px;
            background: #fff;
            border: 1px solid #e2e8f0;
        }}
        .toc-index {{
            grid-area: idx;
            width: 27px;
            height: 27px;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            border-radius: 999px;
            background: #0f172a;
            color: #fff;
            font-size: 11px;
            font-weight: 900;
        }}
        .toc-name {{
            grid-area: name;
            min-width: 0;
            color: #111827;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            font-size: 11px;
            font-weight: 900;
        }}
        .toc-meta {{
            grid-area: meta;
            color: #64748b;
            font-size: 10px;
            font-weight: 700;
        }}
        .restaurant-card {{
            position: relative;
            overflow: hidden;
            margin-bottom: 12px;
            border-radius: 22px;
            background: #fffdf9;
            border: 1px solid rgba(203, 213, 225, .82);
            box-shadow: 0 11px 26px rgba(15, 23, 42, .07);
            break-inside: avoid;
            page-break-inside: avoid;
        }}
        .restaurant-card:before {{
            content: "";
            position: absolute;
            left: 0;
            top: 0;
            bottom: 0;
            width: 7px;
            background: linear-gradient(180deg, #10b981, #06b6d4);
        }}
        .restaurant-card.is-warning:before {{ background: linear-gradient(180deg, #f59e0b, #ef4444); }}
        .restaurant-header {{
            display: flex;
            gap: 13px;
            align-items: center;
            padding: 15px 18px 12px 20px;
            background: linear-gradient(180deg, rgba(248,250,252,.96), rgba(255,255,255,.55));
            border-bottom: 1px solid #edf2f7;
        }}
        .restaurant-number {{
            flex: 0 0 auto;
            width: 42px;
            height: 42px;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            border-radius: 14px;
            background: #0f172a;
            color: #fff;
            font-size: 18px;
            font-weight: 900;
            box-shadow: inset 0 -2px 0 rgba(255,255,255,.12);
        }}
        .restaurant-heading {{ min-width: 0; }}
        .restaurant-heading h2 {{
            margin: 0;
            color: #0f172a;
            font-size: 21px;
            font-weight: 900;
            letter-spacing: -.01em;
            line-height: 1.05;
        }}
        .restaurant-subline {{
            margin-top: 4px;
            display: flex;
            gap: 10px;
            align-items: center;
            color: #64748b;
            font-size: 10px;
            font-weight: 900;
            letter-spacing: .1em;
            text-transform: uppercase;
        }}
        .restaurant-subline a {{ color: #64748b; text-decoration: none; }}
        .restaurant-body {{ padding: 10px 18px 15px 20px; }}
        .menu-table {{
            border-radius: 16px;
            overflow: hidden;
            border: 1px solid #e5e7eb;
            background: #fff;
        }}
        .category-row {{
            padding: 10px 12px 6px 12px;
        }}
        .category-row:first-child {{ padding-top: 0; }}
        .category-divider {{
            border: none;
            border-top: 2px solid #cbd5e1;
            margin: 6px 12px 0 12px;
        }}
        .category-row span {{
            display: inline-block;
            padding: 4px 8px;
            border-radius: 999px;
            background: #e0f2fe;
            color: #075985;
            font-size: 10px;
            font-weight: 900;
            letter-spacing: .12em;
            text-transform: uppercase;
        }}
        .food-row {{
            display: grid;
            grid-template-columns: 34px minmax(0, 1fr) auto;
            gap: 10px;
            align-items: baseline;
            padding: 8px 12px;
            border-bottom: 1px solid #edf2f7;
        }}
        .food-row:nth-child(even) {{ background: #fffaf2; }}
        .food-row:last-child {{ border-bottom: 0; }}
        .food-row--unpriced {{ grid-template-columns: 1fr; }}
        .food-index {{
            align-self: start;
            justify-self: start;
            min-width: 28px;
            padding: 4px 0;
            border-radius: 999px;
            background: #fef3c7;
            color: #92400e;
            text-align: center;
            font-size: 11px;
            font-weight: 900;
        }}
        .food-name {{
            min-width: 0;
            color: #1f2937;
            font-size: 13px;
            font-weight: 650;
        }}
        .food-price {{
            justify-self: end;
            padding: 4px 9px;
            border-radius: 999px;
            background: #dcfce7;
            color: #166534;
            white-space: nowrap;
            font-size: 12px;
            font-weight: 900;
        }}
        .note-row {{
            padding: 7px 12px;
            color: #64748b;
            font-size: 12px;
            font-style: italic;
            border-bottom: 1px solid #edf2f7;
        }}
        .static-images {{ margin-top: 0; }}
        .image-frame {{ margin: 0; break-inside: avoid; page-break-inside: avoid; }}
        .image-frame img {{
            display: block;
            width: 100%;
            max-height: 300px;
            object-fit: cover;
            border-radius: 16px;
            border: 1px solid #e5e7eb;
            background: #fff;
        }}
        .fallback-images {{
            margin-top: 12px;
            padding-top: 10px;
            border-top: 1px solid #e5e7eb;
        }}
        .fallback-images figcaption {{
            margin-top: 4px;
            color: #64748b;
            font-size: 10px;
            text-align: center;
        }}
        .error-box {{
            padding: 12px 14px;
            border-radius: 14px;
            background: #fffbeb;
            border: 1px solid #fde68a;
            color: #92400e;
            font-size: 13px;
            font-weight: 800;
        }}
        .warning-note {{
            margin-top: 8px;
            color: #64748b;
            font-size: 12px;
            font-style: italic;
        }}
        .footer {{
            margin-top: 16px;
            color: #64748b;
            text-align: center;
            font-size: 10px;
            letter-spacing: .06em;
            text-transform: uppercase;
        }}
        @media print {{
            body {{ background: white; }}
            .page {{ padding: 0; }}
            .topbar, .toc, .restaurant-card {{ box-shadow: none; }}
        }}
    </style>
</head>
<body>
    <main class="page">
        <header class="topbar">
            <div>
                <h1>Brněnská polední menu</h1>
                <p class="date">{escape(pretty_date)}</p>
            </div>
            <div class="stats">
                <div class="stat"><b>{total_restaurants}</b><span>restaurací</span></div>
                <div class="stat"><b>{total_options}</b><span>jídel</span></div>
            </div>
        </header>

        <section class="toc">
            <p class="toc-title">Mapa pro hlasování</p>
            <div class="toc-grid">
                {summary_html}
            </div>
        </section>

        {cards_html}
        <p class="footer">Vygenerováno Lunchbotem</p>
    </main>
</body>
</html>
"""


def write_html_report(results, target_date: date, report_dir: str | Path = "reports") -> Path:
    output_dir = Path(report_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    html_path = output_dir / _report_filename(target_date, ".html")
    html_path.write_text(build_html_report(results, target_date), encoding="utf-8")
    return html_path


def write_pdf_report(results, target_date: date, report_dir: str | Path = "reports") -> Path:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Export PDF vyžaduje Playwright. Spusť: python -m pip install playwright && python -m playwright install chromium"
        ) from exc

    html_path = write_html_report(results, target_date, report_dir)
    pdf_path = Path(report_dir) / _report_filename(target_date, ".pdf")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1240, "height": 1754})
        page.goto(html_path.resolve().as_uri(), wait_until="networkidle")
        page.pdf(
            path=str(pdf_path),
            format="A4",
            print_background=True,
            margin={"top": "8mm", "right": "8mm", "bottom": "8mm", "left": "8mm"},
        )
        browser.close()

    return pdf_path
