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

async function expectNoHorizontalOverflow(page) {
    const overflow = await page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );

    expect(overflow).toBeLessThanOrEqual(1);
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
    const focusedLink = navigation.getByRole("link", { name: "Team" });

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
    await expect(navigation.getByRole("link", { name: "Team" })).toBeFocused();

    await page.keyboard.press("Escape");
    await expect(menuToggle).toHaveAttribute("aria-expanded", "false");
    await expect(navigation).not.toHaveClass(/is-open/);
    await expect(menuToggle).toBeFocused();
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

test("reduced motion keeps primary Home content reachable", async ({ page }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.goto("index.html", { waitUntil: "networkidle" });

    await expect(page.getByRole("heading", { name: /Explore what/i })).toBeVisible();
    expect(
        await page.evaluate(() => matchMedia("(prefers-reduced-motion: reduce)").matches),
    ).toBe(true);
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
            ratio: bounds.width / bounds.height,
        };
    });
    expect(renderingMetrics.currentSrc).toMatch(/rendering-engine-(800|1600)\.webp$/);
    expect(renderingMetrics.ratio).toBeCloseTo(2558 / 1556, 2);

    await page.goto("about.html", { waitUntil: "networkidle" });
    const firstPortrait = page.locator(".about-person picture img").first();
    await expect(firstPortrait).toHaveAttribute("width", "1206");
    await expect(firstPortrait).toHaveAttribute("height", "1193");
    expect(await firstPortrait.evaluate(image => image.currentSrc)).toMatch(
        /e02af4158100878810221f4cc8db33f52026e293-(640|960)\.webp$/,
    );
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
