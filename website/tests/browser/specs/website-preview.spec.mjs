import { expect, test } from "@playwright/test";

const canonicalPages = [
    { name: "Home", path: "index.html", content: "Explore what" },
    { name: "Why CaveViewer", path: "advantage.html", content: "View Ginormous Maps" },
    { name: "Docs", path: "docs.html", content: "System Requirements and Compatibility" },
    { name: "Projects", path: "media.html", content: "Wes Skiles Peacock Springs", waitUntil: "domcontentloaded" },
    { name: "Team", path: "about.html", content: "Magic Mr_V" },
    { name: "Sponsors", path: "sponsors.html", content: "KISS Rebreathers" },
    { name: "Contact", path: "contact.html", content: "Contact Us" },
];

const reviewViewports = [
    { name: "desktop", width: 1440, height: 900 },
    { name: "tablet", width: 768, height: 1024 },
    { name: "mobile", width: 390, height: 844 },
];

const wideHeroViewports = [
    { name: "1920 × 1080", width: 1920, height: 1080 },
    { name: "2560 × 1440", width: 2560, height: 1440 },
    { name: "2560 × 1080", width: 2560, height: 1080 },
    { name: "2625 × 1187", width: 2625, height: 1187 },
];

async function expectNoHorizontalOverflow(page) {
    const overflow = await page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );

    expect(overflow).toBeLessThanOrEqual(1);
}

async function expectFontSizeAtLeast(locator, minimumPixels = 12) {
    const sizes = await locator.evaluateAll(elements => elements
        .filter(element => element.getClientRects().length > 0)
        .map(element => Number.parseFloat(getComputedStyle(element).fontSize)));

    expect(sizes).not.toHaveLength(0);
    for (const size of sizes) {
        expect(size).toBeGreaterThanOrEqual(minimumPixels);
    }
}

async function expectContactTargetsReachable(page) {
    for (const selector of ["#cf-message", ".contact-form__submit", ".site-endcap"]) {
        const target = page.locator(selector);

        await target.scrollIntoViewIfNeeded();
        await expect(target).toBeInViewport();
    }
}

test.describe("canonical website-preview routes", () => {
    for (const viewport of reviewViewports) {
        test(`${viewport.name} renders every canonical route without horizontal overflow`, async ({ page }) => {
            await page.setViewportSize(viewport);

            for (const route of canonicalPages) {
                await page.goto(route.path, { waitUntil: route.waitUntil ?? "networkidle" });
                await expect(page.locator("main")).toBeVisible();
                if (route.content) {
                    await expect(page.locator("main")).toContainText(route.content);
                }
                await expectNoHorizontalOverflow(page);
            }
        });
    }
});

test("the skip link moves keyboard focus to main content", async ({ page }) => {
    await page.goto("advantage.html", { waitUntil: "networkidle" });

    const skipLink = page.getByRole("link", { name: "Skip to main content" });
    const main = page.locator("#main-content");

    await page.keyboard.press("Tab");
    await expect(skipLink).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(main).toBeFocused();
});

test("navigation current and focus states have non-color indicators", async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto("advantage.html", { waitUntil: "networkidle" });

    const navigation = page.getByRole("navigation", { name: "Primary navigation" });
    const currentLink = navigation.getByRole("link", { name: "Why CaveViewer" });
    const focusedControl = navigation.getByText("Docs", { exact: true });

    await page.keyboard.press("Tab");
    await page.keyboard.press("Tab");
    await page.keyboard.press("Tab");
    await expect(currentLink).toBeFocused();
    await expect(currentLink).toHaveCSS("text-decoration-line", "underline");

    await page.keyboard.press("Tab");
    await expect(focusedControl).toBeFocused();
    await expect(focusedControl).toHaveCSS("outline-style", "solid");
});

test("desktop dropdown chevrons align optically with their labels", async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto("advantage.html", { waitUntil: "networkidle" });

    const offsets = await page.locator(".primary-nav__dropdown > summary").evaluateAll(
        summaries => summaries.map(summary => getComputedStyle(summary, "::after").top),
    );

    expect(offsets).toEqual(["-1px", "-1px"]);
});

