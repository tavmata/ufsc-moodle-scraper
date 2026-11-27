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

CONTENT_TYPE_EXTENSIONS = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/vnd.ms-powerpoint": "ppt",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.ms-excel": "xls",
    "application/epub+zip": "epub",
    "application/zip": "zip",
    "text/csv": "csv",
    "text/plain": "txt",
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "video/mp4": "mp4",
    "audio/mpeg": "mp3",
}


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


@dataclass(frozen=True)
class InlineFolder:
    """Represents an inline `mod_folder` activity rendered directly inside a section."""

    title: str
    files: list[tuple[str, str]]


@dataclass
class SectionData:
    """Parsed content of a single Moodle course section."""

    section_num: int
    title: str
    summary_text: str = ""
    activities: list[ActivityItem] = field(default_factory=list)
    embedded_files: list[LinkItem] = field(default_factory=list)
    external_links: list[LinkItem] = field(default_factory=list)
    inline_folders: list[InlineFolder] = field(default_factory=list)


def _extract_course_id_from_url(abs_url: str) -> str | None:
    """Extract course ID from a `/course/view.php` URL regardless of parameter order."""
    parsed = urlparse(abs_url)
    if not parsed.path.endswith("/course/view.php"):
        return None
    ids = parse_qs(parsed.query).get("id")
    if ids and ids[0].isdigit():
        return ids[0]
    return None


def _extract_course_section_from_url(abs_url: str) -> tuple[str, int] | None:
    """Extract `(course_id, section_num)` from a `/course/view.php` URL."""
    parsed = urlparse(abs_url)
    if not parsed.path.endswith("/course/view.php"):
        return None
    qs = parse_qs(parsed.query)
    ids = qs.get("id")
    secs = qs.get("section")
    if ids and ids[0].isdigit() and secs and secs[0].isdigit():
        return ids[0], int(secs[0])
    return None


def _extract_mod_info_from_url(abs_url: str) -> tuple[str, str] | None:
    """Extract `(mod_type, cmid)` from a `/mod/<type>/view.php` URL."""
    parsed = urlparse(abs_url)
    match = re.search(r"/mod/([a-z0-9_]+)/view\.php$", parsed.path)
    if not match:
        return None
    ids = parse_qs(parsed.query).get("id")
    cmid = ids[0] if ids and ids[0].isdigit() else ""
    return match.group(1), cmid


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
        if ext:
            stem = stem.rstrip(" .") or fallback
            cleaned = f"{stem}{dot}{ext}"
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


def _find_ufsc_course_list_container(soup: BeautifulSoup) -> Tag | None:
    """Locate the `blocks/ufsc_course_list` block container if present."""
    container = soup.select_one("section.block_ufsc_course_list, aside.block_ufsc_course_list")
    if container is not None:
        return container
    for block in soup.select("section.block, aside[role='complementary'], div.block"):
        heading = block.select_one("h5, h4, h3, .card-title")
        if heading and "disciplinas/turmas" in heading.get_text(" ", strip=True).lower():
            return block
    return None


def has_ufsc_course_list(html: str) -> bool:
    """Return True if the dashboard HTML contains the UFSC `block_ufsc_course_list` block."""
    soup = BeautifulSoup(html, "lxml")
    return _find_ufsc_course_list_container(soup) is not None


def extract_sesskey(html: str) -> str | None:
    """Extract Moodle's `sesskey` CSRF token from `M.cfg` JSON, hidden inputs, or links."""
    cfg_match = re.search(r'"sesskey"\s*:\s*"([A-Za-z0-9_-]+)"', html)
    if cfg_match:
        return cfg_match.group(1)

    soup = BeautifulSoup(html, "lxml")
    inp = soup.select_one('input[name="sesskey"][value]')
    if inp is not None:
        val = (inp.get("value") or "").strip()
        if val:
            return val

    for anchor in soup.select('a[href*="sesskey="]'):
        href = (anchor.get("href") or "").strip()
        keys = parse_qs(urlparse(href).query).get("sesskey")
        if keys and keys[0].strip():
            return keys[0].strip()
    return None


