from __future__ import annotations

import html
import re
import socket
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

import requests
import urllib3.util.connection as urllib3_connection
from bs4 import BeautifulSoup

# Some restaurant sites (e.g. nasolnici.cz) publish both an A and an AAAA record, and
# their IPv6 route is not reliably reachable from every network (GitHub Actions runner,
# home network, etc.). socket.create_connection() tries each resolved address in turn and
# gives each one the *full* connect timeout before moving on, so an IPv6 address that
# black-holes (rather than actively refusing the connection) silently doubles the
# request's latency and can push it past the configured timeout even though IPv4 alone
# would have succeeded almost instantly. None of these sites need IPv6, so force
# requests/urllib3 to resolve IPv4 addresses only, process-wide.
urllib3_connection.allowed_gai_family = lambda: socket.AF_INET

PLAYWRIGHT_MISSING_HINT = (
    "Playwright is not installed. Run: pip install playwright && python -m playwright install chromium"
)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36 Lunchbot/1.0"
    ),
    "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.8",
}

MENU_SCREENSHOT_KEYWORDS = [
    "polední menu",
    "poledni menu",
    "denní menu",
    "denni menu",
    "týdenní menu",
    "tydenni menu",
    "denní nabídka",
    "denni nabidka",
    "týdenní nabídka",
    "tydenni nabidka",
    "oběd",
    "obed",
    "menu",
    "polévka",
    "polevka",
    "hlavní chody",
    "hlavni chody",
    "pondělí",
    "pondeli",
    "úterý",
    "utery",
    "středa",
    "streda",
    "čtvrtek",
    "ctvrtek",
    "pátek",
    "patek",
]


@dataclass
class FetchResult:
    url: str
    text: str


def fetch_html_requests(url: str, timeout_seconds: int) -> FetchResult:
    response = requests.get(url, headers=DEFAULT_HEADERS, timeout=timeout_seconds)
    response.raise_for_status()
    return FetchResult(url=response.url, text=response.text)


