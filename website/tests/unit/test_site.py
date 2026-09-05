"""Contracts for the production static CaveViewer website."""

import json
import importlib.util
import re
import subprocess
import sys
from datetime import date
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

import pytest


SITE_ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = SITE_ROOT.parent
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
    return sorted(SITE_ROOT.glob("*.html"))


def _sync_release_module():
    spec = importlib.util.spec_from_file_location(
        "sync_release", SITE_ROOT / "scripts" / "sync_release.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_pages_workflow_builds_only_the_approved_production_artifact(
    tmp_path: Path,
) -> None:
    workflow = (REPOSITORY_ROOT / ".github/workflows/pages.yml").read_text(
        encoding="utf-8"
    )

    assert "website/scripts/build_site.py" in workflow
    assert "${{ runner.temp }}/caveviewer-pages" in workflow
    assert '"website/**"' in workflow
    assert '"docs/development/**"' in workflow
    assert "github.ref == 'refs/heads/main'" in workflow
    assert (SITE_ROOT / "CNAME").read_text(encoding="utf-8") == "www.caveviewer.com"

    artifact = tmp_path / "artifact"
    generated = subprocess.run(
        [
            sys.executable,
            str(SITE_ROOT / "scripts" / "build_site.py"),
            "--output",
            str(artifact),
        ],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert generated.returncode == 0, generated.stderr
    assert {path.name for path in artifact.iterdir()} == {
        "CNAME",
        "about.html",
        "advantage.html",
        "assets",
        "contact.html",
        "development",
        "docs.html",
        "index.html",
        "media.html",
        "sponsors.html",
        "storage",
    }
    assert (artifact / "CNAME").read_text(encoding="utf-8") == "www.caveviewer.com"
    assert (artifact / "development" / "AGENTS.md").is_file()
    assert not (artifact / "previous-site").exists()


def test_ci_and_dependency_workflows_keep_the_website_supply_chain_bounded() -> None:
    checks = (REPOSITORY_ROOT / ".github/workflows/site-checks.yml").read_text(
        encoding="utf-8"
    )
    dependabot = (REPOSITORY_ROOT / ".github/dependabot.yml").read_text(
        encoding="utf-8"
    )

    assert "pull_request:" in checks
    assert "workflow_dispatch:" in checks
    assert "branches:\n      - main" in checks
    assert "permissions:\n  contents: read" in checks
    assert "cancel-in-progress: true" in checks
    assert "name: Static website contracts" in checks
    assert "name: Browser website checks" in checks
    assert "pytest==8.4.2" in checks
    assert "pytest -o addopts='' website/tests/unit/test_site.py -q" in checks
    assert "npm ci --ignore-scripts" in checks
    assert "playwright install --with-deps chromium" in checks
    assert "run: |\n          set -e" in checks
    assert "website/tests/unit/test_site.py" in checks
    assert "website/scripts/build_site.py" in checks
    assert "secrets:" not in checks

    for workflow_path in sorted((REPOSITORY_ROOT / ".github/workflows").glob("*.yml")):
        workflow = workflow_path.read_text(encoding="utf-8")
        action_references = re.findall(r"uses:\s+([^\s#]+)", workflow)
        assert action_references, workflow_path
        for action in action_references:
            if action.startswith("./.github/workflows/"):
                assert (REPOSITORY_ROOT / action.removeprefix("./")).is_file()
                continue
            assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", action), (
                workflow_path,
                action,
            )

    assert 'package-ecosystem: "github-actions"' in dependabot
    assert 'package-ecosystem: "npm"' in dependabot
    assert 'directory: "/website/tests/browser"' in dependabot
    assert 'interval: "weekly"' in dependabot
    assert "default-days: 7" in dependabot
    assert "open-pull-requests-limit: 2" in dependabot
    assert "open-pull-requests-limit: 1" in dependabot


@pytest.mark.parametrize(
    ("field", "value", "error"),
    (
        (
            "repository",
            "https://example.invalid/CaveViewer",
            "official CaveViewer GitHub repository",
        ),
        ("channel", "Nightly", "Preview or Stable"),
        ("version", "1.0.92-preview", "exactly three decimal components"),
    ),
)
def test_release_manifest_rejects_untrusted_release_coordinates(
    tmp_path: Path, field: str, value: str, error: str
) -> None:
    sync_release = _sync_release_module()
    release = json.loads(
        (SITE_ROOT / "assets/data/release.json").read_text(encoding="utf-8")
    )
    release[field] = value
    manifest_path = tmp_path / "release.json"
    manifest_path.write_text(json.dumps(release), encoding="utf-8")

    with pytest.raises(ValueError, match=error):
        sync_release.load_release_data(manifest_path)


def test_release_manifest_rejects_noncanonical_package_names(tmp_path: Path) -> None:
    sync_release = _sync_release_module()
    release = json.loads(
        (SITE_ROOT / "assets/data/release.json").read_text(encoding="utf-8")
    )
    release["platforms"]["windows"]["artifact"] = "other-installer.exe"
    manifest_path = tmp_path / "release.json"
    manifest_path.write_text(json.dumps(release), encoding="utf-8")

    with pytest.raises(ValueError, match="official, version-matched"):
        sync_release.load_release_data(manifest_path)