def parse_courses_from_ajax_payload(payload: object, base_url: str) -> list[CourseInfo]:
    """Parse Moodle 4.x `core_course_get_enrolled_courses_by_timeline_classification` JSON."""
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        return []
    first = payload[0]
    if first.get("error"):
        return []
    data = first.get("data")
    if not isinstance(data, dict):
        return []
    raw_courses = data.get("courses")
    if not isinstance(raw_courses, list):
        return []

    courses: list[CourseInfo] = []
    seen_ids: set[str] = set()
    for item in raw_courses:
        if not isinstance(item, dict):
            continue
        course_id = str(item.get("id", "")).strip()
        if not course_id.isdigit() or course_id in seen_ids:
            continue
        seen_ids.add(course_id)
        title = str(
            item.get("fullname")
            or item.get("fullnamedisplay")
            or item.get("shortname")
            or f"Course {course_id}"
        ).strip()
        short_code = str(item.get("shortname") or "").strip()
        if short_code == title:
            short_code = ""
        raw_url = str(item.get("viewurl") or f"course/view.php?id={course_id}").strip()
        semester = str(item.get("coursecategory") or "").strip() or "Outros"
        courses.append(
            CourseInfo(
                course_id=course_id,
                title=title or f"Course {course_id}",
                url=urljoin(base_url, raw_url),
                semester=semester,
                short_code=short_code,
            )
        )
    return courses


