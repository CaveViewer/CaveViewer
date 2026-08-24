# CaveViewer website refresh

This preliminary work definition proposes a smaller, more polished CaveViewer
website for local repository review. It uses the visual direction of the
Lognova prototype while reducing its information architecture to the decisions
a prospective user actually needs to make. Nothing in this plan authorizes a
GitHub Pages deployment, DNS change, or replacement of the live site.

The review implementation will remain static-site compatible and will be
previewed locally. This plan remains in ignored root `.work/` unless the user
later asks to share or retain it in `docs/development/work/`.

## Audited baseline

- The live site and `docs/index.html` are a single, very long page with nine
  navigation destinations and detailed material about formats, controls,
  recording, preferences, media, and installation internals.
- The Lognova prototype provides a stronger visual hierarchy, responsive
  navigation, more polished product imagery, and clearer platform cards, but
  spreads the product across many pages and repeats secondary material in its
  header, hero rail, body, and footer.
- The Lognova About page incorrectly shows a human photo and the name “Vitaly.”
  The replacement uses the approved hacker-cat portrait and identifies this
  contributor only as **Magic Mr_V**, **Chief Technology Wizard**.
- The repository contact form posts to FormSubmit and has a honeypot. The
  Lognova form adds a honeypot and CSRF token, but neither visible markup alone
  demonstrates server-verified human detection or rate limiting.
- GitHub release `v1.0.92` is a published preview release. Its installable
  assets are `CaveViewer-1.0.92-windows.exe`,
  `CaveViewer-1.0.92-macos-arm64.dmg`,
  `CaveViewer-1.0.92-macos-x86_64.dmg`, and
  `CaveViewer-1.0.92-x86_64.AppImage`.
- GitHub Pages uploads only `docs/` through `.github/workflows/pages.yml` and
  deploys only from `main`. The candidate is therefore isolated in root
  `website-preview/`; it contains static assets only and is outside the
  production Pages artifact.

## Approved content clarification

The approved About-card wording is **Magic Mr_V** with the exact title
**Co-Creator / Chief Technology Wizard**. This supersedes earlier references
to **Chief Technology Wizard** alone in the baseline and master table.
The card also includes **Zero Viz Co-Op** beneath the title, using
the existing affiliation treatment used for the Bottomline Projects label.

### Contact-form implementation decision

The review site will restore contact as a dedicated `contact.html` page, linked
as **Contact Us** from every canonical header. It will carry forward the
current site form's existing FormSubmit action, hidden `_subject` and
`_template` fields, `_honey` honeypot, and required name, email, and message
fields. The page will use the preview's dark cave visual language and stay
inside the unpublished root `website-preview/` directory; it introduces no
database, endpoint, secret, Pages configuration, or deployment change.

This is a like-for-like restoration of the current public form—not the
server-verified human-validation solution described in item 16. That security
upgrade remains deferred until its external configuration and publication
boundary are separately approved.

The restored FormSubmit surface is recorded as complete in task 4. The
stronger server-verified protection remains pending in task 16.

Brian Deatherage's `K3rnalPanic` handle is intentionally omitted from the
concise About card.

The canonical header uses the same text-only display treatment for
`CaveViewer` and `Our Team`: one font family, size, weight, letter spacing,
and no underline decoration. `Our Team` remains the active destination on the
About page.

The concise About page ends after the team cards. Sponsor material and the
“Get in touch” form are intentionally absent from this local review, so the
contact-delivery work in task 16 is deferred until a future approved contact
surface exists.

Team-card captions use a compact name → role → affiliation stack. The old
fixed name-row height is removed; desktop spacing is 14px above the name, 8px
to the role, and 5px to the affiliation, with proportional compact spacing on
mobile.

Desktop team portraits use a 4:3 frame rather than a shallow landscape crop.
Each image supplies a static, top-biased `--photo-position` focal point, so
faces remain in view without face detection, client-side image processing, or
additional services.

The visually verified desktop focal points are Brian `50% 0%`, Zsolt
`50% 40%`, and Filipp `50% 45%`; the remaining portraits retain their existing
focal points.

