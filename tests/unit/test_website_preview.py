"""Contracts for the isolated static Lognova website mirror."""

from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PREVIEW_ROOT = REPOSITORY_ROOT / "website-preview"
REMOVED_MARKETING_SECTIONS = {
    "formats",
    "controls",
    "recording",
    "preferences",
}


class _ReferenceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.references: list[str] = []
        self.identifiers: set[str] = set()

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        values = dict(attrs)
        identifier = values.get("id")
        if identifier:
            self.identifiers.add(identifier)
        for attribute in ("href", "src"):
            value = values.get(attribute)
            if value:
                self.references.append(value)


def _html_pages() -> list[Path]:
    return sorted(PREVIEW_ROOT.glob("*.html"))


def test_preview_is_outside_the_github_pages_artifact() -> None:
    workflow = (REPOSITORY_ROOT / ".github/workflows/pages.yml").read_text(
        encoding="utf-8"
    )

    assert PREVIEW_ROOT.is_dir()
    assert "path: docs" in workflow
    assert "website-preview" not in workflow
    assert not (PREVIEW_ROOT / "CNAME").exists()


def test_preview_contains_only_the_canonical_public_routes() -> None:
    expected_pages = {
        "about.html",
        "contact.html",
        "features.html",
        "index.html",
    }

    actual_pages = {path.name for path in _html_pages()}
    assert actual_pages == expected_pages


def test_removed_marketing_sections_have_no_routes_links_or_assets() -> None:
    removed_assets = {
        "assets/css/controls.css",
        "assets/css/formats.css",
        "assets/css/recording.css",
        "assets/icons/hero/datasets.svg",
        "assets/images/software/controls-help.jpg",
        "assets/images/software/controls-panel.jpg",
        "assets/images/software/preferences-import.jpg",
        "assets/images/software/preferences-storage.jpg",
        "assets/images/software/preferences-streaming.jpg",
        "assets/images/software/recording-confirm.jpg",
        "assets/images/software/recording-countdown.jpg",
    }

    for section in REMOVED_MARKETING_SECTIONS:
        assert not (PREVIEW_ROOT / f"{section}.html").exists()
    for asset in removed_assets:
        assert not (PREVIEW_ROOT / asset).exists()

    for page in _html_pages():
        text = page.read_text(encoding="utf-8")
        for section in REMOVED_MARKETING_SECTIONS:
            assert f"{section}.html" not in text
            assert f">{section.title()}<" not in text


def test_preview_contains_only_static_file_types() -> None:
    forbidden_suffixes = {
        ".db",
        ".php",
        ".py",
        ".rb",
        ".sql",
        ".sqlite",
        ".sqlite3",
    }
    files = [path for path in PREVIEW_ROOT.rglob("*") if path.is_file()]

    assert files
    assert not [path for path in files if path.suffix.lower() in forbidden_suffixes]
    for page in _html_pages():
        text = page.read_text(encoding="utf-8")
        assert ".php" not in text
        assert "/api/" not in text


def test_lognova_design_assets_are_local() -> None:
    index = (PREVIEW_ROOT / "index.html").read_text(encoding="utf-8")

    for stylesheet in ("global.css", "readability.css", "home.css"):
        assert f'assets/css/{stylesheet}' in index
        assert (PREVIEW_ROOT / "assets/css" / stylesheet).is_file()
    for asset in (
        "assets/js/app.js",
        "assets/js/platform-download.js",
    ):
        assert asset in index
        assert (PREVIEW_ROOT / asset).is_file()
    assert (
        PREVIEW_ROOT / "assets/images/software-hero-cave-strokes-full.png"
    ).is_file()
    assert "Explore what" in index
    assert "data-platform-download" in index
    assert "See the whole cave" not in index
    assert "home-moment-grid" not in index
    assert "formats home-formats" not in index


