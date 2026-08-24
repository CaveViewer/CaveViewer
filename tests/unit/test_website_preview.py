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


def test_contact_layout_uses_content_safe_document_flow() -> None:
    styles = (PREVIEW_ROOT / "assets/css/contact.css").read_text(encoding="utf-8")

    assert "html.page-contact-root {\n    overflow-y: auto;\n}" in styles
    assert (
        "body.page-contact {\n"
        "    display: flex;\n"
        "    flex-direction: column;\n"
        "    min-height: 100vh;\n"
        "    min-height: 100svh;\n"
        "    overflow: visible;\n"
        "}"
    ) in styles
    assert (
        ".page-contact main {\n"
        "    flex: 1 0 auto;\n"
        "    min-height: 0;\n"
        "    overflow: visible;\n"
        "}"
    ) in styles
    assert "grid-template-rows: minmax(0, 1fr) 36px;" not in styles
    assert "    height: 100svh;" not in styles
    assert "min-height: calc(100svh - 36px);" in styles


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


def test_image_delivery_uses_responsive_webp_and_reserves_layout_space() -> None:
    index = (PREVIEW_ROOT / "index.html").read_text(encoding="utf-8")
    features = (PREVIEW_ROOT / "features.html").read_text(encoding="utf-8")
    about = (PREVIEW_ROOT / "about.html").read_text(encoding="utf-8")
    home_styles = (PREVIEW_ROOT / "assets/css/home.css").read_text(encoding="utf-8")
    feature_styles = (PREVIEW_ROOT / "assets/css/features.css").read_text(
        encoding="utf-8"
    )
    about_styles = (PREVIEW_ROOT / "assets/css/about.css").read_text(
        encoding="utf-8"
    )
    readme = (PREVIEW_ROOT / "README.md").read_text(encoding="utf-8")

    page_budgets = {
        "Home": (
            1_300_000,
            (
                "assets/images/ginnie1.webp",
                "assets/images/software-hero-cave-strokes-full.webp",
            ),
        ),
        "Features": (
            400_000,
            (
                "assets/images/features/rendering-engine-1600.webp",
                "assets/images/features/map-library-1600.webp",
                "assets/images/features/capture-recording-1600.webp",
            ),
        ),
        "Team": (
            800_000,
            (
                "storage/uploads/2026/08/e02af4158100878810221f4cc8db33f52026e293-960.webp",
                "storage/uploads/2026/08/46afd31b727aa673872050329b90d75db21bd831-960.webp",
                "assets/images/magic-mr-v-cat-hacker-960.webp",
                "storage/uploads/2026/08/0dfffc22c2177fa30ec1e13d531c71b8eb71100d-850.webp",
                "storage/uploads/2026/08/32c8839d88fe923a90c84a1206c967245f98ef57-960.webp",
                "storage/uploads/2026/08/4278cd57d55958ba1979cfc0ef999019c70455de-960.webp",
            ),
        ),
    }

    for route, (budget, assets) in page_budgets.items():
        sizes = [(PREVIEW_ROOT / asset).stat().st_size for asset in assets]
        assert sum(sizes) <= budget, f"{route} preferred image budget exceeded"

    assert "## Image delivery budget" in readme
    assert "picture`/`srcset`" in readme
    assert "CSS `image-set`" in readme

    assert "ginnie1.webp" in home_styles
    assert "software-hero-cave-strokes-full.webp" in home_styles
    assert "image-set(" in home_styles
    assert 'width="64" height="32"' in index

    assert features.count("<picture>") == 3
    for source in (
        "rendering-engine-800.webp",
        "rendering-engine-1600.webp",
        "map-library-800.webp",
        "map-library-1600.webp",
        "capture-recording-800.webp",
        "capture-recording-1600.webp",
    ):
        assert source in features
    assert 'width="2558" height="1556" loading="eager" fetchpriority="high"' in features
    assert features.count('loading="lazy" decoding="async"') == 2
    assert ".feature-section__visual picture" in feature_styles

    assert about.count("<picture>") == 6
    assert about.count('sizes="(max-width: 900px) 50vw, 476px"') == 6
    assert about.count('loading="lazy" decoding="async"') == 3
    assert 'loading="eager" fetchpriority="high"' in about
    assert about.count(' width="') >= 6
    assert ".about-person__media picture" in about_styles


def test_wide_home_hero_has_explicit_art_direction_and_aligned_gutters() -> None:
    index = (PREVIEW_ROOT / "index.html").read_text(encoding="utf-8")
    home_styles = (PREVIEW_ROOT / "assets/css/home.css").read_text(encoding="utf-8")
    global_styles = (PREVIEW_ROOT / "assets/css/global.css").read_text(
        encoding="utf-8"
    )

    assert '<h1 id="hero-title">Explore what<br><span>lies beneath</span></h1>' in index
    assert "@media (min-width: 1600px) and (min-height: 900px)" in home_styles
    assert ".page-home .hero__content {\n        align-items: center;" in home_styles
    assert "@media (min-width: 1600px) and (min-aspect-ratio: 16 / 9)" in home_styles
    assert "auto 120%" in home_styles
    assert "70% center" in home_styles
    assert "width: min(calc(100% - 64px), var(--max));" in global_styles