test("the mobile navigation is keyboard-operable", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("index.html", { waitUntil: "networkidle" });

    const menuToggle = page.locator("[data-menu-toggle]");
    const headerDownload = page.locator(".header-download");
    const navigation = page.getByRole("navigation", { name: "Primary navigation" });

    await expect(headerDownload).toBeHidden();
    expect(await menuToggle.evaluate(element => {
        const bounds = element.getBoundingClientRect();
        return { height: bounds.height, width: bounds.width };
    })).toEqual({ height: 44, width: 44 });

    await expect(menuToggle).toHaveAttribute("aria-label", "Open navigation");
    await menuToggle.focus();
    await page.keyboard.press("Enter");
    await expect(menuToggle).toHaveAttribute("aria-expanded", "true");
    await expect(menuToggle).toHaveAttribute("aria-label", "Close navigation");
    await expect(navigation).toHaveClass(/is-open/);
    await expect(navigation.getByRole("link", { name: "Why CaveViewer" })).toBeFocused();

    const labelInsets = await navigation.evaluate(element => {
        const textLeft = target => {
            const textNode = [...target.childNodes].find(node => node.nodeType === Node.TEXT_NODE);
            const range = document.createRange();
            range.selectNodeContents(textNode);
            return range.getBoundingClientRect().left;
        };
        const directLink = element.querySelector(":scope > a");
        const summaries = [...element.querySelectorAll(":scope > details > summary")];

        return [textLeft(directLink), ...summaries.map(textLeft)];
    });
    expect(labelInsets[1]).toBeCloseTo(labelInsets[0], 1);
    expect(labelInsets[2]).toBeCloseTo(labelInsets[0], 1);

    await page.keyboard.press("Tab");
    await expect(navigation.getByText("Docs", { exact: true })).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(navigation.locator(".primary-nav__dropdown").first()).toHaveAttribute("open", "");
    for (const label of [
        "System Requirements",
        "Installation",
        "Performance Tuning",
        "Troubleshooting",
    ]) {
        await page.keyboard.press("Tab");
        await expect(navigation.getByRole("link", { name: label, exact: true })).toBeFocused();
    }
    await page.keyboard.press("Tab");
    await expect(navigation.getByText("Team & Partners", { exact: true })).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(navigation.locator(".primary-nav__dropdown").nth(1)).toHaveAttribute("open", "");
    await page.keyboard.press("Tab");
    await expect(navigation.getByRole("link", { name: "Team" })).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(navigation.getByRole("link", { name: "Mapping Projects" })).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(navigation.getByRole("link", { name: "Sponsors" })).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(navigation.getByRole("link", { name: "Contact" })).toBeFocused();

    await page.keyboard.press("Escape");
    await expect(menuToggle).toHaveAttribute("aria-expanded", "false");
    await expect(navigation).not.toHaveClass(/is-open/);
    await expect(menuToggle).toBeFocused();
});

test("the mobile hero separates its copy from the visible download action", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("index.html", { waitUntil: "networkidle" });

    const layout = await page.evaluate(() => {
        const chooser = document.querySelector(".platform-download");
        const primaryAction = document.querySelector(".platform-download__primary");
        const chooserStyle = getComputedStyle(chooser);
        const actionBounds = primaryAction.getBoundingClientRect();

        return {
            actionBottom: actionBounds.bottom,
            marginTop: Number.parseFloat(chooserStyle.marginTop),
            viewportHeight: innerHeight,
        };
    });

    expect(layout.marginTop).toBeGreaterThanOrEqual(18);
    expect(layout.actionBottom).toBeLessThanOrEqual(layout.viewportHeight);
    await expectNoHorizontalOverflow(page);
});

test("the mobile navigation stays reachable on a short viewport", async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 320 });
    await page.goto("docs.html", { waitUntil: "networkidle" });

    await page.locator("[data-menu-toggle]").click();
    await page.locator(".primary-nav__dropdown summary").first().click();

    const navigation = page.getByRole("navigation", { name: "Primary navigation" });
    const geometry = await navigation.evaluate(element => {
        const bounds = element.getBoundingClientRect();
        return {
            bottom: bounds.bottom,
            clientHeight: element.clientHeight,
            scrollHeight: element.scrollHeight,
            viewportHeight: window.innerHeight,
        };
    });

    expect(geometry.bottom).toBeLessThanOrEqual(geometry.viewportHeight);
    expect(geometry.scrollHeight).toBeGreaterThan(geometry.clientHeight);
    await navigation.getByRole("link", { name: "Troubleshooting" }).scrollIntoViewIfNeeded();
    await expect(navigation.getByRole("link", { name: "Troubleshooting" })).toBeVisible();
});

