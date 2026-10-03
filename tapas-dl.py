#!/usr/bin/env python3

import argparse
import http.cookiejar
import json
import os
import random
import re
import time
from pathlib import Path
from urllib.parse import urlparse, urljoin, unquote

import requests
from pyquery import PyQuery as pq

# ============================================================================
# Optional Playwright
# ============================================================================

try:
    from playwright.sync_api import (
        sync_playwright,
        TimeoutError as PlaywrightTimeoutError,
    )
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

# ============================================================================
# Configuration
# ============================================================================

REQUEST_MIN_DELAY = 0
REQUEST_MAX_DELAY = 1.0

MAX_RETRIES = 2

STATE_FILENAME = ".tapas_state.json"

TAPAS_BASE_URL = "https://tapas.io"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0 Safari/537.36"
)

episode_cache = {}

# ============================================================================
# Command line
# ============================================================================

parser = argparse.ArgumentParser(
    description=(
        "Scan one or more Tapas series. Series can be supplied as IDs, "
        "slugs, URLs, a newline-separated series file, or @file."
    ),
    formatter_class=argparse.RawTextHelpFormatter,
)

parser.add_argument(
    "series",
    metavar="SERIES",
    nargs="*",
    help=(
        "Tapas series ID, slug, URL, or @file.\n"
        "\n"
        "Examples:\n"
        "  313219\n"
        "  the-survivor\n"
        "  https://tapas.io/series/313219\n"
        "  https://tapas.io/series/the-survivor\n"
        "  @series.txt\n"
    ),
)

parser.add_argument(
    "-l",
    "--series-file",
    action="append",
    default=[],
    metavar="PATH",
    help=(
        "Read series arguments from a text file. "
        "One series per line. Can be supplied multiple times."
    ),
)

parser.add_argument(
    "-f",
    "--force",
    action="store_true",
    help="Reprocess episodes marked complete in the state file.",
)

parser.add_argument(
    "-v",
    "--verbose",
    action="store_true",
    help="Enable verbose output.",
)

parser.add_argument(
    "-c",
    "--cookies",
    type=str,
    default="",
    metavar="PATH",
    help="Optional Netscape/Mozilla cookies.txt file.",
)

parser.add_argument(
    "-o",
    "--output-dir",
    type=str,
    default="",
    dest="baseDir",
    metavar="PATH",
    help="Base output directory.",
)

parser.add_argument(
    "--headed",
    action="store_true",
    help="Show Chromium while loading/scrolling/probing episodes.",
)

parser.add_argument(
    "-wuf",
    action="store_true",
    help=(
        "Allow Playwright to verify and click the first sequential locked "
        "episode when its live HTML contains the required WUF conditions."
    ),
)

parser.add_argument(
    "-tn",
    action="store_true",
    help=(
        "Download thumbnails for ALL discovered episodes. Without -tn, "
        "only thumbnails for episodes being scraped are downloaded."
    ),
)

args = parser.parse_args()

# ============================================================================
# Utility
# ============================================================================

def lead0(num, max_num):
    return str(num).zfill(max(1, len(str(max_num))))

def terminal_size():
    try:
        import fcntl
        import termios
        import struct

        th, tw, hp, wp = struct.unpack(
            "HHHH",
            fcntl.ioctl(
                0,
                termios.TIOCGWINSZ,
                struct.pack("HHHH", 0, 0, 0, 0),
            ),
        )

        return tw, th

    except (IOError, ModuleNotFoundError, OSError):
        return 200, 80

def printLine(msg="", noNewLine=False):
    terminal_width = terminal_size()[0]
    msg = str(msg)

    spaces = max(
        0,
        terminal_width - len(msg),
    )

    if noNewLine:

        if args.verbose:
            print(
                " " + msg +
                (" " * max(0, spaces - 1))
            )
        else:
            print(
                msg + (" " * spaces),
                end="\r",
                flush=True,
            )

    else:
        print(
            msg + (" " * spaces)
        )

def clean_text(value):
    if not value:
        return ""

    value = re.sub(
        r"\s+",
        " ",
        str(value),
    )

    return value.strip()

# ============================================================================
# Filename/path sanitization
# ============================================================================

INVALID_FILENAME_RE = re.compile(
    r'[<>:"/\\|?*\x00-\x1F\x7F]'
)

WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "COM1",
    "COM2",
    "COM3",
    "COM4",
    "COM5",
    "COM6",
    "COM7",
    "COM8",
    "COM9",
    "LPT1",
    "LPT2",
    "LPT3",
    "LPT4",
    "LPT5",
    "LPT6",
    "LPT7",
    "LPT8",
    "LPT9",
}

def safe_filename(
    value,
    fallback="untitled",
    max_length=180,
):
    if value is None:
        value = ""

    value = str(value)

    value = INVALID_FILENAME_RE.sub(
        "",
        value,
    )

    value = value.strip().rstrip(". ")

    value = re.sub(
        r"\s+",
        " ",
        value,
    ).strip()

    if not value:
        value = fallback

    stem = value.split(".", 1)[0].upper()

    if stem in WINDOWS_RESERVED_NAMES:
        value = "_" + value

    if len(value) > max_length:
        value = value[:max_length].rstrip(". ")

    if not value:
        value = fallback

    return value

def safe_path_component(
    value,
    fallback="untitled",
):
    return safe_filename(
        value,
        fallback=fallback,
        max_length=150,
    )

def get_extension(
    url,
    default="jpg",
):
    try:
        path = urlparse(url).path

        extension = os.path.splitext(
            path
        )[1].lstrip(".").lower()

        if extension:
            extension = re.sub(
                r"[^a-z0-9]",
                "",
                extension,
            )

            if extension:
                return extension

    except Exception:
        pass

    return default

# ============================================================================
# HTTP
# ============================================================================

def polite_delay():
    delay = random.uniform(
        REQUEST_MIN_DELAY,
        REQUEST_MAX_DELAY,
    )

    if delay > 0:
        time.sleep(delay)

def request_with_backoff(
    session,
    method,
    url,
    **kwargs,
):
    last_response = None

    for attempt in range(MAX_RETRIES):

        polite_delay()

        try:
            response = session.request(
                method,
                url,
                timeout=60,
                **kwargs,
            )

            last_response = response

        except requests.RequestException as e:

            printLine(
                f"Network error: {e}"
            )

            if attempt + 1 < MAX_RETRIES:

                wait = min(
                    60,
                    5 * (2 ** attempt),
                )

                printLine(
                    f"Waiting {wait} seconds before retrying...",
                    True,
                )

                time.sleep(wait)

            continue

        if response.status_code < 400:
            return response

        if response.status_code == 429:

            retry_after = response.headers.get(
                "Retry-After"
            )

            try:
                wait = float(retry_after)
            except (TypeError, ValueError):
                wait = min(
                    300,
                    30 * (2 ** attempt),
                )

            printLine(
                "HTTP 429: rate limited."
            )

            if attempt + 1 < MAX_RETRIES:

                printLine(
                    f"Waiting {wait:.0f} seconds before retrying...",
                    True,
                )

                time.sleep(wait)

            continue

        if response.status_code == 403:

            printLine(
                "HTTP 403 Forbidden."
            )

            if attempt + 1 < MAX_RETRIES:

                wait = 120

                printLine(
                    f"Waiting {wait} seconds before retrying...",
                    True,
                )

                time.sleep(wait)

            continue

        if response.status_code in (
            500,
            502,
            503,
            504,
        ):

            printLine(
                f"HTTP {response.status_code}."
            )

            if attempt + 1 < MAX_RETRIES:

                wait = min(
                    120,
                    10 * (2 ** attempt),
                )

                printLine(
                    f"Waiting {wait} seconds before retrying...",
                    True,
                )

                time.sleep(wait)

            continue

        return response

    return last_response

# ============================================================================
# Input-file handling
# ============================================================================

def read_series_file(path):
    values = []

    try:
        with open(
            path,
            "r",
            encoding="utf-8",
        ) as f:

            for line in f:

                line = line.strip()

                if not line:
                    continue

                if line.startswith("#"):
                    continue

                values.append(line)

    except OSError as e:

        printLine(
            f"Could not read series file '{path}': {e}"
        )

    return values

def collect_series_inputs():
    values = []

    for value in args.series:

        value = value.strip()

        if not value:
            continue

        if value.startswith("@"):

            filename = value[1:].strip()

            if filename:
                values.extend(
                    read_series_file(filename)
                )

            continue

        if os.path.isfile(value):

            values.extend(
                read_series_file(value)
            )

            continue

        values.append(value)

    for filename in args.series_file:

        values.extend(
            read_series_file(filename)
        )

    return values