@contextmanager
def playwright_page(viewport: dict | None = None, error_hint: str = PLAYWRIGHT_MISSING_HINT):
    """Launch a headless Chromium page and guarantee the browser is closed afterwards.

    Centralizes the sync_playwright/browser.launch/browser.close boilerplate that every
    Playwright-based helper below needs, so each helper only has to describe what it does
    with the page.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(error_hint) from exc

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(locale="cs-CZ", viewport=viewport) if viewport else browser.new_page(locale="cs-CZ")
            yield page
        finally:
            browser.close()


def fetch_html_playwright(url: str, timeout_seconds: int) -> FetchResult:
    with playwright_page() as page:
        page.goto(url, wait_until="networkidle", timeout=timeout_seconds * 1000)
        return FetchResult(url=page.url, text=page.content())





def _scroll_page_for_lazy_iframes(page, wait_ms: int = 400) -> None:
    """Scroll the page a little so lazy-loaded iframe widgets are created/populated.

    Some restaurant pages create the Zomato iframe only after the weekly-menu section
    comes close to the viewport. Without scrolling, Playwright can see only the parent
    page and miss the actual daily_menu.php iframe that is visible in a real browser.
    """
    try:
        height = page.evaluate("() => Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)") or 0
    except Exception:
        height = 0

    # Visit a few useful positions, not every pixel. This is enough to trigger most
    # lazy widgets while keeping the scraper fast.
    positions = [0, 500, 1000, 1500, 2200, 3000]
    if height:
        positions.extend([max(0, int(height * 0.35)), max(0, int(height * 0.65)), max(0, height - 900)])

    seen: set[int] = set()
    for y in positions:
        y = max(0, int(y))
        if y in seen:
            continue
        seen.add(y)
        try:
            page.evaluate("y => window.scrollTo(0, y)", y)
            page.wait_for_timeout(wait_ms)
        except Exception:
            pass


def fetch_html_playwright_all_frames(url: str, timeout_seconds: int) -> FetchResult:
    """Render a page and return HTML from the main page plus all iframes.

    A normal requests/BeautifulSoup fetch only sees the top-level document. If the menu is
    embedded in an iframe, that iframe is a separate document, so its text needs to be read
    separately. Playwright exposes those documents as page.frames.
    """
    with playwright_page(viewport={"width": 1365, "height": 1200}) as page:
        _goto_best_effort(page, url, timeout_seconds)
        _dismiss_common_banners(page)
        _scroll_page_for_lazy_iframes(page)
        page.wait_for_timeout(1000)

        parts: list[str] = [page.content()]

        for index, frame in enumerate(page.frames):
            if frame == page.main_frame:
                continue

            frame_url = frame.url or "about:blank"
            parts.append(f'\n<section data-lunchbot-frame="{index}" data-lunchbot-frame-url="{html.escape(frame_url)}">\n')

            try:
                parts.append(frame.content())
            except Exception:
                # Some frames do not expose full HTML content reliably. Inner text is still often enough.
                try:
                    frame_text = frame.locator("body").inner_text(timeout=1500)
                    parts.append(f"<pre>{html.escape(frame_text)}</pre>")
                except Exception as exc:
                    parts.append(f"<pre>Could not read frame {html.escape(frame_url)}: {html.escape(str(exc))}</pre>")

            parts.append("\n</section>\n")

        return FetchResult(url=page.url, text="\n".join(parts))



def fetch_html_playwright_frames(url: str, timeout_seconds: int) -> list[FetchResult]:
    """Render a page and return each iframe as a separate FetchResult.

    This is safer than merging the parent page and frames when a restaurant page contains
    marketing text plus a menu iframe. Callers can then parse only the frame whose URL is
    relevant, for example Zomato daily_menu.php.
    """
    with playwright_page(viewport={"width": 1365, "height": 1200}) as page:
        _goto_best_effort(page, url, timeout_seconds)
        _dismiss_common_banners(page)
        _scroll_page_for_lazy_iframes(page)
        page.wait_for_timeout(1500)

        results: list[FetchResult] = []
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            frame_url = frame.url or "about:blank"
            try:
                frame_html = frame.content()
            except Exception:
                try:
                    frame_text = frame.locator("body").inner_text(timeout=1500)
                    frame_html = f"<pre>{html.escape(frame_text)}</pre>"
                except Exception as exc:
                    frame_html = f"<pre>Could not read frame {html.escape(frame_url)}: {html.escape(str(exc))}</pre>"
            results.append(FetchResult(url=frame_url, text=frame_html))
        return results





def fetch_playwright_frame_texts(url: str, timeout_seconds: int, wait_seconds: float = 8.0) -> list[FetchResult]:
    """Render a page and return visible text from every iframe.

    This is intentionally different from fetching iframe URLs directly. Some third-party
    widgets, especially Zomato, fail when their iframe URL is opened by itself, but the
    text is still visible when the widget is embedded in the restaurant page. Playwright
    can read that rendered frame text without us navigating to the Zomato URL.
    """
    def frame_text(frame) -> str:
        try:
            return frame.locator("body").inner_text(timeout=2000)
        except Exception:
            try:
                return frame.evaluate("() => document.body ? document.body.innerText : ''") or ""
            except Exception:
                return ""

    with playwright_page(viewport={"width": 1365, "height": 1200}) as page:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_seconds * 1000)
        except Exception:
            _goto_best_effort(page, url, timeout_seconds)
        _dismiss_common_banners(page)
        _scroll_page_for_lazy_iframes(page)

        # Give widget scripts time to create/populate iframes. We poll because these
        # widgets often load after DOMContentLoaded and networkidle can be unreliable.
        deadline_ms = int(wait_seconds * 1000)
        step_ms = 500
        elapsed = 0
        best_results: list[FetchResult] = []
        while elapsed <= deadline_ms:
            try:
                page.evaluate("y => window.scrollTo(0, y)", min(3200, elapsed * 2))
            except Exception:
                pass
            results: list[FetchResult] = []
            for index, frame in enumerate(page.frames):
                if frame == page.main_frame:
                    continue
                text = frame_text(frame).strip()
                if not text:
                    continue
                frame_url = frame.url or f"{url}#iframe-{index}"
                results.append(FetchResult(url=frame_url, text=text))

            if results:
                best_results = results

            joined = "\n".join(result.text for result in results).casefold()
            if (
                "daily menu" in joined
                or "denní menu" in joined
                or "denni menu" in joined
                or "polední menu" in joined
                or "poledni menu" in joined
            ):
                break

            page.wait_for_timeout(step_ms)
            elapsed += step_ms

        return best_results

def find_rendered_iframe_sources(url: str, timeout_seconds: int, wait_seconds: float = 6.0) -> list[str]:
    """Render a page and return iframe src URLs seen in the live DOM and page.frames.

    Some sites do not put the final iframe URL in the server HTML. Instead, a widget
    script creates an iframe after JavaScript runs. This helper waits for those iframes
    and reads their src attributes without trying to navigate directly to widget scripts
    such as /widgets/daily_menu_widget.
    """
    found: list[str] = []

    def add(value: str | None) -> None:
        if not value:
            return
        value = str(value).strip()
        if value and value not in found:
            found.append(value)

    with playwright_page(viewport={"width": 1365, "height": 1200}) as page:
        # DOMContentLoaded is more reliable here than networkidle because external
        # widgets can keep network requests open or fail independently.
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_seconds * 1000)
        except Exception:
            _goto_best_effort(page, url, timeout_seconds)
        _dismiss_common_banners(page)
        _scroll_page_for_lazy_iframes(page)

        deadline_ms = int(wait_seconds * 1000)
        step_ms = 500
        elapsed = 0
        while elapsed <= deadline_ms:
            try:
                page.evaluate("y => window.scrollTo(0, y)", min(3200, elapsed * 2))
            except Exception:
                pass
            try:
                iframe_srcs = page.evaluate(
                    """
                    () => Array.from(document.querySelectorAll('iframe'))
                        .map(frame => frame.src || frame.getAttribute('src') || '')
                        .filter(Boolean)
                    """
                )
                for src in iframe_srcs:
                    add(src)
            except Exception:
                pass

            try:
                for frame in page.frames:
                    if frame != page.main_frame:
                        add(frame.url)
            except Exception:
                pass

            if any("daily_menu.php" in src or "entity_id=" in src for src in found):
                break
            page.wait_for_timeout(step_ms)
            elapsed += step_ms

        # Last sweep of raw rendered HTML; sometimes the widget URL is present as
        # escaped text/script content rather than as an iframe src attribute.
        try:
            html_text = page.content()
            for match in re.finditer(r"(?:https?:)?//[^\s\"'<>]+zomato\.com/[^\s\"'<>]*(?:daily_menu\.php|entity_id=)[^\s\"'<>]*", html_text, flags=re.I):
                value = html.unescape(match.group(0)).replace("&amp;", "&").rstrip(".,);]")
                if value.startswith("//"):
                    value = "https:" + value
                add(value)
        except Exception:
            pass

    return found


def fetch_html(url: str, timeout_seconds: int, allow_playwright_fallback: bool = False) -> FetchResult:
    try:
        return fetch_html_requests(url, timeout_seconds)
    except Exception:
        if not allow_playwright_fallback:
            raise
        return fetch_html_playwright(url, timeout_seconds)


def make_soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def find_pdf_links(page_url: str, soup: BeautifulSoup) -> list[str]:
    links: list[str] = []
    for tag in soup.find_all("a", href=True):
        href = tag.get("href", "")
        text = tag.get_text(" ", strip=True)
        if ".pdf" in href.lower() or text.lower().endswith(".pdf"):
            links.append(urljoin(page_url, href))
    return links


def find_external_links(page_url: str, soup: BeautifulSoup, keywords: list[str]) -> list[str]:
    found: list[str] = []
    for tag in soup.find_all("a", href=True):
        href = urljoin(page_url, tag["href"])
        label = tag.get_text(" ", strip=True)
        combined = f"{href} {label}".casefold()
        if any(keyword.casefold() in combined for keyword in keywords):
            found.append(href)
    return found


def extract_pdf_text_from_url(url: str, timeout_seconds: int) -> str:
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:
        raise RuntimeError("PyMuPDF is not installed. Run: pip install PyMuPDF") from exc

    response = requests.get(url, headers=DEFAULT_HEADERS, timeout=timeout_seconds)
    response.raise_for_status()

    content_type = response.headers.get("Content-Type", "").casefold()
    content_start = response.content[:10]
    if "pdf" not in content_type and not content_start.startswith(b"%PDF"):
        raise RuntimeError(
            f"The linked file does not look like a PDF. Content-Type was: {content_type or 'unknown'}"
        )

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as temp_file:
        temp_file.write(response.content)
        temp_file.flush()
        doc = fitz.open(temp_file.name)
        try:
            return "\n".join(page.get_text() for page in doc)
        finally:
            doc.close()


def _download_pdf_bytes(url: str, timeout_seconds: int) -> bytes:
    response = requests.get(url, headers=DEFAULT_HEADERS, timeout=timeout_seconds)
    response.raise_for_status()
    content_start = response.content[:10]
    content_type = response.headers.get("Content-Type", "").casefold()
    if not content_start.startswith(b"%PDF") and "pdf" not in content_type:
        raise RuntimeError(f"URL does not look like a PDF. Content-Type was: {content_type or 'unknown'}")
    return response.content


def render_pdf_pages_from_url(
    url: str,
    output_dir: str | Path,
    filename_prefix: str,
    timeout_seconds: int,
    max_pages: int = 2,
) -> list[Path]:
    """Render the first pages of a linked PDF as PNG files for the report.

    This is a better fallback than a browser screenshot for restaurants that publish the menu as a PDF.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:
        raise RuntimeError("PDF screenshots need PyMuPDF. Run: python -m pip install PyMuPDF") from exc

    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    pdf_bytes = _download_pdf_bytes(url, timeout_seconds)

    paths: list[Path] = []
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        page_count = min(max_pages, len(doc))
        for index in range(page_count):
            page = doc[index]
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            path = output / f"{filename_prefix}-pdf-page-{index + 1}.png"
            pix.save(str(path))
            paths.append(path)
    finally:
        doc.close()
    return paths