test("Sponsors uses responsive cards with official-site links", async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto("sponsors.html", { waitUntil: "networkidle" });

    const navigation = page.getByRole("navigation", { name: "Primary navigation" });
    const grid = page.locator(".sponsors-page__grid");
    const cards = grid.locator(".sponsor-card");
    const kiss = cards.filter({ has: page.getByRole("heading", { name: "KISS Rebreathers" }) });
    const xdeep = cards.filter({ has: page.getByRole("heading", { name: "XDEEP" }) });

    await navigation.getByText("Team & Partners", { exact: true }).click();
    await expect(navigation.getByRole("link", { name: "Sponsors" })).toHaveAttribute(
        "aria-current",
        "page",
    );
    const sponsorHeading = page.getByRole("heading", { name: "Sponsors", level: 1 });
    await expect(sponsorHeading).toHaveClass(/sr-only/);
    expect(await sponsorHeading.evaluate(heading => {
        const bounds = heading.getBoundingClientRect();

        return { height: bounds.height, width: bounds.width };
    })).toEqual({ height: 1, width: 1 });
    await expect(cards).toHaveCount(5);
    await expect(kiss).toHaveAttribute("href", "https://www.kissrebreathers.com/");
    await expect(xdeep).toHaveAttribute("href", "https://www.xdeep.eu/");
    await expect(kiss).toHaveAttribute("target", "_blank");
    await expect(xdeep).toHaveAttribute("rel", "noopener noreferrer");
    await expect(kiss.getByRole("img", { name: "KISS Rebreathers logo" })).toBeVisible();
    await expect(xdeep.getByRole("img", { name: "XDEEP logo" })).toBeVisible();
    await kiss.focus();
    await expect(kiss).toHaveCSS("outline-style", "solid");
    expect(await kiss.locator("img").evaluate(image => image.currentSrc)).toMatch(
        /kiss-rebreathers-logo\.webp$/,
    );
    expect(await xdeep.locator("img").evaluate(image => image.currentSrc)).toMatch(
        /xdeep-logo\.webp$/,
    );
    await expectNoHorizontalOverflow(page);

    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("sponsors.html", { waitUntil: "networkidle" });
    const mobileLayout = await page.locator(".sponsors-page__grid").evaluate(gridElement => {
        const cards = [...gridElement.querySelectorAll(".sponsor-card")];

        return cards.map(card => {
            const bounds = card.getBoundingClientRect();
            return { left: bounds.left, top: bounds.top, width: bounds.width };
        });
    });

    expect(mobileLayout).toHaveLength(5);
    for (let index = 1; index < mobileLayout.length; index += 1) {
        expect(mobileLayout[index].top).toBeGreaterThan(mobileLayout[index - 1].top);
        expect(mobileLayout[index].left).toBeCloseTo(mobileLayout[0].left, 1);
    }
    await expectNoHorizontalOverflow(page);
});

test("the shared header switches cleanly between inline and compact navigation", async ({ page }) => {
    const navigation = page.getByRole("navigation", { name: "Primary navigation" });
    const menuToggle = page.locator("[data-menu-toggle]");

    await page.setViewportSize({ width: 1100, height: 900 });
    await page.goto("advantage.html", { waitUntil: "networkidle" });
    await expect(navigation).toHaveCSS("position", "static");
    await expect(menuToggle).toHaveCSS("display", "none");
    await expect(page.locator(".header-download")).toBeVisible();
    await expectNoHorizontalOverflow(page);

    await page.setViewportSize({ width: 900, height: 900 });
    await expect(navigation).toHaveCSS("position", "fixed");
    await expect(menuToggle).toHaveCSS("display", "grid");
    await expect(page.locator(".header-download")).toBeHidden();
    await menuToggle.click();
    await expect(navigation).toHaveClass(/is-open/);
    await expectNoHorizontalOverflow(page);
});

test("Docs tables stack without internal horizontal scrolling on phones", async ({ page }) => {
    await page.setViewportSize({ width: 430, height: 932 });
    await page.goto("docs.html", { waitUntil: "networkidle" });

    const tables = page.locator(".docs-article table");
    expect(await tables.count()).toBeGreaterThan(0);
    for (const table of await tables.all()) {
        expect(await table.evaluate(element => ({
            clientWidth: element.clientWidth,
            scrollWidth: element.scrollWidth,
        }))).toEqual(expect.objectContaining({
            clientWidth: expect.any(Number),
            scrollWidth: expect.any(Number),
        }));
        expect(await table.evaluate(element => element.scrollWidth <= element.clientWidth + 1))
            .toBe(true);
    }
    await expect(tables.first().locator("tbody tr").first()).toHaveCSS("display", "block");
    await expectNoHorizontalOverflow(page);
});

