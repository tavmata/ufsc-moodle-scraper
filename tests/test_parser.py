"""Unit tests for UFSC Moodle HTML parsing and Windows-safe path handling."""

import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from moodle_parser import (
    filename_from_headers_or_url,
    find_subpage_section_urls,
    is_login_page,
    parse_book_chapter_content,
    parse_book_chapters,
    parse_courses_from_dashboard,
    parse_course_sections,
    parse_folder_files,
    parse_folder_metadata,
    parse_page_or_assign_content,
    parse_resource_redirect_url,
    parse_url_activity_target,
    sanitize_filename,
    sanitize_name,
)
from scraper import parse_cookie_arg

BASE_URL = "https://presencial.moodle.ufsc.br/"


class SanitizationTests(unittest.TestCase):
    def test_strips_leading_and_trailing_dots_and_invalid_chars(self) -> None:
        self.assertEqual(
            sanitize_name(". Manual do Estudante - Resolução 227 ."),
            "Manual do Estudante - Resolução 227",
        )
        self.assertEqual(sanitize_name('A:B/C\\D*E?"F<G>H|I'), "A-B-C-D-E--F-G-H-I")

    def test_preserves_extension_when_truncating_and_handles_reserved_names(self) -> None:
        long_pdf = ("a" * 150) + ".pdf"
        out = sanitize_filename(long_pdf, max_length=40)
        self.assertTrue(out.endswith(".pdf"))
        self.assertLessEqual(len(out), 40)
        self.assertEqual(sanitize_name("CON"), "_CON")
        self.assertEqual(sanitize_filename("nul.txt"), "_nul.txt")

    def test_folder_name_truncation_does_not_treat_middle_dot_as_extension(self) -> None:
        long_title = "01 - Prof. Dr. Maria da Silva sobre Politica Externa Brasileira e Integracao Regional"
        out = sanitize_name(long_title, max_length=45)
        self.assertLessEqual(len(out), 45)
        self.assertTrue(out.startswith("01 - Prof. Dr. Maria"))
        self.assertEqual(
            sanitize_filename("Documento sem extensao. texto muito longo aqui", max_length=25),
            "Documento sem extensao. t",
        )


class DashboardParsingTests(unittest.TestCase):
    def test_parses_ufsc_course_list_with_bare_text_and_wrapped_headers(self) -> None:
        html = """
        <html><body>
          <section class="block_ufsc_course_list">
            <h5>Disciplinas/Turmas por período</h5>
            <div class="content">
              2026 - 2
              <div>
                <a href="https://presencial.moodle.ufsc.br/course/view.php?id=8566" title="Manual (20262)">
                  . Manual do Estudante - Resolução 227 .
                </a>
                <a href="https://presencial.moodle.ufsc.br/course/view.php?id=50035" title="CNM7261-06340 (20262)">
                  CNM7261-06340 (20262) - Política Externa Brasileira I
                </a>
              </div>
              <div class="semester">2025 - 1</div>
              <div>
                <a href="/course/view.php?id=24237">CNM6016|CNM7240 (20251)</a>
              </div>
              <div><strong>Outros</strong></div>
              <div>
                <a href="/course/view.php?id=18328">Espaço do professor</a>
                <a href="/blocks/ufsc_course_list/change_visibility.php?courseid=18328&amp;action=hide">Ocultar</a>
              </div>
            </div>
          </section>
        </body></html>
        """
        courses = parse_courses_from_dashboard(html, BASE_URL)
        self.assertEqual(len(courses), 4)
        self.assertEqual((courses[0].course_id, courses[0].semester), ("8566", "2026 - 2"))
        self.assertEqual((courses[1].course_id, courses[1].semester), ("50035", "2026 - 2"))
        self.assertEqual((courses[2].course_id, courses[2].semester), ("24237", "2025 - 1"))
        self.assertEqual((courses[3].course_id, courses[3].semester), ("18328", "Outros"))