def test_reveal_content_is_visible_without_javascript() -> None:
    styles = (PREVIEW_ROOT / "assets/css/global.css").read_text(encoding="utf-8")
    script = (PREVIEW_ROOT / "assets/js/app.js").read_text(encoding="utf-8")

    for page in _html_pages():
        page_text = page.read_text(encoding="utf-8")
        assert 'assets/css/global.css' in page_text
        assert "data-reveal" in page_text

    assert "html.reveal-enhanced [data-reveal] { opacity:0;" in styles
    assert "html.reveal-enhanced [data-reveal].is-visible { opacity:1;" in styles
    assert "\n[data-reveal] { opacity:0;" not in styles
    assert "document.documentElement.classList.add('reveal-enhanced');" in script
    assert script.index("reveal.forEach(el => observer.observe(el));") < script.index(
        "document.documentElement.classList.add('reveal-enhanced');"
    )
    assert "reveal.forEach(el => el.classList.add('is-visible'));" in script


def test_pages_expose_skip_paths_headings_and_noncolor_navigation_cues() -> None:
    styles = (PREVIEW_ROOT / "assets/css/global.css").read_text(encoding="utf-8")
    expected_headings = {
        "features.html": '<h1 class="sr-only">CaveViewer features</h1>',
        "about.html": '<h1 class="sr-only">CaveViewer team</h1>',
    }

    for page in _html_pages():
        page_text = page.read_text(encoding="utf-8")
        assert '<a class="skip-link" href="#main-content">Skip to main content</a>' in page_text
        assert '<main id="main-content" tabindex="-1">' in page_text

    for page_name, heading in expected_headings.items():
        assert heading in (PREVIEW_ROOT / page_name).read_text(encoding="utf-8")

    assert ".skip-link:focus-visible" in styles
    assert "#main-content {\n    scroll-margin-top:" in styles
    assert ".primary-nav > a[aria-current=\"page\"]" in styles
    assert "text-decoration-thickness: 2px;" in styles
    assert ".primary-nav > a:focus-visible {\n    outline: 2px solid" in styles


def test_preview_uses_one_header_and_has_no_member_profile_routes() -> None:
    pages = _html_pages()

    assert pages
    assert not list(PREVIEW_ROOT.glob("member-*.html"))

    for page in pages:
        text = page.read_text(encoding="utf-8")

        assert text.count('class="site-home"') == 1
        assert 'href="index.html" aria-label="CaveViewer home"><img' in text
        assert '<nav class="primary-nav" aria-label="Primary navigation">' in text
        assert 'href="features.html">Features</a>' in text or (
            'href="features.html" aria-current="page">Features</a>' in text
        )
        assert 'href="about.html">Team</a>' in text or (
            'href="about.html" aria-current="page">Team</a>' in text
        )
        assert 'href="contact.html">Contact</a>' in text or (
            'href="contact.html" aria-current="page">Contact</a>' in text
        )
        expected_download = (
            '#get-caveviewer' if page.name == "index.html"
            else 'index.html#get-caveviewer'
        )
        assert f'class="header-download" href="{expected_download}"' in text
        assert "member-" not in text

    about = (PREVIEW_ROOT / "about.html").read_text(encoding="utf-8")
    assert 'href="about.html" aria-current="page">Team</a>' in about
    assert '<article class="about-person" data-reveal>' in about
    assert '<a class="about-person"' not in about
    assert "Co-Creator / Chief Technology Wizard" in about
    assert '<p class="about-person__affiliation">Zero Viz Co-Op</p>' in about
    assert ">Chief Technology Wizard<" not in about
    assert "K3rnalPanic" not in about

    contact = (PREVIEW_ROOT / "contact.html").read_text(encoding="utf-8")
    assert 'href="contact.html" aria-current="page">Contact</a>' in contact

    features = (PREVIEW_ROOT / "features.html").read_text(encoding="utf-8")
    assert 'href="features.html" aria-current="page">Features</a>' in features
    for feature in (
        "Render What Others Can’t",
        "Explore the Map Library",
        "Record &amp; Share Dives",
    ):
        assert f">{feature}<" in features