series_inputs = collect_series_inputs()

if not series_inputs:

    parser.error(
        "At least one series, @file, or --series-file is required."
    )

# ============================================================================
# Series input normalization
# ============================================================================

def normalize_series_input(value):
    value = str(value).strip()

    if not value:
        raise ValueError(
            "Empty series value."
        )

    if re.match(
        r"^https?://",
        value,
        flags=re.IGNORECASE,
    ):

        parsed = urlparse(value)

        if parsed.netloc.lower() not in (
            "tapas.io",
            "www.tapas.io",
        ):
            raise ValueError(
                "URL is not a Tapas series URL."
            )

        path = parsed.path.rstrip("/")

        match = re.match(
            r"^/series/([^/]+)(?:/info)?$",
            path,
            flags=re.IGNORECASE,
        )

        if not match:

            raise ValueError(
                "Expected a URL such as "
                "https://tapas.io/series/313219 "
                "or https://tapas.io/series/the-survivor"
            )

        component = unquote(
            match.group(1)
        )

    else:

        value = value.strip("/")

        if value.lower().startswith("series/"):
            value = value[7:]

        value = value.rstrip("/")

        if value.lower().endswith("/info"):
            value = value[:-5].rstrip("/")

        component = value

    component = component.strip()

    if not component:
        raise ValueError(
            "Could not determine the Tapas series component."
        )

    if "/" in component or "\\" in component:
        raise ValueError(
            "Invalid series component."
        )

    series_url = (
        f"{TAPAS_BASE_URL}/series/{component}"
    )

    info_url = (
        f"{series_url}/info"
    )

    return (
        series_url,
        info_url,
        component,
    )

# ============================================================================
# Canonical series identity
# ============================================================================

def canonicalize_series(
    info_page,
    requested_series_url,
    requested_info_url,
    requested_component,
):
    canonical_url = ""

    if info_page is not None:

        selectors = [
            'link[rel="canonical"]',
            'meta[property="og:url"]',
        ]

        for selector in selectors:

            for element in info_page(selector):

                element = pq(element)

                candidate = (
                    element.attr("href")
                    or element.attr("content")
                    or ""
                ).strip()

                if not candidate:
                    continue

                candidate = urljoin(
                    TAPAS_BASE_URL,
                    candidate,
                )

                parsed = urlparse(candidate)

                if parsed.netloc.lower() in (
                    "tapas.io",
                    "www.tapas.io",
                ):

                    match = re.match(
                        r"^/series/([^/]+)(?:/info)?/?$",
                        parsed.path,
                        flags=re.IGNORECASE,
                    )

                    if match:

                        canonical_url = (
                            f"{TAPAS_BASE_URL}/series/"
                            f"{match.group(1)}"
                        )

                        break

            if canonical_url:
                break

    if not canonical_url:
        canonical_url = requested_series_url

    parsed = urlparse(
        canonical_url
    )

    match = re.match(
        r"^/series/([^/]+)",
        parsed.path,
        flags=re.IGNORECASE,
    )

    if match:
        canonical_component = unquote(
            match.group(1)
        )
    else:
        canonical_component = requested_component

    canonical_component = (
        canonical_component.strip()
    )

    canonical_series_url = (
        f"{TAPAS_BASE_URL}/series/"
        f"{canonical_component}"
    )

    canonical_info_url = (
        f"{canonical_series_url}/info"
    )

    canonical_id = None

    if canonical_component.isdigit():
        canonical_id = int(
            canonical_component
        )

    if canonical_id is None and info_page is not None:

        html = ""

        try:
            html = info_page.outer_html()
        except Exception:
            pass

        patterns = [
            r'"seriesId"\s*:\s*"(\d+)"',
            r'"series_id"\s*:\s*"(\d+)"',
            r'data-series-id=["\'](\d+)',
        ]

        for pattern in patterns:

            match = re.search(
                pattern,
                html,
                flags=re.IGNORECASE,
            )

            if match:

                try:
                    canonical_id = int(
                        match.group(1)
                    )
                    break
                except ValueError:
                    pass

    return {
        "canonical_component": canonical_component,
        "canonical_id": canonical_id,
        "series_url": canonical_series_url,
        "info_url": canonical_info_url,
    }

# ============================================================================
# State
# ============================================================================

def default_state():
    return {
        "version": 4,

        "series": {
            "name": "",
            "description": "",
            "genres": [],
            "creators": [],
            "publishing_details": "",
            "thumbnail_url": "",
            "header_url": "",
            "canonical_component": "",
            "canonical_id": None,
            "series_url": "",
            "info_url": "",
        },

        "first_episode_id": None,

        "episodes": {},
    }

def get_state_path(save_path):
    return os.path.join(
        save_path,
        STATE_FILENAME,
    )

def load_state(save_path):
    path = get_state_path(
        save_path
    )

    if not os.path.isfile(path):
        return default_state()

    try:

        with open(
            path,
            "r",
            encoding="utf-8",
        ) as f:

            state = json.load(f)

        if not isinstance(state, dict):
            return default_state()

        base = default_state()

        for key, value in base.items():

            if key not in state:
                state[key] = value

        if not isinstance(
            state.get("series"),
            dict,
        ):
            state["series"] = {}

        for key, value in base["series"].items():

            if key not in state["series"]:
                state["series"][key] = value

        if not isinstance(
            state.get("episodes"),
            dict,
        ):
            state["episodes"] = {}

        return state

    except (
        OSError,
        ValueError,
        json.JSONDecodeError,
    ) as e:

        printLine(
            f"Could not read state file: {e}"
        )

        return default_state()

def save_state(
    save_path,
    state,
):
    path = get_state_path(
        save_path
    )

    temp_path = path + ".tmp"

    try:

        with open(
            temp_path,
            "w",
            encoding="utf-8",
        ) as f:

            json.dump(
                state,
                f,
                indent=2,
                ensure_ascii=False,
            )

            f.write("\n")

        os.replace(
            temp_path,
            path,
        )

    except OSError as e:

        printLine(
            f"Could not save state: {e}"
        )

        try:
            if os.path.isfile(temp_path):
                os.remove(temp_path)
        except OSError:
            pass

def set_episode_state(
    save_path,
    state,
    episode_id,
    status,
    title="",
    image_count=0,
    thumbnail="",
    episode_number=None,
):
    episode_id = str(
        episode_id
    )

    state["episodes"][episode_id] = {
        "status": status,
        "title": title,
        "image_count": image_count,
        "thumbnail": thumbnail,
        "episode_number": episode_number,
        "updated": int(time.time()),
    }

    save_state(
        save_path,
        state,
    )

# ============================================================================
# Series metadata
# ============================================================================

def get_series_name(
    info_page,
    component,
):
    if info_page is None:
        return safe_filename(
            component.replace(
                "-",
                " ",
            ).title()
        )

    selectors = [
        ".series-title",
        ".info-title",
        ".center-info__title",
        ".center-info__title--small",
        "h1.series-title",
        "h1",
    ]

    for selector in selectors:

        value = clean_text(
            info_page(selector).text()
        )

        if value:
            return value

    og_title = info_page(
        'meta[property="og:title"]'
    ).attr("content")

    if og_title:

        value = clean_text(
            og_title
        )

        value = re.sub(
            r"^Read\s+",
            "",
            value,
            flags=re.IGNORECASE,
        )

        value = re.sub(
            r"\s*\|\s*Tapas.*$",
            "",
            value,
            flags=re.IGNORECASE,
        )

        if value:
            return value

    return component.replace(
        "-",
        " ",
    ).title()

def get_series_description(info_page):
    if info_page is None:
        return ""

    selectors = [
        "p.description.js-series-description",
        "p.description",
        ".js-series-description",
        ".description__body",
    ]

    for selector in selectors:

        value = clean_text(
            info_page(selector).text()
        )

        if value:
            return value

    return ""

def get_series_genres(info_page):
    if info_page is None:
        return []

    genres = []

    for element in info_page(".genre-btn"):

        value = clean_text(
            pq(element).text()
        )

        if value and value not in genres:
            genres.append(value)

    return genres

def get_series_creators(info_page):
    if info_page is None:
        return []

    creators = []

    selectors = [
        ".creator-section .creator-info__top a.name",
        ".creator-info a.name",
        "a.creator-name",
    ]

    for selector in selectors:

        for element in info_page(selector):

            value = clean_text(
                pq(element).text()
            )

            if value and value not in creators:
                creators.append(value)

        if creators:
            break

    return creators