def test_release_manifest_rejects_invalid_release_date(tmp_path: Path) -> None:
    sync_release = _sync_release_module()
    release = json.loads(
        (SITE_ROOT / "assets/data/release.json").read_text(encoding="utf-8")
    )
    release["release_date"] = "2026-02-30"
    manifest_path = tmp_path / "release.json"
    manifest_path.write_text(json.dumps(release), encoding="utf-8")

    with pytest.raises(ValueError, match="valid ISO date"):
        sync_release.load_release_data(manifest_path)


def test_website_contains_only_the_canonical_public_routes() -> None:
    expected_pages = {
        "about.html",
        "advantage.html",
        "contact.html",
        "docs.html",
        "index.html",
        "media.html",
        "sponsors.html",
    }

    actual_pages = {path.name for path in _html_pages()}
    assert actual_pages == expected_pages


def test_canonical_metadata_uses_navigation_names_without_visible_preview_copy() -> None:
    expected_titles = {
        "index.html": "CaveViewer — Explore What Lies Beneath",
        "advantage.html": "Why CaveViewer",
        "docs.html": "Docs — CaveViewer",
        "about.html": "Team — CaveViewer",
        "media.html": "Projects — CaveViewer",
        "sponsors.html": "Sponsors — CaveViewer",
        "contact.html": "Contact — CaveViewer",
    }

    assert set(expected_titles) == {path.name for path in _html_pages()}
    assert len(set(expected_titles.values())) == len(expected_titles)
    for page_name, title in expected_titles.items():
        page = (SITE_ROOT / page_name).read_text(encoding="utf-8")
        assert f"<title>{title}</title>" in page
        assert "Site Preview" not in page
        assert '<meta name="description" content="' in page
        assert '<meta name="robots" content="noindex">' in page

    assert not (SITE_ROOT / "features.html").exists()


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
        assert not (SITE_ROOT / f"{section}.html").exists()
    for asset in removed_assets:
        assert not (SITE_ROOT / asset).exists()

    for page in _html_pages():
        text = page.read_text(encoding="utf-8")
        for section in REMOVED_MARKETING_SECTIONS:
            assert f"{section}.html" not in text


def test_published_site_contains_only_static_file_types() -> None:
    forbidden_suffixes = {
        ".db",
        ".php",
        ".py",
        ".rb",
        ".sql",
        ".sqlite",
        ".sqlite3",
    }
    files = _html_pages()
    for directory in (SITE_ROOT / "assets", SITE_ROOT / "storage"):
        files.extend(path for path in directory.rglob("*") if path.is_file())

    assert files
    assert not [path for path in files if path.suffix.lower() in forbidden_suffixes]
    for page in _html_pages():
        text = page.read_text(encoding="utf-8")
        assert ".php" not in text
        assert "/api/" not in text


def test_lognova_design_assets_are_local() -> None:
    index = (SITE_ROOT / "index.html").read_text(encoding="utf-8")

    for stylesheet in ("global.css", "readability.css", "home.css"):
        assert f'assets/css/{stylesheet}' in index
        assert (SITE_ROOT / "assets/css" / stylesheet).is_file()
    for asset in (
        "assets/js/app.js",
        "assets/js/platform-download.js",
    ):
        assert asset in index
        assert (SITE_ROOT / asset).is_file()
    assert (
        SITE_ROOT / "assets/images/software-hero-cave-strokes-full.png"
    ).is_file()
    assert "Explore what" in index
    assert "data-platform-download" in index
    assert "Designed for cave divers, explorers, cartographers" in index
    assert "The app is completely free—no advertisements, no subscriptions" in index
    assert "hero__support" not in index
    assert "256 GB of free disk space" not in index
    assert "hero__formats" not in index
    assert "See the whole cave" not in index
    assert "home-moment-grid" not in index
    assert "formats home-formats" not in index


