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

    await page.keyboard.press("Escape");
    await expect(menuToggle).toHaveAttribute("aria-expanded", "false");
    await expect(navigation).not.toHaveClass(/is-open/);
});

test("the Contact route remains horizontally reachable at a 200%-zoom-equivalent viewport", async ({ page }) => {
    // A 720 CSS-pixel viewport approximates a 1440-pixel desktop window at 200% zoom.
    await page.setViewportSize({ width: 720, height: 900 });
    await page.goto("contact.html", { waitUntil: "networkidle" });

    await expect(page.getByRole("heading", { name: "Contact Us" })).toBeVisible();
    await expectNoHorizontalOverflow(page);
});

test("reduced motion keeps primary Home content reachable", async ({ page }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.goto("index.html", { waitUntil: "networkidle" });

    await expect(page.getByRole("heading", { name: /Explore what/i })).toBeVisible();
    expect(
        await page.evaluate(() => matchMedia("(prefers-reduced-motion: reduce)").matches),
    ).toBe(true);
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