def get_publishing_details(info_page):
    if info_page is None:
        return ""

    return clean_text(
        info_page(".colophon").text()
    )

def extract_css_background_url(style):
    if not style:
        return None

    match = re.search(
        r"background-image\s*:\s*url\(\s*['\"]?(.*?)['\"]?\s*\)",
        style,
        flags=re.IGNORECASE,
    )

    if not match:
        return None

    value = match.group(1).strip()

    if value.startswith("//"):
        value = "https:" + value

    return value or None

def get_series_header(info_page):
    if info_page is None:
        return None

    selectors = [
        ".info.info--top",
        ".js-top-banner",
    ]

    for selector in selectors:

        for element in info_page(selector):

            url = extract_css_background_url(
                pq(element).attr("style")
            )

            if url:
                return url

    return None

def get_series_thumbnail(info_page):
    if info_page is None:
        return None

    selectors = [
        "a.thumb.js-thumbnail img",
        ".thumb-wrapper img",
        'meta[property="og:image"]',
    ]

    for selector in selectors:

        for element in info_page(selector):

            element = pq(element)

            src = (
                element.attr("src")
                or element.attr("data-src")
                or element.attr("data-original")
                or element.attr("content")
            )

            if src:

                src = src.strip()

                if src.startswith("//"):
                    src = "https:" + src

                return src

    return None

def get_series_metadata(
    info_page,
    canonical,
):
    return {
        "name": get_series_name(
            info_page,
            canonical["canonical_component"],
        ),

        "description": get_series_description(
            info_page
        ),

        "genres": get_series_genres(
            info_page
        ),

        "creators": get_series_creators(
            info_page
        ),

        "publishing_details": get_publishing_details(
            info_page
        ),

        "thumbnail_url": get_series_thumbnail(
            info_page
        ),

        "header_url": get_series_header(
            info_page
        ),

        "canonical_component": (
            canonical["canonical_component"]
        ),

        "canonical_id": (
            canonical["canonical_id"]
        ),

        "series_url": canonical["series_url"],

        "info_url": canonical["info_url"],
    }

# ============================================================================
# Playwright cookie handling
# ============================================================================

def load_cookies_into_playwright(
    browser_context,
    session,
):
    cookies = []

    try:

        for cookie in session.cookies:

            domain = cookie.domain or "tapas.io"

            if not domain:
                domain = "tapas.io"

            cookie_data = {
                "name": cookie.name,
                "value": cookie.value,
                "domain": domain,
                "path": cookie.path or "/",
            }

            if cookie.expires:
                cookie_data["expires"] = cookie.expires

            cookies.append(
                cookie_data
            )

    except Exception as e:

        printLine(
            f"Could not transfer cookies: {e}"
        )

    if cookies:

        try:

            browser_context.add_cookies(
                cookies
            )

        except Exception as e:

            printLine(
                f"Could not add browser cookies: {e}"
            )

def update_session_from_playwright(
    browser_context,
    session,
):
    try:

        cookies = browser_context.cookies()

        for cookie in cookies:

            session.cookies.set(
                cookie["name"],
                cookie["value"],
                domain=cookie.get("domain"),
                path=cookie.get("path", "/"),
            )

    except Exception:
        pass

# ============================================================================
# Series /info
# ============================================================================

def get_series_info_page(
    session,
    info_url,
):
    response = request_with_backoff(
        session,
        "GET",
        info_url,
    )

    if response is None:
        return None

    if response.status_code != 200:

        printLine(
            f"Could not load {info_url}: "
            f"HTTP {response.status_code}"
        )

        return None

    return pq(
        response.content
    )

# ============================================================================
# Episode list parsing
# ============================================================================

def parse_episode_number(element):
    element = pq(element)

    value = (
        element.attr("data-scene-number")
        or ""
    )

    try:
        number = int(
            str(value).strip()
        )

        if number > 0:
            return number

    except (TypeError, ValueError):
        pass

    scene = clean_text(
        element(".scene").text()
    )

    match = re.search(
        r"episode\s+(\d+)",
        scene,
        flags=re.IGNORECASE,
    )

    if match:
        return int(
            match.group(1)
        )

    title = clean_text(
        element(".title__body").text()
    )

    match = re.search(
        r"episode\s+(\d+)",
        title,
        flags=re.IGNORECASE,
    )

    if match:
        return int(
            match.group(1)
        )

    return None

def episode_anchor_is_locked(anchor):
    anchor = pq(anchor)

    if len(
        anchor(".thumb-overlay--locked")
    ) > 0:
        return True

    classes = set(
        clean_text(
            anchor.attr("class") or ""
        ).split()
    )

    if "js-unlock" in classes:
        return True

    if "body__item--opaque" in classes:
        return True

    if len(
        anchor(".ico--lock")
    ) > 0:
        return True

    data_locked = (
        anchor.attr("data-is-locked")
        or anchor.attr("data-locked")
    )

    if str(data_locked).lower() in (
        "true",
        "1",
        "yes",
    ):
        return True

    return False

def parse_episode_list_html(html):
    page = pq(html)

    episodes = []

    items = page(
        "ul.episode-list.js-episode-list "
        "li.episode-list__item"
    )

    if len(items) == 0:

        items = page(
            "ul.js-episode-list li"
        )

    for item_element in items:

        item = pq(item_element)

        anchor = item(
            'a.episode-item[href*="/episode/"]'
        )

        if len(anchor) == 0:

            anchor = item(
                'a[href*="/episode/"]'
            )

        if len(anchor) == 0:
            continue

        anchor = pq(
            anchor[0]
        )

        href = (
            anchor.attr("href")
            or ""
        ).strip()

        match = re.search(
            r"/episode/(\d+)",
            href,
        )

        if not match:
            continue

        episode_id = int(
            match.group(1)
        )

        episode_number = parse_episode_number(
            anchor
        )

        title = clean_text(
            anchor(".title__body").text()
        )

        if not title:

            title = clean_text(
                anchor(".title").text()
            )

        if not title and episode_number:

            title = (
                f"Episode {episode_number}"
            )

        if not title:

            title = (
                f"Episode {episode_id}"
            )

        thumbnail_url = None

        image = anchor(
            ".thumb img"
        )

        if len(image) > 0:

            image = pq(
                image[0]
            )

            thumbnail_url = (
                image.attr("src")
                or image.attr("data-src")
                or image.attr("data-original")
                or image.attr("data-lazy-src")
            )

            if thumbnail_url:

                thumbnail_url = (
                    thumbnail_url.strip()
                )

                if thumbnail_url.startswith("//"):
                    thumbnail_url = (
                        "https:" + thumbnail_url
                    )

        episodes.append({
            "id": episode_id,
            "episode_number": episode_number,
            "title": title,
            "url": urljoin(
                TAPAS_BASE_URL + "/",
                href,
            ),
            "thumbnail_url": thumbnail_url,
            "locked": episode_anchor_is_locked(
                anchor
            ),
        })

    unique = {}

    for episode in episodes:
        unique[
            episode["id"]
        ] = episode

    episodes = list(
        unique.values()
    )

    if episodes:

        if all(
            episode["episode_number"] is not None
            for episode in episodes
        ):

            episodes.sort(
                key=lambda episode: (
                    episode["episode_number"],
                    episode["id"],
                )
            )

        else:

            episodes.sort(
                key=lambda episode: episode["id"]
            )

    return episodes

# ============================================================================
# Playwright episode-list helpers
# ============================================================================

EPISODE_LIST_SELECTOR = (
    "ul.episode-list.js-episode-list"
)

def playwright_wait_for_episode_list(
    page,
    timeout=30000,
):
    episode_list = page.locator(
        EPISODE_LIST_SELECTOR
    )

    try:

        episode_list.wait_for(
            state="attached",
            timeout=timeout,
        )

    except PlaywrightTimeoutError:

        episode_list = page.locator(
            "ul.js-episode-list"
        )

        episode_list.wait_for(
            state="attached",
            timeout=timeout,
        )

    page.wait_for_timeout(
        1000
    )

    return episode_list

def get_playwright_episode_list_html(
    episode_list,
):
    return episode_list.evaluate(
        "(el) => el.outerHTML"
    )

