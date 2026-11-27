# UFSC MoodleScraper

Python 3 scraper built for UFSC Moodle (`https://presencial.moodle.ufsc.br/`) and compatible with **any Moodle 4.x instance** (via `--base-url`), inspired by [`doebi/MoodleScraper`](https://github.com/doebi/MoodleScraper). Supports SSO login via Playwright, standard Moodle 4.x Boost AJAX course discovery, and UFSC `blocks/ufsc_course_list`.

## Folder Layout

```text
downloads/
+-- 2026 - 2/
|   +-- CNM7261-06340 (20262) - Política Externa Brasileira I/
|   |   +-- 00 - Informações gerais/
|   |   |   +-- Biblioteca/
|   |   |   |   +-- ...
|   |   |   +-- info.md
|   |   +-- 01 - Unidade 1- O que é política externa-/
|   |       +-- ...
```

## Features

- Discovers semesters and courses from UFSC `/my/index.php?seeall=1` (`blocks/ufsc_course_list`) or from standard Moodle 4.x instances via `lib/ajax/service.php` (`core_course_get_enrolled_courses_by_timeline_classification`) and `/my/courses.php`.
- Handles both inline topic/week courses and one-section-per-page courses (`coursedisplay = 1`, `&section=N` and Moodle 4.4+ `section.php`).
- Downloads & extracts:
  - `mod_resource` files (direct redirects and HTML wrapper pages), preserving extensions cleanly even when activity titles contain dots (e.g. `2026.1`, `J. Nabuco`)
  - `mod_folder` trees (preserving internal subfolders safely within Windows `MAX_PATH`) plus folder descriptions as `info.md`
  - Embedded `pluginfile.php` links and media (`<video>`, `<audio>`, `<iframe>`, `<object>`, `<img>`) inside `mod_label`, section summaries, and inline activity descriptions
  - `mod_page`, `mod_book` (multi-chapter), and `mod_assign` full `#region-main` prompts, due dates, and attachments as Markdown + downloaded files
  - `mod_url` links as `.url` shortcuts, entries in section `info.md`, and direct external PDF/document downloads (`download_external_files = true`) without forwarding Moodle cookies
- Skips already-downloaded non-empty files cleanly (`--overwrite` to force redownload), uses atomic `.part` writes, and retries transient `429`/`5xx` responses.
- Windows-safe filename, reserved-name, and path-length truncation.

## Setup

```bash
pip install -r requirements.txt
```

(`--login` automatically uses Playwright Chromium if installed, or falls back to local Google Chrome / Microsoft Edge.)

## Authentication & Usage

### Option 1: Interactive SSO Login (Playwright)

Opens a browser window at `/login/index.php` so you can sign in via SSO (e.g. `idUFSC`, SAML, CAS, or standard Moodle login); cookies are saved locally in `.session/cookies.json`:

```bash
python scraper.py --login --list
python scraper.py --semester "2026 - 2"
python scraper.py --all

# Any other Moodle 4.x site:
python scraper.py --base-url "https://moodle.example.edu" --login --all
```

### Option 2: Reuse Your Open Chrome Session Cookie (`MoodleSessionmdlpre`)

Copy the `MoodleSessionmdlpre` (or `MoodleSession`) cookie value—or full `Cookie:` header—from Chrome DevTools (`Application -> Cookies -> https://presencial.moodle.ufsc.br`), then run:

```bash
python scraper.py --cookie "YOUR_COOKIE_VALUE" --list
python scraper.py --cookie "MoodleSessionmdlpre=..." --semester "2026 - 2"
```

Or copy `scraper.conf.sample` to `scraper.conf` (or set `MOODLE_SESSION` in your environment). Passing a bare cookie value automatically sends both `MoodleSession` and `MoodleSessionmdlpre` for `presencial.moodle.ufsc.br`.

### Interactive Mode (like `doebi/MoodleScraper`)

```bash
python scraper.py
```

### Useful Flags

- `--base-url URL`: Moodle base URL (overrides `scraper.conf`, defaults to `https://presencial.moodle.ufsc.br/`).
- `--list`: List semesters/categories and courses without downloading.
- `--semester "2026 - 2"`: Scrape a specific semester or course category (repeatable).
- `--course-id 50035`: Scrape a specific course ID (repeatable).
- `--all`: Scrape all enrolled courses.
- `--overwrite`: Redownload existing files.
- `--dry-run`: Preview actions without writing files.
- `--dump-html`: Save raw course HTML into `.dump/`.
- `--output ./downloads`: Custom destination directory.

## Running Tests

```bash
python -m unittest discover -s tests -v
```

## License

Released under the [MIT License](LICENSE).