test("the Why CaveViewer link reaches practical, readable map guidance", async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto("index.html", { waitUntil: "networkidle" });

    const navigation = page.getByRole("navigation", { name: "Primary navigation" });
    await navigation.getByRole("link", { name: "Why CaveViewer" }).click();
    await expect(page).toHaveURL(/advantage\.html$/);

    const advantage = page.locator("#advantage");
    await expect(advantage).toBeInViewport();
    await expect(advantage.getByRole("heading", { name: "View Ginormous Maps" }))
        .toBeVisible();
    await expect(advantage).toContainText("consumer-grade hardware");
    await expect(advantage.getByRole("img", { name: "CaveViewer rendering a textured cave passage with a minimap and viewer controls" }))
        .toBeVisible();
    const streaming = page.locator("#advantage-streaming");
    await streaming.scrollIntoViewIfNeeded();
    await expect(streaming).toContainText("system and graphics-memory budgets");
    await expect(streaming.getByRole("img", { name: "CaveViewer Preferences with the Streaming tab selected, showing memory, loading, and upload settings" }))
        .toBeVisible();
    const freedom = page.locator("#advantage-freedom");
    await freedom.scrollIntoViewIfNeeded();
    await expect(freedom.getByRole("heading", { name: "Pay Nothing" })).toBeVisible();
    await expect(freedom.getByRole("link", { name: "GNU Affero General Public License v3.0 (AGPLv3)" }))
        .toHaveAttribute("href", "https://www.gnu.org/licenses/agpl-3.0.en.html");
    await expectNoHorizontalOverflow(page);

    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("advantage.html", { waitUntil: "networkidle" });
    await expectFontSizeAtLeast(page.locator(
        ".feature-section--advantage .feature-section__copy p",
    ), 14);
    await expectNoHorizontalOverflow(page);
});

test("Projects presents the two original expedition videos in the Feature layout", async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto("media.html", { waitUntil: "domcontentloaded" });

    const navigation = page.getByRole("navigation", { name: "Primary navigation" });
    const videos = page.locator(".feature-section__visual--video iframe");

    await navigation.getByText("Team & Partners", { exact: true }).click();
    await expect(navigation.getByRole("link", { name: "Mapping Projects" })).toHaveAttribute(
        "aria-current",
        "page",
    );
    const pageHeading = page.getByRole("heading", { name: "Projects", level: 1 });
    await expect(pageHeading).toHaveClass(/sr-only/);
    await expect(page.getByText("Dives behind the data", { exact: true })).toHaveCount(0);
    await expect(page.getByRole("heading", {
        name: "Wes Skiles Peacock Springs State Park — 3-D Mapping Initiative",
        level: 2,
    })).toBeVisible();
    await expect(videos).toHaveCount(2);
    await expect(videos.nth(0)).toHaveAttribute(
        "src",
        "https://www.youtube-nocookie.com/embed/ZytYB0jpe38",
    );
    await expect(videos.nth(1)).toHaveAttribute(
        "src",
        "https://www.youtube-nocookie.com/embed/BSv9UILf6DI",
    );
    await expect(videos.nth(0)).toHaveAttribute("loading", "lazy");
    await expect(videos.nth(1)).toHaveAttribute("loading", "lazy");
    await expectNoHorizontalOverflow(page);
});

test("Contact preserves its normal desktop composition while short screens scroll", async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto("contact.html", { waitUntil: "networkidle" });

    expect(
        await page.evaluate(
            () => document.documentElement.scrollHeight - document.documentElement.clientHeight,
        ),
    ).toBeLessThanOrEqual(1);
    await expect(page.locator(".site-endcap")).toBeInViewport();

    await page.setViewportSize({ width: 1440, height: 400 });
    await page.goto("contact.html", { waitUntil: "networkidle" });

    expect(
        await page.evaluate(
            () => document.documentElement.scrollHeight - document.documentElement.clientHeight,
        ),
    ).toBeGreaterThan(1);
    await expectContactTargetsReachable(page);
});

test("the Contact route remains reachable at a 200%-zoom-equivalent viewport", async ({ page }) => {
    // A 720 × 450 CSS-pixel viewport approximates a 1440 × 900 desktop window at 200% zoom.
    await page.setViewportSize({ width: 720, height: 450 });
    await page.goto("contact.html", { waitUntil: "networkidle" });

    await expect(page.getByRole("heading", { name: "Contact Us" })).toBeVisible();
    await expectNoHorizontalOverflow(page);
    await expectContactTargetsReachable(page);
});

test("Contact keeps simulated large text and mobile content reachable", async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 500 });
    await page.goto("contact.html", { waitUntil: "networkidle" });
    await page.addStyleTag({
        content: `
            .page-contact .contact-card h1,
            .page-contact .contact-form label,
            .page-contact .contact-form input:not([type="hidden"]),
            .page-contact .contact-form textarea,
            .page-contact .contact-form__submit { font-size: 200% !important; }
        `,
    });

    await expect(page.locator("#cf-name")).toHaveCSS("font-size", "32px");
    expect(
        await page.evaluate(
            () => document.documentElement.scrollHeight - document.documentElement.clientHeight,
        ),
    ).toBeGreaterThan(1);
    await expectContactTargetsReachable(page);

    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("contact.html", { waitUntil: "networkidle" });

    await expectNoHorizontalOverflow(page);
    await expectContactTargetsReachable(page);
});