def _goto_best_effort(page, url: str, timeout_seconds: int) -> None:
    try:
        page.goto(url, wait_until="networkidle", timeout=timeout_seconds * 1000)
    except Exception:
        # Some restaurant pages keep network requests open. DOMContentLoaded is enough for screenshots.
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_seconds * 1000)


def _dismiss_common_banners(page) -> None:
    # Try to close common cookie banners. Failing to click is harmless.
    for pattern in [
        r"Souhlasím",
        r"Přijmout",
        r"Přijmout vše",
        r"Accept",
        r"Accept all",
        r"OK",
        r"Rozumím",
        r"Allow all",
        r"Agree",
    ]:
        try:
            page.get_by_role("button", name=re.compile(pattern, re.I)).click(timeout=700)
            break
        except Exception:
            pass


def _safe_clip(clip: dict, page_width: int, page_height: int) -> dict | None:
    x = max(0, int(clip.get("x", 0)))
    y = max(0, int(clip.get("y", 0)))
    width = max(1, min(int(clip.get("width", page_width)), page_width - x))
    height = max(1, min(int(clip.get("height", 800)), page_height - y))
    if width <= 10 or height <= 10:
        return None
    return {"x": x, "y": y, "width": width, "height": height}


def capture_page_screenshot(url: str, output_path: str | Path, timeout_seconds: int) -> Path:
    """Save a full-page screenshot of a problematic menu page.

    This uses Playwright because it can render JavaScript-heavy pages and visual/image menus.
    It does not bypass CAPTCHAs, logins, or hard anti-bot protections; in those cases it will
    simply save the best page view it can load or raise a clear error.
    """
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    screenshot_hint = "Screenshots need Playwright. Run: python -m pip install playwright && python -m playwright install chromium"
    with playwright_page(viewport={"width": 1365, "height": 1800}, error_hint=screenshot_hint) as page:
        _goto_best_effort(page, url, timeout_seconds)
        _dismiss_common_banners(page)
        page.screenshot(path=str(output), full_page=True)

    return output


