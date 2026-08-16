import { defineConfig } from "@playwright/test";

const webPort = 3100;

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  reporter: process.env.CI ? "github" : "list",
  use: {
    baseURL: `http://127.0.0.1:${webPort}`,
    trace: "retain-on-failure",
  },
  webServer: {
    command: `pnpm --filter @pia/web dev --hostname 127.0.0.1 --port ${webPort}`,
    url: `http://127.0.0.1:${webPort}`,
    reuseExistingServer: !process.env.CI,
    env: {
      NEXT_PUBLIC_PIA_API_URL: "http://pia-api.e2e.test",
      NEXT_PUBLIC_SUPABASE_ANON_KEY: "synthetic-e2e-anon-key",
      NEXT_PUBLIC_SUPABASE_URL: "http://supabase.e2e.test",
    },
  },
  projects: [
    {
      name: "desktop",
      use: { viewport: { width: 1440, height: 900 } },
    },
    {
      name: "mobile",
      use: {
        viewport: { width: 390, height: 844 },
        isMobile: true,
        hasTouch: true,
      },
    },
  ],
});