test("essential labels, metadata, and prose retain readable minimums at mobile and zoomed layouts", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("index.html", { waitUntil: "networkidle" });

    await expectFontSizeAtLeast(page.locator(".hero__lead"), 14);
    await expectFontSizeAtLeast(page.locator(
        ".platform-download__primary small, .platform-download__alternatives, .site-endcap__inner",
    ));
    await page.locator("[data-platform-dialog-open]").click();
    await page.locator("[data-mac-download-toggle]").click();
    await expectFontSizeAtLeast(page.locator(
        ".platform-download__dialog-header small, .platform-download__options small, .platform-download__mac-choices p, .platform-download__dialog-note",
    ));
    await expectNoHorizontalOverflow(page);

    await page.goto("advantage.html", { waitUntil: "networkidle" });
    await expectFontSizeAtLeast(page.locator(
        ".feature-section--advantage .feature-section__copy p",
    ), 14);
    await expectNoHorizontalOverflow(page);

    await page.goto("about.html", { waitUntil: "networkidle" });
    await expectFontSizeAtLeast(page.locator(
        ".about-person__role, .about-person__affiliation, .site-endcap__inner",
    ));
    await expectNoHorizontalOverflow(page);

    await page.goto("contact.html", { waitUntil: "networkidle" });
    await expectFontSizeAtLeast(page.locator(
        ".contact-form__field > label, .contact-form__submit, .site-endcap__inner",
    ));
    await expectNoHorizontalOverflow(page);

    // A 720 × 450 CSS-pixel viewport approximates a 1440 × 900 window at 200% zoom.
    await page.setViewportSize({ width: 720, height: 450 });
    await page.goto("index.html", { waitUntil: "networkidle" });
    await expectFontSizeAtLeast(page.locator(".hero__lead"), 14);
    await expectNoHorizontalOverflow(page);

    await page.goto("about.html", { waitUntil: "networkidle" });
    await expectFontSizeAtLeast(page.locator(".about-person__role, .about-person__affiliation"));
    await page.locator(".about-person").nth(2).scrollIntoViewIfNeeded();
    await expect(page.locator(".about-person").nth(2)).toBeInViewport();
    await expectNoHorizontalOverflow(page);
});

test("reduced motion settles primary Home content without hiding reveals", async ({ page }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.goto("index.html", { waitUntil: "networkidle" });

    await expect(page.getByRole("heading", { name: /Explore what/i })).toBeVisible();
    const motionState = await page.evaluate(() => ({
        heroAnimation: getComputedStyle(document.querySelector(".hero__media")).animationName,
        heroTransition: getComputedStyle(document.querySelector(".hero__media")).transitionProperty,
        reducedMotion: matchMedia("(prefers-reduced-motion: reduce)").matches,
        revealEnhanced: document.documentElement.classList.contains("reveal-enhanced"),
        revealTargets: [...document.querySelectorAll("[data-reveal]")].map(target => {
            const style = getComputedStyle(target);

            return {
                opacity: style.opacity,
                transform: style.transform,
                transition: style.transitionProperty,
            };
        }),
    }));

    expect(motionState.reducedMotion).toBe(true);
    expect(motionState.revealEnhanced).toBe(false);
    expect(motionState.heroAnimation).toBe("none");
    expect(motionState.heroTransition).toBe("none");
    expect(motionState.revealTargets).not.toHaveLength(0);
    for (const target of motionState.revealTargets) {
        expect(target).toEqual({ opacity: "1", transform: "none", transition: "none" });
    }
});

test("Team cards remain static presentational articles on hover", async ({ page }) => {
    await page.goto("about.html", { waitUntil: "networkidle" });

    const card = page.locator(".about-person").first();
    const portrait = card.locator(".about-person__media img");
    await expect(page.locator(".about-person__scan")).toHaveCount(0);
    expect(await card.evaluate(element => element.tabIndex)).toBe(-1);

    const beforeHover = await portrait.evaluate(image => {
        const style = getComputedStyle(image);

        return {
            filter: style.filter,
            transform: style.transform,
            transition: style.transitionProperty,
        };
    });
    await card.hover();
    const afterHover = await portrait.evaluate(image => {
        const style = getComputedStyle(image);

        return {
            filter: style.filter,
            transform: style.transform,
            transition: style.transitionProperty,
        };
    });

    expect(beforeHover).toEqual(afterHover);
    expect(beforeHover.transform).toBe("none");
    expect(beforeHover.transition).toBe("all");
});