def capture_menu_screenshots(
    url: str,
    output_dir: str | Path,
    filename_prefix: str,
    timeout_seconds: int,
    max_keyword_shots: int = 3,
    max_image_shots: int = 3,
    max_slice_shots: int = 5,
) -> list[Path]:
    """Capture several useful screenshots for a page that could not be parsed.

    Strategy:
    1. If the URL is a PDF, render PDF pages directly.
    2. Render the page with Playwright.
    3. Search the DOM for elements containing menu-related words and screenshot those areas.
    4. Screenshot large images, because some restaurants publish the menu as an image.
    5. Add viewport slices through the whole page so the menu is still visible even if keyword search fails.

    This does not bypass logins, CAPTCHAs, or hard bot protection. It only captures publicly rendered content.
    """
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    if ".pdf" in url.casefold():
        return render_pdf_pages_from_url(
            url,
            output,
            filename_prefix,
            timeout_seconds=timeout_seconds,
            max_pages=2,
        )

    paths: list[Path] = []
    used_y: list[float] = []

    def y_is_duplicate(y: float, tolerance: float = 180) -> bool:
        return any(abs(y - seen) < tolerance for seen in used_y)

    screenshot_hint = "Smart screenshots need Playwright. Run: python -m pip install playwright && python -m playwright install chromium"
    with playwright_page(viewport={"width": 1365, "height": 950}, error_hint=screenshot_hint) as page:
        _goto_best_effort(page, url, timeout_seconds)
        _dismiss_common_banners(page)
        page.wait_for_timeout(800)

        # 0) Iframe candidates. Some restaurants embed the whole menu in a separate
        # document. Main-page text search will miss that unless we inspect page.frames.
        iframe_index = 1
        lower_keywords = [keyword.casefold() for keyword in MENU_SCREENSHOT_KEYWORDS]
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            if len([p for p in paths if "iframe" in p.name]) >= max_keyword_shots:
                break
            try:
                frame_text = frame.locator("body").inner_text(timeout=1500)
            except Exception:
                frame_text = ""
            if not frame_text or not any(keyword in frame_text.casefold() for keyword in lower_keywords):
                continue
            try:
                element = frame.frame_element()
                box = element.bounding_box()
                if not box:
                    continue
                path = output / f"{filename_prefix}-iframe-{iframe_index}.png"
                element.screenshot(path=str(path))
                paths.append(path)
                used_y.append(float(box.get("y", 0)))
                iframe_index += 1
            except Exception:
                pass

        page_size = page.evaluate(
            """
            () => ({
                width: Math.max(document.documentElement.scrollWidth, document.body.scrollWidth, 1365),
                height: Math.max(document.documentElement.scrollHeight, document.body.scrollHeight, 950)
            })
            """
        )
        page_width = int(page_size.get("width", 1365))
        page_height = int(page_size.get("height", 950))

        # 1) Text/DOM candidates around menu-related words.
        keyword_candidates = page.evaluate(
            """
            (keywords) => {
                const lowerKeywords = keywords.map(k => k.toLocaleLowerCase('cs-CZ'));
                const elements = Array.from(document.querySelectorAll('body *'));
                const candidates = [];

                for (const el of elements) {
                    const style = window.getComputedStyle(el);
                    if (style.visibility === 'hidden' || style.display === 'none') continue;

                    const rect = el.getBoundingClientRect();
                    if (rect.width < 160 || rect.height < 20) continue;
                    if (rect.top + window.scrollY < 0) continue;

                    const text = (el.innerText || el.textContent || '').trim();
                    if (!text || text.length < 4) continue;
                    if (text.length > 5000) continue;

                    const lower = text.toLocaleLowerCase('cs-CZ');
                    let score = 0;
                    for (const kw of lowerKeywords) {
                        if (lower.includes(kw)) score += kw.length;
                    }
                    if (!score) continue;

                    // Avoid selecting the whole page wrapper when a smaller element also matches.
                    const area = rect.width * rect.height;
                    const y = rect.top + window.scrollY;
                    candidates.push({
                        x: Math.max(0, rect.left + window.scrollX - 20),
                        y: Math.max(0, y - 140),
                        width: Math.min(document.documentElement.scrollWidth, rect.width + 40),
                        height: Math.min(1200, Math.max(480, rect.height + 280)),
                        score,
                        area,
                        text: text.slice(0, 160)
                    });
                }

                return candidates
                    .sort((a, b) => (b.score - a.score) || (a.area - b.area))
                    .slice(0, 12);
            }
            """,
            MENU_SCREENSHOT_KEYWORDS,
        )

        shot_index = 1
        for candidate in keyword_candidates:
            if len([p for p in paths if "keyword" in p.name]) >= max_keyword_shots:
                break
            y = float(candidate.get("y", 0))
            if y_is_duplicate(y):
                continue
            clip = _safe_clip(candidate, page_width, page_height)
            if not clip:
                continue
            path = output / f"{filename_prefix}-keyword-{shot_index}.png"
            page.screenshot(path=str(path), clip=clip)
            paths.append(path)
            used_y.append(y)
            shot_index += 1

        # 2) Large image candidates. Useful when the menu is not text at all.
        image_candidates = page.evaluate(
            """
            () => {
                const images = Array.from(document.images || []);
                const candidates = [];
                for (const img of images) {
                    const rect = img.getBoundingClientRect();
                    const style = window.getComputedStyle(img);
                    if (style.visibility === 'hidden' || style.display === 'none') continue;
                    if (rect.width < 220 || rect.height < 140) continue;
                    const y = rect.top + window.scrollY;
                    const src = img.currentSrc || img.src || '';
                    candidates.push({
                        x: Math.max(0, rect.left + window.scrollX - 20),
                        y: Math.max(0, y - 80),
                        width: Math.min(document.documentElement.scrollWidth, rect.width + 40),
                        height: Math.min(1400, Math.max(420, rect.height + 160)),
                        area: rect.width * rect.height,
                        src
                    });
                }
                return candidates.sort((a, b) => b.area - a.area).slice(0, 8);
            }
            """
        )

        image_index = 1
        for candidate in image_candidates:
            if len([p for p in paths if "image" in p.name]) >= max_image_shots:
                break
            y = float(candidate.get("y", 0))
            if y_is_duplicate(y, tolerance=260):
                continue
            clip = _safe_clip(candidate, page_width, page_height)
            if not clip:
                continue
            path = output / f"{filename_prefix}-image-{image_index}.png"
            page.screenshot(path=str(path), clip=clip)
            paths.append(path)
            used_y.append(y)
            image_index += 1

        # 3) Page slices. This is the safety net when the menu is lower on the page or hidden in a section.
        # We include a few slices even if keyword/image screenshots were found, because it helps diagnose
        # when the wrong section was selected.
        viewport_height = 950
        step = 850
        max_scroll_y = max(0, page_height - viewport_height)
        slice_index = 1
        y = 0
        while y <= max_scroll_y and slice_index <= max_slice_shots:
            if not y_is_duplicate(float(y), tolerance=320):
                page.evaluate("(y) => window.scrollTo(0, y)", y)
                page.wait_for_timeout(250)
                path = output / f"{filename_prefix}-slice-{slice_index}.png"
                page.screenshot(path=str(path), full_page=False)
                paths.append(path)
                used_y.append(float(y))
                slice_index += 1
            y += step

        # If the page is very short or every slice was considered duplicate, at least save one viewport.
        if not paths:
            path = output / f"{filename_prefix}-viewport.png"
            page.screenshot(path=str(path), full_page=False)
            paths.append(path)

    return paths


