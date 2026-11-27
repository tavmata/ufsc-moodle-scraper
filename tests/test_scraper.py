"""Unit and local loopback HTTP integration tests for scraper.py."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scraper
from scraper import (
    ScraperConfig,
    ScrapeStats,
    build_http_session,
    main,
    parse_cookie_arg,
    parse_cookie_pairs,
    resolve_download_filename,
)


class _DummyFailureResponse:
    url = "https://presencial.moodle.ufsc.br/pluginfile.php/1/mod_resource/content/1/broken.pdf"
    headers = {"Content-Disposition": 'attachment; filename="broken.pdf"'}

    def iter_content(self, chunk_size: int = 65536):
        yield b"%PDF-1.4 partial"
        raise RuntimeError("simulated dropped connection")


class CookieAndFilenameTests(unittest.TestCase):
    def test_cookie_parsing_single_and_header_strings(self) -> None:
        self.assertEqual(parse_cookie_arg("Cookie: MoodleSession=abc"), ("MoodleSession", "abc"))
        self.assertEqual(
            parse_cookie_pairs("Cookie: MoodleSessionpresencial=tok1; MOODLEID1_=tok2; "),
            [("MoodleSessionpresencial", "tok1"), ("MOODLEID1_", "tok2")],
        )

    def test_resolve_download_filename_preserves_extension_with_dots_in_title(self) -> None:
        self.assertEqual(
            resolve_download_filename("NABUCO, J. Abolicionismo..pdf", "NABUCO, J. (1883). Abolicionismo."),
            "NABUCO, J. (1883). Abolicionismo.pdf",
        )
        self.assertEqual(
            resolve_download_filename("plano.docx", "Plano de Ensino 2026.1"),
            "Plano de Ensino 2026.1.docx",
        )
        self.assertEqual(
            resolve_download_filename("Apresentacao_Maquiavel.pptx", "Apresentação Maquiavel.ppt"),
            "Apresentação Maquiavel.pptx",
        )
        self.assertEqual(
            resolve_download_filename("Manual 2.pdf", "Manual 2.pdf"),
            "Manual 2.pdf",
        )

    def test_build_http_session_skips_expired_saved_cookies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sess_path = Path(tmp) / "cookies.json"
            now = time.time()
            sess_path.write_text(
                json.dumps(
                    [
                        {
                            "name": "MoodleSession",
                            "value": "expired-token",
                            "domain": "presencial.moodle.ufsc.br",
                            "path": "/",
                            "expires": now - 60,
                        },
                        {
                            "name": "MoodleSessionValid",
                            "value": "fresh-token",
                            "domain": "presencial.moodle.ufsc.br",
                            "path": "/",
                            "expires": now + 3600,
                        },
                    ]
                ),
                encoding="utf-8",
            )
            old_session_file = scraper.SESSION_FILE
            scraper.SESSION_FILE = sess_path
            try:
                ses = build_http_session("https://presencial.moodle.ufsc.br/")
            finally:
                scraper.SESSION_FILE = old_session_file

            cookie_dict = ses.cookies.get_dict()
            self.assertNotIn("MoodleSession", cookie_dict)
            self.assertEqual(cookie_dict.get("MoodleSessionValid"), "fresh-token")

    def test_windows_path_fits_deep_folders_and_cleans_part_files_on_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deep_parts = [f"Subfolder_Level_{i}_With_Very_Long_Portuguese_Name" for i in range(6)]
            target_dir = scraper._fit_subdir_path(root / "folder", deep_parts)
            self.assertLessEqual(len(str(target_dir)), scraper.MAX_WIN_PATH)

            stats = ScrapeStats()
            long_name = ("Documento_Extremamente_Longo_" * 4) + ".pdf"
            p1 = scraper._disambiguate_path(target_dir, long_name, stats)
            stats.written_paths.add(p1)
            p2 = scraper._disambiguate_path(target_dir, long_name, stats)
            self.assertNotEqual(p1, p2)
            self.assertLessEqual(len(str(p1)), scraper.MAX_WIN_PATH)
            self.assertLessEqual(len(str(p2)), scraper.MAX_WIN_PATH)
            self.assertTrue(p2.name.endswith(".pdf"))

            cfg = ScraperConfig(root=root)
            with self.assertRaises(RuntimeError):
                scraper._stream_response_to_file(_DummyFailureResponse(), root, cfg, stats)
            self.assertEqual(list(root.glob("*.part")), [])
            self.assertFalse((root / "broken.pdf").exists())


class EndToEndLoopbackMoodleTests(unittest.TestCase):
    def test_full_cli_scrape_loopback_handles_coursedisplay_resources_folders_assign_book_and_urls(self) -> None:
        seen_cookies: list[str] = []
        ext_seen_cookies: list[str] = []

        class ExternalHostHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args) -> None:
                return

            def do_GET(self) -> None:
                ext_seen_cookies.append(self.headers.get("Cookie", ""))
                if self.path.endswith(".pdf"):
                    body = b"%PDF-1.4\n%ExternalFUNAG"
                    self.send_response(200)
                    self.send_header("Content-Type", "application/pdf")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(404)
                self.end_headers()

        ext_server = ThreadingHTTPServer(("127.0.0.1", 0), ExternalHostHandler)
        ext_thread = threading.Thread(target=ext_server.serve_forever, daemon=True)
        ext_thread.start()

        class MockMoodleHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args) -> None:
                return

            def _send(self, status: int, body: bytes, ctype: str, headers: dict[str, str] | None = None) -> None:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                if headers:
                    for k, v in headers.items():
                        self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                seen_cookies.append(self.headers.get("Cookie", ""))
                parsed = urlparse(self.path)
                qs = parse_qs(parsed.query)
                base_host_hdr = self.headers.get("Host", "127.0.0.1")

                if parsed.path == "/my/index.php":
                    html = f"""
                    <html><body>
                      <div class="usermenu"><a href="/login/logout.php">Sair</a></div>
                      <section class="block_ufsc_course_list"><div class="content">
                        2026 - 2
                        <div>
                          <a href="http://{base_host_hdr}/course/view.php?id=50017">
                            CNM7001 - Curso Teste
                          </a>
                        </div>
                      </div></section>
                    </body></html>
                    """.encode("utf-8")
                    self._send(200, html, "text/html; charset=utf-8")
                    return

                if parsed.path == "/course/view.php" and qs.get("id") == ["50017"]:
                    sec = qs.get("section", [None])[0]
                    if sec == "1":
                        html = """
                        <html><body><div role="main"><div class="course-content"><ul class="topics">
                          <li id="section-1" class="section course-section main">
                            <h3 class="sectionname">Unidade 1</h3>
                            <div class="content"><ul class="section">
                              <li id="module-20" class="activity modtype_folder">
                                <a class="aalink" href="/mod/folder/view.php?id=20">
                                  <span class="instancename">Biblioteca<span class="accesshide"> Pasta</span></span>
                                </a>
                              </li>
                              <li id="module-30" class="activity modtype_book">
                                <a class="aalink" href="/mod/book/view.php?id=30">
                                  <span class="instancename">Livro Base<span class="accesshide"> Livro</span></span>
                                </a>
                              </li>
                              <li id="module-40" class="activity modtype_assign">
                                <a class="aalink" href="/mod/assign/view.php?id=40">
                                  <span class="instancename">Resenha 1<span class="accesshide"> Tarefa</span></span>
                                </a>
                                <div class="activity-altcontent">Enviar ate sexta.</div>
                              </li>
                              <li id="module-50" class="activity modtype_url">
                                <a class="aalink" href="/mod/url/view.php?id=50">
                                  <span class="instancename">FUNAG Texto<span class="accesshide"> URL</span></span>
                                </a>
                              </li>
                              <li id="module-51" class="activity modtype_url">
                                <a class="aalink" href="/mod/url/view.php?id=51">
                                  <span class="instancename">FUNAG Wrapper<span class="accesshide"> URL</span></span>
                                </a>
                              </li>
                            </ul></div>
                          </li>
                        </ul></div></div></body></html>
                        """.encode("utf-8")
                        self._send(200, html, "text/html; charset=utf-8")
                        return

                    html = """
                    <html><body><div role="main"><div class="course-content"><ul class="topics">
                      <li id="section-0" class="section course-section main">
                        <h3 class="sectionname accesshide">Geral</h3>
                        <div class="content">
                          <ul class="section">
                            <li id="module-10" class="activity modtype_resource">
                              <a class="aalink" href="/mod/resource/view.php?id=10">
                                <span class="instancename">NABUCO, J. (1883). Abolicionismo.<span class="accesshide"> Arquivo</span></span>
                              </a>
                            </li>
                          </ul>
                        </div>
                      </li>
                      <li id="section-1" class="section course-section main section-summary">
                        <a href="/course/view.php?id=50017&amp;section=1">Unidade 1</a>
                      </li>
                    </ul></div></div></body></html>
                    """.encode("utf-8")
                    self._send(200, html, "text/html; charset=utf-8")
                    return

                if parsed.path == "/mod/resource/view.php":
                    self.send_response(302)
                    self.send_header("Location", "/pluginfile.php/100/mod_resource/content/1/Abolicionismo.pdf")
                    self.end_headers()
                    return

                if parsed.path == "/pluginfile.php/100/mod_resource/content/1/Abolicionismo.pdf":
                    self._send(
                        200,
                        b"%PDF-1.4\n%Nabuco",
                        "application/pdf",
                        {"Content-Disposition": 'attachment; filename="Abolicionismo.pdf"'},
                    )
                    return

                if parsed.path == "/mod/folder/view.php":
                    html = """
                    <html><body><section id="region-main">
                      <div class="box generalbox">Cronograma de leituras da pasta.</div>
                      <div role="main">
                        <div id="folder_tree0" class="foldertree">
                          <a href="/pluginfile.php/200/mod_folder/content/0/Sub/Artigo.pdf?forcedownload=1">Artigo.pdf</a>
                        </div>
                        <form action="/mod/folder/download_folder.php"><button>Download da pasta</button></form>
                      </div>
                    </section></body></html>
                    """.encode("utf-8")
                    self._send(200, html, "text/html; charset=utf-8")
                    return

                if parsed.path == "/pluginfile.php/200/mod_folder/content/0/Sub/Artigo.pdf":
                    self._send(200, b"%PDF-1.4\n%Artigo", "application/pdf")
                    return

                if parsed.path == "/mod/assign/view.php":
                    html = """
                    <html><body><section id="region-main">
                      <div class="activity-description">
                        Questao unica sobre politica externa.
                        <a href="/pluginfile.php/300/mod_assign/intro/Guia_Resenha.pdf">Guia_Resenha.pdf</a>
                      </div>
                      <div role="main"><div>Status de envio: Nenhuma tentativa</div></div>
                    </section></body></html>
                    """.encode("utf-8")
                    self._send(200, html, "text/html; charset=utf-8")
                    return

                if parsed.path == "/pluginfile.php/300/mod_assign/intro/Guia_Resenha.pdf":
                    self._send(200, b"%PDF-1.4\n%Guia", "application/pdf")
                    return

                if parsed.path == "/mod/book/view.php":
                    chapter = qs.get("chapterid", ["1"])[0]
                    html = f"""
                    <html><body>
                      <nav aria-label="Navegação"><a href="/my/">Painel</a></nav>
                      <aside class="block_book_toc">
                        <a href="/mod/book/view.php?id=30&amp;chapterid=1">Cap 1</a>
                        <a href="/mod/book/view.php?id=30&amp;chapterid=2">Cap 2</a>
                      </aside>
                      <div role="main"><p>Conteudo do capitulo {chapter}</p></div>
                    </body></html>
                    """.encode("utf-8")
                    self._send(200, html, "text/html; charset=utf-8")
                    return

                if parsed.path == "/mod/url/view.php" and qs.get("id") == ["50"]:
                    self.send_response(302)
                    self.send_header("Location", f"http://127.0.0.1:{ext_server.server_port}/funag/public.pdf")
                    self.end_headers()
                    return

                if parsed.path == "/mod/url/view.php" and qs.get("id") == ["51"]:
                    html = f"""
                    <html><body><div role="main">
                      <div class="urlworkaround">
                        Clique em <a href="http://127.0.0.1:{ext_server.server_port}/funag/wrapped.pdf">wrapped</a>
                      </div>
                    </div></body></html>
                    """.encode("utf-8")
                    self._send(200, html, "text/html; charset=utf-8")
                    return

                self._send(404, b"Not found", "text/plain")

        server = ThreadingHTTPServer(("127.0.0.1", 0), MockMoodleHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root_dir = Path(tmp)
                conf_path = root_dir / "scraper.conf"
                out_dir = root_dir / "downloads"
                conf_path.write_text(
                    f"[scraper]\nbaseurl = http://127.0.0.1:{server.server_port}/\nroot = {out_dir}\n",
                    encoding="utf-8",
                )

                # Dry-run should not create downloaded files or touch external URLs.
                rc_dry = main(
                    [
                        "--conf",
                        str(conf_path),
                        "--cookie",
                        "MoodleSessionpresencial=test-session-123",
                        "--all",
                        "--dry-run",
                    ]
                )
                self.assertEqual(rc_dry, 0)
                self.assertFalse(out_dir.exists())
                self.assertEqual(ext_seen_cookies, [])

                # Real scrape run with --dump-html.
                rc_run = main(
                    [
                        "--conf",
                        str(conf_path),
                        "--cookie",
                        "MoodleSessionpresencial=test-session-123",
                        "--semester",
                        "2026 - 2",
                        "--dump-html",
                    ]
                )
                self.assertEqual(rc_run, 0)
                self.assertTrue(any("MoodleSessionpresencial=test-session-123" in c for c in seen_cookies))
                self.assertTrue(ext_seen_cookies)
                self.assertTrue(all("MoodleSession" not in c for c in ext_seen_cookies))

                course_dir = out_dir / "2026 - 2" / "CNM7001 - Curso Teste"
                sec0 = course_dir / "00 - Geral"
                sec1 = course_dir / "01 - Unidade 1"

                self.assertTrue((sec0 / "NABUCO, J. (1883). Abolicionismo.pdf").is_file())
                self.assertTrue((sec1 / "Biblioteca" / "Sub" / "Artigo.pdf").is_file())
                self.assertIn(
                    "Cronograma de leituras da pasta.",
                    (sec1 / "Biblioteca" / "info.md").read_text(encoding="utf-8"),
                )
                self.assertTrue((sec1 / "Guia_Resenha.pdf").is_file())
                assign_md = (sec1 / "assign_Resenha 1.md").read_text(encoding="utf-8")
                self.assertIn("Questao unica sobre politica externa.", assign_md)
                book_md = (sec1 / "book_Livro Base.md").read_text(encoding="utf-8")
                self.assertIn("Conteudo do capitulo 1", book_md)
                self.assertIn("Conteudo do capitulo 2", book_md)
                self.assertTrue((sec1 / "FUNAG Texto.url").is_file())
                self.assertTrue((sec1 / "FUNAG Texto.pdf").is_file())
                self.assertTrue((sec1 / "FUNAG Wrapper.url").is_file())
                self.assertTrue((sec1 / "FUNAG Wrapper.pdf").is_file())
                self.assertIn("### Resenha 1", (sec1 / "info.md").read_text(encoding="utf-8"))
                self.assertTrue(any((course_dir / ".dump").glob("course-50017*.html")))

                # Second run should cleanly skip all downloaded files and notes.
                rc_rerun = main(
                    [
                        "--conf",
                        str(conf_path),
                        "--cookie",
                        "MoodleSessionpresencial=test-session-123",
                        "--all",
                    ]
                )
                self.assertEqual(rc_rerun, 0)
        finally:
            server.shutdown()
            server.server_close()
            ext_server.shutdown()
            ext_server.server_close()


if __name__ == "__main__":
    unittest.main()