test("Magic Mr_V has a responsive cave-diving Team portrait", async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto("about.html", { waitUntil: "networkidle" });

    const card = page.locator(".about-person").filter({
        has: page.getByRole("heading", { name: "Magic Mr_V", exact: true }),
    });

    await expect(card).toHaveCount(1);
    await expect(card).not.toHaveClass(/about-person--text-only/);
    const portrait = card.getByRole("img", { name: "Magic Mr_V" });
    await expect(portrait).toBeVisible();
    await expect(portrait).toHaveAttribute("width", "1536");
    await expect(portrait).toHaveAttribute("height", "1169");
    expect(await portrait.evaluate(image => image.currentSrc)).toMatch(
        /magic-mr-v-cave-diver-(640|960)\.webp$/,
    );
    await expectNoHorizontalOverflow(page);
});

test("modern browsers choose responsive images with reserved layout geometry", async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto("index.html", { waitUntil: "networkidle" });

    const heroBackground = await page.locator(".hero__media").evaluate(
        element => getComputedStyle(element).backgroundImage,
    );
    expect(heroBackground).toContain("ginnie1-faceted-survey.webp");
    expect(heroBackground).toContain("ginnie1-faceted-survey.png");

    await page.goto("advantage.html", { waitUntil: "networkidle" });
    const renderingImage = page.locator("#rendering picture img");
    await expect(renderingImage).toHaveAttribute("width", "2558");
    await expect(renderingImage).toHaveAttribute("height", "1556");

    const renderingMetrics = await renderingImage.evaluate(image => {
        const bounds = image.getBoundingClientRect();

        return {
            currentSrc: image.currentSrc,
            naturalHeight: image.naturalHeight,
            naturalWidth: image.naturalWidth,
            opacity: getComputedStyle(image).opacity,
            ratio: bounds.width / bounds.height,
            visibility: getComputedStyle(image).visibility,
        };
    });
    expect(renderingMetrics.currentSrc).toMatch(/rendering-engine-(800|1600)\.webp$/);
    expect(renderingMetrics.naturalWidth).toBeGreaterThan(0);
    expect(renderingMetrics.naturalHeight).toBeGreaterThan(0);
    expect(renderingMetrics.opacity).toBe("1");
    expect(renderingMetrics.visibility).toBe("visible");
    expect(renderingMetrics.ratio).toBeCloseTo(2558 / 1556, 2);

    const mapLibraryImage = page.locator("#map-library picture img");
    await mapLibraryImage.scrollIntoViewIfNeeded();
    await expect(mapLibraryImage).toBeVisible();
    await expect(mapLibraryImage).toHaveAttribute("width", "2420");
    await expect(mapLibraryImage).toHaveAttribute("height", "1634");
    await expect.poll(async () => mapLibraryImage.evaluate(image => image.naturalWidth))
        .toBeGreaterThan(0);
    expect(await mapLibraryImage.evaluate(image => image.currentSrc)).toMatch(
        /map-library-(800|1600)\.webp$/,
    );

    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("advantage.html", { waitUntil: "networkidle" });
    const mobileMapLibraryImage = page.locator("#map-library picture img");
    await mobileMapLibraryImage.scrollIntoViewIfNeeded();
    await expect(mobileMapLibraryImage).toBeVisible();
    await expect.poll(async () => mobileMapLibraryImage.evaluate(image => image.naturalWidth))
        .toBeGreaterThan(0);
    expect(await mobileMapLibraryImage.evaluate(image => image.currentSrc)).toMatch(
        /map-library-800\.webp$/,
    );
    await expectNoHorizontalOverflow(page);

    await page.goto("docs.html", { waitUntil: "networkidle" });
    const importImage = page.getByRole("img", {
        name: "CaveViewer Preferences with the Import tab selected, showing cache and worker settings",
    });
    await importImage.scrollIntoViewIfNeeded();
    await expect(importImage).toBeVisible();
    await expect(importImage).toHaveAttribute("width", "2420");
    await expect(importImage).toHaveAttribute("height", "1634");
    await expect.poll(async () => importImage.evaluate(image => image.naturalWidth))
        .toBeGreaterThan(0);
    expect(await importImage.evaluate(image => image.currentSrc)).toMatch(
        /preferences-import-800\.webp$/,
    );
    await expectNoHorizontalOverflow(page);

    const streamingImage = page.getByRole("img", {
        name: "CaveViewer Preferences with the Streaming tab selected, showing memory, loading, and upload settings",
    });
    await streamingImage.scrollIntoViewIfNeeded();
    await expect(streamingImage).toBeVisible();
    await expect(streamingImage).toHaveAttribute("width", "2420");
    await expect(streamingImage).toHaveAttribute("height", "1634");
    await expect.poll(async () => streamingImage.evaluate(image => image.naturalWidth))
        .toBeGreaterThan(0);
    expect(await streamingImage.evaluate(image => image.currentSrc)).toMatch(
        /preferences-streaming-800\.webp$/,
    );
    await expectNoHorizontalOverflow(page);

    const restoreDefaults = page.getByRole("heading", {
        name: "Restore Default Settings",
        level: 2,
    });
    await restoreDefaults.scrollIntoViewIfNeeded();
    await expect(page.locator(".docs-article")).toContainText(
        "Don’t worry about messing up your preferences—you can always save, import, and restore them in the app. Note that platform-specific settings, such as directory paths, will not be restored.",
    );
    const backupImage = page.getByRole("img", {
        name: "CaveViewer Preferences with the Backup tab selected, showing controls to save, load, and restore preferences",
    });
    await backupImage.scrollIntoViewIfNeeded();
    await expect(backupImage).toBeVisible();
    await expect(backupImage).toHaveAttribute("width", "2420");
    await expect(backupImage).toHaveAttribute("height", "1634");
    await expect.poll(async () => backupImage.evaluate(image => image.naturalWidth))
        .toBeGreaterThan(0);
    expect(await backupImage.evaluate(image => image.currentSrc)).toMatch(
        /preferences-backup-800\.webp$/,
    );
    await expectNoHorizontalOverflow(page);

    const gettingHelp = page.getByRole("heading", { name: "Getting Help", level: 3 });
    await gettingHelp.scrollIntoViewIfNeeded();
    await expect(page.locator(".docs-article")).toContainText(
        "Copy any error message you find, then include it with a short description of the problem when you contact support.",
    );
    await expect(page.getByRole("link", { name: "contact support" }))
        .toHaveAttribute("href", "contact.html");
    const troubleshootingImage = page.getByRole("img", {
        name: "CaveViewer Help with the Troubleshooting tab selected, showing the latest application log and last error controls",
    });
    await troubleshootingImage.scrollIntoViewIfNeeded();
    await expect(troubleshootingImage).toBeVisible();
    await expect(troubleshootingImage).toHaveAttribute("width", "2420");
    await expect(troubleshootingImage).toHaveAttribute("height", "1634");
    await expect.poll(async () => troubleshootingImage.evaluate(image => image.naturalWidth))
        .toBeGreaterThan(0);
    expect(await troubleshootingImage.evaluate(image => image.currentSrc)).toMatch(
        /help-troubleshooting-800\.webp$/,
    );
    await expectNoHorizontalOverflow(page);

    await page.goto("about.html", { waitUntil: "networkidle" });
    const firstPortrait = page.locator(".about-person picture img").first();
    await expect(firstPortrait).toHaveAttribute("width", "1206");
    await expect(firstPortrait).toHaveAttribute("height", "1193");
    expect(await firstPortrait.evaluate(image => image.currentSrc)).toMatch(
        /e02af4158100878810221f4cc8db33f52026e293-(640|960)\.webp$/,
    );
});

