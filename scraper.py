#!/usr/bin/env python3
"""UFSC Moodle Scraper — Python 3 scraper inspired by doebi/MoodleScraper.

Downloads course resources, folders, embedded pluginfile attachments, pages,
books, assignments, and links from UFSC Moodle into a clean
`Semester / Course / Section / Resource` directory hierarchy.
"""

from __future__ import annotations

import argparse
from configparser import ConfigParser
from dataclasses import dataclass, field
import datetime as dt
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Callable
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from moodle_parser import (
    ActivityItem,
    CourseInfo,
    InlineFolder,
    LinkItem,
    SectionData,
    filename_from_headers_or_url,
    find_subpage_section_urls,
    extract_sesskey,
    has_ufsc_course_list,
    is_login_page,
    parse_book_chapter_content,
    parse_book_chapters,
    parse_courses_from_ajax_payload,
    parse_courses_from_dashboard,
    parse_course_sections,
    parse_folder_files,
    parse_folder_metadata,
    parse_page_or_assign_content,
    parse_resource_redirect_url,
    parse_url_activity_target,
    sanitize_filename,
    sanitize_name,
    split_valid_extension,
)

DEFAULT_BASE_URL = "https://presencial.moodle.ufsc.br/"
SESSION_FILE = Path(".session/cookies.json")
MAX_WIN_PATH = 235
REQUEST_TIMEOUT = 60
DIRECT_FILE_EXTENSIONS = (
    ".pdf",
    ".docx",
    ".doc",
    ".pptx",
    ".ppt",
    ".xlsx",
    ".xls",
    ".epub",
    ".zip",
    ".csv",
)
LEGACY_OFFICE_PAIRS = frozenset(
    {
        ("ppt", "pptx"),
        ("pptx", "ppt"),
        ("doc", "docx"),
        ("docx", "doc"),
        ("xls", "xlsx"),
        ("xlsx", "xls"),
    }
)
BINARY_MIME_PREFIXES = (
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.",
    "application/vnd.ms-",
    "application/epub+zip",
    "application/zip",
)


@dataclass
class ScraperConfig:
    """Runtime settings for the Moodle scraper."""

    base_url: str = DEFAULT_BASE_URL
    root: Path = Path("./downloads")
    moodle_session: str = ""
    dump_html: bool = False
    save_notes: bool = True
    save_url_files: bool = True
    download_external_files: bool = True
    include_pages: bool = True
    include_books: bool = True
    include_assigns: bool = True
    overwrite: bool = False
    dry_run: bool = False


@dataclass
class ScrapeStats:
    """Counters for reporting scrape progress."""

    saved: int = 0
    skipped: int = 0
    notes: int = 0
    links: int = 0
    errors: int = 0
    written_paths: set[Path] = field(default_factory=set)