def parse_courses_from_dashboard(html: str, base_url: str) -> list[CourseInfo]:
    """Parse courses and semester groupings from UFSC Moodle `/my/index.php?seeall=1`."""
    soup = BeautifulSoup(html, "lxml")

    container = _find_ufsc_course_list_container(soup)

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
            course_id = _extract_course_id_from_url(href)
            if not course_id:
                continue
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
    for anchor in soup.select('a[href*="course/view.php"]'):
        href = urljoin(base_url, anchor.get("href", ""))
        if "change_visibility.php" in href:
            continue
        course_id = _extract_course_id_from_url(href)
        if not course_id:
            continue
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
    """Find `&section=N` or Moodle 4.4+ `section.php` URLs when `coursedisplay=1`."""
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
        has_inline_activities = bool(li.select("li.activity"))
        is_summary_only_card = "section-summary" in (li.get("class") or [])
        if has_inline_activities or (num == 0 and not is_summary_only_card):
            inline_full_sections.add(num)

    subpages: dict[int, str] = {}
    for li in main.select('li[id^="section-"].section-summary'):
        sec_id = li.get("id", "")
        try:
            sec_num = int(sec_id.split("-", 1)[1])
        except (IndexError, ValueError):
            continue
        if sec_num in inline_full_sections:
            continue
        for anchor in li.select('a[href*="course/section.php"]'):
            abs_url = urljoin(base_url, anchor.get("href", ""))
            parsed = urlparse(abs_url)
            if parsed.path.endswith("/course/section.php") and parse_qs(parsed.query).get("id"):
                subpages.setdefault(sec_num, abs_url.split("#", 1)[0])
                break

    for anchor in main.select("a[href]"):
        abs_url = urljoin(base_url, anchor.get("href", ""))
        extracted = _extract_course_section_from_url(abs_url)
        if not extracted or extracted[0] != course_id:
            continue
        sec_num = extracted[1]
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

        summary_el: Tag | None = None
        for sel in (".content > .summary", ".summarytext", ".summary"):
            for cand in sec_node.select(sel):
                if cand.find_parent("li", class_="activity") is None:
                    summary_el = cand
                    break
            if summary_el is not None:
                break
        summary_text = _clean_element_text(summary_el)

        embedded_files: list[LinkItem] = []
        external_links: list[LinkItem] = []
        inline_folders: list[InlineFolder] = []
        seen_files: set[str] = set()
        seen_ext: set[str] = set()

        def add_links(container: Tag | None, exclude_mod_folder: bool = False) -> None:
            if container is None:
                return
            p_links, e_links = extract_links_from_container(container, base_url)
            for item in p_links:
                if exclude_mod_folder and "/mod_folder/content/" in item.url:
                    continue
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
                if mod_type == "folder":
                    folder_files = parse_folder_files(str(act), base_url)
                    if folder_files:
                        name_el = act.select_one(".instancename") or act.select_one("h3, h4, .activityname")
                        folder_title = _clean_element_text(name_el) or "Folder"
                        inline_folders.append(InlineFolder(title=folder_title, files=folder_files))
                        add_links(act, exclude_mod_folder=True)
                        for desc_el in act.select(".contentafterlink, .activity-description, .activity-altcontent, .description"):
                            desc_txt = _clean_element_text(desc_el)
                            if desc_txt:
                                label_notes.append(f"### {folder_title}\n{desc_txt}")
                        continue
                # Some custom themes or labels put raw pluginfile links inside activities without modtype_label.
                add_links(act)
                continue

            act_url = urljoin(base_url, act_link["href"])
            mod_info = _extract_mod_info_from_url(act_url)
            cmid = mod_info[1] if mod_info else ""
            if not mod_type and mod_info:
                mod_type = mod_info[0]

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
                inline_folders=inline_folders,
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
        ".resourcecontent img[src*='pluginfile.php']",
        ".resourcecontent video[src*='pluginfile.php']",
        ".resourcecontent audio[src*='pluginfile.php']",
        ".resourcecontent source[src*='pluginfile.php']",
        '[role="main"] a[href*="pluginfile.php"]',
    )
    for selector in selectors:
        for el in soup.select(selector):
            raw = el.get("href") or el.get("data") or el.get("src")
            if not raw:
                continue
            abs_url = urljoin(base_url, raw)
            if _is_valid_pluginfile(abs_url):
                return abs_url
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
    selectors = (
        (".urlworkaround a[href]", ("href",), True),
        ("iframe#resourceobject[src], .resourcecontent iframe[src]", ("src",), False),
        ("object#resourceobject[data], .resourcecontent object[data]", ("data",), False),
        ('[role="main"] a[href]', ("href",), False),
    )
    for selector, attrs, allow_same_host in selectors:
        for el in soup.select(selector):
            raw = ""
            for attr in attrs:
                val = (el.get(attr) or "").strip()
                if val:
                    raw = val
                    break
            if not raw or raw.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
                continue
            abs_url = urljoin(base_url, raw)
            parsed = urlparse(abs_url)
            if parsed.scheme not in ("http", "https"):
                continue
            if allow_same_host or parsed.netloc.lower() != base_host:
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


def _is_valid_book_chapter_link(abs_url: str, cmid: str) -> tuple[bool, str]:
    """Validate that a URL points to a specific chapter of `mod_book` with matching `cmid`."""
    parsed = urlparse(abs_url)
    if not parsed.path.endswith("/mod/book/view.php"):
        return False, ""
    qs = parse_qs(parsed.query)
    if cmid and qs.get("id") != [cmid]:
        return False, ""
    ch_ids = qs.get("chapterid")
    if not ch_ids:
        return False, ""
    return True, ch_ids[0]


def _extract_active_toc_title(li: Tag) -> str:
    """Extract the active (non-linked) chapter title from an `<li>` without pulling nested `<li>` text."""
    for child in li.children:
        if isinstance(child, Tag) and child.name in ("strong", "b"):
            txt = _clean_element_text(child)
            if txt:
                return txt
    clone = BeautifulSoup(str(li), "lxml")
    li_copy = clone.find("li")
    if li_copy is None:
        return ""
    for nested in li_copy.select("ul, ol"):
        nested.decompose()
    return _clean_element_text(li_copy)