def scroll_episode_list_down(
    page,
    episode_list,
):
    episode_list.evaluate(
        """
        (el) => {
            el.scrollTop = el.scrollHeight;
            el.dispatchEvent(
                new Event("scroll", { bubbles: true })
            );
        }
        """
    )

    try:

        items = episode_list.locator(
            "li.episode-list__item"
        )

        if items.count() == 0:
            items = episode_list.locator("li")

        count = items.count()

        if count > 0:

            items.nth(
                count - 1
            ).scroll_into_view_if_needed(
                timeout=5000
            )

    except Exception:
        pass

    page.wait_for_timeout(
        1200
    )

def discover_all_episode_list_items(
    page,
    episode_list,
):
    """
    Discover the dynamically loaded episode list from /info.

    The scan is DOWNWARD ONLY and stops when the same episode count is
    observed twice consecutively.
    """

    discovered = {}

    previous_count = None
    repeated_count = 0

    for iteration in range(1, 201):

        html = get_playwright_episode_list_html(
            episode_list
        )

        episodes = parse_episode_list_html(
            html
        )

        for episode in episodes:
            discovered[
                episode["id"]
            ] = episode

        count = len(
            episodes
        )

        if count == previous_count:
            repeated_count += 1
        else:
            repeated_count = 0

        printLine(
            f"Episode list downward scan: "
            f"{len(discovered)} discovered; "
            f"current count={count}; "
            f"same count repeats={repeated_count}",
            True,
        )

        if repeated_count >= 1:
            break

        previous_count = count

        scroll_episode_list_down(
            page,
            episode_list,
        )

    result = list(
        discovered.values()
    )

    if result:

        if all(
            episode["episode_number"] is not None
            for episode in result
        ):

            result.sort(
                key=lambda episode: (
                    episode["episode_number"],
                    episode["id"],
                )
            )

        else:

            result.sort(
                key=lambda episode: episode["id"]
            )

    return result

# ============================================================================
# First sequential locked WUF episode
# ============================================================================

def find_first_sequential_locked_episode(
    episodes,
    state,
):
    """
    Find the earliest sequential episode that is:

      1. locked;
      2. not already complete.

    The LIVE HTML is checked later immediately before clicking.
    """

    candidates = []

    for index, episode in enumerate(
        episodes
    ):

        episode_id = str(
            episode["id"]
        )

        state_entry = state["episodes"].get(
            episode_id,
            {},
        )

        if state_entry.get(
            "status"
        ) == "complete":
            continue

        if not episode.get(
            "locked"
        ):
            continue

        episode_number = (
            episode.get(
                "episode_number"
            )
            or (index + 1)
        )

        candidates.append(
            (
                episode_number,
                episode["id"],
                episode,
            )
        )

    if not candidates:
        return None

    candidates.sort(
        key=lambda item: (
            item[0],
            item[1],
        )
    )

    return candidates[0][2]

# ============================================================================
# URL comparison
# ============================================================================

def normalize_episode_url(url):
    parsed = urlparse(
        url
    )

    path = parsed.path.rstrip("/")

    match = re.match(
        r"^/episode/(\d+)$",
        path,
        flags=re.IGNORECASE,
    )

    if not match:
        return None

    return (
        f"{TAPAS_BASE_URL}/episode/"
        f"{match.group(1)}"
    )

def url_is_episode(
    url,
    episode_id,
):
    normalized = normalize_episode_url(
        url
    )

    expected = (
        f"{TAPAS_BASE_URL}/episode/"
        f"{int(episode_id)}"
    )

    return normalized == expected

# ============================================================================
# WUF HTML condition
# ============================================================================

def episode_html_allows_click(
    link,
):
    """
    The episode may ONLY be clicked when the LIVE episode-anchor HTML
    contains BOTH:

        data-is-charging="false"

    AND:

        <span class="info__text">WUF</span>

    No click is attempted if either condition is absent.
    """

    try:

        html = link.first.evaluate(
            "(el) => el.outerHTML"
        )

    except Exception as e:

        printLine(
            f"Could not inspect episode anchor HTML: {e}"
        )

        return False

    if not html:

        printLine(
            "Episode anchor HTML was empty; refusing to click."
        )

        return False

    charging_match = re.search(
        r'\bdata-is-charging\s*=\s*["\']false["\']',
        html,
        flags=re.IGNORECASE,
    )

    wuf_match = re.search(
        r'<span\b[^>]*class\s*=\s*["\'][^"\']*\binfo__text\b[^"\']*["\'][^>]*>'
        r'\s*WUF\s*'
        r'</span>',
        html,
        flags=re.IGNORECASE,
    )

    if not charging_match:

        charging_value_match = re.search(
            r'\bdata-is-charging\s*=\s*["\']([^"\']*)["\']',
            html,
            flags=re.IGNORECASE,
        )

        if charging_value_match:

            charging_value = (
                charging_value_match.group(1)
            )

            printLine(
                f'Episode anchor has data-is-charging="{charging_value}"; '
                "click will NOT be attempted."
            )

        else:

            printLine(
                'Episode anchor does not contain data-is-charging="false"; '
                "click will NOT be attempted."
            )

    if not wuf_match:

        printLine(
            'Episode anchor does not contain '
            '<span class="info__text">WUF</span>; '
            "click will NOT be attempted."
        )

    if charging_match and wuf_match:

        printLine(
            'Episode anchor contains data-is-charging="false" '
            'and <span class="info__text">WUF</span>; '
            "click allowed."
        )

        return True

    return False

# ============================================================================
# WUF popup
# ============================================================================

def dismiss_wuf_popup_if_present(
    page,
):
    selector = (
        "button.popup-use-wuf__btn-wrapper__btn."
        "popup-use-wuf__btn-wrapper__btn__ok."
        "js-use-wuf-popup-ok"
    )

    popup_button = page.locator(
        selector
    )

    try:

        popup_button.first.wait_for(
            state="visible",
            timeout=10000,
        )

    except PlaywrightTimeoutError:

        return False

    except Exception:

        return False

    printLine(
        "WUF confirmation popup detected; clicking Yes..."
    )

    try:

        popup_button.first.click(
            timeout=10000
        )

    except Exception as e:

        printLine(
            f"Could not click WUF confirmation: {e}"
        )

        return False

    page.wait_for_timeout(
        1000
    )

    return True

# ============================================================================
# Probe the first sequential locked WUF episode
# ============================================================================

