"""HTML parsing and path sanitization helpers for UFSC Moodle (Moodle 4.x Classic)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Iterable
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from bs4 import BeautifulSoup, NavigableString, Tag

WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)

SEMESTER_PATTERN = re.compile(r"^(?:\d{4}\s*-\s*\d+|Outros)$", re.IGNORECASE)
COURSE_ID_PATTERN = re.compile(r"/course/view\.php\?id=(\d+)")
SECTION_PARAM_PATTERN = re.compile(r"/course/view\.php\?id=(\d+)&(?:amp;)?section=(\d+)")
MOD_VIEW_PATTERN = re.compile(r"/mod/([a-z0-9_]+)/view\.php\?id=(\d+)")

IGNORED_PLUGINFILE_FRAGMENTS = (
    "/theme/image.php",
    "/core_admin/logocompact",
    "/core_admin/logo",
    "/user/icon/",
    "/badges/",
    "/filter/tex/",
)


@dataclass(frozen=True)
class CourseInfo:
    """Metadata for an enrolled Moodle course."""

    course_id: str
    title: str
    url: str
    semester: str
    short_code: str = ""


@dataclass(frozen=True)
class ActivityItem:
    """Represents a course activity module (resource, folder, url, page, etc.)."""

    cmid: str
    mod_type: str
    title: str
    url: str
    details: str = ""


@dataclass(frozen=True)
class LinkItem:
    """Represents a file or external link found in HTML text."""

    label: str
    url: str
    is_pluginfile: bool


@dataclass
class SectionData:
    """Parsed content of a single Moodle course section."""

    section_num: int
    title: str
    summary_text: str = ""
    activities: list[ActivityItem] = field(default_factory=list)
    embedded_files: list[LinkItem] = field(default_factory=list)
    external_links: list[LinkItem] = field(default_factory=list)


def split_valid_extension(name: str) -> tuple[str, str]:
    """Split `(stem, ext)` only when `ext` looks like a real filename extension."""
    stem, dot, ext = name.rpartition(".")
    if dot and stem and 1 <= len(ext) <= 10 and re.fullmatch(r"[A-Za-z0-9]+", ext):
        return stem, ext
    return name, ""


def sanitize_name(
    name: str,
    max_length: int = 80,
    fallback: str = "item",
    preserve_extension: bool = False,
) -> str:
    """Return a Windows- and POSIX-safe file or folder name component."""
    cleaned = unquote(name or "")
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned:
        cleaned = fallback

    if preserve_extension:
        stem, ext = split_valid_extension(cleaned)
        dot = "." if ext else ""
    else:
        stem, dot, ext = cleaned, "", ""

    if len(cleaned) > max_length:
        if ext:
            keep_stem = max(8, max_length - len(ext) - 1)
            stem = stem[:keep_stem].rstrip(" .")
            cleaned = f"{stem}{dot}{ext}"
        else:
            cleaned = cleaned[:max_length].rstrip(" .")

    if cleaned.split(".")[0].upper() in WINDOWS_RESERVED_NAMES:
        cleaned = f"_{cleaned}"
        if len(cleaned) > max_length:
            cleaned = cleaned[:max_length].rstrip(" .")

    return cleaned or fallback


def sanitize_filename(filename: str, max_length: int = 120) -> str:
    """Sanitize a downloaded filename while preserving its extension."""
    return sanitize_name(
        filename,
        max_length=max_length,
        fallback="download",
        preserve_extension=True,
    )


def is_login_page(html: str, final_url: str) -> bool:
    """Detect whether Moodle redirected to a login/SSO page or guest landing view."""
    parsed = urlparse(final_url)
    path = parsed.path.lower()
    if any(token in path for token in ("/login/", "/shibboleth", "/cas/login", "/idp/")):
        return True
    if "sistemas.ufsc.br" in parsed.netloc.lower():
        return True

    soup = BeautifulSoup(html, "lxml")
    if soup.select_one("form#login, body#page-login-index, body.notloggedin"):
        return True
    if soup.select_one(".logininfo a[href*='login/index.php']") and not soup.select_one(
        "a[href*='logout.php']"
    ):
        return True
    return False


def _clean_element_text(element: Tag | None) -> str:
    """Extract normalized readable text from a BeautifulSoup tag."""
    if element is None:
        return ""
    clone = BeautifulSoup(str(element), "lxml")
    root = clone.body.find() if clone.body else None
    search_root = root or clone
    # Use search_root.select so descendants like `<span class="accesshide">` are stripped
    # without decomposing the root heading itself (e.g. `h3.sectionname.accesshide`).
    for hidden in search_root.select(".accesshide, .sr-only, script, style"):
        hidden.decompose()
    lines = [line.strip() for line in search_root.get_text("\n").splitlines()]
    return "\n".join(line for line in lines if line)


def _is_valid_pluginfile(url: str) -> bool:
    """Return True if URL points to a course/activity pluginfile rather than a UI icon."""
    if "pluginfile.php" not in url:
        return False
    return not any(frag in url for frag in IGNORED_PLUGINFILE_FRAGMENTS)


def extract_links_from_container(container: Tag, base_url: str) -> tuple[list[LinkItem], list[LinkItem]]:
    """Collect deduplicated pluginfile links/media and external HTTP links inside a tag."""
    pluginfiles: list[LinkItem] = []
    externals: list[LinkItem] = []
    seen_plugin: set[str] = set()
    seen_ext: set[str] = set()
    base_host = urlparse(base_url).netloc.lower()

    def add_pluginfile(abs_url: str, label: str) -> None:
        key = abs_url.split("?")[0]
        if key not in seen_plugin:
            seen_plugin.add(key)
            pluginfiles.append(LinkItem(label=label, url=abs_url, is_pluginfile=True))

    for anchor in container.select("a[href]"):
        raw_href = (anchor.get("href") or "").strip()
        if not raw_href or raw_href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
            continue
        abs_url = urljoin(base_url, raw_href)
        label = anchor.get_text(" ", strip=True) or Path(urlparse(abs_url).path).name or abs_url

        if _is_valid_pluginfile(abs_url):
            add_pluginfile(abs_url, label)
        else:
            parsed = urlparse(abs_url)
            if parsed.scheme in ("http", "https") and parsed.netloc.lower() != base_host:
                if abs_url not in seen_ext:
                    seen_ext.add(abs_url)
                    externals.append(LinkItem(label=label, url=abs_url, is_pluginfile=False))

    media_specs = (
        ("img[src*='pluginfile.php']", "src"),
        ("video[src*='pluginfile.php']", "src"),
        ("audio[src*='pluginfile.php']", "src"),
        ("source[src*='pluginfile.php']", "src"),
        ("iframe[src*='pluginfile.php']", "src"),
        ("embed[src*='pluginfile.php']", "src"),
        ("object[data*='pluginfile.php']", "data"),
    )
    for selector, attr in media_specs:
        for media_el in container.select(selector):
            raw_url = (media_el.get(attr) or "").strip()
            if not raw_url:
                continue
            abs_url = urljoin(base_url, raw_url)
            if _is_valid_pluginfile(abs_url):
                label = (
                    (media_el.get("alt") or media_el.get("title") or "").strip()
                    or Path(unquote(urlparse(abs_url).path)).name
                    or abs_url
                )
                add_pluginfile(abs_url, label)

    for frame in container.select("iframe[src], video[src], audio[src], source[src]"):
        raw_src = (frame.get("src") or "").strip()
        if not raw_src or _is_valid_pluginfile(raw_src):
            continue
        abs_url = urljoin(base_url, raw_src)
        parsed = urlparse(abs_url)
        if parsed.scheme in ("http", "https") and parsed.netloc.lower() != base_host:
            if abs_url not in seen_ext:
                seen_ext.add(abs_url)
                label = (frame.get("title") or "").strip() or f"Embedded ({parsed.netloc})"
                externals.append(LinkItem(label=label, url=abs_url, is_pluginfile=False))

    return pluginfiles, externals


def parse_courses_from_dashboard(html: str, base_url: str) -> list[CourseInfo]:
    """Parse courses and semester groupings from UFSC Moodle `/my/index.php?seeall=1`."""
    soup = BeautifulSoup(html, "lxml")

    container = soup.select_one("section.block_ufsc_course_list, aside.block_ufsc_course_list")
    if container is None:
        for block in soup.select("section.block, aside[role='complementary'], div.block"):
            heading = block.select_one("h5, h4, h3, .card-title")
            if heading and "disciplinas/turmas" in heading.get_text(" ", strip=True).lower():
                container = block
                break

    if container is None:
        return _parse_courses_fallback(soup, base_url)

    courses: list[CourseInfo] = []
    seen_ids: set[str] = set()
    current_semester = "Outros"

    for node in container.descendants:
        if isinstance(node, NavigableString):
            if node.find_parent("a") is not None:
                continue
            text = " ".join(str(node).split())
            if SEMESTER_PATTERN.match(text):
                current_semester = "Outros" if text.lower() == "outros" else re.sub(r"\s*-\s*", " - ", text)
            continue

        if not isinstance(node, Tag):
            continue

        if node.name == "a" and node.get("href"):
            href = urljoin(base_url, node["href"])
            if "change_visibility.php" in href:
                continue
            match = COURSE_ID_PATTERN.search(href)
            if not match:
                continue
            course_id = match.group(1)
            if course_id in seen_ids:
                continue
            seen_ids.add(course_id)
            title = node.get_text(" ", strip=True)
            short_code = (node.get("title") or node.get("aria-label") or "").strip()
            if short_code == title:
                short_code = ""
            courses.append(
                CourseInfo(
                    course_id=course_id,
                    title=title or f"Course {course_id}",
                    url=urljoin(base_url, f"course/view.php?id={course_id}"),
                    semester=current_semester,
                    short_code=short_code,
                )
            )
            continue

        if node.name in ("h3", "h4", "h5", "h6", "strong", "b", "p", "div", "span", "li"):
            # Only treat leaf-like headings without nested course links as semester headers.
            if node.select_one("a[href*='/course/view.php']"):
                continue
            text = " ".join(node.get_text(" ", strip=True).split())
            if SEMESTER_PATTERN.match(text):
                current_semester = "Outros" if text.lower() == "outros" else re.sub(r"\s*-\s*", " - ", text)

    return courses


def _parse_courses_fallback(soup: BeautifulSoup, base_url: str) -> list[CourseInfo]:
    """Fallback course extraction when `block_ufsc_course_list` is absent."""
    courses: list[CourseInfo] = []
    seen_ids: set[str] = set()
    for anchor in soup.select("a[href*='/course/view.php?id=']"):
        href = urljoin(base_url, anchor.get("href", ""))
        match = COURSE_ID_PATTERN.search(href)
        if not match:
            continue
        course_id = match.group(1)
        if course_id in seen_ids:
            continue
        seen_ids.add(course_id)
        title = anchor.get_text(" ", strip=True)
        if not title:
            continue
        courses.append(
            CourseInfo(
                course_id=course_id,
                title=title,
                url=urljoin(base_url, f"course/view.php?id={course_id}"),
                semester="Outros",
            )
        )
    return courses


def find_subpage_section_urls(html: str, course_id: str, base_url: str) -> dict[int, str]:
    """Find `&section=N` URLs when a course uses one-section-per-page mode (`coursedisplay=1`)."""
    soup = BeautifulSoup(html, "lxml")
    main = soup.select_one('[role="main"]') or soup

    # Sections that already have rendered activity lists or non-summary content inline don't need sub-fetches.
    inline_full_sections: set[int] = set()
    for li in main.select('li[id^="section-"]'):
        sec_id = li.get("id", "")
        if not sec_id.startswith("section-"):
            continue
        try:
            num = int(sec_id.split("-", 1)[1])
        except ValueError:
            continue
        has_inline_activities = bool(li.select("ul.section li.activity"))
        is_summary_only_card = "section-summary" in (li.get("class") or [])
        if has_inline_activities or (num == 0 and not is_summary_only_card):
            inline_full_sections.add(num)

    subpages: dict[int, str] = {}
    for anchor in main.select("a[href]"):
        abs_url = urljoin(base_url, anchor.get("href", ""))
        match = SECTION_PARAM_PATTERN.search(abs_url)
        if not match or match.group(1) != course_id:
            continue
        sec_num = int(match.group(2))
        if sec_num in inline_full_sections:
            continue
        subpages.setdefault(sec_num, abs_url.split("#", 1)[0])

    return dict(sorted(subpages.items()))


def parse_course_sections(html: str, base_url: str) -> list[SectionData]:
    """Parse all rendered sections and their activities/embedded files from a course page."""
    soup = BeautifulSoup(html, "lxml")
    main = soup.select_one('[role="main"]') or soup
    section_nodes = main.select('.course-content li[id^="section-"]')
    if not section_nodes:
        section_nodes = main.select('li[id^="section-"]')

    sections: list[SectionData] = []
    for sec_node in section_nodes:
        if "section-summary" in (sec_node.get("class") or []):
            # Summary card linking to &section=N; actual contents are fetched via subpages.
            continue

        sec_id = sec_node.get("id", "")
        try:
            sec_num = int(sec_id.split("-", 1)[1])
        except (IndexError, ValueError):
            continue

        title_el = sec_node.select_one("h3.sectionname, h3[id^='sectionid-'], .sectionname")
        title = _clean_element_text(title_el) or (sec_node.get("aria-label") or "").strip() or f"Section {sec_num}"

        summary_el = sec_node.select_one(".content > .summary, .summarytext, .summary")
        summary_text = _clean_element_text(summary_el)

        embedded_files: list[LinkItem] = []
        external_links: list[LinkItem] = []
        seen_files: set[str] = set()
        seen_ext: set[str] = set()

        def add_links(container: Tag | None) -> None:
            if container is None:
                return
            p_links, e_links = extract_links_from_container(container, base_url)
            for item in p_links:
                key = item.url.split("?")[0]
                if key not in seen_files:
                    seen_files.add(key)
                    embedded_files.append(item)
            for item in e_links:
                if item.url not in seen_ext:
                    seen_ext.add(item.url)
                    external_links.append(item)

        add_links(summary_el)

        activities: list[ActivityItem] = []
        label_notes: list[str] = []
        for act in sec_node.select("li.activity"):
            classes = act.get("class") or []
            mod_type = next((c.removeprefix("modtype_") for c in classes if c.startswith("modtype_")), "")

            if mod_type == "label":
                add_links(act)
                txt = _clean_element_text(act)
                if txt:
                    label_notes.append(txt)
                continue

            act_link = act.select_one("a.aalink[href], .activityname a[href], a[href*='/mod/']")
            if act_link is None:
                # Some custom themes or labels put raw pluginfile links inside activities without modtype_label.
                add_links(act)
                continue

            act_url = urljoin(base_url, act_link["href"])
            mod_match = MOD_VIEW_PATTERN.search(act_url)
            cmid = mod_match.group(2) if mod_match else ""
            if not mod_type and mod_match:
                mod_type = mod_match.group(1)

            name_el = act_link.select_one(".instancename") or act_link
            act_title = _clean_element_text(name_el) or f"{mod_type}_{cmid}"
            details = _clean_element_text(act.select_one(".resourcelinkdetails"))

            # Inspect outermost inline activity description containers once for links and notes.
            desc_candidates = act.select(".contentafterlink, .activity-description, .activity-altcontent, .description")
            candidate_ids = {id(el) for el in desc_candidates}
            desc_parts: list[str] = []
            for desc_el in desc_candidates:
                if any(id(parent) in candidate_ids for parent in desc_el.parents):
                    continue
                add_links(desc_el)
                desc_txt = _clean_element_text(desc_el)
                if desc_txt and desc_txt != details and desc_txt not in desc_parts:
                    desc_parts.append(desc_txt)
            if desc_parts:
                label_notes.append(f"### {act_title}\n" + "\n\n".join(desc_parts))

            activities.append(
                ActivityItem(
                    cmid=cmid,
                    mod_type=mod_type or "unknown",
                    title=act_title,
                    url=act_url,
                    details=details,
                )
            )

        combined_summary = "\n\n".join(part for part in [summary_text, *label_notes] if part)
        sections.append(
            SectionData(
                section_num=sec_num,
                title=title,
                summary_text=combined_summary,
                activities=activities,
                embedded_files=embedded_files,
                external_links=external_links,
            )
        )

    return sections


def parse_resource_redirect_url(html: str, base_url: str) -> str | None:
    """Extract the underlying pluginfile URL when `mod/resource/view.php` renders an HTML wrapper."""
    soup = BeautifulSoup(html, "lxml")
    selectors = (
        ".resourceworkaround a[href*='pluginfile.php']",
        "object[data*='pluginfile.php']",
        "iframe[src*='pluginfile.php']",
        "embed[src*='pluginfile.php']",
        '[role="main"] a[href*="pluginfile.php"]',
    )
    for selector in selectors:
        el = soup.select_one(selector)
        if el is None:
            continue
        raw = el.get("href") or el.get("data") or el.get("src")
        if raw:
            return urljoin(base_url, raw)
    return None


def parse_folder_files(html: str, base_url: str) -> list[tuple[str, str]]:
    """Extract `(relative_path, download_url)` tuples from a `mod_folder` page."""
    soup = BeautifulSoup(html, "lxml")
    main = soup.select_one('[role="main"]') or soup
    files: list[tuple[str, str]] = []
    seen_urls: set[str] = set()

    for anchor in main.select('a[href*="pluginfile.php"]'):
        abs_url = urljoin(base_url, anchor.get("href", ""))
        if "/mod_folder/content/" not in abs_url:
            continue
        clean_key = abs_url.split("?")[0]
        if clean_key in seen_urls:
            continue
        seen_urls.add(clean_key)

        path_part = unquote(urlparse(abs_url).path)
        rel_match = re.search(r"/mod_folder/content/\d+/(.+)$", path_part)
        rel_path = rel_match.group(1) if rel_match else (anchor.get_text(" ", strip=True) or "file")
        files.append((rel_path, abs_url))

    return files


def parse_url_activity_target(html: str, base_url: str) -> str | None:
    """Extract target URL when `mod/url/view.php` renders an HTML wrapper."""
    soup = BeautifulSoup(html, "lxml")
    base_host = urlparse(base_url).netloc.lower()
    for selector in (".urlworkaround a[href]", '[role="main"] a[href]'):
        for anchor in soup.select(selector):
            href = (anchor.get("href") or "").strip()
            if not href or href.startswith(("#", "javascript:", "mailto:")):
                continue
            abs_url = urljoin(base_url, href)
            parsed = urlparse(abs_url)
            if parsed.scheme not in ("http", "https"):
                continue
            if selector.startswith(".urlworkaround") or parsed.netloc.lower() != base_host:
                return abs_url
    return None


def _clean_region_main(
    html: str,
    root_selectors: tuple[str, ...] = ("section#region-main", "#region-main", "[role='main']"),
    strip_selectors: tuple[str, ...] = (),
) -> Tag:
    """Return a cloned main content container matching `root_selectors` in priority order."""
    soup = BeautifulSoup(html, "lxml")
    region = next((el for sel in root_selectors if (el := soup.select_one(sel)) is not None), soup)
    clone = BeautifulSoup(str(region), "lxml")
    root = (clone.body.find() if clone.body else None) or clone
    chrome_selectors = (
        ".activity-navigation",
        "nav.navigation",
        "form.jumpmenu",
        ".tertiary-navigation",
        "[data-region='activity-completion']",
        ".completion-info",
        *strip_selectors,
    )
    for sel in chrome_selectors:
        for el in root.select(sel):
            el.decompose()
    return root


def _extract_region_bundle(
    html: str,
    base_url: str,
    root_selectors: tuple[str, ...] = ("section#region-main", "#region-main", "[role='main']"),
    strip_selectors: tuple[str, ...] = (),
) -> tuple[str, list[LinkItem], list[LinkItem]]:
    """Extract cleaned text and deduplicated links from a selected page region."""
    region = _clean_region_main(html, root_selectors=root_selectors, strip_selectors=strip_selectors)
    text = _clean_element_text(region)
    p_links, e_links = extract_links_from_container(region, base_url)
    return text, p_links, e_links


def parse_page_or_assign_content(html: str, base_url: str) -> tuple[str, list[LinkItem], list[LinkItem]]:
    """Extract readable text, prompts, dates, and linked files/URLs from `mod_page` or `mod_assign`."""
    return _extract_region_bundle(html, base_url)


def parse_book_chapter_content(html: str, base_url: str) -> tuple[str, list[LinkItem], list[LinkItem]]:
    """Extract a single `mod_book` chapter body without repeating book-level headers or TOC."""
    return _extract_region_bundle(
        html,
        base_url,
        root_selectors=(".book_content", "[role='main']", "section#region-main", "#region-main"),
        strip_selectors=(".navtop", ".navbottom", ".book_toc", "#mod_book-chapters", ".block_book_toc"),
    )


def parse_folder_metadata(html: str, base_url: str) -> tuple[str, list[LinkItem], list[LinkItem]]:
    """Extract folder description text and non-tree links/attachments from `#region-main`."""
    text, p_links, e_links = _extract_region_bundle(
        html,
        base_url,
        strip_selectors=(
            ".foldertree",
            "#folder_tree0",
            "form[action*='download_folder.php']",
            "button",
            ".btn",
        ),
    )
    p_links = [item for item in p_links if "/mod_folder/content/" not in item.url]
    return text, p_links, e_links


def parse_book_chapters(html: str, base_url: str, cmid: str) -> list[tuple[str, str]]:
    """Extract `(chapter_title, chapter_url)` items from a `mod_book` page."""
    soup = BeautifulSoup(html, "lxml")
    toc = soup.select_one(".book_toc, #mod_book-chapters, .block_book_toc")
    chapters: list[tuple[str, str]] = []
    seen_urls: set[str] = set()
    search_roots = [toc, soup] if toc is not None else [soup]

    for search_root in search_roots:
        for anchor in search_root.select(f'a[href*="mod/book/view.php"][href*="id={cmid}"]'):
            abs_url = urljoin(base_url, anchor.get("href", ""))
            qs = parse_qs(urlparse(abs_url).query)
            if "chapterid" not in qs:
                continue
            if abs_url in seen_urls:
                continue
            seen_urls.add(abs_url)
            chapters.append((anchor.get_text(" ", strip=True) or f"Chapter {qs['chapterid'][0]}", abs_url))
        if chapters:
            break

    return chapters


def filename_from_headers_or_url(headers: Iterable[tuple[str, str]] | dict[str, str], url: str) -> str:
    """Resolve the best filename from Content-Disposition headers or the URL path."""
    hdr_map = {k.lower(): v for k, v in dict(headers).items()}
    disp = hdr_map.get("content-disposition", "")

    # RFC 5987 filename*=UTF-8'<optional-lang>'<encoded> takes precedence over filename="..."
    match_ext = re.search(r"filename\*\s*=\s*[^']*'(?:[^']*')?([^;]+)", disp, re.IGNORECASE)
    if match_ext:
        raw_ext = match_ext.group(1).strip().strip('"')
        return sanitize_filename(unquote(raw_ext))

    match_plain = re.search(r'filename\s*=\s*"([^"]+)"|filename\s*=\s*([^;\s]+)', disp, re.IGNORECASE)
    if match_plain:
        raw = match_plain.group(1) or match_plain.group(2)
        try:
            # Handle occasional ISO-8859-1 header encoding from older PHP servers.
            raw = raw.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
        return sanitize_filename(unquote(raw))

    path_name = unquote(Path(urlparse(url).path).name)
    return sanitize_filename(path_name or "download")