def _ensure_forcedownload(url: str) -> str:
    """Append `forcedownload=1` to `pluginfile.php` URLs when absent."""
    if "pluginfile.php" not in url:
        return url
    parsed = urlparse(url)
    qs = parse_qs(parsed.query, keep_blank_values=True)
    if "forcedownload" in qs:
        return url
    qs["forcedownload"] = ["1"]
    new_query = urlencode(qs, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def _fit_windows_path(dest_dir: Path, filename: str) -> Path:
    """Ensure full target path stays safely under Windows MAX_PATH limits."""
    candidate = dest_dir / filename
    if len(str(candidate)) <= MAX_WIN_PATH:
        return candidate

    available = max(12, MAX_WIN_PATH - len(str(dest_dir)) - 1)
    shortened = sanitize_filename(filename, max_length=available)
    return dest_dir / shortened


def _fit_subdir_path(base_dir: Path, parts: list[str], reserved_filename_chars: int = 45) -> Path:
    """Keep nested subfolder paths short enough so child filenames fit under MAX_WIN_PATH."""
    filtered = [p for p in parts if p and p not in (".", "..", "/", "\\")]
    if not filtered:
        return base_dir

    dir_budget = max(len(str(base_dir)) + 12, MAX_WIN_PATH - reserved_filename_chars)
    clean_parts = [sanitize_name(p, max_length=50) for p in filtered]
    candidate = base_dir.joinpath(*clean_parts)
    if len(str(candidate)) <= dir_budget:
        return candidate

    shrunk = [sanitize_name(p, max_length=20) for p in clean_parts]
    candidate = base_dir.joinpath(*shrunk)
    while len(shrunk) > 1 and len(str(candidate)) > dir_budget:
        merged = sanitize_name(f"{shrunk[-2]}_{shrunk[-1]}", max_length=24)
        shrunk = [*shrunk[:-2], merged]
        candidate = base_dir.joinpath(*shrunk)
    if len(shrunk) == 1 and len(str(candidate)) > dir_budget:
        single_budget = max(8, dir_budget - len(str(base_dir)) - 1)
        shrunk[0] = sanitize_name(shrunk[0], max_length=single_budget)
        candidate = base_dir / shrunk[0]
    return candidate


def resolve_download_filename(resolved_name: str, preferred_name: str | None) -> str:
    """Combine preferred activity title with the server file's verified extension."""
    clean_resolved = sanitize_filename(resolved_name)
    if not preferred_name:
        return clean_resolved

    pref_clean = sanitize_filename(preferred_name)
    _, ext = split_valid_extension(clean_resolved)
    if not ext or pref_clean.lower().endswith(f".{ext.lower()}"):
        return pref_clean

    pref_stem_only, pref_ext = split_valid_extension(pref_clean)
    if pref_ext and (pref_ext.lower(), ext.lower()) in LEGACY_OFFICE_PAIRS:
        pref_clean = pref_stem_only

    stem_budget = max(16, 120 - len(ext) - 1)
    pref_stem = sanitize_name(pref_clean, max_length=stem_budget)
    return sanitize_filename(f"{pref_stem}.{ext}")


def load_config(conf_path: Path) -> ScraperConfig:
    """Load `scraper.conf` if present and overlay `MOODLE_SESSION` env var."""
    cfg = ScraperConfig()
    if conf_path.exists():
        parser = ConfigParser()
        parser.read(conf_path, encoding="utf-8")
        if parser.has_section("scraper"):
            sec = parser["scraper"]
            cfg.base_url = sec.get("baseurl", cfg.base_url).rstrip("/") + "/"
            cfg.root = Path(sec.get("root", str(cfg.root)))
            cfg.moodle_session = sec.get("moodle_session", "").strip()
            cfg.dump_html = sec.getboolean("dump_html", cfg.dump_html)
            cfg.save_notes = sec.getboolean("save_notes", cfg.save_notes)
            cfg.save_url_files = sec.getboolean("save_url_files", cfg.save_url_files)
            cfg.download_external_files = sec.getboolean(
                "download_external_files",
                cfg.download_external_files,
            )
            cfg.include_pages = sec.getboolean("include_pages", cfg.include_pages)
            cfg.include_books = sec.getboolean("include_books", cfg.include_books)
            cfg.include_assigns = sec.getboolean("include_assigns", cfg.include_assigns)

    env_cookie = os.environ.get("MOODLE_SESSION", "").strip()
    if env_cookie:
        cfg.moodle_session = env_cookie
    return cfg


def parse_cookie_arg(raw_cookie: str, default_name: str = "MoodleSession") -> tuple[str, str]:
    """Parse single `--cookie` input supporting either `VALUE` or `NAME=VALUE`."""
    cleaned = raw_cookie.strip()
    if cleaned.lower().startswith("cookie:"):
        cleaned = cleaned[7:].strip()
    if "=" in cleaned:
        name, value = cleaned.split("=", 1)
        if name.strip():
            return name.strip(), value.strip()
    return default_name, cleaned


def parse_cookie_pairs(raw_cookie: str, default_name: str = "MoodleSession") -> list[tuple[str, str]]:
    """Parse either one cookie (`VALUE` / `NAME=VALUE`) or a full `Cookie:` header."""
    cleaned = raw_cookie.strip()
    if cleaned.lower().startswith("cookie:"):
        cleaned = cleaned[7:].strip()
    if not cleaned:
        return []
    if ";" not in cleaned:
        name, value = parse_cookie_arg(cleaned, default_name=default_name)
        return [(name, value)] if value else []

    pairs: list[tuple[str, str]] = []
    for chunk in cleaned.split(";"):
        part = chunk.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        if name.strip() and value.strip():
            pairs.append((name.strip(), value.strip()))
    return pairs


def _matching_domain_cookies(cookies: list[dict], host: str) -> list[dict]:
    """Filter Playwright cookies whose domain applies to `host`."""
    host_lower = host.lower()
    matched: list[dict] = []
    for cookie in cookies:
        dom = (cookie.get("domain") or "").lstrip(".").lower()
        if dom and (host_lower == dom or host_lower.endswith("." + dom)):
            matched.append(cookie)
    return matched


def build_http_session(base_url: str, moodle_session: str = "") -> requests.Session:
    """Create a `requests.Session` preloaded with retry policy and saved/explicit cookies."""
    ses = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD"}),
    )
    adapter = HTTPAdapter(max_retries=retry)
    ses.mount("https://", adapter)
    ses.mount("http://", adapter)
    ses.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            )
        }
    )
    host = urlparse(base_url).hostname or "presencial.moodle.ufsc.br"

    if SESSION_FILE.exists() and not moodle_session:
        now = time.time()
        try:
            cookies = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
            for item in cookies:
                expires = item.get("expires", -1)
                if isinstance(expires, (int, float)) and expires > 0 and expires < now:
                    continue
                ses.cookies.set(
                    item["name"],
                    item["value"],
                    domain=item.get("domain", host),
                    path=item.get("path", "/"),
                )
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            pass

    if moodle_session:
        has_explicit_name = "=" in moodle_session
        for cookie_name, cookie_value in parse_cookie_pairs(moodle_session):
            ses.cookies.set(cookie_name, cookie_value, domain=host, path="/")
            if not has_explicit_name and host == "presencial.moodle.ufsc.br" and cookie_name == "MoodleSession":
                ses.cookies.set("MoodleSessionmdlpre", cookie_value, domain=host, path="/")

    return ses


def _launch_login_browser(playwright_ctx):
    """Launch bundled Chromium if installed, falling back to system Chrome or Edge."""
    if os.path.exists(playwright_ctx.chromium.executable_path):
        return playwright_ctx.chromium.launch(headless=False)

    for channel in ("chrome", "msedge"):
        try:
            return playwright_ctx.chromium.launch(channel=channel, headless=False)
        except Exception:
            continue

    raise RuntimeError(
        "No Playwright browser binary found. Run `playwright install chromium` or install Google Chrome."
    )