def probe_first_sequential_locked_episode(
    page,
    episodes,
    state,
):
    """
    This function is only called when -wuf was supplied.

    Workflow:

        /info
          |
          v
        scan episode list downward
          |
          v
        find first sequential locked + not complete episode
          |
          v
        scroll directly to that episode
          |
          v
        inspect LIVE HTML
          |
          +-- data-is-charging="false"
          |   AND info__text == WUF
          |       --> click it
          |
          +-- otherwise --> do NOT click
          |
          v
        if clicked:
          handle WUF "Yes" popup
          |
          v
        check URL
          |
          +-- URL == /episode/<id> --> add to scan queue
          |
          +-- otherwise -----------> do not add it

    The page is not navigated back to /info.
    """

    locked_episode = find_first_sequential_locked_episode(
        episodes,
        state,
    )

    if locked_episode is None:

        printLine(
            "No sequential locked episode requiring verification."
        )

        return episodes

    episode_id = int(
        locked_episode["id"]
    )

    expected_url = (
        f"{TAPAS_BASE_URL}/episode/"
        f"{episode_id}"
    )

    printLine()
    printLine(
        f"First sequential locked episode requiring verification: "
        f"Episode {locked_episode.get('episode_number', '?')} "
        f"({episode_id})"
    )

    printLine(
        f"Expected episode URL: {expected_url}"
    )

    link = page.locator(
        f'{EPISODE_LIST_SELECTOR} '
        f'a.episode-item[href*="/episode/{episode_id}"]'
    )

    if link.count() == 0:

        link = page.locator(
            f'{EPISODE_LIST_SELECTOR} '
            f'a[href*="/episode/{episode_id}"]'
        )

    if link.count() == 0:

        printLine(
            f"Could not locate locked episode {episode_id} "
            f"on the already-scanned /info list."
        )

        return episodes

    printLine(
        f"Scrolling directly to episode {episode_id}..."
    )

    try:

        link.first.scroll_into_view_if_needed(
            timeout=10000
        )

    except Exception as e:

        printLine(
            f"Could not scroll directly to episode "
            f"{episode_id}: {e}"
        )

    page.wait_for_timeout(
        500
    )

    if not episode_html_allows_click(
        link
    ):

        printLine(
            f"Episode {episode_id} was not clicked because "
            "its LIVE HTML does not contain both required WUF conditions."
        )

        printLine(
            "Continuing directly to the scraping phase."
        )

        return episodes

    before_url = page.url

    printLine(
        f"URL before click: {before_url}"
    )

    printLine(
        f"Clicking locked WUF episode {episode_id}..."
    )

    try:

        link.first.click(
            timeout=15000
        )

    except Exception as e:

        printLine(
            f"Could not click episode {episode_id}: {e}"
        )

        return episodes

    dismiss_wuf_popup_if_present(
        page
    )

    try:

        page.wait_for_load_state(
            "domcontentloaded",
            timeout=15000,
        )

    except PlaywrightTimeoutError:
        pass

    page.wait_for_timeout(
        1500
    )

    after_url = page.url

    printLine(
        f"URL after click/popup: {after_url}"
    )

    if url_is_episode(
        after_url,
        episode_id,
    ):

        printLine(
            f"Episode {episode_id} URL changed successfully."
        )

        printLine(
            f"Adding episode {episode_id} to scan queue."
        )

        locked_episode["locked"] = False
        locked_episode["unlocked_by_playwright"] = True

        old_state = state["episodes"].get(
            str(episode_id),
            {},
        )

        state["episodes"][
            str(episode_id)
        ] = {
            "status": (
                "queued"
                if old_state.get(
                    "status"
                ) != "complete"
                else "complete"
            ),
            "title": locked_episode.get(
                "title",
                old_state.get(
                    "title",
                    f"Episode {episode_id}",
                ),
            ),
            "image_count": old_state.get(
                "image_count",
                0,
            ),
            "thumbnail": old_state.get(
                "thumbnail",
                "",
            ),
            "episode_number": locked_episode.get(
                "episode_number"
            ),
            "updated": int(time.time()),
        }

        replaced = False

        for index, episode in enumerate(
            episodes
        ):

            if int(
                episode["id"]
            ) == episode_id:

                episodes[index] = locked_episode
                replaced = True
                break

        if not replaced:
            episodes.append(
                locked_episode
            )

        if all(
            episode.get("episode_number") is not None
            for episode in episodes
        ):

            episodes.sort(
                key=lambda episode: (
                    episode["episode_number"],
                    episode["id"],
                )
            )

        else:

            episodes.sort(
                key=lambda episode: episode["id"]
            )

        return episodes

    printLine(
        f"URL did not change to {expected_url}."
    )

    printLine(
        f"Episode {episode_id} was NOT added to the scan queue."
    )

    printLine(
        "Continuing directly to the scraping phase."
    )

    return episodes

# ============================================================================
# Complete Playwright discovery
# ============================================================================

def load_complete_episode_list(
    session,
    info_url,
    state,
):
    if not PLAYWRIGHT_AVAILABLE:

        raise RuntimeError(
            "Playwright is required to load the dynamically scrolling "
            "Tapas episode list.\n\n"
            "Install it with:\n"
            "  python -m pip install playwright\n"
            "  python -m playwright install chromium"
        )

    printLine(
        "Loading Tapas /info with Chromium..."
    )

    with sync_playwright() as playwright:

        browser = playwright.chromium.launch(
            headless=not args.headed
        )

        context = browser.new_context(
            user_agent=USER_AGENT,
            viewport={
                "width": 1440,
                "height": 1200,
            },
        )

        load_cookies_into_playwright(
            context,
            session,
        )

        page = context.new_page()

        try:

            page.goto(
                info_url,
                wait_until="domcontentloaded",
                timeout=60000,
            )

            try:

                page.wait_for_load_state(
                    "networkidle",
                    timeout=15000,
                )

            except PlaywrightTimeoutError:
                pass

            printLine(
                f"Playwright current URL: {page.url}"
            )

            episode_list = (
                playwright_wait_for_episode_list(
                    page
                )
            )

            episodes = (
                discover_all_episode_list_items(
                    page,
                    episode_list,
                )
            )

            if not episodes:

                raise RuntimeError(
                    "No episodes were found in "
                    "ul.episode-list.js-episode-list."
                )

            printLine(
                f"Initial /info discovery found "
                f"{len(episodes)} episodes."
            )

            # WUF unlocking is explicitly opt-in.
            if args.wuf:

                printLine(
                    "WUF mode enabled (-wuf); "
                    "checking the first sequential locked episode."
                )

                episodes = (
                    probe_first_sequential_locked_episode(
                        page,
                        episodes,
                        state,
                    )
                )

            else:

                printLine(
                    "WUF mode disabled; "
                    "no locked episode will be clicked."
                )

            update_session_from_playwright(
                context,
                session,
            )

            first_episode = min(
                episodes,
                key=lambda episode: (
                    episode.get(
                        "episode_number"
                    )
                    if episode.get(
                        "episode_number"
                    ) is not None
                    else 10**12,
                    episode["id"],
                )
            )

            return {
                "first_episode_url": first_episode["url"],
                "first_episode_id": first_episode["id"],
                "episodes": episodes,
            }

        finally:

            try:
                browser.close()
            except Exception:
                pass

# ============================================================================
# Episode HTTP page
# ============================================================================

def get_episode_page(
    session,
    episode_id,
    force=False,
):
    episode_id = int(
        episode_id
    )

    if (
        not force
        and episode_id in episode_cache
    ):
        return episode_cache[
            episode_id
        ], "ok"

    url = (
        f"{TAPAS_BASE_URL}/episode/"
        f"{episode_id}"
    )

    response = request_with_backoff(
        session,
        "GET",
        url,
    )

    if response is None:
        return None, "network"

    if response.status_code == 403:
        return None, "forbidden"

    if response.status_code in (
        500,
        502,
        503,
        504,
    ):
        return None, "temporary"

    if response.status_code != 200:

        printLine(
            f"Episode {episode_id} returned "
            f"HTTP {response.status_code}"
        )

        return None, "http"

    page = pq(
        response.content
    )

    episode_cache[
        episode_id
    ] = page

    return page, "ok"

def get_episode_title(
    page,
    episode_id,
    fallback="",
):
    if page is None:
        return fallback or f"Episode {episode_id}"

    selectors = [
        ".info__title",
        ".viewer__header > .title",
        ".viewer__header .title",
    ]

    for selector in selectors:

        title = clean_text(
            page(selector).text()
        )

        if title:
            return title

    return fallback or f"Episode {episode_id}"

# ============================================================================
# Episode lock verification
# ============================================================================

def is_episode_locked(
    page,
    episode_id,
):
    if page is None:
        return True

    selectors = [
        f"#ep-{episode_id}",
        f'[data-id="{episode_id}"]',
        f'[data-ep-id="{episode_id}"]',
    ]

    for selector in selectors:

        for element in page(selector):

            element = pq(element)

            classes = set(
                clean_text(
                    element.attr("class") or ""
                ).split()
            )

            if "js-unlock" in classes:
                return True

            if "body__item--opaque" in classes:
                return True

            if len(
                element(".thumb__overlay--locked")
            ) > 0:
                return True

            if len(
                element(".ico--lock")
            ) > 0:
                return True

    locked_selectors = [
        ".viewer__locked",
        ".viewer--locked",
        ".episode-locked",
        ".locked-episode",
        ".js-viewer-locked",
        ".js-episode-locked",
    ]

    for selector in locked_selectors:

        if len(page(selector)) > 0:
            return True

    return False

# ============================================================================
# Images
# ============================================================================

def get_image_url(img):
    img = pq(img)

    attributes = [
        "data-src",
        "src",
        "data-original",
        "data-lazy-src",
        "data-image",
        "data-url",
        "data-original-src",
    ]

    for attribute in attributes:

        value = img.attr(
            attribute
        )

        if not value:
            continue

        value = value.strip()

        if not value:
            continue

        if value.startswith("data:"):
            continue

        if value.startswith("//"):
            value = "https:" + value

        return value

    return None

def get_episode_images(page):
    if page is None:
        return []

    selectors = [
        "article.viewer__body img.content__img.js-lazy",
        "article.viewer__body img.content__img",
        ".viewer__body img.content__img.js-lazy",
        ".viewer__body img.content__img",
        "img.content__img.js-lazy",
        "img.content__img",
    ]

    images = []

    for selector in selectors:

        images = page(
            selector
        )

        if len(images) > 0:
            break

    result = []

    for img in images:

        image_url = get_image_url(
            img
        )

        if image_url:
            result.append(
                image_url
            )

    return result

