import { expect, test, type Page } from "@playwright/test";

const apiUrl = "http://pia-api.e2e.test";
const authStorageKey = "sb-supabase-auth-token";

const authenticatedSession = {
  access_token: "synthetic-owner-token",
  expires_at: 4_102_444_800,
  expires_in: 3_600,
  refresh_token: "synthetic-refresh-token",
  token_type: "bearer",
  user: {
    aud: "authenticated",
    email: "owner@example.test",
    id: "00000000-0000-4000-8000-000000000081",
    role: "authenticated",
  },
};

const freshPicture = {
  account_summaries: [
    {
      account_id: "synthetic-cash",
      archived_at: null,
      emergency_reserve_target_eur: null,
      name: "Synthetic cash",
      role: "cash",
    },
    {
      account_id: "synthetic-reserve",
      archived_at: null,
      emergency_reserve_target_eur: "2000.00",
      name: "Synthetic reserve",
      role: "emergency_reserve",
    },
  ],
  as_of: "2026-08-01T10:00:00Z",
  cash_by_currency: {
    accounts: [
      {
        account_id: "synthetic-cash",
        amount: "540.00",
        currency: "EUR",
        evidence_event_ids: ["synthetic-event-1"],
      },
    ],
    owner: { EUR: "540.00" },
  },
  completeness: { diagnostic_count: 0, status: "complete" },
  diagnostics: [],
  evidence_event_ids: ["synthetic-event-1", "synthetic-event-2"],
  fifo: { open_lots: [], realized_sales: [] },
  freshness: { status: "fresh" },
  positions: {
    accounts: [],
    owner: [
      {
        evidence_event_ids: ["synthetic-event-2"],
        instrument_id: "SYNTHETIC-ETF",
        quantity: "2.5000",
      },
    ],
  },
  refreshed_at: "2026-08-01T10:05:00Z",
  reserve_progress: {
    available_eur_balance: "1500.00",
    configured_target_eur: "2000.00",
    status: "available",
  },
  snapshot_id: "synthetic-snapshot-1",
  state: "ready",
};

type Picture = typeof freshPicture;

type Requests = {
  read: number;
  refresh: number;
  tokens: string[];
};

async function authenticate(page: Page) {
  await page.addInitScript(
    ({ key, session }) => window.localStorage.setItem(key, JSON.stringify(session)),
    { key: authStorageKey, session: authenticatedSession },
  );
}

async function openDashboard(
  page: Page,
  initial: Picture | undefined,
  refreshed: Picture = freshPicture,
) {
  const requests: Requests = { read: 0, refresh: 0, tokens: [] };
  await authenticate(page);
  await page.route(`${apiUrl}/v1/financial-picture`, async (route) => {
    requests.read += 1;
    requests.tokens.push(route.request().headers().authorization ?? "");
    if (!initial) {
      await route.fulfill({ status: 404 });
      return;
    }
    await route.fulfill({
      body: JSON.stringify(initial),
      contentType: "application/json",
      headers: { "access-control-allow-origin": "*" },
    });
  });
  await page.route(`${apiUrl}/v1/financial-picture/refresh`, async (route) => {
    requests.refresh += 1;
    requests.tokens.push(route.request().headers().authorization ?? "");
    await route.fulfill({
      body: JSON.stringify(refreshed),
      contentType: "application/json",
      headers: { "access-control-allow-origin": "*" },
    });
  });
  await page.goto("/");
  return requests;
}

test("reads and explicitly refreshes a synthetic authenticated financial picture", async ({ page }) => {
  const refreshed = { ...freshPicture, snapshot_id: "synthetic-snapshot-2" };
  const requests = await openDashboard(page, freshPicture, refreshed);

  await expect(page.getByRole("heading", { name: "Portfolio and savings" })).toBeVisible();
  await expect(page.getByText("540.00 EUR")).toBeVisible();
  await expect(page.getByRole("progressbar", { name: "Emergency reserve target progress" })).toHaveAttribute("aria-valuenow", "1500.00");
  await expect(page.getByRole("progressbar", { name: "Emergency reserve target progress" })).toHaveAttribute("aria-valuemax", "2000.00");
  await expect.poll(() => requests.read).toBe(1);
  expect(requests.refresh).toBe(0);
  expect(requests.tokens).toEqual(["Bearer synthetic-owner-token"]);

  await page.getByRole("button", { name: "Refresh financial picture" }).click();

  await expect(page.getByRole("status").filter({ hasText: "Financial picture refreshed from your recorded ledger facts." })).toBeVisible();
  await expect.poll(() => requests.refresh).toBe(1);
  expect(requests.tokens).toEqual([
    "Bearer synthetic-owner-token",
    "Bearer synthetic-owner-token",
  ]);
});

test("supports keyboard tabs, visible focus, and accessible portfolio content", async ({ page }) => {
  await openDashboard(page, freshPicture);
  const overview = page.getByRole("tab", { name: "Overview" });
  const portfolio = page.getByRole("tab", { name: "Portfolio" });

  await expect(overview).toBeVisible();
  await overview.focus();
  await overview.press("ArrowRight");

  await expect(portfolio).toBeFocused();
  await expect(portfolio).toHaveAttribute("aria-selected", "true");
  await expect(portfolio).toHaveCSS("outline-width", "3px");
  await expect(page.getByRole("tabpanel", { name: "Portfolio" })).toContainText("2.5000 units");
});

test("keeps stale and incomplete non-EUR information explicit", async ({ page }) => {
  const incomplete = {
    ...freshPicture,
    cash_by_currency: {
      accounts: [
        ...freshPicture.cash_by_currency.accounts,
        {
          account_id: "synthetic-reserve",
          amount: "80.00",
          currency: "USD",
          evidence_event_ids: ["synthetic-event-usd"],
        },
      ],
      owner: { EUR: "540.00", USD: "80.00" },
    },
    completeness: { diagnostic_count: 1, status: "incomplete" as const },
    diagnostics: [
      {
        account_id: "synthetic-reserve",
        code: "SNAPSHOT_RESERVE_NON_EUR_BALANCE",
        evidence_event_ids: ["synthetic-event-usd"],
      },
    ],
    freshness: { status: "stale" as const },
    reserve_progress: {
      available_eur_balance: "1500.00",
      configured_target_eur: "2000.00",
      status: "incomplete" as const,
    },
    state: "incomplete",
  };
  await openDashboard(page, incomplete);

  await expect(page.getByText("80.00 USD")).toBeVisible();
  await expect(page.getByText("New ledger inputs exist. Refresh explicitly to produce a current snapshot.")).toBeVisible();
  await expect(page.getByText("Progress is incomplete because some reserve evidence cannot be represented in EUR.")).toBeVisible();
  await expect(page.getByRole("list", { name: "Accounting diagnostics" })).toContainText("non-EUR reserve balance cannot be included in EUR reserve progress");
});

test("renders the empty snapshot state", async ({ page }) => {
  await openDashboard(page, undefined);

  await expect(page.getByRole("heading", { name: "No financial snapshot yet" })).toBeVisible();
});

test("renders an API failure without exposing financial details", async ({ page }) => {
  await authenticate(page);
  await page.route(`${apiUrl}/v1/financial-picture`, (route) => route.fulfill({ status: 500 }));
  await page.goto("/");

  const unavailableNotice = page
    .locator('[role="status"]')
    .filter({ has: page.getByRole("heading", { name: "Financial picture unavailable" }) });
  await expect(unavailableNotice).toBeVisible();
  await expect(unavailableNotice.getByText("We could not load your latest financial picture. Try again when the PIA API is available.")).toBeVisible();
});