test("the generated release manifest preserves the Windows primary action and macOS chooser", async ({ page }) => {
    await page.addInitScript(() => {
        Object.defineProperty(navigator, "userAgentData", {
            configurable: true,
            value: { platform: "Windows" },
        });
        Object.defineProperty(navigator, "platform", {
            configurable: true,
            value: "Win32",
        });
    });
    await page.goto("index.html", { waitUntil: "networkidle" });

    const release = await page.locator("[data-release-data]").evaluate(element => (
        JSON.parse(element.textContent)
    ));
    const windowsUrl = (
        `${release.repository}/releases/download/v${release.version}/`
        + release.platforms.windows.artifact
    );
    const primary = page.locator("[data-primary-download]");
    const dialog = page.locator("[data-platform-dialog]");

    await expect(primary).toHaveAttribute("href", windowsUrl);
    await expect(primary).toContainText(release.platforms.windows.primary_label);
    await expect(primary).toContainText(`${release.channel} ${release.version}`);

    await page.locator("[data-platform-dialog-open]").click();
    await expect(dialog).toHaveAttribute("open", "");
    const releaseDate = new Date(`${release.release_date}T00:00:00Z`).toLocaleDateString("en-US", {
        month: "long",
        day: "numeric",
        year: "numeric",
        timeZone: "UTC",
    }).replace(",", "");
    await expect(dialog.locator(".platform-download__dialog-note"))
        .toHaveText(`Build on ${releaseDate}`);
    await expect(dialog.locator('[data-release-platform="windows"] b svg')).toHaveCount(1);
    await expect(dialog.locator('[data-release-platform="linux"] b svg')).toHaveCount(1);
    await expect(dialog.locator('[data-release-platform="windows"]'))
        .toContainText(release.platforms.windows.detail);
    await expect(dialog.locator('[data-release-platform="macos"]'))
        .toContainText(release.platforms.macos.detail);
    await expect(dialog.locator('[data-release-platform="linux"]'))
        .toContainText(release.platforms.linux.detail);
    await expect(dialog.locator("[data-platform-install-note]")).toHaveCount(0);
    await page.locator("[data-mac-download-toggle]").click();
    await expect(dialog.locator('[data-release-platform="macos-arm64"] b svg')).toHaveCount(1);
    await expect(dialog.locator('[data-release-platform="macos-x86_64"] b svg')).toHaveCount(1);
    await expect(page.locator("[data-mac-download-options]")).toBeVisible();
    await expect(dialog.locator('[data-release-platform="windows"]')).toHaveCSS("opacity", "0.38");
    await expect(dialog.locator('[data-release-platform="linux"]')).toHaveCSS("opacity", "0.38");
});