def parse_book_chapters(
    html: str,
    base_url: str,
    cmid: str,
    current_url: str | None = None,
) -> list[tuple[str, str]]:
    """Extract `(chapter_title, chapter_url)` items from a `mod_book` page, including the active chapter."""
    soup = BeautifulSoup(html, "lxml")
    toc = soup.select_one(".book_toc, #mod_book-chapters, .block_book_toc")
    chapters: list[tuple[str, str]] = []
    seen_urls: set[str] = set()
    active_added = False

    if toc is not None and toc.select("li"):
        for li in toc.select("li"):
            direct_anchor: Tag | None = None
            for anchor in li.select('a[href*="mod/book/view.php"]'):
                if anchor.find_parent("li") is li:
                    direct_anchor = anchor
                    break

            if direct_anchor is not None:
                abs_url = urljoin(base_url, direct_anchor.get("href", ""))
                valid, ch_id = _is_valid_book_chapter_link(abs_url, cmid)
                if valid and abs_url not in seen_urls:
                    seen_urls.add(abs_url)
                    chapters.append((direct_anchor.get_text(" ", strip=True) or f"Chapter {ch_id}", abs_url))
            elif not active_added:
                title = _extract_active_toc_title(li)
                if title:
                    target = current_url or ""
                    if target:
                        seen_urls.add(target)
                    chapters.append((title, target))
                    active_added = True

        if chapters:
            return chapters

    search_roots = [toc, soup] if toc is not None else [soup]
    for search_root in search_roots:
        for anchor in search_root.select('a[href*="mod/book/view.php"]'):
            abs_url = urljoin(base_url, anchor.get("href", ""))
            valid, ch_id = _is_valid_book_chapter_link(abs_url, cmid)
            if not valid or abs_url in seen_urls:
                continue
            seen_urls.add(abs_url)
            chapters.append((anchor.get_text(" ", strip=True) or f"Chapter {ch_id}", abs_url))
        if chapters:
            break

    return chapters


def filename_from_headers_or_url(headers: Iterable[tuple[str, str]] | dict[str, str], url: str) -> str:
    """Resolve the best filename from Content-Disposition headers, URL path, or Content-Type."""
    hdr_map = {k.lower(): v for k, v in dict(headers).items()}
    disp = hdr_map.get("content-disposition", "")
    name = ""

    # RFC 5987 filename*=charset'<optional-lang>'<encoded> takes precedence over filename="..."
    match_ext = re.search(r"filename\*\s*=\s*([^']*)'(?:[^']*')?([^;]+)", disp, re.IGNORECASE)
    if match_ext:
        charset = (match_ext.group(1) or "utf-8").strip() or "utf-8"
        raw_ext = match_ext.group(2).strip().strip('"')
        try:
            decoded = unquote(raw_ext, encoding=charset)
        except LookupError:
            decoded = unquote(raw_ext, encoding="utf-8")
        name = sanitize_filename(decoded)

    if not name:
        match_plain = re.search(r'filename\s*=\s*"([^"]+)"|filename\s*=\s*([^;\s]+)', disp, re.IGNORECASE)
        if match_plain:
            raw = match_plain.group(1) or match_plain.group(2)
            try:
                # Handle occasional ISO-8859-1 header encoding from older PHP servers.
                raw = raw.encode("latin-1").decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass
            name = sanitize_filename(unquote(raw))

    if not name:
        path_name = unquote(Path(urlparse(url).path).name)
        name = sanitize_filename(path_name or "download")

    _, ext = split_valid_extension(name)
    if not ext:
        ctype = hdr_map.get("content-type", "").split(";", 1)[0].strip().lower()
        inferred_ext = CONTENT_TYPE_EXTENSIONS.get(ctype, "")
        if inferred_ext:
            name = sanitize_filename(f"{name}.{inferred_ext}")

    return name