def save_page_debug_dump(
    url: str,
    output_dir: str | Path,
    filename_prefix: str,
    timeout_seconds: int,
) -> list[Path]:
    """Save rendered visible text and HTML for a problematic page.

    This is meant for parser tuning. If a screenshot shows the menu but parsing failed, these
    files help you see whether the text is present in the DOM or only inside an image/PDF.
    """
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []

    if ".pdf" in url.casefold():
        try:
            text = extract_pdf_text_from_url(url, timeout_seconds)
        except Exception as exc:
            text = f"Could not extract PDF text: {exc}\nSource: {url}"
        text_path = output / f"{filename_prefix}-pdf-text.txt"
        text_path.write_text(text, encoding="utf-8")
        paths.append(text_path)
        return paths

    debug_hint = "Debug dumps need Playwright. Run: python -m pip install playwright && python -m playwright install chromium"
    with playwright_page(viewport={"width": 1365, "height": 950}, error_hint=debug_hint) as page:
        _goto_best_effort(page, url, timeout_seconds)
        _dismiss_common_banners(page)
        page.wait_for_timeout(800)

        text_parts = ["# Main page\n", page.evaluate("() => document.body ? document.body.innerText : ''") or ""]
        html_parts = ["<!-- Main page -->\n", page.content()]

        for index, frame in enumerate(page.frames):
            if frame == page.main_frame:
                continue
            frame_url = frame.url or "about:blank"
            text_parts.append(f"\n\n# IFRAME {index}: {frame_url}\n")
            html_parts.append(f"\n\n<!-- IFRAME {index}: {html.escape(frame_url)} -->\n")
            try:
                text_parts.append(frame.locator("body").inner_text(timeout=1500))
            except Exception as exc:
                text_parts.append(f"Could not read frame text: {exc}")
            try:
                html_parts.append(frame.content())
            except Exception as exc:
                html_parts.append(f"<!-- Could not read frame HTML: {html.escape(str(exc))} -->")

        visible_text = "".join(text_parts)
        rendered_html = "".join(html_parts)

        text_path = output / f"{filename_prefix}-visible-text.txt"
        html_path = output / f"{filename_prefix}-rendered-with-iframes.html"
        text_path.write_text(visible_text, encoding="utf-8")
        html_path.write_text(rendered_html, encoding="utf-8")
        paths.extend([text_path, html_path])

    return paths