# ============================================================================
# Novel extraction
# ============================================================================

def get_novel_content_container(page):
    if page is None:
        return None

    selectors = [
        (
            "article.viewer__body."
            "js-episode-article."
            "main__body--book."
            "main__body--small."
            "ep-epub-contents "
            ".ep-epub-content"
        ),
        "article.viewer__body .ep-epub-content",
        ".viewer__body .ep-epub-content",
        ".ep-epub-content",
    ]

    for selector in selectors:

        elements = page(
            selector
        )

        if len(elements) > 0:
            return pq(
                elements[0]
            )

    return None

def extract_novel_html(
    page,
    title,
):
    """
    Extract the actual Tapas EPUB content while preserving inline HTML
    formatting such as <i>, <b>, <strong>, <em>, and nested <span> tags.
    """

    container = get_novel_content_container(
        page
    )

    if container is None:
        return ""

    page_html = (
        "<h1>"
        + safe_html_text(title)
        + "</h1>"
    )

    paragraphs = container(
        "p"
    )

    for paragraph in paragraphs:

        paragraph = pq(
            paragraph
        )

        text_value = clean_text(
            paragraph.text()
        )

        if not text_value:
            continue

        inner_html = paragraph.html()

        if inner_html is None:
            inner_html = (
                safe_html_text(
                    text_value
                )
            )

        inner_html = clean_novel_inline_html(
            inner_html
        )

        page_html += (
            "<p>"
            + inner_html
            + "</p>"
        )

    if len(page_html) <= len(
        f"<h1>{safe_html_text(title)}</h1>"
    ):
        return ""

    return page_html

def safe_html_text(value):
    value = "" if value is None else str(value)

    return (
        value
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )

def clean_novel_inline_html(value):
    if not value:
        return ""

    value = re.sub(
        r"<(?:script|style|noscript|iframe)\b[^>]*>.*?</(?:script|style|noscript|iframe)>",
        "",
        value,
        flags=re.IGNORECASE | re.DOTALL,
    )

    value = re.sub(
        r"\s+",
        " ",
        value,
    )

    value = re.sub(
        r"<br\s*/?>",
        "<br/>",
        value,
        flags=re.IGNORECASE,
    )

    return value.strip()

# ============================================================================
# Download
# ============================================================================

def download_file(
    session,
    url,
    output_path,
    referer=None,
):
    if not url:
        return False

    if os.path.isfile(
        output_path
    ):

        printLine(
            f"Skipping existing file: "
            f"{os.path.basename(output_path)}",
            True,
        )

        return True

    headers = {}

    if referer:
        headers["Referer"] = referer

    response = request_with_backoff(
        session,
        "GET",
        url,
        headers=headers,
    )

    if response is None:
        return False

    if response.status_code != 200:

        printLine(
            f"Could not download file: "
            f"HTTP {response.status_code}"
        )

        return False

    temp_path = (
        output_path +
        ".part"
    )

    try:

        with open(
            temp_path,
            "wb",
        ) as f:

            f.write(
                response.content
            )

        os.replace(
            temp_path,
            output_path
        )

        return True

    except OSError as e:

        printLine(
            f"Could not save file: {e}"
        )

        try:
            if os.path.isfile(temp_path):
                os.remove(temp_path)
        except OSError:
            pass

        return False

# ============================================================================
# Series artwork
# ============================================================================

def download_series_artwork(
    session,
    save_path,
    metadata,
    referer,
):
    thumbnail_url = metadata.get(
        "thumbnail_url"
    )

    if thumbnail_url:

        extension = get_extension(
            thumbnail_url
        )

        filename = safe_filename(
            f"-2 - series thumb.{extension}"
        )

        output_path = os.path.join(
            save_path,
            filename,
        )

        if download_file(
            session,
            thumbnail_url,
            output_path,
            referer,
        ):

            printLine(
                "Series thumbnail ready."
            )

    else:

        printLine(
            "Series thumbnail not found."
        )

    header_url = metadata.get(
        "header_url"
    )

    if header_url:

        extension = get_extension(
            header_url
        )

        filename = safe_filename(
            f"-1 - header.{extension}"
        )

        output_path = os.path.join(
            save_path,
            filename,
        )

        if download_file(
            session,
            header_url,
            output_path,
            referer,
        ):

            printLine(
                "Series header ready."
            )

    else:

        printLine(
            "Series header not found."
        )

# ============================================================================
# Episode thumbnails
# ============================================================================

def download_episode_thumbnail(
    session,
    save_path,
    episode,
    total_episodes,
    referer,
):
    thumbnail_url = episode.get(
        "thumbnail_url"
    )

    if not thumbnail_url:
        return ""

    episode_number = (
        episode.get(
            "episode_number"
        )
        or 0
    )

    title = safe_filename(
        episode.get(
            "title",
            f"Episode {episode['id']}",
        )
    )

    extension = get_extension(
        thumbnail_url
    )

    if episode_number:

        number = lead0(
            episode_number,
            max(
                total_episodes,
                1,
            ),
        )

    else:

        number = lead0(
            episode["id"],
            max(
                episode["id"],
                1,
            ),
        )

    filename = safe_filename(
        f"{number} - {title} - "
        f"#{episode['id']}.{extension}"
    )

    output_path = os.path.join(
        save_path,
        filename,
    )

    if download_file(
        session,
        thumbnail_url,
        output_path,
        referer,
    ):
        return filename

    return ""

# ============================================================================
# EPUB
# ============================================================================

def create_novel_epub(
    save_path,
    name,
    author,
    first_episode_id,
    episode_records,
):
    if not episode_records:
        return False

    try:
        from ebooklib import epub

    except ImportError:

        printLine(
            "ebooklib is required for novels."
        )

        printLine(
            "Install it with: python -m pip install ebooklib"
        )

        return False

    book = epub.EpubBook()

    book.set_identifier(
        str(first_episode_id)
    )

    book.set_title(
        name
    )

    book.set_language(
        "en"
    )

    if author:

        book.add_author(
            author
        )

    header_files = [
        f.name
        for f in os.scandir(
            save_path
        )
        if f.is_file()
        and "header" in f.name.lower()
    ]

    if header_files:

        header_path = os.path.join(
            save_path,
            header_files[0],
        )

        try:

            with open(
                header_path,
                "rb",
            ) as f:

                book.set_cover(
                    "cover.jpg",
                    f.read(),
                )

        except OSError:
            pass

    book.toc = []

    about = epub.EpubHtml(
        title="About",
        file_name="about.xhtml",
        lang="en",
    )

    about.content = (
        "<h1>About</h1>"
        f"<p>Title: {safe_html_text(name)}</p>"
        f"<p>Author: {safe_html_text(author)}</p>"
        f"<p>First Episode: "
        f"{safe_html_text(first_episode_id)}</p>"
    )

    book.add_item(
        about
    )

    book.spine = [
        about
    ]

    for record in episode_records:

        episode_id = int(
            record["id"]
        )

        title = record.get(
            "title",
            f"Episode {episode_id}",
        )

        page_html = record.get(
            "html",
            "",
        )

        if not page_html:
            continue

        chapter_filename = (
            f"episode-{episode_id}.xhtml"
        )

        chapter = epub.EpubHtml(
            title=title,
            file_name=chapter_filename,
            lang="en",
        )

        chapter.content = page_html

        book.add_item(
            chapter
        )

        book.toc.append(
            epub.Link(
                chapter_filename,
                title,
                f"episode-{episode_id}",
            )
        )

        book.spine.append(
            chapter
        )

    book.add_item(
        epub.EpubNcx()
    )

    book.add_item(
        epub.EpubNav()
    )

    epub_filename = safe_filename(
        f"{name}.epub"
    )

    epub_path = os.path.join(
        save_path,
        epub_filename,
    )

    try:

        epub.write_epub(
            epub_path,
            book,
        )

        printLine(
            f"Created EPUB: {epub_path}"
        )

        return True

    except Exception as e:

        printLine(
            f"Error creating EPUB: {e}"
        )

        return False

# ============================================================================
# HTTP session
# ============================================================================

s = requests.Session()

s.headers.update({
    "User-Agent": USER_AGENT,

    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
    ),

    "Accept-Language": "en-US,en;q=0.9",

    "Referer": f"{TAPAS_BASE_URL}/",
})