def test_release_manifest_generates_every_download_reference() -> None:
    manifest_path = SITE_ROOT / "assets/data/release.json"
    generator = SITE_ROOT / "scripts/sync_release.py"
    index_path = SITE_ROOT / "index.html"
    index = index_path.read_text(encoding="utf-8")
    docs = (SITE_ROOT / "docs.html").read_text(encoding="utf-8")
    script = (SITE_ROOT / "assets/js/platform-download.js").read_text(
        encoding="utf-8"
    )

    generated = subprocess.run(
        [sys.executable, str(generator), "--check"],
        cwd=SITE_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert generated.returncode == 0, generated.stderr

    release = json.loads(manifest_path.read_text(encoding="utf-8"))
    base_url = (
        f"{release['repository']}/releases/download/v{release['version']}/"
    )
    platforms = release["platforms"]
    expected_urls = {
        "windows": f"{base_url}{platforms['windows']['artifact']}",
        "linux": f"{base_url}{platforms['linux']['artifact']}",
        "macos-arm64": f"{base_url}{platforms['macos']['architectures']['arm64']['artifact']}",
        "macos-x86_64": f"{base_url}{platforms['macos']['architectures']['x86_64']['artifact']}",
    }
    inline_manifest = re.search(
        r'<script type="application/json" data-release-data>\s*(.*?)\s*</script>',
        index,
        flags=re.DOTALL,
    )

    assert inline_manifest is not None
    assert json.loads(inline_manifest.group(1)) == release
    assert '<!-- Generated from assets/data/release.json by ' in index
    assert f'href="{expected_urls["windows"]}" data-primary-download' in index
    assert all(url in index for url in expected_urls.values())
    assert index.count('<path d="M12 3v11m0 0 4-4m-4 4-4-4M5 17v3h14v-3"') == 6
    assert '<b aria-hidden="true">↓</b>' not in index
    assert "data-platform-install-note=" not in index
    for platform in (platforms["windows"], platforms["macos"], platforms["linux"]):
        assert platform["install_note"] in docs

    assert all(url in docs for url in expected_urls.values())
    assert "Download CaveViewer for your desktop platform" not in docs
    assert docs.count('<div class="docs-install-card__actions">') == 3
    assert 'class="docs-install-card__help"' not in docs
    assert "learn.microsoft.com" not in docs
    assert "support.apple.com" not in docs
    assert docs.count('class="docs-install-card__downloads"') == 3
    for label in (
        "Download CaveViewer for Windows",
        "Download CaveViewer for Apple silicon",
        "Download CaveViewer for Intel Mac",
        "Download CaveViewer for Linux",
    ):
        assert f'aria-label="{label}"' in docs

    assert "<!-- installation-guidance:start -->" in docs
    assert "<!-- installation-guidance:end -->" in docs
    assert "Generated from assets/data/release.json" in docs
    for platform_name in ("windows", "macos"):
        security = platforms[platform_name]["security"]
        assert security["explanation"] in docs
        assert escape(security["action"]) in docs

    noscript = index[index.index("<noscript>") : index.index("</noscript>")]
    assert all(url in noscript for url in expected_urls.values())
    assert release["channel"] in index
    assert release["version"] in index
    release_date = date.fromisoformat(release["release_date"])
    assert (
        f"Build on {release_date.strftime('%B')} {release_date.day} "
        f"{release_date.year}"
    ) in index
    assert release["repository"] not in script
    assert release["version"] not in script
    assert all(
        artifact not in script
        for artifact in (
            platforms["windows"]["artifact"],
            platforms["linux"]["artifact"],
            platforms["macos"]["architectures"]["arm64"]["artifact"],
            platforms["macos"]["architectures"]["x86_64"]["artifact"],
        )
    )
    assert "data-release-data" in script


def test_reveal_content_is_visible_without_javascript() -> None:
    styles = (SITE_ROOT / "assets/css/global.css").read_text(encoding="utf-8")
    script = (SITE_ROOT / "assets/js/app.js").read_text(encoding="utf-8")
    pages_with_reveals = []

    for page in _html_pages():
        page_text = page.read_text(encoding="utf-8")
        assert 'assets/css/global.css' in page_text
        if "data-reveal" in page_text:
            pages_with_reveals.append(page.name)

    assert pages_with_reveals

    assert re.search(
        r"html\.reveal-enhanced \[data-reveal\] \{\s*opacity:\s*0;", styles
    )
    assert re.search(
        r"html\.reveal-enhanced \[data-reveal\]\.is-visible \{\s*opacity:\s*1;",
        styles,
    )
    assert not re.search(r"(?m)^\[data-reveal\]\s*\{\s*opacity:\s*0;", styles)
    assert "document.documentElement.classList.add('reveal-enhanced');" in script
    assert script.index("reveal.forEach(el => observer.observe(el));") < script.index(
        "document.documentElement.classList.add('reveal-enhanced');"
    )
    assert "reveal.forEach(el => el.classList.add('is-visible'));" in script


def test_reduced_motion_settles_all_pages_and_team_cards_stay_presentational() -> None:
    global_styles = (SITE_ROOT / "assets/css/global.css").read_text(
        encoding="utf-8"
    )
    about_styles = (SITE_ROOT / "assets/css/about.css").read_text(
        encoding="utf-8"
    )
    theme_styles = (SITE_ROOT / "assets/css/app-theme.css").read_text(
        encoding="utf-8"
    )
    about = (SITE_ROOT / "about.html").read_text(encoding="utf-8")
    script = (SITE_ROOT / "assets/js/app.js").read_text(encoding="utf-8")

    assert "@media (prefers-reduced-motion: reduce)" in global_styles
    assert "animation: none !important;" in global_styles
    assert "transition: none !important;" in global_styles
    assert "html.reveal-enhanced [data-reveal] {\n        opacity: 1 !important;" in global_styles
    assert "const prefersReducedMotion = window.matchMedia?.(" in script
    assert "if (!prefersReducedMotion && 'IntersectionObserver' in window)" in script

    assert "about-person__scan" not in about
    assert "about-person__scan" not in about_styles
    assert "about-person__scan" not in theme_styles
    assert ".about-person:hover" not in about_styles
    assert ".about-person:focus" not in about_styles
    assert "cv-scan" not in about_styles
    assert "transition: filter" not in about_styles
    assert "transform: scale(1.01)" not in about_styles
    assert "filter: grayscale(0) saturate(1) contrast(1.02) brightness(.97);" in about_styles


def test_pages_expose_skip_paths_headings_and_noncolor_navigation_cues() -> None:
    styles = (SITE_ROOT / "assets/css/global.css").read_text(encoding="utf-8")
    expected_headings = {
        "advantage.html": '<h1 class="sr-only">Why CaveViewer</h1>',
        "about.html": '<h1 class="sr-only">CaveViewer team</h1>',
        "docs.html": '<h1 class="sr-only" id="docs-title">CaveViewer Documentation</h1>',
        "media.html": '<h1 id="media-page-title" class="sr-only">Projects</h1>',
        "sponsors.html": '<h1 id="sponsors-page-title" class="sr-only">Sponsors</h1>',
    }

    for page in _html_pages():
        page_text = page.read_text(encoding="utf-8")
        assert '<a class="skip-link" href="#main-content">Skip to main content</a>' in page_text
        assert '<main id="main-content" tabindex="-1">' in page_text

    for page_name, heading in expected_headings.items():
        assert heading in (SITE_ROOT / page_name).read_text(encoding="utf-8")

    assert ".skip-link:focus-visible" in styles
    assert "#main-content {\n    scroll-margin-top:" in styles
    assert ".primary-nav > a[aria-current=\"page\"]" in styles
    assert "text-decoration-thickness: 2px;" in styles
    assert ".primary-nav > a:focus-visible {\n    outline: 2px solid" in styles


def test_shared_styles_have_one_current_header_and_endcap_contract() -> None:
    global_styles = (SITE_ROOT / "assets/css/global.css").read_text(
        encoding="utf-8"
    )
    readability_styles = (SITE_ROOT / "assets/css/readability.css").read_text(
        encoding="utf-8"
    )

    assert global_styles.count(".site-header {\n") == 1
    assert "/* Shared header" in global_styles
    assert "/* Intermediate desktop keeps the primary navigation inline. */" in global_styles
    assert "/* Small screens replace the inline destinations with a focused menu. */" in global_styles
    assert "@media (max-width: 1180px)" not in global_styles

    for retired_selector in (
        ".site-footer",
        ".footer-",
        ".header-action",
        ".brand__",
        ".cv-survey",
    ):
        assert retired_selector not in global_styles

    for retired_selector in (".site-footer", ".section", ".content-section"):
        assert retired_selector not in readability_styles


def test_website_uses_one_header_and_has_no_member_profile_routes() -> None:
    pages = _html_pages()

    assert pages
    assert not list(SITE_ROOT.glob("member-*.html"))

    for page in pages:
        text = page.read_text(encoding="utf-8")

        assert text.count('class="site-home"') == 1
        assert 'href="index.html" aria-label="CaveViewer home"><img' in text
        assert 'src="assets/icons/caveviewer-logo-faceted-rim-beam.svg" width="64" height="32"' in text
        assert '<nav class="primary-nav" aria-label="Primary navigation">' in text
        nav_start = text.index(
            '<nav class="primary-nav" aria-label="Primary navigation">'
        )
        nav = text[nav_start : text.index("</nav>", nav_start)]
        assert 'href="features.html"' not in text
        assert 'href="advantage.html">Why CaveViewer</a>' in text or (
            'href="advantage.html" aria-current="page">Why CaveViewer</a>' in text
        )
        assert text.count('<details class="primary-nav__dropdown">') == 2
        assert ">Docs</summary>" in text
        assert ">Team &amp; Partners</summary>" in text
        assert "primary-nav__subdropdown" not in text
        for section in (
            "system-requirements",
            "installation",
            "before-you-change-anything",
            "troubleshooting-by-symptom",
        ):
            assert f'href="docs.html#{section}"' in nav
        assert nav.count('href="docs.html#') == 4
        assert 'assets/css/navigation-dropdown.css' in text
        assert 'href="media.html">Mapping Projects</a>' in text or (
            'href="media.html" aria-current="page">Mapping Projects</a>' in text
        )
        assert (
            text.index(">Why CaveViewer</a>")
            < text.index(">Docs</summary>")
            < text.index(">Team &amp; Partners</summary>")
            < text.index(">Team</a>")
            < text.index(">Mapping Projects</a>")
        )
        assert 'href="about.html">Team</a>' in text or (
            'href="about.html" aria-current="page">Team</a>' in text
        )
        assert text.index(">Team</a>") < text.index(">Sponsors</a>") < text.index(">Contact</a>")
        assert 'href="sponsors.html">Sponsors</a>' in text or (
            'href="sponsors.html" aria-current="page">Sponsors</a>' in text
        )
        assert 'href="contact.html">Contact</a>' in text or (
            'href="contact.html" aria-current="page">Contact</a>' in text
        )
        top_level_nav = re.sub(
            r'<div class="primary-nav__dropdown-menu">.*?</div>',
            "",
            nav,
            flags=re.DOTALL,
        )
        assert top_level_nav.count('aria-current="page"') <= 1
        assert 'class="header-download" href="docs.html#installation"' in text
        assert "member-" not in text

    about = (SITE_ROOT / "about.html").read_text(encoding="utf-8")
    assert 'href="about.html" aria-current="page">Team</a>' in about
    assert '<article class="about-person" data-reveal>' in about
    assert '<a class="about-person"' not in about
    assert "Co-Creator / Chief Technology Wizard" in about
    assert '<p class="about-person__affiliation">Zero Viz Co-Op</p>' in about
    assert ">Chief Technology Wizard<" not in about
    assert "K3rnalPanic" not in about

    docs = (SITE_ROOT / "docs.html").read_text(encoding="utf-8")
    assert '<summary aria-current="page">Docs</summary>' in docs
    assert '<article class="docs-article">' in docs
    assert "docs-toc" not in docs
    assert "System Requirements and Compatibility" in docs
    assert ">System RAM target<" in docs
    assert ">Import chunk size<" in docs
    assert "Rebuild cache" in docs
    assert docs.count('<figure class="docs-figure">') == 4
    assert "preferences-import-800.webp" in docs
    assert "preferences-import-1600.webp" in docs
    assert (
        '<img src="assets/images/features/preferences-import.png" '
        'width="2420" height="1634"' in docs
    )
    assert "preferences-streaming-800.webp" in docs
    assert "preferences-streaming-1600.webp" in docs
    assert (
        '<img src="assets/images/features/preferences-streaming.png" '
        'width="2420" height="1634"' in docs
    )
    assert "preferences-backup-800.webp" in docs
    assert "preferences-backup-1600.webp" in docs
    assert "help-troubleshooting-800.webp" in docs
    assert "help-troubleshooting-1600.webp" in docs
    assert 'id="getting-help"' in docs
    assert "Copy any error message you find" in docs
    assert '<a href="contact.html">contact support</a>' in docs
    assert (
        "Don’t worry about messing up your preferences—you can always save, "
        "import, and restore them in the app. Note that platform-specific settings, "
        "such as directory paths, will not be restored."
    ) in docs
    assert "docs-mobile-table--defaults" not in docs
    assert docs.count("<h2 ") == 9
    assert docs.index('id="system-requirements"') < docs.index('id="installation"')
    assert docs.index('id="installation"') < docs.index('id="before-you-change-anything"')
    assert docs.index('id="before-you-change-anything"') < docs.index('id="quick-recommendations"')
    assert docs.index('id="quick-recommendations"') < docs.index('id="import-settings"') < docs.index('id="streaming-settings"')
    assert docs.index('id="import-settings"') < docs.index("preferences-import-800.webp") < docs.index('id="streaming-settings"')
    assert docs.index('id="streaming-settings"') < docs.index("preferences-streaming-800.webp")
    assert docs.index('id="streaming-settings"') < docs.index('id="restore-the-defaults"') < docs.index('id="troubleshooting-by-symptom"')
    assert docs.index('id="restore-the-defaults"') < docs.index("preferences-backup-800.webp") < docs.index('id="troubleshooting-by-symptom"')
    assert docs.index('id="troubleshooting-by-symptom"') < docs.index('id="getting-help"') < docs.index("help-troubleshooting-800.webp")
    assert "Microsoft does not yet recognize its publisher" in docs
    assert "Apple does not yet recognize its developer" in docs
    assert "Linux does not display an equivalent publisher-verification warning" in docs

    dropdown_styles = (SITE_ROOT / "assets/css/navigation-dropdown.css").read_text(encoding="utf-8")
    assert ".primary-nav__dropdown-menu" in dropdown_styles
    assert "border-radius: 0;" in dropdown_styles
    assert "box-shadow: none;" in dropdown_styles

    global_styles = (SITE_ROOT / "assets/css/global.css").read_text(encoding="utf-8")
    assert "max-height: calc(100dvh - var(--header-h) - 8px);" in global_styles
    assert "overflow-y: auto;" in global_styles
    assert "width: 44px;" in global_styles
    assert ".header-download {\n        display: none;\n    }" in global_styles
    assert "height: 44px;" in global_styles

    assert "padding: 10px;" in dropdown_styles

    contact = (SITE_ROOT / "contact.html").read_text(encoding="utf-8")
    assert 'href="contact.html" aria-current="page">Contact</a>' in contact

    sponsors = (SITE_ROOT / "sponsors.html").read_text(encoding="utf-8")
    assert 'href="sponsors.html" aria-current="page">Sponsors</a>' in sponsors
    assert '<section class="sponsors-page" aria-labelledby="sponsors-page-title">' in sponsors
    assert '<div class="sponsors-page__grid">' in sponsors
    assert sponsors.count('<a class="sponsor-card ') == 5
    for sponsor, href, image, width, height in (
        (
            "KISS Rebreathers",
            "https://www.kissrebreathers.com/",
            "kiss-rebreathers-logo",
            "487",
            "82",
        ),
        ("XDEEP", "https://www.xdeep.eu/", "xdeep-logo", "95", "24"),
    ):
        assert f'href="{href}" target="_blank" rel="noopener noreferrer"' in sponsors
        assert f'<h2>{sponsor}</h2>' in sponsors
        assert f'assets/images/sponsors/{image}.webp' in sponsors
        assert f'assets/images/sponsors/{image}.png' in sponsors
        assert f'width="{width}" height="{height}"' in sponsors

    advantage = (SITE_ROOT / "advantage.html").read_text(encoding="utf-8")
    assert 'href="advantage.html" aria-current="page">Why CaveViewer</a>' in advantage
    assert ">View Ginormous Maps<" in advantage

    media = (SITE_ROOT / "media.html").read_text(encoding="utf-8")
    assert 'href="media.html" aria-current="page">Mapping Projects</a>' in media
    assert '<section class="media-page" aria-labelledby="media-page-title">' in media
    assert "Dives behind the data" not in media
    assert media.count('class="media-page__video-title"') == 2
    assert media.count('class="feature-section__visual feature-section__visual--video"') == 2
    for video_id, title in (
        (
            "ZytYB0jpe38",
            "Wes Skiles Peacock Springs State Park — 3-D Mapping Initiative",
        ),
        ("BSv9UILf6DI", "Devil's Eye — Mapping Update, February 2026"),
    ):
        assert f"https://www.youtube.com/watch?v={video_id}" in media
        assert f"https://www.youtube-nocookie.com/embed/{video_id}" in media
        assert f'title="{title}"' in media
    assert 'loading="lazy"' in media
    assert 'referrerpolicy="strict-origin-when-cross-origin"' in media


def test_sponsors_grid_uses_local_logo_fallbacks_and_accepts_future_cards() -> None:
    sponsors = (SITE_ROOT / "sponsors.html").read_text(encoding="utf-8")
    styles = (SITE_ROOT / "assets/css/sponsors.css").read_text(encoding="utf-8")

    assert 'assets/css/sponsors.css' in sponsors
    assert sponsors.count("<picture>") == 2
    assert sponsors.count('type="image/webp"') == 2
    assert sponsors.count('<a class="sponsor-card ') == 5
    assert sponsors.count('decoding="async"') == 5
    assert 'loading="eager" fetchpriority="high"' in sponsors
    assert (SITE_ROOT / "assets/images/sponsors/kiss-rebreathers-logo.png").is_file()
    assert (SITE_ROOT / "assets/images/sponsors/kiss-rebreathers-logo.webp").is_file()
    assert (SITE_ROOT / "assets/images/sponsors/xdeep-logo.png").is_file()
    assert (SITE_ROOT / "assets/images/sponsors/xdeep-logo.webp").is_file()
    for logo in (
        "seal-drysuits-logo.svg",
        "agisoft-logo.svg",
        "synergy-geomatics-logo.png",
    ):
        assert (SITE_ROOT / "assets/images/sponsors" / logo).is_file()
    assert "grid-template-columns: repeat(auto-fit, minmax(min(100%, 340px), 1fr));" in styles
    assert ".sponsor-card:focus-visible {\n    outline: 2px solid" in styles
    assert "@media (max-width: 620px)" in styles


def test_advantage_section_uses_the_real_preferences_and_capabilities() -> None:
    advantage = (SITE_ROOT / "advantage.html").read_text(encoding="utf-8")
    styles = (SITE_ROOT / "assets/css/features.css").read_text(encoding="utf-8")

    assert '<section class="advantages-page" id="advantage"' in advantage
    assert '<a href="advantage.html" aria-current="page">Why CaveViewer</a>' in advantage
    assert advantage.count("feature-section--advantage") == 5
    for text in (
        "View Ginormous Maps",
        "consumer-grade hardware",
        "Keep Moving as Maps Load",
        "Enjoy Free Maps",
        "Record &amp; Share Dives",
        "Pay Nothing",
        "CaveViewer and its standard maps are free.",
        "no accounts, subscriptions, ads, or trackers.",
        "GNU Affero General Public License v3.0 (AGPLv3)",
        "https://www.gnu.org/licenses/agpl-3.0.en.html",
        "rendering-engine-1600.webp",
        "map-library-1600.webp",
        "capture-recording-1600.webp",
        "advantage-streaming-1600.webp",
    ):
        assert text in advantage

    assert ".advantages-page {" in styles
    assert ".advantages-page__sections {" in styles
    assert ".feature-section--advantage {" in styles
    assert ".feature-section--freedom {" in styles
    assert "scroll-margin-top: calc(var(--header-h) + 24px);" in styles


def test_contact_page_preserves_the_current_form_submission_contract() -> None:
    contact = (SITE_ROOT / "contact.html").read_text(encoding="utf-8")
    styles = (SITE_ROOT / "assets/css/contact.css").read_text(encoding="utf-8")
    readme = (SITE_ROOT / "README.md").read_text(encoding="utf-8")

    assert '<form class="contact-form" action="https://formsubmit.co/azdeatherage@gmail.com" method="POST">' in contact
    assert '<input type="hidden" name="_subject" value="CaveViewer contact form">' in contact
    assert '<input type="hidden" name="_template" value="table">' in contact
    assert 'name="_honey" tabindex="-1" autocomplete="off"' in contact
    assert 'name="_captcha" value="false"' not in contact
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
    assert "default CAPTCHA remains enabled" in readme
    assert "`_captcha=false`" in readme
    assert 'no contact backend or database' in " ".join(readme.split())


def test_contact_layout_uses_content_safe_document_flow() -> None:
    styles = (SITE_ROOT / "assets/css/contact.css").read_text(encoding="utf-8")

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
    assert "min-height: calc(100svh - var(--site-endcap-h));" in styles


def test_about_team_captions_use_compact_spacing() -> None:
    styles = (SITE_ROOT / "assets/css/about.css").read_text(encoding="utf-8")

    assert ".about-person__caption {\n    flex: 1;\n    padding: 14px 3px 0;" in styles
    assert ".about-person__name-row { min-height: 0; }" in styles
    assert ".about-person__role {\n    margin: 8px 0 0;" in styles
    assert ".about-person__affiliation {\n    margin: 5px 0 0;" in styles


def test_about_team_photos_use_the_intended_treatment() -> None:
    about = (SITE_ROOT / "about.html").read_text(encoding="utf-8")
    styles = (SITE_ROOT / "assets/css/about.css").read_text(encoding="utf-8")

    assert about.count("--photo-position:") == 6
    assert 'alt="Brian Deatherage" style="--photo-position: 50% 0%;"' in about
    assert 'alt="Zsolt Szabo" style="--photo-position: 50% 40%;"' in about
    assert 'alt="Filipp R. Loginova" style="--photo-position: 50% 45%;"' in about
    assert 'alt="Magic Mr_V" style="--photo-position: 62% 50%;"' in about
    assert "magic-mr-v-cave-diver-640.webp" in about
    assert "magic-mr-v-cave-diver-960.webp" in about
    assert "Co-Creator / Chief Technology Wizard" in about
    assert about.index("<h2>Zsolt Szabo</h2>") < about.index("<h2>Magic Mr_V</h2>") < about.index("<h2>Filipp R. Loginova</h2>")
    assert "about-person__media--blank" not in about
    assert "about-person--text-only" not in about
    assert "object-position: var(--photo-position, 50% 20%);" in styles
    assert "about-person__media--blank" not in styles
    assert "about-person--text-only" not in styles
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
            target = SITE_ROOT / parsed.path if parsed.path else page
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


def test_about_omits_inline_sponsors_and_contact_content() -> None:
    about = (SITE_ROOT / "about.html").read_text(encoding="utf-8")

    for removed_content in (
        "about-sponsors",
        "about-contact",
        "Get in touch",
        "data-static-preview-form",
        "static-preview.js",
        "<form",
    ):
        assert removed_content not in about

    assert not (SITE_ROOT / "assets/js/static-preview.js").exists()


def test_website_documents_its_public_static_boundary() -> None:
    readme = (SITE_ROOT / "README.md").read_text(encoding="utf-8")
    normalized = " ".join(readme.split())

    assert "public, static website" in readme
    assert "no server application, persistent storage, or database" in normalized
    assert "exported static artifact" in normalized
    assert "www.caveviewer.com" in readme
    assert "noindex" in readme
    assert "docs/previous-site/" in readme


def test_image_delivery_uses_responsive_webp_and_reserves_layout_space() -> None:
    index = (SITE_ROOT / "index.html").read_text(encoding="utf-8")
    advantage = (SITE_ROOT / "advantage.html").read_text(encoding="utf-8")
    about = (SITE_ROOT / "about.html").read_text(encoding="utf-8")
    sponsors = (SITE_ROOT / "sponsors.html").read_text(encoding="utf-8")
    home_styles = (SITE_ROOT / "assets/css/home.css").read_text(encoding="utf-8")
    feature_styles = (SITE_ROOT / "assets/css/features.css").read_text(
        encoding="utf-8"
    )
    about_styles = (SITE_ROOT / "assets/css/about.css").read_text(
        encoding="utf-8"
    )
    readme = (SITE_ROOT / "README.md").read_text(encoding="utf-8")

    page_budgets = {
        "Home": (
            1_300_000,
            (
                "assets/images/ginnie1-faceted-survey.webp",
                "assets/images/software-hero-cave-strokes-full.webp",
            ),
        ),
        "Why CaveViewer": (
            450_000,
            (
                "assets/images/features/rendering-engine-1600.webp",
                "assets/images/features/map-library-1600.webp",
                "assets/images/features/capture-recording-1600.webp",
                "assets/images/features/advantage-streaming-1600.webp",
            ),
        ),
        "Documentation": (
            130_000,
            (
                "assets/images/features/preferences-import-1600.webp",
                "assets/images/features/preferences-streaming-1600.webp",
                "assets/images/features/preferences-backup-1600.webp",
                "assets/images/features/help-troubleshooting-1600.webp",
            ),
        ),
        "Team": (
            800_000,
            (
                "storage/uploads/2026/08/e02af4158100878810221f4cc8db33f52026e293-960.webp",
                "storage/uploads/2026/08/46afd31b727aa673872050329b90d75db21bd831-960.webp",
                "storage/uploads/2026/08/0dfffc22c2177fa30ec1e13d531c71b8eb71100d-850.webp",
                "storage/uploads/2026/08/magic-mr-v-cave-diver-960.webp",
                "storage/uploads/2026/08/32c8839d88fe923a90c84a1206c967245f98ef57-960.webp",
                "storage/uploads/2026/08/4278cd57d55958ba1979cfc0ef999019c70455de-960.webp",
            ),
        ),
        "Sponsors": (
            50_000,
            (
                "assets/images/sponsors/kiss-rebreathers-logo.webp",
                "assets/images/sponsors/xdeep-logo.webp",
            ),
        ),
    }

    for route, (budget, assets) in page_budgets.items():
        sizes = [(SITE_ROOT / asset).stat().st_size for asset in assets]
        assert sum(sizes) <= budget, f"{route} preferred image budget exceeded"

    assert "## Image delivery budget" in readme
    assert "picture`/`srcset`" in readme
    assert "`image-set`" in readme
    assert "Six responsive portrait WebP images" in readme
    assert "Import, Streaming, Backup, and Troubleshooting WebP images" in readme
    assert "KISS Rebreathers and XDEEP logo WebP images" in readme
    assert "two privacy-enhanced YouTube embeds" in readme

    for asset in (
        "assets/images/ginnie1.jpg",
        "assets/images/ginnie1.webp",
        "assets/images/ginnie1-faceted-survey.png",
        "assets/images/ginnie1-faceted-survey.webp",
    ):
        assert (SITE_ROOT / asset).is_file()
    assert "ginnie1-faceted-survey.png" in home_styles
    assert "ginnie1-faceted-survey.webp" in home_styles
    assert "ginnie1.jpg" not in home_styles
    assert "ginnie1.webp" not in home_styles
    assert "software-hero-cave-strokes-full.webp" in home_styles
    assert "image-set(" in home_styles
    assert (SITE_ROOT / "assets/icons/caveviewer-logo.svg").is_file()
    assert (SITE_ROOT / "assets/icons/caveviewer-logo-mesh.svg").is_file()
    assert (SITE_ROOT / "assets/icons/caveviewer-logo-mesh-expanded.svg").is_file()
    assert (SITE_ROOT / "assets/icons/caveviewer-logo-faceted-rim.svg").is_file()
    assert (SITE_ROOT / "assets/icons/caveviewer-logo-faceted-rim-beam.svg").is_file()
    assert 'src="assets/icons/caveviewer-logo-faceted-rim-beam.svg" width="64" height="32"' in index

    assert advantage.count("<picture>") == 4
    for source in (
        "rendering-engine-800.webp",
        "rendering-engine-1600.webp",
        "map-library-800.webp",
        "map-library-1600.webp",
        "capture-recording-800.webp",
        "capture-recording-1600.webp",
        "advantage-streaming-800.webp",
        "advantage-streaming-1600.webp",
    ):
        assert source in advantage
    assert 'width="2558" height="1556" loading="eager" fetchpriority="high"' in advantage
    assert (
        '<img src="assets/images/features/advantage-streaming.png" '
        'width="2420" height="1634"' in advantage
    )
    assert (
        '<img src="assets/images/features/map-library.png" '
        'width="2420" height="1634"' in advantage
    )
    assert advantage.count('loading="lazy" decoding="async"') == 3
    assert ".feature-section__visual picture" in feature_styles

    assert about.count("<picture>") == 6
    assert about.count('sizes="(max-width: 900px) 50vw, 476px"') == 6
    assert about.count('loading="lazy" decoding="async"') == 4
    assert 'loading="eager" fetchpriority="high"' in about
    assert about.count(' width="') >= 6
    assert ".about-person__media picture" in about_styles

    assert sponsors.count("<picture>") == 2
    assert 'alt="KISS Rebreathers logo"' in sponsors
    assert 'alt="XDEEP logo"' in sponsors


def test_wide_home_hero_has_explicit_art_direction_and_aligned_gutters() -> None:
    index = (SITE_ROOT / "index.html").read_text(encoding="utf-8")
    home_styles = (SITE_ROOT / "assets/css/home.css").read_text(encoding="utf-8")
    global_styles = (SITE_ROOT / "assets/css/global.css").read_text(
        encoding="utf-8"
    )

    assert '<h1 id="hero-title">Explore what<br><span>lies beneath</span></h1>' in index
    assert "@media (min-width: 1600px) and (min-height: 900px)" in home_styles
    assert ".page-home .hero__content { align-items: center; }" in home_styles
    assert ".hero__media {\n    --hero-photo:" in home_styles
    assert "--hero-survey-mesh: url('../images/ginnie1-faceted-survey-mesh.svg');" in home_styles
    assert "100% 100%, 100% 100%, 100% 100%, 100% 100%, cover, cover;" in home_styles
    assert "background-position: center, center, center, center, center, center;" in home_styles
    assert "60% center, 60% center;" in home_styles
    assert "--hero-survey-mesh: none;" in home_styles
    assert (SITE_ROOT / "assets/images/ginnie1-faceted-survey-mesh.svg").is_file()
    assert "clamp(176px, 11vw, 320px)" in home_styles
    assert "max(32px, calc((100% - var(--max)) / 2))" in home_styles
    assert "auto 100%" not in home_styles
    assert "right center" not in home_styles
    assert "width: min(calc(100% - 64px), var(--max));" in global_styles