def test_contact_page_preserves_the_current_form_submission_contract() -> None:
    contact = (PREVIEW_ROOT / "contact.html").read_text(encoding="utf-8")
    styles = (PREVIEW_ROOT / "assets/css/contact.css").read_text(encoding="utf-8")
    readme = (PREVIEW_ROOT / "README.md").read_text(encoding="utf-8")

    assert '<form class="contact-form" action="https://formsubmit.co/azdeatherage@gmail.com" method="POST">' in contact
    assert '<input type="hidden" name="_subject" value="CaveViewer contact form">' in contact
    assert '<input type="hidden" name="_template" value="table">' in contact
    assert 'name="_honey" tabindex="-1" autocomplete="off"' in contact
    assert '<label for="cf-name">Your name</label>' in contact
    assert '<input type="text" id="cf-name" name="name" placeholder="Paul" required>' in contact
    assert '<label for="cf-email">Your email</label>' in contact
    assert '<input type="email" id="cf-email" name="email" placeholder="you@example.com" required>' in contact
    assert '<label for="cf-message">Message</label>' in contact
    assert '<textarea id="cf-message" name="message"' in contact
    assert 'required></textarea>' in contact
    assert 'assets/css/contact.css' in contact
    assert '.contact-form__honeypot {' in styles
    assert 'FormSubmit' in readme
    assert 'no contact backend or database' in readme


def test_about_team_captions_use_compact_spacing() -> None:
    styles = (PREVIEW_ROOT / "assets/css/about.css").read_text(encoding="utf-8")

    assert ".about-person__caption {\n    flex: 1;\n    padding: 14px 3px 0;" in styles
    assert ".about-person__name-row { min-height: 0; }" in styles
    assert ".about-person__role {\n    margin: 8px 0 0;" in styles
    assert ".about-person__affiliation {\n    margin: 5px 0 0;" in styles


def test_about_team_photos_use_taller_frames_and_focal_points() -> None:
    about = (PREVIEW_ROOT / "about.html").read_text(encoding="utf-8")
    styles = (PREVIEW_ROOT / "assets/css/about.css").read_text(encoding="utf-8")

    assert about.count("--photo-position:") == 6
    assert 'alt="Brian Deatherage" style="--photo-position: 50% 0%;"' in about
    assert 'alt="Zsolt Szabo" style="--photo-position: 50% 40%;"' in about
    assert 'alt="Filipp R. Loginova" style="--photo-position: 50% 45%;"' in about
    assert "object-position: var(--photo-position, 50% 20%);" in styles
    assert "@media (min-width: 1181px) {\n    .about-person__media {\n        aspect-ratio: 4 / 3;" in styles


def test_every_local_page_reference_resolves() -> None:
    missing: list[tuple[str, str]] = []
    missing_fragments: list[tuple[str, str]] = []

    for page in _html_pages():
        parser = _ReferenceParser()
        parser.feed(page.read_text(encoding="utf-8"))
        for reference in parser.references:
            parsed = urlparse(reference)
            if parsed.scheme or reference.startswith(("mailto:", "about:")):
                continue
            target = PREVIEW_ROOT / parsed.path if parsed.path else page
            if not target.is_file():
                missing.append((page.name, reference))
                continue
            if parsed.fragment:
                target_parser = _ReferenceParser()
                target_parser.feed(target.read_text(encoding="utf-8"))
                if parsed.fragment not in target_parser.identifiers:
                    missing_fragments.append((page.name, reference))

    assert not missing
    assert not missing_fragments


def test_about_omits_sponsors_and_contact() -> None:
    about = (PREVIEW_ROOT / "about.html").read_text(encoding="utf-8")

    for removed_content in (
        "about-sponsors",
        "Sponsors",
        "about-contact",
        "Get in touch",
        "data-static-preview-form",
        "static-preview.js",
        "<form",
    ):
        assert removed_content not in about

    assert not (PREVIEW_ROOT / "assets/js/static-preview.js").exists()


def test_preview_documents_static_and_database_free_boundary() -> None:
    readme = (PREVIEW_ROOT / "README.md").read_text(encoding="utf-8")
    normalized = " ".join(readme.split())

    assert "unpublished static conversion" in readme
    assert "no PHP runtime" in readme
    assert "persistent storage, or database" in normalized
    assert "uploads only `docs/`" in readme