if args.cookies:

    cookieJar = http.cookiejar.MozillaCookieJar()

    try:

        cookieJar.load(
            args.cookies,
            ignore_discard=True,
            ignore_expires=True,
        )

        s.cookies = cookieJar

        printLine(
            "Loaded cookies."
        )

    except Exception as e:

        printLine(
            f"Warning: Could not load cookies: {e}"
        )

# ============================================================================
# Base output
# ============================================================================

basePath = ""

if args.baseDir:

    basePath = Path(
        args.baseDir
    )

    basePath.mkdir(
        parents=True,
        exist_ok=True,
    )

# ============================================================================
# Main
# ============================================================================

for series_index, supplied_series in enumerate(
    series_inputs
):

    printLine()
    printLine(
        "=" * 70
    )

    printLine(
        f"Starting series: {supplied_series}"
    )

    # ------------------------------------------------------------------------
    # Normalize supplied input.
    # ------------------------------------------------------------------------

    try:

        (
            requested_series_url,
            requested_info_url,
            requested_component,
        ) = normalize_series_input(
            supplied_series
        )

    except ValueError as e:

        printLine(
            f"Invalid series: {e}"
        )

        continue

    printLine(
        f"Requested URL: {requested_series_url}"
    )

    # ------------------------------------------------------------------------
    # Load requested /info.
    # ------------------------------------------------------------------------

    info_page = get_series_info_page(
        s,
        requested_info_url,
    )

    if info_page is None:

        printLine(
            "Could not load series /info."
        )

        continue

    # ------------------------------------------------------------------------
    # Resolve canonical identity.
    # ------------------------------------------------------------------------

    canonical = canonicalize_series(
        info_page,
        requested_series_url,
        requested_info_url,
        requested_component,
    )

    printLine(
        f"Canonical series URL: {canonical['series_url']}"
    )

    printLine(
        f"Canonical component:   "
        f"{canonical['canonical_component']}"
    )

    if canonical["canonical_id"] is not None:

        printLine(
            f"Canonical series ID:   "
            f"{canonical['canonical_id']}"
        )

    if canonical["info_url"] != requested_info_url:

        canonical_info_page = get_series_info_page(
            s,
            canonical["info_url"],
        )

        if canonical_info_page is not None:
            info_page = canonical_info_page

    # ------------------------------------------------------------------------
    # Series metadata.
    # ------------------------------------------------------------------------

    metadata = get_series_metadata(
        info_page,
        canonical,
    )

    name = metadata["name"]

    printLine(
        f"Series: {name}"
    )

    if metadata["genres"]:

        printLine(
            "Genres: " +
            ", ".join(
                metadata["genres"]
            )
        )

    if metadata["creators"]:

        printLine(
            "Creators: " +
            ", ".join(
                metadata["creators"]
            )
        )

    # ------------------------------------------------------------------------
    # Canonical series folder.
    # ------------------------------------------------------------------------

    canonical_folder_identity = (
        metadata["canonical_component"]
    )

    folder_name = safe_path_component(
        (
            f"{name} "
            f"[{canonical_folder_identity}]"
        ),
        fallback=(
            f"Tapas Series "
            f"[{canonical_folder_identity}]"
        ),
    )

    if basePath:

        save_path = os.path.join(
            str(basePath),
            folder_name,
        )

    else:

        save_path = folder_name

    try:

        os.makedirs(
            save_path,
            exist_ok=True,
        )

    except OSError as e:

        printLine(
            f"Could not create series directory: {e}"
        )

        continue

    printLine(
        f"Series folder: {save_path}"
    )

    # ------------------------------------------------------------------------
    # State.
    # ------------------------------------------------------------------------

    state = load_state(
        save_path
    )

    state["series"]["name"] = name

    state["series"]["description"] = (
        metadata["description"]
    )

    state["series"]["genres"] = (
        metadata["genres"]
    )

    state["series"]["creators"] = (
        metadata["creators"]
    )

    state["series"]["publishing_details"] = (
        metadata["publishing_details"]
    )

    state["series"]["thumbnail_url"] = (
        metadata["thumbnail_url"] or ""
    )

    state["series"]["header_url"] = (
        metadata["header_url"] or ""
    )

    state["series"]["canonical_component"] = (
        metadata["canonical_component"]
    )

    state["series"]["canonical_id"] = (
        metadata["canonical_id"]
    )

    state["series"]["series_url"] = (
        metadata["series_url"]
    )

    state["series"]["info_url"] = (
        metadata["info_url"]
    )

    save_state(
        save_path,
        state,
    )

    # ------------------------------------------------------------------------
    # Series artwork.
    # ------------------------------------------------------------------------

    download_series_artwork(
        s,
        save_path,
        metadata,
        metadata["info_url"],
    )

    # ------------------------------------------------------------------------
    # Playwright /info episode discovery.
    #
    # WUF unlocking is ONLY performed when -wuf is supplied.
    # ------------------------------------------------------------------------

    try:

        episode_list_result = (
            load_complete_episode_list(
                s,
                metadata["info_url"],
                state,
            )
        )

    except Exception as e:

        printLine(
            f"Could not load episode list: {e}"
        )

        continue

    all_episodes = (
        episode_list_result["episodes"]
    )

    if not all_episodes:

        printLine(
            "No episodes were found."
        )

        continue

    first_episode_id = (
        episode_list_result[
            "first_episode_id"
        ]
    )

    first_episode_url = (
        episode_list_result[
            "first_episode_url"
        ]
    )

    state["first_episode_id"] = (
        first_episode_id
    )

    save_state(
        save_path,
        state,
    )

    printLine(
        f"First sequential episode: "
        f"{first_episode_id}"
    )

    # ------------------------------------------------------------------------
    # Record every discovered episode.
    # ------------------------------------------------------------------------

    for episode in all_episodes:

        episode_id = str(
            episode["id"]
        )

        old_state = state["episodes"].get(
            episode_id,
            {},
        )

        old_status = old_state.get(
            "status",
            "queued",
        )

        if old_status == "complete":

            status = "complete"

        elif episode.get(
            "unlocked_by_playwright"
        ):

            status = "queued"

        elif not episode["locked"]:

            status = (
                "queued"
                if old_status in (
                    "",
                    "locked",
                    "failed",
                    None,
                )
                else old_status
            )

        else:

            status = (
                "locked"
                if old_status not in (
                    "complete",
                    "queued",
                )
                else old_status
            )

        state["episodes"][episode_id] = {
            "status": status,

            "title": episode["title"],

            "image_count": old_state.get(
                "image_count",
                0,
            ),

            "thumbnail": old_state.get(
                "thumbnail",
                "",
            ),

            "episode_number": episode.get(
                "episode_number"
            ),

            "updated": int(
                time.time()
            ),
        }

    save_state(
        save_path,
        state,
    )

    # ------------------------------------------------------------------------
    # Build scraping queue.
    #
    # Only episodes that are unlocked are scraped.
    # A locked episode enters this queue only if -wuf was supplied and
    # Playwright verified its URL successfully.
    # ------------------------------------------------------------------------

    unlocked_episodes = [
        episode
        for episode in all_episodes
        if (
            not episode["locked"]
            or episode.get(
                "unlocked_by_playwright"
            )
        )
    ]

    locked_episodes = [
        episode
        for episode in all_episodes
        if episode["locked"]
        and not episode.get(
            "unlocked_by_playwright"
        )
        and state["episodes"].get(
            str(episode["id"]),
            {},
        ).get("status") != "complete"
    ]

    printLine(
        f"Total episodes discovered: "
        f"{len(all_episodes)}"
    )

    printLine(
        f"Unlocked episodes to scan: "
        f"{len(unlocked_episodes)}"
    )

    printLine(
        f"Locked episodes remaining: "
        f"{len(locked_episodes)}"
    )

    save_state(
        save_path,
        state,
    )

    # ------------------------------------------------------------------------
    # Thumbnail selection.
    #
    # Default:
    #     Only thumbnails belonging to episodes being scraped.
    #
    # With -tn:
    #     Thumbnails for ALL discovered episodes.
    # ------------------------------------------------------------------------

    if args.tn:

        thumbnail_episodes = all_episodes

        printLine(
            "Thumbnail mode: ALL discovered episodes (-tn)."
        )

    else:

        thumbnail_episodes = unlocked_episodes

        printLine(
            "Thumbnail mode: episodes being scraped."
        )

    printLine(
        f"Downloading thumbnails for "
        f"{len(thumbnail_episodes)} episodes..."
    )

    for index, episode in enumerate(
        thumbnail_episodes,
        start=1,
    ):

        printLine(
            f"Thumbnail {index}/"
            f"{len(thumbnail_episodes)}: "
            f"Episode {episode['id']}",
            True,
        )

        thumbnail_filename = (
            download_episode_thumbnail(
                s,
                save_path,
                episode,
                len(all_episodes),
                episode["url"],
            )
        )

        if thumbnail_filename:

            episode[
                "thumbnail_filename"
            ] = thumbnail_filename

            state["episodes"][
                str(episode["id"])
            ]["thumbnail"] = (
                thumbnail_filename
            )

    save_state(
        save_path,
        state,
    )

    # ------------------------------------------------------------------------
    # EPUB episode records.
    # ------------------------------------------------------------------------

    episode_records = {}

    for episode in all_episodes:

        episode_records[
            str(episode["id"])
        ] = {
            "id": episode["id"],
            "episode_number": episode.get(
                "episode_number"
            ),
            "title": episode.get(
                "title",
                f"Episode {episode['id']}",
            ),
            "url": episode.get(
                "url",
                f"{TAPAS_BASE_URL}/episode/{episode['id']}",
            ),
            "html": "",
        }

    # ------------------------------------------------------------------------
    # Scraping.
    # ------------------------------------------------------------------------

    global_image_number = 0

    for episode_state in state["episodes"].values():

        if episode_state.get(
            "status"
        ) == "complete":

            global_image_number += int(
                episode_state.get(
                    "image_count",
                    0,
                )
            )

    total_unlocked = len(
        unlocked_episodes
    )

    for scan_index, episode in enumerate(
        unlocked_episodes,
        start=1,
    ):

        current_episode_id = int(
            episode["id"]
        )

        episode_number = (
            episode.get(
                "episode_number"
            )
            or scan_index
        )

        title = episode.get(
            "title",
            f"Episode {episode_number}",
        )

        printLine()
        printLine(
            f"[{scan_index}/{total_unlocked}] "
            f"Scanning episode {episode_number}: "
            f"{title}"
        )

        existing_state = state["episodes"].get(
            str(current_episode_id),
            {},
        )

        if (
            not args.force
            and existing_state.get(
                "status"
            ) == "complete"
        ):

            printLine(
                f"Episode {current_episode_id} "
                f"is already complete; skipping.",
                True,
            )

            continue

        # --------------------------------------------------------------------
        # Load episode over HTTP.
        # --------------------------------------------------------------------

        page, status = get_episode_page(
            s,
            current_episode_id,
        )

        if page is None:

            set_episode_state(
                save_path,
                state,
                current_episode_id,
                "failed",
                title,
                0,
                episode.get(
                    "thumbnail_filename",
                    "",
                ),
                episode_number,
            )

            printLine(
                f"Could not load episode "
                f"{current_episode_id}: {status}"
            )

            continue

        # --------------------------------------------------------------------
        # Verify it is actually accessible for scraping.
        # --------------------------------------------------------------------

        if is_episode_locked(
            page,
            current_episode_id,
        ):

            set_episode_state(
                save_path,
                state,
                current_episode_id,
                "locked",
                title,
                0,
                episode.get(
                    "thumbnail_filename",
                    "",
                ),
                episode_number,
            )

            printLine(
                f"Episode {current_episode_id} "
                f"is now locked; skipping."
            )

            continue

        title = get_episode_title(
            page,
            current_episode_id,
            title,
        )

        episode_url = (
            f"{TAPAS_BASE_URL}/episode/"
            f"{current_episode_id}"
        )

        if str(current_episode_id) in episode_records:
            episode_records[
                str(current_episode_id)
            ]["title"] = title

        # --------------------------------------------------------------------
        # Comic episode.
        # --------------------------------------------------------------------

        images = get_episode_images(
            page
        )

        if images:

            printLine(
                f"{len(images)} comic images found."
            )

            episode_success = True

            for img_index, image_url in enumerate(
                images,
                start=1,
            ):

                extension = get_extension(
                    image_url
                )

                number = lead0(
                    global_image_number + 1,
                    max(
                        global_image_number +
                        len(images),
                        1,
                    ),
                )

                ep_number = lead0(
                    episode_number,
                    max(
                        episode_number,
                        1,
                    ),
                )

                image_number = lead0(
                    img_index,
                    len(images),
                )

                filename = safe_filename(
                    (
                        f"{number} - "
                        f"{ep_number} - "
                        f"{image_number} - "
                        f"{title} - "
                        f"#{current_episode_id}."
                        f"{extension}"
                    )
                )

                output_path = os.path.join(
                    save_path,
                    filename,
                )

                success = download_file(
                    s,
                    image_url,
                    output_path,
                    episode_url,
                )

                if not success:

                    episode_success = False

                    printLine(
                        f"Failed image "
                        f"{img_index}/"
                        f"{len(images)} from episode "
                        f"{current_episode_id}."
                    )

                    break

                global_image_number += 1

                printLine(
                    f"Downloaded image "
                    f"{img_index}/{len(images)}",
                    True,
                )

            if not episode_success:

                set_episode_state(
                    save_path,
                    state,
                    current_episode_id,
                    "failed",
                    title,
                    len(images),
                    episode.get(
                        "thumbnail_filename",
                        "",
                    ),
                    episode_number,
                )

                continue

            set_episode_state(
                save_path,
                state,
                current_episode_id,
                "complete",
                title,
                len(images),
                episode.get(
                    "thumbnail_filename",
                    "",
                ),
                episode_number,
            )

            printLine(
                f"Episode {current_episode_id} complete."
            )

        # --------------------------------------------------------------------
        # Novel/text episode.
        # --------------------------------------------------------------------

        else:

            printLine(
                "No comic images found; checking for novel text..."
            )

            novel_html = extract_novel_html(
                page,
                title,
            )

            minimum_html_length = len(
                f"<h1>{safe_html_text(title)}</h1>"
            )

            if (
                novel_html
                and len(novel_html) >
                minimum_html_length
            ):

                if str(current_episode_id) in episode_records:

                    episode_records[
                        str(current_episode_id)
                    ]["html"] = novel_html

                    episode_records[
                        str(current_episode_id)
                    ]["title"] = title

                    episode_records[
                        str(current_episode_id)
                    ]["episode_number"] = (
                        episode_number
                    )

                set_episode_state(
                    save_path,
                    state,
                    current_episode_id,
                    "complete",
                    title,
                    0,
                    episode.get(
                        "thumbnail_filename",
                        "",
                    ),
                    episode_number,
                )

                printLine(
                    f"Collected novel episode "
                    f"{current_episode_id}."
                )

            else:

                set_episode_state(
                    save_path,
                    state,
                    current_episode_id,
                    "failed",
                    title,
                    0,
                    episode.get(
                        "thumbnail_filename",
                        "",
                    ),
                    episode_number,
                )

                printLine(
                    f"No readable content found in "
                    f"episode {current_episode_id}."
                )

    # =========================================================================
    # EPUB
    # =========================================================================

    epub_episode_records = [
        record
        for record in episode_records.values()
        if record.get("html")
    ]

    if epub_episode_records:

        epub_episode_records.sort(
            key=lambda record: (
                record.get(
                    "episode_number"
                )
                if record.get(
                    "episode_number"
                ) is not None
                else 10**12,
                int(
                    record["id"]
                ),
            )
        )

        author = ""

        if state["series"].get(
            "creators"
        ):

            author = ", ".join(
                state["series"]["creators"]
            )

        create_novel_epub(
            save_path,
            name,
            author,
            first_episode_id,
            epub_episode_records,
        )

    else:

        printLine(
            "No novel episode records were collected; EPUB was not created."
        )

    # =========================================================================
    # Summary
    # =========================================================================

    complete_count = 0
    failed_count = 0
    locked_count = 0
    queued_count = 0

    for episode_state in state["episodes"].values():

        status = episode_state.get(
            "status"
        )

        if status == "complete":
            complete_count += 1

        elif status == "failed":
            failed_count += 1

        elif status == "locked":
            locked_count += 1

        elif status == "queued":
            queued_count += 1

    printLine()
    printLine(
        f"Finished series: {name}"
    )

    printLine(
        f"Discovered: {len(all_episodes)} | "
        f"Unlocked: {len(unlocked_episodes)} | "
        f"Complete: {complete_count} | "
        f"Failed: {failed_count} | "
        f"Locked: {locked_count} | "
        f"Queued: {queued_count}"
    )

    if series_index + 1 != len(
        series_inputs
    ):
        printLine()

printLine()
printLine(
    "Done."
)
