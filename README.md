# UFSC MoodleScraper

Python 3 scraper for UFSC Moodle (`https://presencial.moodle.ufsc.br/`), inspired by [`doebi/MoodleScraper`](https://github.com/doebi/MoodleScraper) and built for Moodle 4.x Classic + UFSC SSO (`idUFSC`) + `blocks/ufsc_course_list`.

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

- Parses all semesters and courses from `/my/index.php?seeall=1` (`blocks/ufsc_course_list`).
- Handles both inline topic/week courses and one-section-per-page courses (`coursedisplay = 1`, `&section=N`).
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

### Option 1: Interactive UFSC SSO Login (Playwright)

Opens a browser window at `/login/index.php` so you can sign in via `idUFSC`; cookies are saved locally in `.session/cookies.json`:

```bash
python scraper.py --login --list
python scraper.py --semester "2026 - 2"
python scraper.py --all
```

### Option 2: Reuse Your Open Chrome `MoodleSession` Cookie

Copy the `MoodleSession` (or `MoodleSessionpresencial`) cookie value—or full `Cookie:` header—from Chrome DevTools (`Application -> Cookies -> https://presencial.moodle.ufsc.br`), then run:

```bash
python scraper.py --cookie "YOUR_MOODLE_SESSION" --list
python scraper.py --cookie "MoodleSessionpresencial=..." --semester "2026 - 2"
```

Or copy `scraper.conf.sample` to `scraper.conf` (or set `MOODLE_SESSION` in your environment).

### Interactive Mode (like `doebi/MoodleScraper`)

```bash
python scraper.py
```

### Useful Flags

- `--list`: List semesters and courses without downloading.
- `--semester "2026 - 2"`: Scrape a specific semester (repeatable).
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