## Master plan

Rows are ordered by implementation sequence. Work is active on
`feature/website-refresh`; status and verification evidence belong in the
applicable row so the table remains the authoritative execution record.

<style>
table th,
table td {
  vertical-align: top;
}
</style>

| ID | Description | Problem | Current implementation | Desired solution | Task details | Branch | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 0 | Establish the Lognova design as a faithful static review baseline. | The first custom reinterpretation departed too far from the approved Lognova design, making its hierarchy and visual decisions unsuitable as a refinement baseline. The public prototype is PHP-rendered and includes query routes, form state, and API/database-dependent cave-map behavior that cannot be copied directly into a database-free static review site. | The rejected custom preview is preserved temporarily outside the repository. The public Lognova prototype exposes 22 rendered pages plus shared CSS, JavaScript, images, maps, uploads, and member/cave routes. GitHub Pages publishes only `docs/`, not root `website-preview/`. | `website-preview/` faithfully preserves the rendered Lognova pages and local design assets while using ordinary `.html` routes. No PHP, API call, server application, persistent storage, or database is required. The form and dynamic cave routes are inert in local review. Every local page and asset resolves over the local server, and nothing is deployed. | Mirror the public `/caveviewer/` tree and its page requisites.<br>Normalize `.php` and query-string routes to static `.html` files and repair all internal navigation.<br>Disable CSRF/form submission, route APIs, and location-record links that require server state; keep the visual layouts intact.<br>Add a local-only form response and static-review README.<br>Add automated contracts for page inventory, link integrity, static-only file types, disabled dynamic routes, and Pages isolation.<br>Serve and crawl every HTML page locally before handoff. | `feature/website-refresh` | Complete — accepted as the refinement baseline |
| 1 | Define a concise information architecture and verified content baseline. | Both existing designs ask visitors to process too many navigation choices, feature explanations, and implementation details before finding the product or installer. Incorrect contributor information also undermines trust. | The local review now has a concise header, focused home/download path, Features, Our Team, and Contact Us. Obsolete Preferences, Recording, Controls, and Formats destinations remain absent; identity and contact content have been corrected for review. | A visitor can understand what CaveViewer does, choose an installer, explore the three core feature areas, identify the team, and contact it without documentation-like navigation density. | Keep the current concise content structure as the baseline for remaining visual and page-content work.<br>Retain `Magic Mr_V`, the approved portrait, `Co-Creator / Chief Technology Wizard`, and `Zero Viz Co-Op` exactly as reviewed.<br>Do not restore removed destinations or profile pages without a new approved work item. | `feature/website-refresh` | Complete — accepted for local review |
| 2 | Build a focused visual system from the approved Lognova direction and CaveViewer app. | The preview inherited a blue-centric accent treatment that conflicted with CaveViewer's established dark charcoal and amber application UI. | The four canonical pages now use the reviewed near-black/charcoal presentation, amber primary actions, unified sharp-edged menus, shared cave-profile logo, compact footer, and consistent header. | The accepted visual hierarchy remains consistent across Home, Features, Team, and Contact without restoring the discarded blue-centric or mixed menu treatments. | Preserve the accepted app-derived tokens and page treatments.<br>Keep imagery naturally colored while using amber selectively for actions and emphasis.<br>Protect the shared header, footer, menus, and responsive composition with focused contracts where practical. | `feature/website-refresh` | Complete — visually reviewed and accepted |
| 3 | Offer direct, operating-system-aware preview downloads. | A generic release-page link makes users identify the correct artifact themselves, while automatic OS detection can misclassify architecture or hide alternatives. | Home provides direct `1.0.92` platform artifacts, uses browser hints to select the likely platform, preserves the Windows installer as the primary/default action, and exposes all alternatives in the platform chooser. The redundant `install.html` route has been removed. | The Home download area remains the single download experience. Its approved Windows primary action is preserved, likely-platform selection enhances it when JavaScript is available, and every supported platform remains available without sending users to a repository page. | Keep the exact `v1.0.92` artifact URLs and existing Windows primary action unchanged.<br>Keep client-side platform hints limited to recommendation and retain every platform in the accessible chooser.<br>Keep both Apple Silicon and Intel DMGs with concise architecture guidance.<br>Route every shared-header Download action to `index.html#get-caveviewer` (or the local fragment on Home).<br>Test platform selection deterministically without introducing another Download page. | `feature/website-refresh` | Complete — dedicated Download route removed; Home chooser retained |
| 4 | Correct and simplify Team and Contact content. | The prototype published an incorrect personal identity and title and devoted excessive space to biographies and peripheral material. | Team now uses the approved people, portraits, focal points, concise roles, and affiliations. Magic Mr_V uses the approved hacker-cat portrait, exact name, **Co-Creator / Chief Technology Wizard** title, and **Zero Viz Co-Op** affiliation. Contact is a dedicated three-field page. Sponsors, marketing biography copy, profile routes, and profile links are absent. | The reviewed concise Team and Contact content remains accurate and no removed identity, biography, sponsor, or profile content returns. | Preserve the approved identity strings and portrait.<br>Keep team cards presentational and concise.<br>Keep Contact dedicated and visually consistent.<br>Retain focused content contracts for removed material and approved identity. | `feature/website-refresh` | Complete — visually reviewed and contract-covered |
| 5 | Make content visible without JavaScript. | `[data-reveal]` applies `opacity: 0` before JavaScript runs, so a blocked script or initialization failure can hide the Home hero, feature sections, team cards, and Contact form. | `global.css` now keeps every reveal target visible until `html.reveal-enhanced` is added after observer setup; `app.js` preserves both IntersectionObserver reveals and the no-observer `.is-visible` fallback. | All meaningful content is visible in the HTML/CSS baseline. Reveal animation is enabled only after successful JavaScript initialization and never blocks access to content. | Add an initialization class only after the reveal system is ready.<br>Scope hidden reveal states to that enhancement class.<br>Retain the observer and no-observer behavior.<br>Add a no-JavaScript contract or browser check proving all primary content remains visible. | `feature/website-refresh` | In progress — implementation committed as `7a8f7b3` and pushed to `origin/feature/website-refresh`; no PR is open. 13 focused website tests, JavaScript syntax, observer/no-observer simulation, the full 1,869-test suite, and `git diff --check` passed. |
| 6 | Maintain an unpublished review and regression workflow. | The redesign must remain inspectable without accidentally replacing the live site, and future edits could restore deleted routes or break local references. | `website-preview/` is outside the Pages artifact and contains no `CNAME`. Its README provides a local server command. `tests/unit/test_website_preview.py` verifies the four-page inventory, static-only boundary, canonical navigation, selected content, and all local references. It currently passes 13 tests. `tests/browser/` now provides a locked Chromium Playwright smoke suite that reuses `127.0.0.1:4173` rather than starting another server; it covers the four routes at three widths, keyboard menu use, a 200%-equivalent viewport, reduced motion, and no JavaScript. | Local review remains isolated from deployment; focused contracts prevent route and link regressions; representative browser checks cover responsive, keyboard, zoom, reduced-motion, and no-JavaScript behavior before publication. | Preserve Pages isolation and the local-preview instructions.<br>Maintain the focused static contracts.<br>Add browser-level checks for representative widths, keyboard use, 200% zoom, reduced motion, and no JavaScript when implementation reaches publication readiness.<br>Keep publication a separate explicitly approved task. | `feature/website-refresh` | In progress — implementation committed as `7fafd91` and pushed to `origin/feature/website-refresh`; no PR is open. Isolation, static contracts, and 7 Chromium browser checks are complete; this host uses Playwright’s Ubuntu 24.04 fallback build. |
| 7 | Unify navigation and remove member profile routes. | The former About page was a functional dead end and individual member pages duplicated navigation and stale biographies. | All four pages share the cave-profile Home mark, Features, Team, Contact, and Download navigation. Current pages use `aria-current`; Download targets the Home chooser. Team cards are noninteractive articles and no member-profile route remains. | Every page retains the same concise, responsive header and a working route back Home; no member profile or link returns. | Preserve the canonical header and current-page semantics.<br>Keep member cards noninteractive.<br>Keep route/link contracts covering all four pages and the absence of profile routes. | `feature/website-refresh` | Complete — visually reviewed; route contract passes |
| 8 | Remove unreachable and contradictory routes and assets. | Unlinked legacy pages remained publicly addressable, contradicted the approved OBJ/MTL format wording, contained a broken installation-guide fragment, and retained unused cave-route and video-modal code. | The candidate now contains only `index.html`, `features.html`, `about.html`, and `contact.html`. `documentation.html`, `install.html`, `datasets.html`, `media.html`, `research.html`, their exclusive assets, obsolete cave-route assets, and dormant YouTube modal are removed. Header Download links target Home, and the approved Windows primary installer is unchanged. | Only intentional canonical routes and their required assets exist; all local paths and fragments resolve; legacy GLB claims and deleted route names cannot return unnoticed. | Keep the four-page inventory assertion.<br>Keep the local path/fragment resolver test.<br>Retain only assets referenced by the canonical pages.<br>Preserve the Home Windows primary action. | `feature/website-refresh` | Complete — 13 focused website tests pass |
| 9 | Correct page headings and keyboard navigation. | Features and Team lack page-level `<h1>` elements; there is no skip link; and the mobile menu does not move focus into the opened menu or return it to the toggle. Color carries too much of the desktop current/focus distinction. | Home and Contact have `<h1>` elements. Features and Team begin with `<h2>`. The menu supports toggle, link-close, Escape, and breakpoint close, but not explicit focus placement/restoration. Focus styling exists but needs non-color verification. | Every page has a coherent heading hierarchy and skip path. Mobile navigation has predictable focus entry, Escape behavior, and focus restoration, with a visible non-color focus/current indicator. | Add a page-level visible or screen-reader-only `<h1>` to Features and Team.<br>Add a shared skip link targeting main content.<br>Assign stable main targets.<br>Move focus on menu open, restore it on close, and verify keyboard order without creating a focus trap that harms simple navigation.<br>Add focused semantic and interaction tests. | `feature/website-refresh` | Complete — `6b5b842` adds page-level headings, shared skip targets, managed mobile focus, and non-color navigation cues. 14 focused static tests, 9 Chromium checks on the existing local preview, JavaScript syntax checks, the full suite (1,870 passed; one PyGLM deprecation), and `git diff --check` pass. |
| 10 | Make Contact responsive without clipping content. | Desktop Contact forces root/body/main overflow hidden so short wide viewports, browser zoom, large text, or translated content can become unreachable. | The accepted normal-height layout keeps the 36px footer visible without scrolling, but desktop safety still depends on fixed viewport rows and clipped overflow; only widths at or below 620px restore natural flow. | Contact shows no unnecessary scrollbar at ordinary sizes, retains its footer, and permits vertical scrolling whenever content cannot fit at short height, 200% zoom, large text, or mobile widths. | Replace unconditional desktop clipping with a content-safe min-height/layout rule or height media query.<br>Preserve the accepted normal-height composition.<br>Verify representative short desktop, 200% zoom, large-text, and mobile cases.<br>Add the lowest practical regression coverage. | `feature/website-refresh` | Complete — `0a73294` preserves the normal desktop composition while letting short, zoomed, large-text, and mobile pages use document flow. 15 focused static tests, 11 Chromium checks on the existing preview, JavaScript syntax checks, the full suite (1,871 passed; one PyGLM deprecation), and `git diff --check` pass. |
| 11 | Optimize image delivery and prevent layout shift. | Several essential images are oversized: Rendering is about 7.5 MB, Filipp's portrait 4.0 MB, Magic Mr_V 2.0 MB, and Capture 1.5 MB. HTML images omit intrinsic dimensions and loading hints. | Original PNG/JPEG assets are served directly. The browser must discover their dimensions after fetch, and below-fold images load eagerly. | Images retain acceptable visual quality at their rendered sizes while meeting a documented page-weight budget; markup supplies dimensions or aspect ratio and appropriate eager/lazy loading so layout remains stable. | Determine maximum rendered dimensions and generate efficient responsive assets.<br>Use suitable WebP/AVIF or optimized PNG/JPEG fallbacks as supported by the static design.<br>Add width/height or equivalent intrinsic sizing.<br>Load the hero/first meaningful image eagerly and below-fold images lazily.<br>Record before/after weights and add a budget contract. | `feature/website-refresh` | Complete — `422f70d` adds responsive WebP candidates with source fallbacks, intrinsic dimensions, and appropriate eager/lazy loading. Preferred high-resolution route budgets are Home 1.21 MB (≤1.30 MB), Features 0.33 MB (≤0.40 MB), and Team 0.76 MB (≤0.80 MB). 16 focused tests, 12 Chromium checks on the existing preview, JavaScript syntax checks, full suite (1,872 passed; one PyGLM deprecation), and `git diff --check` passed. |
| 11A | Add wide/tall Home-hero art direction. | At maximized or high-aspect desktop sizes, the Home hero keeps a fixed-width, top-aligned copy block while `ginnie1.jpg` uses `background-size: cover`. Its crop changes with the viewport, and the header (1540px) and hero shell (1480px) use different caps, producing the unbalanced wide-screen composition reported in review. | Home has no large-desktop or aspect-ratio art direction; `hero__content` is vertically `flex-start`, the title has an explicit line break, and the background is centered `cover`. | Normal and mobile composition remains intact, while wide/tall desktop has vertically balanced readable copy, aligned header/hero gutters, and a stable cave focal area using a suitable wide art treatment. | After item 11 provides optimized assets, add an explicit wide/tall desktop breakpoint.<br>Use height-fit/right-positioned media or a dedicated 16:9/21:9 crop rather than unrestricted `cover` behavior.<br>Align the header container with `--max`.<br>Retain or remove the forced heading break only after visual review.<br>Add deterministic browser geometry or screenshot checks at 1920×1080, 2560×1440, and 2560×1080. | `feature/website-refresh` | Complete — `53dfb95` keeps the reviewed two-line title, vertically centers the copy on tall desktop, uses a 120%-height/70%-focal wide media treatment, and aligns header/hero gutters with `--max`. Deterministic geometry checks pass at 1920×1080, 2560×1440, and 2560×1080. 17 focused tests, 15 Chromium checks on the existing preview, JavaScript syntax checks, full suite (1,873 passed; one PyGLM deprecation), and `git diff --check` passed. |
| 12 | Centralize preview release download data. | Version `1.0.92`, preview wording, and artifact URLs are duplicated between Home HTML and `platform-download.js`, allowing partial edits and stale downloads. | Direct links work and platform selection is functional, but the same release facts appear in multiple HTML elements, no-script content, and JavaScript configuration. | One maintained release-data source produces or initializes all visible version/channel/platform details while retaining direct official artifacts, the no-script alternatives, and the approved Windows primary action. | Choose a static-compatible single source or generation step.<br>Populate platform selection and visible metadata from it without requiring a database or runtime service.<br>Preserve useful no-JavaScript links.<br>Add a contract that every artifact/version reference agrees. | `feature/website-refresh` | Pending |
| 13 | Honor reduced motion and remove false Team-card affordances. | Reveal, Home background, Team scan, hover scaling, and color transitions continue under reduced-motion preferences. Team cards visually react like controls despite having no action. | Reduced-motion CSS covers only part of navigation behavior. Team card hover/focus rules animate the image and scan line even though cards are presentational articles. | Reduced-motion preference disables all nonessential motion, and noninteractive Team cards no longer imply clickability through control-like hover/focus animation. | Inventory all transitions, transforms, scans, and background motion.<br>Extend the reduced-motion override across the canonical pages.<br>Remove interactive-only Team-card hover/focus effects while keeping legible static portraits.<br>Add source or browser-level regression checks. | `feature/website-refresh` | Pending |
| 14 | Raise essential small text to a legible baseline. | The styles contain many 8–11px declarations; download metadata, affiliations, notes, labels, and footer text can be difficult to read on high-density and mobile displays. | Essential secondary text is often rendered below common consumer-site readability baselines to keep layouts compact. | User-facing text remains compact but readable across supported widths, zoom, and high-density displays; decorative microcopy does not carry essential meaning. | Classify essential versus decorative small text.<br>Raise essential labels and metadata to an accessible responsive minimum while preserving hierarchy.<br>Reflow components rather than clipping text.<br>Review at mobile width and 200% zoom. | `feature/website-refresh` | Pending |
| 15 | Consolidate shared CSS and enforce current contracts. | `global.css` contains multiple generations of header, navigation, and footer rules that override earlier declarations, making behavior difficult to predict and easy to regress. | The stale cave-page/navigation test expectations were corrected and the focused suite passes, but layered legacy declarations remain in the shared stylesheet. | Each active shared component has one understandable rule set, deleted designs have no residual rules, and focused tests describe the current four-page site. | Map active selectors across the four pages.<br>Remove superseded declarations in small reviewable passes.<br>Preserve the accepted visuals at representative widths.<br>Run the 12 website contracts, JavaScript syntax checks, local reference validation, and `git diff --check` after each consolidation. | `feature/website-refresh` | In progress — test contract corrected; CSS consolidation pending |
| 16 | Add layered, server-verified contact-form abuse protection. | A client-only honeypot can reduce basic spam but cannot reliably determine that a submission came from a human. Static GitHub Pages cannot safely hold a CAPTCHA secret or enforce server-side rate limits. | The repo form submits to FormSubmit with a hidden `_honey` field. The Lognova form includes a honeypot and CSRF value, but the inspected page does not expose evidence of a server-verified challenge or rate limiting. | The form retains a low-friction human check and rejects automated/replayed submissions through server-side token verification, validation, rate limiting, and a honeypot. No secret is committed to the repository or exposed in browser code. The local review remains usable without sending real mail. | Use Cloudflare Turnstile in managed/non-interactive mode, or an equivalently privacy-conscious service approved before implementation; the public site key may be client-side, but the secret must live only in protected external configuration.<br>Post through a minimal serverless endpoint that verifies the single-use token with the provider before forwarding mail; apply method/origin checks, strict field length/type validation, honeypot rejection, request-size limits, IP/token rate limits, generic responses, and safe logging that excludes message bodies and secrets.<br>Document the static-site/backend boundary and required external configuration without putting credentials in GitHub Pages or the repository.<br>Provide a local review mode that exercises validation and success/error UI without sending email or requiring production keys.<br>Add unit/contract tests for missing, invalid, expired/replayed, and successful human tokens; honeypot hits; rate limiting; malformed input; and provider/network failure.<br>Do not deploy the endpoint, provision production secrets, or activate delivery until separately authorized. | `feature/website-refresh` plus `External settings — deferred until publication approval` | Pending |

## Decisions required before implementation

1. Approve Cloudflare Turnstile plus a minimal stateless verification endpoint,
   or select an existing form provider that offers equivalent server-verified
   human checks and rate limiting. GitHub Pages alone cannot securely perform
   this verification. The endpoint must forward messages without persistence;
   no CaveViewer database is permitted.
2. Confirm whether the Lognova credit should remain in the footer and which
   Lognova visual/assets are approved for reuse in this repository.
3. Confirm whether version `1.0.92` should remain intentionally pinned for this
   review or later be generated from release metadata before publication.

## Proposed local validation

- Focused HTML/content/download-link and contact-security contract tests.
- Browser review at mobile, tablet, laptop, and wide desktop dimensions.
- Keyboard-only, 200% zoom, reduced-motion, and no-JavaScript checks.
- Automated accessibility scan where repository tooling can support it without
  introducing an unjustified production dependency.
- Broken-link and official GitHub asset verification for `v1.0.92`.
- Page-weight, image-size, and third-party-request audit.
- `git diff --check` and final review confirming no deployment, DNS, Pages,
  secret, or production-form changes.