def interactive_playwright_login(base_url: str, timeout_seconds: int = 300) -> str:
    """Open Chromium/Chrome for manual SSO/Moodle login and persist Moodle cookies locally."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is not installed. Run `pip install playwright` and `playwright install chromium`."
        ) from exc

    target = base_url.rstrip("/") + "/login/index.php"
    host = (urlparse(base_url).hostname or "").lower()

    print(f"[auth] Opening browser for Moodle login: {target}")
    print("[auth] Complete SSO / Moodle login in the opened browser window...")

    with sync_playwright() as p:
        browser = _launch_login_browser(p)
        context = browser.new_context()
        page = context.new_page()
        page.goto(target, wait_until="domcontentloaded")

        # Poll safely across cross-origin SSO redirects without breaking on navigation frames.
        deadline = time.monotonic() + timeout_seconds
        cookies: list[dict] = []
        while time.monotonic() < deadline:
            if page.is_closed():
                try:
                    browser.close()
                except Exception:
                    pass
                raise RuntimeError("Login browser was closed before login completed.")
            try:
                parsed = urlparse(page.url)
                path = parsed.path.lower()
                if parsed.hostname and parsed.hostname.lower() == host and "/login" not in path:
                    if page.locator("a[href*='logout.php']").count() > 0:
                        cookies = _matching_domain_cookies(context.cookies(), host)
                        if any(c["name"].startswith("MoodleSession") for c in cookies):
                            break
            except Exception:
                pass
            time.sleep(0.5)
        else:
            browser.close()
            raise TimeoutError(f"Timed out after {timeout_seconds}s waiting for Moodle login.")

        page.wait_for_load_state("domcontentloaded")
        cookies = _matching_domain_cookies(context.cookies(), host)
        if cookies:
            existing_cookies: list[dict] = []
            if SESSION_FILE.exists():
                try:
                    loaded = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
                    if isinstance(loaded, list):
                        existing_cookies = [
                            c
                            for c in loaded
                            if isinstance(c, dict) and not _matching_domain_cookies([c], host)
                        ]
                except (OSError, json.JSONDecodeError):
                    existing_cookies = []
            merged_cookies = [*existing_cookies, *cookies]
            SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
            SESSION_FILE.write_text(json.dumps(merged_cookies, indent=2), encoding="utf-8")
        session_cookie = next(
            (c["value"] for c in cookies if c["name"].startswith("MoodleSession")),
            "",
        )
        browser.close()

    if not cookies:
        raise RuntimeError("Login completed, but no session cookies were captured.")
    print(f"[auth] Session saved to {SESSION_FILE} ({len(cookies)} cookies)")
    return session_cookie


def fetch_html(ses: requests.Session, url: str) -> tuple[str, str]:
    """Fetch a Moodle HTML page and raise if session is unauthenticated."""
    resp = ses.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    if is_login_page(resp.text, resp.url):
        raise PermissionError(
            f"Session is not authenticated (redirected to {resp.url}). "
            "Run with `--login` or provide a fresh `--cookie <MoodleSession>`."
        )
    return resp.text, resp.url


def _disambiguate_path(dest_dir: Path, filename: str, stats: ScrapeStats) -> Path:
    """Resolve Windows-safe path and avoid collisions between two different items in one run."""
    candidate = _fit_windows_path(dest_dir, filename)
    if candidate not in stats.written_paths:
        return candidate

    stem, ext = split_valid_extension(candidate.name)
    dot = "." if ext else ""
    max_name_len = max(12, MAX_WIN_PATH - len(str(dest_dir)) - 1)
    idx = 2
    while True:
        suffix = f" ({idx})"
        keep_stem = max(4, max_name_len - len(suffix) - (len(ext) + 1 if ext else 0))
        trimmed_stem = stem[:keep_stem].rstrip(" .") or "item"
        alt_name = sanitize_filename(f"{trimmed_stem}{suffix}{dot}{ext}", max_length=max_name_len)
        alt_path = dest_dir / alt_name
        if alt_path not in stats.written_paths:
            return alt_path
        idx += 1


def _read_bounded_bytes(resp: requests.Response, max_bytes: int = 524288) -> bytes:
    """Read up to `max_bytes` from a streaming response without consuming huge binaries."""
    chunks: list[bytes] = []
    total = 0
    for chunk in resp.iter_content(chunk_size=32768):
        if not chunk:
            continue
        chunks.append(chunk)
        total += len(chunk)
        if total >= max_bytes:
            break
    return b"".join(chunks)


def _stream_response_to_file(
    resp: requests.Response,
    dest_dir: Path,
    cfg: ScraperConfig,
    stats: ScrapeStats,
    preferred_name: str | None = None,
    prefix: str = "    ",
    initial_bytes: bytes | None = None,
) -> None:
    """Stream an already-opened HTTP response to disk with skip and atomic rename."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    server_name = filename_from_headers_or_url(resp.headers, resp.url)
    resolved_name = resolve_download_filename(server_name, preferred_name)

    dest_path = _disambiguate_path(dest_dir, resolved_name, stats)
    stats.written_paths.add(dest_path)

    if dest_path.exists() and not cfg.overwrite and dest_path.stat().st_size > 0:
        print(f"{prefix}[skip] {dest_path.name}")
        stats.skipped += 1
        return

    tmp_path = dest_path.with_suffix(dest_path.suffix + ".part")
    try:
        with tmp_path.open("wb") as handle:
            if initial_bytes:
                handle.write(initial_bytes)
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    handle.write(chunk)
        tmp_path.replace(dest_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    print(f"{prefix}[save] {dest_path.name}")
    stats.saved += 1


def download_binary(
    ses: requests.Session,
    url: str,
    dest_dir: Path,
    cfg: ScraperConfig,
    stats: ScrapeStats,
    preferred_name: str | None = None,
    prefix: str = "    ",
) -> None:
    """Stream a file download to `dest_dir` with skip and atomic `.part` support."""
    target_url = _ensure_forcedownload(url)
    if cfg.dry_run:
        preview_name = sanitize_filename(preferred_name or Path(urlparse(url).path).name or "file")
        print(f"{prefix}[dry-run] {preview_name} <- {target_url}")
        return

    with ses.get(target_url, stream=True, timeout=REQUEST_TIMEOUT) as resp:
        resp.raise_for_status()
        if is_login_page("", resp.url):
            raise PermissionError("Session expired while downloading file.")
        ctype = resp.headers.get("content-type", "").lower()
        disp = resp.headers.get("content-disposition", "").lower()
        initial_bytes: bytes | None = None
        if "text/html" in ctype and "attachment" not in disp:
            initial_bytes = _read_bounded_bytes(resp)
            html_preview = initial_bytes.decode(resp.encoding or "utf-8", errors="replace")
            if is_login_page(html_preview, resp.url):
                raise PermissionError("Session expired while downloading file.")
        _stream_response_to_file(
            resp,
            dest_dir,
            cfg,
            stats,
            preferred_name=preferred_name,
            prefix=prefix,
            initial_bytes=initial_bytes,
        )


def write_text_file(
    dest_path: Path,
    content: str,
    cfg: ScraperConfig,
    stats: ScrapeStats,
    kind: str = "note",
    prefix: str = "    ",
) -> Path:
    """Write UTF-8 text content if new or overwrite is enabled, disambiguating collisions."""
    resolved_path = _disambiguate_path(dest_path.parent, dest_path.name, stats)
    stats.written_paths.add(resolved_path)
    if cfg.dry_run:
        print(f"{prefix}[dry-run:{kind}] {resolved_path.name}")
        return resolved_path
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    if resolved_path.exists() and not cfg.overwrite:
        print(f"{prefix}[skip] {resolved_path.name}")
        stats.skipped += 1
        return resolved_path
    resolved_path.write_text(content, encoding="utf-8")
    print(f"{prefix}[{kind}] {resolved_path.name}")
    if kind == "link":
        stats.links += 1
    else:
        stats.notes += 1
    return resolved_path


def handle_resource(
    ses: requests.Session,
    act: ActivityItem,
    sec_dir: Path,
    cfg: ScraperConfig,
    stats: ScrapeStats,
) -> None:
    """Download a `mod_resource` file (handling direct redirects or HTML wrapper pages)."""
    if cfg.dry_run:
        print(f"    [dry-run:resource] {act.title} ({act.url})")
        return

    with ses.get(act.url, stream=True, timeout=REQUEST_TIMEOUT) as resp:
        resp.raise_for_status()
        if is_login_page("", resp.url):
            raise PermissionError("Session expired while fetching resource.")

        ctype = resp.headers.get("content-type", "").lower()
        disp = resp.headers.get("content-disposition", "").lower()
        initial_bytes: bytes | None = None
        if "text/html" in ctype and "attachment" not in disp:
            initial_bytes = _read_bounded_bytes(resp)
            html = initial_bytes.decode(resp.encoding or "utf-8", errors="replace")
            if is_login_page(html, resp.url):
                raise PermissionError("Session expired while fetching resource.")
            if "pluginfile.php" not in resp.url:
                file_url = parse_resource_redirect_url(html, cfg.base_url)
                if file_url:
                    download_binary(ses, file_url, sec_dir, cfg, stats, preferred_name=act.title)
                    return
                print(f"    [warn] No direct file link found in resource wrapper: {act.title}")
                return

        _stream_response_to_file(
            resp,
            sec_dir,
            cfg,
            stats,
            preferred_name=act.title,
            initial_bytes=initial_bytes,
        )


def handle_folder(
    ses: requests.Session,
    act: ActivityItem,
    sec_dir: Path,
    cfg: ScraperConfig,
    stats: ScrapeStats,
) -> None:
    """Download all files inside a `mod_folder`, plus any description notes/attachments."""
    html, _ = fetch_html(ses, act.url)
    files = parse_folder_files(html, cfg.base_url)
    folder_text, extra_plugins, extra_externals = parse_folder_metadata(html, cfg.base_url)
    folder_dir = _fit_subdir_path(sec_dir, [act.title or f"folder_{act.cmid}"], reserved_filename_chars=55)
    print(f"    +-- [folder] {folder_dir.name} ({len(files)} files)")

    for rel_path, file_url in files:
        parts = list(Path(rel_path).parts[:-1])
        target_dir = _fit_subdir_path(folder_dir, parts)
        fname = Path(rel_path).name
        try:
            download_binary(ses, file_url, target_dir, cfg, stats, preferred_name=fname, prefix="        ")
        except PermissionError:
            raise
        except Exception as exc:
            print(f"        [error] {fname}: {exc}")
            stats.errors += 1

    for pfile in extra_plugins:
        try:
            download_binary(ses, pfile.url, folder_dir, cfg, stats, prefix="        ")
        except PermissionError:
            raise
        except Exception as exc:
            print(f"        [error] {pfile.url}: {exc}")
            stats.errors += 1

    has_meaningful_text = bool(folder_text and folder_text.casefold() != act.title.casefold())
    if cfg.save_notes and (has_meaningful_text or extra_externals):
        md_parts = [f"# {act.title}", f"\nSource: {act.url}\n"]
        if has_meaningful_text:
            md_parts.append(folder_text)
        if extra_externals:
            md_parts.append("\n## External Links\n" + "\n".join(f"- [{l.label}]({l.url})" for l in extra_externals))
        write_text_file(
            _fit_windows_path(folder_dir, "info.md"),
            "\n".join(md_parts) + "\n",
            cfg,
            stats,
            kind="note",
            prefix="        ",
        )


def _read_bounded_html(resp: requests.Response, max_bytes: int = 524288) -> str:
    """Read up to `max_bytes` from a streaming HTML response without downloading huge binaries."""
    raw = _read_bounded_bytes(resp, max_bytes=max_bytes)
    encoding = resp.encoding or "utf-8"
    return raw.decode(encoding, errors="replace")


def _is_direct_binary_response(url: str, headers: dict[str, str] | requests.structures.CaseInsensitiveDict) -> bool:
    """Return True if an HTTP response serves a binary document rather than an HTML page."""
    ctype = headers.get("content-type", "").lower()
    disp = headers.get("content-disposition", "").lower()
    if "text/html" in ctype and "attachment" not in disp:
        return False
    if "attachment" in disp or any(prefix in ctype for prefix in BINARY_MIME_PREFIXES):
        return True
    return urlparse(url).path.lower().endswith(DIRECT_FILE_EXTENSIONS)


def _fetch_url_within_host(
    ses: requests.Session,
    start_url: str,
    base_host: str,
    max_hops: int = 5,
) -> tuple[requests.Response | None, str]:
    """Follow internal redirects with `ses`, stopping before any cross-host redirect."""
    current = start_url
    for _ in range(max_hops):
        resp = ses.get(current, allow_redirects=False, stream=True, timeout=REQUEST_TIMEOUT)
        if 300 <= resp.status_code < 400 and resp.headers.get("location"):
            next_url = urljoin(current, resp.headers["location"])
            resp.close()
            next_host = urlparse(next_url).netloc.lower()
            if next_host and next_host != base_host:
                return None, next_url
            current = next_url
            continue
        try:
            resp.raise_for_status()
        except Exception:
            resp.close()
            raise
        return resp, resp.url
    raise requests.TooManyRedirects(f"Exceeded {max_hops} redirects for {start_url}")


def _download_external_target_file(
    target_url: str,
    sec_dir: Path,
    cfg: ScraperConfig,
    stats: ScrapeStats,
    preferred_name: str,
) -> None:
    """Fetch a public external direct-file link (e.g. `.pdf`) using a cookie-free request."""
    if urlparse(target_url).scheme not in ("http", "https"):
        return
    try:
        with requests.get(
            target_url,
            stream=True,
            timeout=(10, 30),
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        ) as ext_resp:
            ext_resp.raise_for_status()
            if _is_direct_binary_response(ext_resp.url, ext_resp.headers):
                _stream_response_to_file(
                    ext_resp,
                    sec_dir,
                    cfg,
                    stats,
                    preferred_name=preferred_name,
                )
    except requests.RequestException as exc:
        print(f"    [warn] External file {target_url}: {exc}")


def handle_url(
    ses: requests.Session,
    act: ActivityItem,
    sec_dir: Path,
    cfg: ScraperConfig,
    stats: ScrapeStats,
    collected_links: list[LinkItem],
) -> None:
    """Resolve `mod_url` external target, optionally save `.url` shortcut and direct PDF/docs."""
    if cfg.dry_run:
        collected_links.append(LinkItem(label=act.title, url=act.url, is_pluginfile=False))
        if cfg.save_url_files:
            fname = sanitize_filename(f"{act.title}.url", max_length=100)
            write_text_file(_fit_windows_path(sec_dir, fname), "", cfg, stats, kind="link")
        return

    target_url = act.url
    base_host = urlparse(cfg.base_url).netloc.lower()
    already_saved = False
    try:
        resp, resolved_url = _fetch_url_within_host(ses, act.url, base_host)
        if is_login_page("", resolved_url):
            if resp is not None:
                resp.close()
            raise PermissionError("Session expired while resolving URL activity.")
        target_url = resolved_url
        if resp is not None:
            with resp:
                if is_login_page("", resp.url):
                    raise PermissionError("Session expired while resolving URL activity.")
                if "text/html" in resp.headers.get("content-type", "").lower():
                    html = _read_bounded_html(resp)
                    if is_login_page(html, resp.url):
                        raise PermissionError("Session expired while resolving URL activity.")
                    extracted = parse_url_activity_target(html, cfg.base_url)
                    if extracted:
                        if is_login_page("", extracted):
                            raise PermissionError("Session expired while resolving URL activity.")
                        target_url = extracted
                elif cfg.download_external_files and _is_direct_binary_response(resp.url, resp.headers):
                    _stream_response_to_file(resp, sec_dir, cfg, stats, preferred_name=act.title)
                    already_saved = True
    except requests.RequestException:
        pass

    target_host = urlparse(target_url).netloc.lower()
    if cfg.download_external_files and not already_saved:
        if target_host == base_host and "pluginfile.php" in target_url:
            download_binary(ses, target_url, sec_dir, cfg, stats, preferred_name=act.title)
        elif target_host and target_host != base_host:
            _download_external_target_file(target_url, sec_dir, cfg, stats, act.title)

    collected_links.append(LinkItem(label=act.title, url=target_url, is_pluginfile=False))
    if cfg.save_url_files:
        fname = sanitize_filename(f"{act.title}.url", max_length=100)
        dest_path = _fit_windows_path(sec_dir, fname)
        write_text_file(dest_path, f"[InternetShortcut]\nURL={target_url}\n", cfg, stats, kind="link")


def handle_page_or_assign(
    ses: requests.Session,
    act: ActivityItem,
    sec_dir: Path,
    cfg: ScraperConfig,
    stats: ScrapeStats,
) -> None:
    """Save `mod_page` or `mod_assign` readable content and embedded attachments."""
    if act.mod_type == "page" and not cfg.include_pages:
        return
    if act.mod_type == "assign" and not cfg.include_assigns:
        return

    html, _ = fetch_html(ses, act.url)
    text, pluginfiles, externals = parse_page_or_assign_content(html, cfg.base_url)

    md_parts = [f"# {act.title}", f"\nSource: {act.url}\n", text]
    if externals:
        md_parts.append("\n## External Links\n" + "\n".join(f"- [{l.label}]({l.url})" for l in externals))

    md_name = sanitize_filename(f"{act.mod_type}_{act.title}.md", max_length=100)
    write_text_file(_fit_windows_path(sec_dir, md_name), "\n".join(md_parts) + "\n", cfg, stats, kind="note")

    for pfile in pluginfiles:
        try:
            download_binary(ses, pfile.url, sec_dir, cfg, stats)
        except PermissionError:
            raise
        except Exception as exc:
            print(f"    [error] Attachment {pfile.url}: {exc}")
            stats.errors += 1


def _extract_chapter_id(url: str) -> str:
    """Return `chapterid` query parameter from a book URL if present."""
    ids = parse_qs(urlparse(url).query).get("chapterid")
    return ids[0] if ids else ""


def handle_book(
    ses: requests.Session,
    act: ActivityItem,
    sec_dir: Path,
    cfg: ScraperConfig,
    stats: ScrapeStats,
) -> None:
    """Save `mod_book` chapters as Markdown and download embedded pluginfile links."""
    if not cfg.include_books:
        return

    html, final_url = fetch_html(ses, act.url)
    chapters = parse_book_chapters(html, cfg.base_url, act.cmid, current_url=final_url)
    sections_md = [f"# {act.title}", f"\nSource: {act.url}\n"]
    seen_files: set[str] = set()

    current_chid = _extract_chapter_id(final_url) or _extract_chapter_id(act.url)
    if chapters:
        has_current = any(
            ch_url in ("", act.url, final_url) or (current_chid and _extract_chapter_id(ch_url) == current_chid)
            for _, ch_url in chapters
        )
        if not has_current:
            chapters = [(act.title, final_url), *chapters]

    targets = chapters or [(act.title, final_url)]
    for ch_title, ch_url in targets:
        try:
            if ch_url in ("", act.url, final_url) or (current_chid and _extract_chapter_id(ch_url) == current_chid):
                ch_html = html
            else:
                ch_html = fetch_html(ses, ch_url)[0]
            ch_text, pluginfiles, externals = parse_book_chapter_content(ch_html, cfg.base_url)
        except PermissionError:
            raise
        except Exception as exc:
            print(f"    [error] book chapter '{ch_title}': {exc}")
            stats.errors += 1
            continue

        sections_md.append(f"## {ch_title}\n\n{ch_text}")
        if externals:
            sections_md.append("\n".join(f"- [{l.label}]({l.url})" for l in externals))
        for pfile in pluginfiles:
            key = pfile.url.split("?")[0]
            if key not in seen_files:
                seen_files.add(key)
                try:
                    download_binary(ses, pfile.url, sec_dir, cfg, stats)
                except PermissionError:
                    raise
                except Exception as exc:
                    print(f"    [error] book attachment {pfile.url}: {exc}")
                    stats.errors += 1

    md_name = sanitize_filename(f"book_{act.title}.md", max_length=100)
    write_text_file(_fit_windows_path(sec_dir, md_name), "\n\n".join(sections_md) + "\n", cfg, stats, kind="note")


def _dump_course_html(course_dir: Path, label: str, html: str, cfg: ScraperConfig) -> None:
    """Optionally save raw course/section HTML into `.dump/` like `doebi/MoodleScraper`."""
    if not cfg.dump_html or cfg.dry_run:
        return
    dump_dir = course_dir / ".dump"
    dump_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.date.today().isoformat()
    out = dump_dir / sanitize_filename(f"{label}-{stamp}.html", max_length=120)
    out.write_text(html, encoding="utf-8")


def _scrape_inline_folder(
    ses: requests.Session,
    inline_folder: InlineFolder,
    sec_dir: Path,
    cfg: ScraperConfig,
    stats: ScrapeStats,
) -> None:
    """Download all files from an inline `mod_folder` rendered directly inside a course section."""
    folder_dir = _fit_subdir_path(sec_dir, [inline_folder.title], reserved_filename_chars=55)
    print(f"    +-- [folder] {folder_dir.name} ({len(inline_folder.files)} files)")
    for rel_path, file_url in inline_folder.files:
        parts = list(Path(rel_path).parts[:-1])
        target_dir = _fit_subdir_path(folder_dir, parts)
        fname = Path(rel_path).name
        try:
            download_binary(ses, file_url, target_dir, cfg, stats, preferred_name=fname, prefix="        ")
        except PermissionError:
            raise
        except Exception as exc:
            print(f"        [error] {fname}: {exc}")
            stats.errors += 1


def scrape_section(
    ses: requests.Session,
    section: SectionData,
    course_dir: Path,
    cfg: ScraperConfig,
    stats: ScrapeStats,
) -> None:
    """Download all files, notes, and links for a single parsed section."""
    sec_label = f"{section.section_num:02d} - {sanitize_name(section.title, max_length=65, fallback='Section')}"
    sec_dir = course_dir / sec_label
    print(f"  +-- {sec_label}")

    section_links: list[LinkItem] = list(section.external_links)

    for pfile in section.embedded_files:
        try:
            download_binary(ses, pfile.url, sec_dir, cfg, stats)
        except PermissionError:
            raise
        except Exception as exc:
            print(f"    [error] Embedded file {pfile.url}: {exc}")
            stats.errors += 1

    for inline_folder in section.inline_folders:
        _scrape_inline_folder(ses, inline_folder, sec_dir, cfg, stats)

    dispatch: dict[str, Callable[[requests.Session, ActivityItem, Path, ScraperConfig, ScrapeStats], None]] = {
        "resource": handle_resource,
        "folder": handle_folder,
        "page": handle_page_or_assign,
        "assign": handle_page_or_assign,
        "book": handle_book,
    }

    for act in section.activities:
        try:
            if act.mod_type in dispatch:
                dispatch[act.mod_type](ses, act, sec_dir, cfg, stats)
            elif act.mod_type == "url":
                handle_url(ses, act, sec_dir, cfg, stats, section_links)
            else:
                section_links.append(LinkItem(label=f"[{act.mod_type}] {act.title}", url=act.url, is_pluginfile=False))
        except PermissionError:
            raise
        except Exception as exc:
            print(f"    [error] {act.mod_type} '{act.title}': {exc}")
            stats.errors += 1

    if cfg.save_notes and (section.summary_text or section_links):
        note_lines = [f"# {section.title}", ""]
        if section.summary_text:
            note_lines.append(section.summary_text)
            note_lines.append("")
        if section_links:
            note_lines.append("## Links & Activities")
            for item in section_links:
                note_lines.append(f"- [{item.label}]({item.url})")
            note_lines.append("")
        write_text_file(_fit_windows_path(sec_dir, "info.md"), "\n".join(note_lines), cfg, stats, kind="note")

    # Remove empty section folders unless dry-run.
    if not cfg.dry_run and sec_dir.exists() and not any(sec_dir.iterdir()):
        sec_dir.rmdir()


def scrape_course(
    ses: requests.Session,
    course: CourseInfo,
    cfg: ScraperConfig,
    stats: ScrapeStats,
) -> None:
    """Fetch a course (including `&section=N` subpages when coursedisplay=1) and scrape all sections."""
    sem_dir = cfg.root / sanitize_name(course.semester, max_length=40, fallback="Outros")
    course_dir = sem_dir / sanitize_name(course.title, max_length=85, fallback=f"course_{course.course_id}")
    print(f"\n=== [{course.semester}] {course.title} (id={course.course_id}) ===")

    html, _ = fetch_html(ses, course.url)
    _dump_course_html(course_dir, f"course-{course.course_id}", html, cfg)

    sections_by_num: dict[int, SectionData] = {
        sec.section_num: sec for sec in parse_course_sections(html, cfg.base_url)
    }

    subpages = find_subpage_section_urls(html, course.course_id, cfg.base_url)
    for sec_num, sub_url in subpages.items():
        try:
            sub_html, _ = fetch_html(ses, sub_url)
        except PermissionError:
            raise
        except Exception as exc:
            print(f"  [error] Section {sec_num} ({sub_url}): {exc}")
            stats.errors += 1
            continue
        _dump_course_html(course_dir, f"course-{course.course_id}-sec-{sec_num}", sub_html, cfg)
        for sec in parse_course_sections(sub_html, cfg.base_url):
            if sec.section_num == sec_num or sec.section_num not in sections_by_num:
                sections_by_num[sec.section_num] = sec

    for sec_num in sorted(sections_by_num):
        scrape_section(ses, sections_by_num[sec_num], course_dir, cfg, stats)

    if not cfg.dry_run and course_dir.exists() and not any(course_dir.iterdir()):
        course_dir.rmdir()


def _normalize_sem(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


def select_courses_interactive(courses: list[CourseInfo]) -> list[CourseInfo]:
    """Prompt user for semester and course selection, modeled after `doebi/MoodleScraper`."""
    semesters: list[str] = []
    for c in courses:
        if c.semester not in semesters:
            semesters.append(c.semester)

    print("\nAvailable semesters:")
    for idx, sem in enumerate(semesters):
        count = sum(1 for c in courses if c.semester == sem)
        print(f"  [{idx}]: {sem} ({count} courses)")

    choice = input("\nSelect semester number, (a) for all, or (q) to quit: ").strip().lower()
    if choice == "q":
        return []
    if choice == "a":
        filtered = courses
    else:
        try:
            target_sem = semesters[int(choice)]
        except (ValueError, IndexError):
            print("Invalid semester choice.")
            return []
        filtered = [c for c in courses if c.semester == target_sem]

    print("\nAvailable courses:")
    for idx, c in enumerate(filtered):
        print(f"  [{idx}]: [{c.semester}] {c.title} (id={c.course_id})")

    c_choice = input("\nChoose course number (comma-separated allowed), (a) for all, or (q) to quit: ").strip().lower()
    if c_choice == "q":
        return []
    if c_choice == "a":
        return filtered

    selected: list[CourseInfo] = []
    for part in c_choice.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            selected.append(filtered[int(part)])
        except (ValueError, IndexError):
            print(f"Ignoring invalid index: {part}")
    return selected


def fetch_courses_via_ajax(ses: requests.Session, base_url: str, sesskey: str) -> list[CourseInfo]:
    """Query Moodle 4.x enrolled courses via `lib/ajax/service.php` using `sesskey`."""
    ajax_url = (
        base_url.rstrip("/")
        + "/lib/ajax/service.php?"
        + urlencode(
            {
                "sesskey": sesskey,
                "info": "core_course_get_enrolled_courses_by_timeline_classification",
            }
        )
    )
    payload = [
        {
            "index": 0,
            "methodname": "core_course_get_enrolled_courses_by_timeline_classification",
            "args": {
                "offset": 0,
                "limit": 0,
                "classification": "all",
                "sort": "fullname",
                "customfieldname": "",
                "customfieldvalue": "",
            },
        }
    ]
    try:
        resp = ses.post(ajax_url, json=payload, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return parse_courses_from_ajax_payload(resp.json(), base_url)
    except (requests.RequestException, ValueError):
        return []


def fetch_courses(ses: requests.Session, base_url: str) -> list[CourseInfo]:
    """Discover enrolled courses via UFSC block, Moodle 4.x AJAX API, or `/my/courses.php`."""
    dashboard_url = base_url.rstrip("/") + "/my/index.php?seeall=1"
    html, _ = fetch_html(ses, dashboard_url)
    if has_ufsc_course_list(html):
        return parse_courses_from_dashboard(html, base_url)

    sesskey = extract_sesskey(html)
    if sesskey:
        ajax_courses = fetch_courses_via_ajax(ses, base_url, sesskey)
        if ajax_courses:
            return ajax_courses

    courses_page_url = base_url.rstrip("/") + "/my/courses.php"
    try:
        courses_html, _ = fetch_html(ses, courses_page_url)
    except requests.RequestException:
        courses_html = ""

    if courses_html:
        courses_sesskey = extract_sesskey(courses_html)
        if courses_sesskey:
            ajax_courses = fetch_courses_via_ajax(ses, base_url, courses_sesskey)
            if ajax_courses:
                return ajax_courses
        parsed_courses = parse_courses_from_dashboard(courses_html, base_url)
        if parsed_courses:
            return parsed_courses

    return parse_courses_from_dashboard(html, base_url)


def build_arg_parser() -> argparse.ArgumentParser:
    """Create CLI argument parser."""
    parser = argparse.ArgumentParser(description="Scrape UFSC Moodle and Moodle 4.x courses and resources.")
    parser.add_argument("--conf", type=Path, default=Path("scraper.conf"), help="Path to scraper.conf")
    parser.add_argument(
        "--base-url",
        help=f"Moodle base URL (overrides scraper.conf, default: {DEFAULT_BASE_URL})",
    )
    parser.add_argument("--output", type=Path, help="Output root directory (overrides scraper.conf)")
    parser.add_argument(
        "--cookie",
        help="Session cookie (`VALUE` or `NAME=VALUE`, e.g. `MoodleSessionmdlpre=...`)",
    )
    parser.add_argument("--login", action="store_true", help="Open Chromium to log in via SSO / Moodle")
    parser.add_argument("--list", action="store_true", help="List semesters and courses, then exit")
    parser.add_argument("--all", action="store_true", help="Scrape all enrolled courses across all semesters")
    parser.add_argument("--semester", action="append", default=[], help="Filter semester (e.g. '2026 - 2')")
    parser.add_argument("--course-id", action="append", default=[], help="Scrape specific course ID(s)")
    parser.add_argument("--dump-html", action="store_true", help="Save raw course HTML in .dump/")
    parser.add_argument("--overwrite", action="store_true", help="Redownload existing files")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without writing files")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for MoodleScraper CLI."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    args = build_arg_parser().parse_args(argv)
    cfg = load_config(args.conf)

    if args.base_url:
        cfg.base_url = args.base_url.strip().rstrip("/") + "/"
    if args.output:
        cfg.root = args.output
    if args.cookie:
        cfg.moodle_session = args.cookie.strip()
    if args.dump_html:
        cfg.dump_html = True
    if args.overwrite:
        cfg.overwrite = True
    if args.dry_run:
        cfg.dry_run = True

    if args.login:
        interactive_playwright_login(cfg.base_url)
        cfg.moodle_session = ""  # Load all captured cookies from SESSION_FILE.

    ses = build_http_session(cfg.base_url, cfg.moodle_session)

    try:
        courses = fetch_courses(ses, cfg.base_url)
    except PermissionError as exc:
        print(f"[auth] {exc}", file=sys.stderr)
        print(
            "[hint] Either run `python scraper.py --login` (Playwright SSO) "
            "or pass `--cookie <MoodleSession>` from Chrome DevTools.",
            file=sys.stderr,
        )
        return 1

    if not courses:
        print("No courses found on dashboard.", file=sys.stderr)
        return 1

    if args.list:
        for c in courses:
            print(f"[{c.semester}] id={c.course_id} | {c.title} | {c.url}")
        return 0

    selected: list[CourseInfo] = []
    if args.all:
        selected = courses
    elif args.semester or args.course_id:
        wanted_sems = {_normalize_sem(s) for s in args.semester}
        wanted_ids = set(args.course_id)
        for c in courses:
            if (wanted_sems and _normalize_sem(c.semester) in wanted_sems) or (c.course_id in wanted_ids):
                selected.append(c)
    else:
        selected = select_courses_interactive(courses)

    if not selected:
        print("No courses selected. Exiting.")
        return 0

    stats = ScrapeStats()
    try:
        for course in selected:
            try:
                scrape_course(ses, course, cfg, stats)
            except PermissionError:
                raise
            except Exception as exc:
                print(f"[error] Failed course {course.course_id} ({course.title}): {exc}")
                stats.errors += 1
    except PermissionError as exc:
        print(f"[auth] {exc}", file=sys.stderr)
        print(
            "[hint] Session expired mid-run. Re-run with `--login` or a fresh `--cookie <MoodleSessionmdlpre>`.",
            file=sys.stderr,
        )
        return 1

    print(
        f"\nDone! saved={stats.saved}, skipped={stats.skipped}, "
        f"notes={stats.notes}, links={stats.links}, errors={stats.errors}"
    )
    return 0 if stats.errors == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
