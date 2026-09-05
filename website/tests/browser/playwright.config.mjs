import { defineConfig } from "@playwright/test";

// The checks reuse a local clone of the site. Override this when reviewing the
// published URL or another host.
const baseURL = process.env.CAVEVIEWER_WEBSITE_URL ?? "http://127.0.0.1:4173";

export default defineConfig({
    testDir: "./specs",
    timeout: 30_000,
    expect: { timeout: 5_000 },
    fullyParallel: false,
    forbidOnly: Boolean(process.env.CI),
    retries: process.env.CI ? 2 : 0,
    workers: 1,
    reporter: "list",
    outputDir: "./test-results",
    use: {
        baseURL,
        headless: true,
        screenshot: "only-on-failure",
        trace: "retain-on-failure",
    },
    projects: [
        {
            name: "chromium",
            use: { browserName: "chromium" },
        },
    ],
});
