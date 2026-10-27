import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/browser",
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  workers: 2,
  use: {
    baseURL: "http://127.0.0.1:8877",
    browserName: "chromium",
    headless: true,
    launchOptions: process.env.V2C_BROWSER_EXECUTABLE
      ? {
          executablePath: process.env.V2C_BROWSER_EXECUTABLE,
          args: ["--no-sandbox", "--disable-dev-shm-usage"],
        }
      : {},
    trace: "retain-on-failure",
  },
  webServer: {
    command: "python3 tests/browser/server.py",
    url: "http://127.0.0.1:8877",
    reuseExistingServer: false,
    timeout: 15000,
  },
});