test.describe("wide Home hero art direction", () => {
    for (const viewport of wideHeroViewports) {
        test(`${viewport.name} keeps the cave image as a centered full-hero background`, async ({ page }) => {
            await page.setViewportSize(viewport);
            await page.goto("index.html", { waitUntil: "networkidle" });

            const geometry = await page.evaluate(() => {
                const header = document.querySelector(".site-header__inner").getBoundingClientRect();
                const hero = document.querySelector(".hero").getBoundingClientRect();
                const content = document.querySelector(".hero__content").getBoundingClientRect();
                const copy = document.querySelector(".hero__copy").getBoundingClientRect();
                const media = getComputedStyle(document.querySelector(".hero__media"));

                return {
                    headerLeft: header.left,
                    headerRight: header.right,
                    contentLeft: content.left,
                    contentRight: content.right,
                    heroCenter: hero.top + hero.height / 2,
                    copyCenter: copy.top + copy.height / 2,
                    backgroundPosition: media.backgroundPosition,
                    backgroundSize: media.backgroundSize,
                    mediaBottom: media.bottom,
                    mediaLeft: media.left,
                    mediaRight: media.right,
                    mediaTop: media.top,
                    viewportWidth: window.innerWidth,
                };
            });

            const normalShellGutter = Math.max(32, (geometry.viewportWidth - 1480) / 2);
            const cappedWideGutter = Math.min(
                normalShellGutter,
                Math.min(Math.max(176, geometry.viewportWidth * .11), 320),
            );

            expect(Math.abs(geometry.headerLeft - geometry.contentLeft)).toBeLessThanOrEqual(1);
            expect(Math.abs(geometry.contentLeft - cappedWideGutter)).toBeLessThanOrEqual(1);
            expect(Math.abs(geometry.viewportWidth - geometry.contentRight - cappedWideGutter)).toBeLessThanOrEqual(1);
            expect(Math.abs(geometry.headerRight - geometry.contentRight)).toBeLessThanOrEqual(1);
            // The fixed header reserves extra space above the centered copy.
            expect(Math.abs(geometry.copyCenter - geometry.heroCenter)).toBeLessThanOrEqual(32);
            expect(geometry.backgroundSize).toContain("cover");
            expect(geometry.backgroundPosition).toContain("50% 50%");
            expect(geometry.mediaTop).toBe("0px");
            expect(geometry.mediaRight).toBe("0px");
            expect(geometry.mediaBottom).toBe("0px");
            expect(geometry.mediaLeft).toBe("0px");
            await expectNoHorizontalOverflow(page);
        });
    }
});

test("disabling JavaScript leaves every reveal target visible", async ({ browser, browserName }, testInfo) => {
    test.skip(browserName !== "chromium", "The suite currently targets Chromium only.");

    const context = await browser.newContext({
        baseURL: testInfo.project.use.baseURL,
        javaScriptEnabled: false,
        viewport: { width: 1440, height: 900 },
    });
    const page = await context.newPage();

    try {
        for (const route of canonicalPages) {
            await page.goto(route.path, { waitUntil: route.waitUntil ?? "networkidle" });
            const revealTargetsAreVisible = await page.locator("[data-reveal]").evaluateAll(
                targets => targets.every(target => {
                    const style = getComputedStyle(target);
                    const bounds = target.getBoundingClientRect();

                    return (
                        style.opacity === "1"
                        && style.visibility !== "hidden"
                        && bounds.width > 0
                        && bounds.height > 0
                    );
                }),
            );

            expect(revealTargetsAreVisible, `${route.name} reveal targets should remain visible`).toBe(true);
        }
    } finally {
        await context.close();
    }
});
