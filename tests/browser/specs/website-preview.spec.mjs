import { expect, test } from "@playwright/test";

const canonicalPages = [
    { name: "Home", path: "index.html", content: "Explore what" },
    { name: "Features", path: "features.html", content: "Render What Others Can’t" },
    { name: "Team", path: "about.html", content: "Magic Mr_V" },
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
                await page.goto(route.path, { waitUntil: "networkidle" });
                await expect(page.locator("main")).toBeVisible();
                await expect(page.locator("main")).toContainText(route.content);
                await expectNoHorizontalOverflow(page);
            }
        });
    }
});

test("the skip link moves keyboard focus to main content", async ({ page }) => {
    await page.goto("features.html", { waitUntil: "networkidle" });

    const skipLink = page.getByRole("link", { name: "Skip to main content" });
    const main = page.locator("#main-content");

    await page.keyboard.press("Tab");
    await expect(skipLink).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(main).toBeFocused();
});

test("navigation current and focus states have non-color indicators", async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto("features.html", { waitUntil: "networkidle" });

    const navigation = page.getByRole("navigation", { name: "Primary navigation" });
    const currentLink = navigation.getByRole("link", { name: "Features" });
    const focusedLink = navigation.getByRole("link", { name: "Performance" });

    await page.keyboard.press("Tab");
    await page.keyboard.press("Tab");
    await page.keyboard.press("Tab");
    await expect(currentLink).toBeFocused();
    await expect(currentLink).toHaveCSS("text-decoration-line", "underline");

    await page.keyboard.press("Tab");
    await expect(focusedLink).toBeFocused();
    await expect(focusedLink).toHaveCSS("outline-style", "solid");
});

test("the mobile navigation is keyboard-operable", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("features.html", { waitUntil: "networkidle" });

    const menuToggle = page.locator("[data-menu-toggle]");
    const navigation = page.getByRole("navigation", { name: "Primary navigation" });

    await expect(menuToggle).toHaveAttribute("aria-label", "Open navigation");
    await menuToggle.focus();
    await page.keyboard.press("Enter");
    await expect(menuToggle).toHaveAttribute("aria-expanded", "true");
    await expect(menuToggle).toHaveAttribute("aria-label", "Close navigation");
    await expect(navigation).toHaveClass(/is-open/);
    await expect(navigation.getByRole("link", { name: "Features" })).toBeFocused();

    await page.keyboard.press("Tab");
    await expect(navigation.getByRole("link", { name: "Performance" })).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(navigation.getByRole("link", { name: "Team" })).toBeFocused();

    await page.keyboard.press("Escape");
    await expect(menuToggle).toHaveAttribute("aria-expanded", "false");
    await expect(navigation).not.toHaveClass(/is-open/);
    await expect(menuToggle).toBeFocused();
});

test("the shared header switches cleanly between inline and compact navigation", async ({ page }) => {
    const navigation = page.getByRole("navigation", { name: "Primary navigation" });
    const menuToggle = page.locator("[data-menu-toggle]");

    await page.setViewportSize({ width: 900, height: 900 });
    await page.goto("features.html", { waitUntil: "networkidle" });
    await expect(navigation).toHaveCSS("position", "static");
    await expect(menuToggle).toHaveCSS("display", "none");
    await expectNoHorizontalOverflow(page);

    await page.setViewportSize({ width: 820, height: 900 });
    await expect(navigation).toHaveCSS("position", "fixed");
    await expect(menuToggle).toHaveCSS("display", "grid");
    await menuToggle.click();
    await expect(navigation).toHaveClass(/is-open/);
    await expectNoHorizontalOverflow(page);
});

test("the Performance link reaches practical, readable map guidance", async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto("index.html", { waitUntil: "networkidle" });

    await page.getByRole("navigation", { name: "Primary navigation" })
        .getByRole("link", { name: "Performance" })
        .click();
    await expect(page).toHaveURL(/features\.html#performance$/);

    const performance = page.locator("#performance");
    await expect(performance).toBeInViewport();
    await expect(performance.getByRole("heading", { name: "Big caves, practical choices." }))
        .toBeVisible();
    await expect(performance).toContainText("System RAM target");
    await expect(performance).toContainText("Older Intel-based Macs");
    await expectNoHorizontalOverflow(page);

    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("features.html#performance", { waitUntil: "networkidle" });
    await expectFontSizeAtLeast(page.locator(
        ".performance-section__intro > p:last-child, .performance-card > p, .performance-card__settings dd, .performance-card__requirements",
    ), 14);
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

    await expectFontSizeAtLeast(page.locator(".hero__formats"), 14);
    await expectFontSizeAtLeast(page.locator(
        ".header-download, .platform-download__primary small, .platform-download__alternatives, .site-endcap__inner",
    ));
    await page.locator("[data-platform-dialog-open]").click();
    await page.locator("[data-mac-download-toggle]").click();
    await expectFontSizeAtLeast(page.locator(
        ".platform-download__dialog-header small, .platform-download__options small, .platform-download__mac-choices p, .platform-download__dialog-note",
    ));
    await expectNoHorizontalOverflow(page);

    await page.goto("features.html", { waitUntil: "networkidle" });
    await expectFontSizeAtLeast(page.locator(
        ".feature-section__copy p, .feature-section__note, .feature-section__capabilities",
    ), 14);
    await expectFontSizeAtLeast(page.locator(
        ".performance-section__intro > p:last-child, .performance-card > p, .performance-card__settings dd, .performance-card__requirements",
    ), 14);
    await expectNoHorizontalOverflow(page);

    await page.goto("about.html", { waitUntil: "networkidle" });
    await expectFontSizeAtLeast(page.locator(
        ".about-person__role, .about-person__affiliation, .site-endcap__inner",
    ));
    await expectNoHorizontalOverflow(page);

    await page.goto("contact.html", { waitUntil: "networkidle" });
    await expectFontSizeAtLeast(page.locator(
        ".header-download, .contact-form__field > label, .contact-form__submit, .site-endcap__inner",
    ));
    await expectNoHorizontalOverflow(page);

    // A 720 × 450 CSS-pixel viewport approximates a 1440 × 900 window at 200% zoom.
    await page.setViewportSize({ width: 720, height: 450 });
    await page.goto("index.html", { waitUntil: "networkidle" });
    await expectFontSizeAtLeast(page.locator(".hero__formats"), 14);
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

test("modern browsers choose responsive images with reserved layout geometry", async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto("index.html", { waitUntil: "networkidle" });

    const heroBackground = await page.locator(".hero__media").evaluate(
        element => getComputedStyle(element).backgroundImage,
    );
    expect(heroBackground).toContain("ginnie1.webp");

    await page.goto("features.html", { waitUntil: "networkidle" });
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
    await page.locator("[data-mac-download-toggle]").click();
    await expect(page.locator("[data-mac-download-options]")).toBeVisible();
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
            await page.goto(route.path, { waitUntil: "networkidle" });
            const revealTargetsAreVisible = await page.locator("[data-reveal]").evaluateAll(
                targets => targets.length > 0 && targets.every(target => {
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