class CourseParsingTests(unittest.TestCase):
    def test_parses_sections_activities_and_embedded_pluginfiles(self) -> None:
        html = """
        <html><body>
          <div role="main">
            <div class="course-content">
              <ul class="topics">
                <li id="section-0" class="section course-section main clearfix">
                  <h3 id="sectionid-1-title" class="sectionname">Informações gerais</h3>
                  <div class="content">
                    <div class="summary">
                      <p>Programa: <a href="/pluginfile.php/100/course/section/410842/Plano.pdf">Plano</a></p>
                      <p><a href="https://example.com/syllabus">External</a></p>
                    </div>
                    <ul class="section">
                      <li id="module-10" class="activity modtype_folder">
                        <a class="aalink" href="/mod/folder/view.php?id=10">
                          <span class="instancename">Biblioteca<span class="accesshide"> Pasta</span></span>
                        </a>
                      </li>
                      <li id="module-11" class="activity modtype_resource">
                        <a class="aalink" href="/mod/resource/view.php?id=11">
                          <span class="instancename">NABUCO<span class="accesshide"> Arquivo</span></span>
                        </a>
                        <span class="resourcelinkdetails">546.1 Kb Documento PDF</span>
                      </li>
                      <li id="module-12" class="activity modtype_label">
                        <div class="contentwithoutlink">
                          <img src="https://presencial.moodle.ufsc.br/theme/image.php/classic/core/1/icon">
                          <a href="https://presencial.moodle.ufsc.br/pluginfile.php/229/mod_label/intro/Artigo.pdf">Artigo</a>
                        </div>
                      </li>
                    </ul>
                  </div>
                </li>
              </ul>
            </div>
          </div>
        </body></html>
        """
        sections = parse_course_sections(html, BASE_URL)
        self.assertEqual(len(sections), 1)
        sec = sections[0]
        self.assertEqual(sec.section_num, 0)
        self.assertEqual(sec.title, "Informações gerais")
        self.assertEqual([a.mod_type for a in sec.activities], ["folder", "resource"])
        self.assertEqual(sec.activities[0].title, "Biblioteca")
        self.assertEqual(
            [f.url for f in sec.embedded_files],
            [
                "https://presencial.moodle.ufsc.br/pluginfile.php/100/course/section/410842/Plano.pdf",
                "https://presencial.moodle.ufsc.br/pluginfile.php/229/mod_label/intro/Artigo.pdf",
            ],
        )
        self.assertEqual([l.url for l in sec.external_links], ["https://example.com/syllabus"])

    def test_detects_coursedisplay_subpages(self) -> None:
        html = """
        <html><body><div role="main">
          <div class="course-content">
            <ul class="topics">
              <li id="section-0" class="section course-section main">
                <ul class="section"><li id="module-1" class="activity modtype_forum"></li></ul>
              </li>
              <li id="section-1" class="section course-section main section-summary">
                <a href="/course/view.php?id=50017&amp;section=1">Informações básicas</a>
              </li>
              <li id="section-3" class="section course-section main section-summary">
                <a href="/course/view.php?id=50017&amp;section=3#section-3">Avaliações</a>
              </li>
            </ul>
          </div>
        </div></body></html>
        """
        subpages = find_subpage_section_urls(html, "50017", BASE_URL)
        self.assertEqual(
            subpages,
            {
                1: "https://presencial.moodle.ufsc.br/course/view.php?id=50017&section=1",
                3: "https://presencial.moodle.ufsc.br/course/view.php?id=50017&section=3",
            },
        )

    def test_parses_folder_files_and_resource_wrapper_and_headers(self) -> None:
        folder_html = """
        <html><body><div role="main">
          <a href="https://presencial.moodle.ufsc.br/pluginfile.php/2220330/mod_folder/content/0/Sub/Texto%201.pdf?forcedownload=1">Texto 1.pdf</a>
        </div></body></html>
        """
        files = parse_folder_files(folder_html, BASE_URL)
        self.assertEqual(
            files,
            [
                (
                    "Sub/Texto 1.pdf",
                    "https://presencial.moodle.ufsc.br/pluginfile.php/2220330/mod_folder/content/0/Sub/Texto%201.pdf?forcedownload=1",
                )
            ],
        )

        wrapper_html = """
        <html><body><div role="main">
          <div class="resourceworkaround">
            <a href="https://presencial.moodle.ufsc.br/pluginfile.php/99/mod_resource/content/1/Guia.pdf">Guia.pdf</a>
          </div>
        </div></body></html>
        """
        self.assertEqual(
            parse_resource_redirect_url(wrapper_html, BASE_URL),
            "https://presencial.moodle.ufsc.br/pluginfile.php/99/mod_resource/content/1/Guia.pdf",
        )

        self.assertEqual(
            filename_from_headers_or_url(
                {"Content-Disposition": "attachment; filename*=UTF-8''Resolu%C3%A7%C3%A3o%20227.pdf"},
                "https://presencial.moodle.ufsc.br/pluginfile.php/1/a.pdf",
            ),
            "Resolução 227.pdf",
        )
        self.assertEqual(
            filename_from_headers_or_url(
                {"Content-Disposition": "attachment; filename*=UTF-8'pt-br'Guia%201.pdf"},
                "https://presencial.moodle.ufsc.br/pluginfile.php/1/a.pdf",
            ),
            "Guia 1.pdf",
        )
        self.assertTrue(is_login_page("<html></html>", "https://sistemas.ufsc.br/login?service=x"))
        self.assertTrue(is_login_page("<html></html>", "https://presencial.moodle.ufsc.br/Shibboleth.sso/Login"))
        self.assertTrue(
            is_login_page(
                '<div class="logininfo">Você acessou como visitante (<a href="/login/index.php">Acessar</a>)</div>',
                "https://presencial.moodle.ufsc.br/?redirect=0",
            )
        )
        self.assertFalse(
            is_login_page(
                '<div class="usermenu"><a href="/login/logout.php">Sair</a></div>',
                "https://presencial.moodle.ufsc.br/my/",
            )
        )
        self.assertEqual(parse_cookie_arg("abc123"), ("MoodleSession", "abc123"))
        self.assertEqual(
            parse_cookie_arg("MoodleSessionpresencial=xyz789"),
            ("MoodleSessionpresencial", "xyz789"),
        )

    def test_preserves_accesshide_section_title_and_extracts_media_and_altcontent(self) -> None:
        html = """
        <html><body><div role="main"><div class="course-content"><ul class="topics">
          <li id="section-0" class="section course-section main">
            <h3 class="sectionname accesshide">Geral</h3>
            <div class="content">
              <div class="summary">
                <video controls><source src="/pluginfile.php/10/course/section/1/Intro.mp4"></video>
              </div>
              <ul class="section">
                <li id="module-55" class="activity modtype_page">
                  <a class="aalink" href="/mod/page/view.php?id=55">
                    <span class="instancename">Leitura<span class="accesshide"> Página</span></span>
                  </a>
                  <div class="activity-altcontent">
                    <img src="/pluginfile.php/10/mod_page/intro/Mapa.png" alt="Mapa">
                  </div>
                </li>
              </ul>
            </div>
          </li>
        </ul></div></div></body></html>
        """
        sections = parse_course_sections(html, BASE_URL)
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0].title, "Geral")
        self.assertEqual(sections[0].activities[0].title, "Leitura")
        self.assertEqual(
            [f.url for f in sections[0].embedded_files],
            [
                "https://presencial.moodle.ufsc.br/pluginfile.php/10/course/section/1/Intro.mp4",
                "https://presencial.moodle.ufsc.br/pluginfile.php/10/mod_page/intro/Mapa.png",
            ],
        )

    def test_parse_book_chapters_ignores_breadcrumb_nav_and_url_target(self) -> None:
        book_html = """
        <html><body>
          <nav aria-label="Navegação"><a href="/my/">Painel</a></nav>
          <aside class="block_book_toc">
            <a href="/mod/book/view.php?id=30&amp;chapterid=101">1. Intro</a>
            <a href="/mod/book/view.php?id=30&amp;chapterid=102">2. Conclusão</a>
          </aside>
        </body></html>
        """
        chapters = parse_book_chapters(book_html, BASE_URL, "30")
        self.assertEqual(
            chapters,
            [
                ("1. Intro", "https://presencial.moodle.ufsc.br/mod/book/view.php?id=30&chapterid=101"),
                ("2. Conclusão", "https://presencial.moodle.ufsc.br/mod/book/view.php?id=30&chapterid=102"),
            ],
        )

        url_html = """
        <html><body><div role="main">
          <div class="urlworkaround">Clique em <a href="https://example.org/material.pdf">material</a></div>
        </div></body></html>
        """
        self.assertEqual(parse_url_activity_target(url_html, BASE_URL), "https://example.org/material.pdf")

    def test_parses_region_main_assign_prompt_folder_metadata_and_iframes(self) -> None:
        assign_html = """
        <html><body>
          <section id="region-main">
            <h2>Trabalho Final Kaplan</h2>
            <div class="activity-dates">Aberto: segunda, 1 ago 2026 | Vencimento: 30/11/2026</div>
            <div id="intro" class="activity-description">
              <p>Responda às questões 1 a 3 com base no texto anexo.</p>
              <a href="/pluginfile.php/77/mod_assign/intro/Roteiro.pdf">Roteiro.pdf</a>
              <iframe src="https://www.youtube.com/embed/dQw4w9WgXcQ" title="Video Aula"></iframe>
            </div>
            <div class="activity-navigation"><a href="/mod/forum/view.php?id=99">Atividade anterior</a></div>
            <div role="main">
              <h3>Status de envio</h3>
              <table><tr><td>Status de envio</td><td>Enviado</td></tr></table>
            </div>
            <nav class="navigation"><form class="jumpmenu"><option>Pular</option></form></nav>
          </section>
        </body></html>
        """
        text, p_links, e_links = parse_page_or_assign_content(assign_html, BASE_URL)
        self.assertIn("Vencimento: 30/11/2026", text)
        self.assertIn("Responda às questões 1 a 3", text)
        self.assertIn("Status de envio", text)
        self.assertNotIn("Atividade anterior", text)
        self.assertNotIn("Pular", text)
        self.assertEqual(
            [f.url for f in p_links],
            ["https://presencial.moodle.ufsc.br/pluginfile.php/77/mod_assign/intro/Roteiro.pdf"],
        )
        self.assertEqual([e.url for e in e_links], ["https://www.youtube.com/embed/dQw4w9WgXcQ"])

        folder_html = """
        <html><body><section id="region-main">
          <div id="intro" class="box generalbox">
            <p>Leituras obrigatorias do semestre. Veja <a href="https://example.edu/plano">plano</a>.</p>
          </div>
          <div role="main">
            <div id="folder_tree0" class="foldertree">
              <a href="https://presencial.moodle.ufsc.br/pluginfile.php/123/mod_folder/content/0/Texto.pdf">Texto.pdf</a>
            </div>
            <form action="/mod/folder/download_folder.php"><button>Download da pasta</button></form>
          </div>
        </section></body></html>
        """
        ftext, f_plugins, f_ext = parse_folder_metadata(folder_html, BASE_URL)
        self.assertIn("Leituras obrigatorias", ftext)
        self.assertNotIn("Download da pasta", ftext)
        self.assertEqual(f_plugins, [])
        self.assertEqual([e.url for e in f_ext], ["https://example.edu/plano"])

    def test_extracts_non_label_activity_descriptions_and_iframes_in_sections(self) -> None:
        html = """
        <html><body><div role="main"><div class="course-content"><ul class="topics">
          <li id="section-1" class="section course-section main">
            <h3 class="sectionname">Unidade 1</h3>
            <div class="content"><ul class="section">
              <li id="module-88" class="activity modtype_forum">
                <a class="aalink" href="/mod/forum/view.php?id=88">
                  <span class="instancename">Atividade 1<span class="accesshide"> Fórum</span></span>
                </a>
                <div class="description">
                  <div class="contentafterlink">
                    <p>Poste sua resenha ate sexta-feira.</p>
                    <iframe src="https://www.youtube.com/embed/abc123" title="Debate"></iframe>
                  </div>
                </div>
              </li>
            </ul></div>
          </li>
        </ul></div></div></body></html>
        """
        sections = parse_course_sections(html, BASE_URL)
        self.assertEqual(len(sections), 1)
        self.assertIn("### Atividade 1", sections[0].summary_text)
        self.assertEqual(sections[0].summary_text.count("Poste sua resenha ate sexta-feira."), 1)
        self.assertEqual(
            [(e.label, e.url) for e in sections[0].external_links],
            [("Debate", "https://www.youtube.com/embed/abc123")],
        )

    def test_parse_book_chapter_content_excludes_outer_book_header_and_nav(self) -> None:
        html = """
        <html><body><section id="region-main">
          <h2>Livro Principal</h2>
          <div class="activity-description">Intro geral do livro</div>
          <div role="main">
            <div class="navtop"><a href="?id=30&amp;chapterid=2">Proximo</a></div>
            <div class="book_content"><p>Texto exclusivo do capitulo 1.</p></div>
            <div class="navbottom"><a href="?id=30&amp;chapterid=2">Proximo</a></div>
          </div>
        </section></body></html>
        """
        text, _, _ = parse_book_chapter_content(html, BASE_URL)
        self.assertEqual(text, "Texto exclusivo do capitulo 1.")


if __name__ == "__main__":
    unittest.main()

