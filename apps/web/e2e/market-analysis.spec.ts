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

const source = {
  provider: "synthetic-eod",
  provider_symbol: "SYNX.XMAD",
  mic: "XMAD",
  quote_currency: "EUR",
  attribution: "Market data: synthetic-eod",
  source_urls: ["https://data.example.test/v1/eod?symbol=SYNX.XMAD"],
  provider_as_of: "2026-08-28T23:00:00Z",
  retrieved_at: "2026-08-29T06:00:00Z",
};

const bars = [
  {
    market_date: "2026-08-27",
    open: "100.100000000000",
    high: "104.200000000000",
    low: "99.500000000000",
    close: "103.300000000000",
    volume: 1200,
    revision: 1,
    provider_as_of: source.provider_as_of,
    retrieved_at: source.retrieved_at,
    source_url: source.source_urls[0],
    completeness_status: "complete",
    corrected: false,
  },
  {
    market_date: "2026-08-28",
    open: "103.300000000000",
    high: "106.000000000000",
    low: "102.900000000000",
    close: "105.500000000000",
    volume: 1500,
    revision: 1,
    provider_as_of: source.provider_as_of,
    retrieved_at: source.retrieved_at,
    source_url: source.source_urls[0],
    completeness_status: "complete",
    corrected: false,
  },
];

function item(sourceKind: "portfolio" | "watchlist", name: string, isin: string) {
  const indicatorBase = {
    status: "available",
    observation_count: 20,
    required_observations: 20,
    window_start: "2026-08-01",
    window_end: "2026-08-28",
    provider_as_of: source.provider_as_of,
    retrieved_at: source.retrieved_at,
    source_urls: source.source_urls,
    freshness_status: "fresh",
    completeness_status: "complete",
    corrected: false,
    diagnostics: [],
  };
  return {
    source_kind: sourceKind,
    source_instrument_id: isin,
    state: "ready",
    instrument: {
      instrument_id: `synthetic-${sourceKind}`,
      isin,
      share_class_figi: null,
      instrument_kind: sourceKind === "portfolio" ? "common_stock" : "etf",
      display_name: name,
      mic: "XMAD",
      quote_currency: "EUR",
      provider: "synthetic-eod",
      provider_symbol: "SYNX.XMAD",
    },
    bars,
    indicators: [
      { ...indicatorBase, code: "sma_20", market_date: "2026-08-27", value: "98.400000000000" },
      { ...indicatorBase, code: "sma_20", market_date: "2026-08-28", value: "99.100000000000" },
      { ...indicatorBase, code: "sma_50", market_date: "2026-08-28", value: null, status: "insufficient_history", observation_count: 2, required_observations: 50 },
      { ...indicatorBase, code: "sma_200", market_date: "2026-08-28", value: null, status: "insufficient_history", observation_count: 2, required_observations: 200 },
      { ...indicatorBase, code: "rsi_14", market_date: "2026-08-27", value: "52.250000000000", required_observations: 15 },
      { ...indicatorBase, code: "rsi_14", market_date: "2026-08-28", value: "57.750000000000", required_observations: 15 },
    ],
    source,
    freshness: { status: "fresh" },
    completeness: { status: "complete" },
    position: sourceKind === "portfolio" ? { quantity: "2.000000000000", evidence_event_ids: ["synthetic-buy"], snapshot_id: "synthetic-snapshot", snapshot_as_of: "2026-08-28T12:00:00Z", snapshot_refreshed_at: "2026-08-28T13:00:00Z", snapshot_input_fingerprint: "b".repeat(64) } : null,
    valuation: sourceKind === "portfolio" ? { status: "available", quote_currency: "EUR", current_price: "105.500000000000", current_value: "211.000000000000", total_basis: "180.000000000000", unrealized_gain: "31.000000000000", unrealized_return_percent: "17.222222222222", evidence_event_ids: ["synthetic-buy"] } : null,
    diagnostics: [],
  };
}

async function authenticate(page: Page) {
  await page.addInitScript(
    ({ key, session }) => window.localStorage.setItem(key, JSON.stringify(session)),
    { key: authStorageKey, session: authenticatedSession },
  );
}

async function openAnalysis(page: Page) {
  const tokens: string[] = [];
  await authenticate(page);
  await page.route(`${apiUrl}/v1/financial-picture`, (route) => route.fulfill({ status: 404 }));
  await page.route(`${apiUrl}/v1/market/analysis`, async (route) => {
    tokens.push(route.request().headers().authorization ?? "");
    await route.fulfill({
      body: JSON.stringify({
        state: "ready",
        items: [
          item("portfolio", "Synthetic Equity", "US0000000002"),
          item("watchlist", "Synthetic Watchlist Fund", "US0000000010"),
        ],
      }),
      contentType: "application/json",
      headers: { "access-control-allow-origin": "*" },
    });
  });
  await page.goto("/");
  return tokens;
}

test("renders authenticated synthetic market charts and an accessible data alternative", async ({ page }) => {
  const tokens = await openAnalysis(page);

  await expect(page.getByRole("heading", { name: "Market analysis", exact: true })).toBeVisible();
  await expect(page.getByRole("img", { name: /Synthetic Equity daily price chart/ })).toBeVisible();
  await expect(page.getByRole("img", { name: /Synthetic Equity RSI-14 chart/ })).toBeVisible();
  await expect(page.getByText("Market data: synthetic-eod")).toBeVisible();
  await expect(page.getByRole("link", { name: "Charts by TradingView" })).toBeVisible();
  await expect.poll(() => tokens).toEqual(["Bearer synthetic-owner-token"]);

  const tableToggle = page.getByRole("button", { name: "Show chart data for Synthetic Equity" });
  await tableToggle.click();
  await expect(page.getByRole("table", { name: "Synthetic Equity daily market data" })).toContainText("105.500000000000");
  await expect(page.getByRole("table", { name: "Synthetic Equity daily market data" })).toContainText("Insufficient history (2/50)");
});

test("supports keyboard view switching with visible focus on mobile and desktop", async ({ page }) => {
  await openAnalysis(page);
  const positions = page.getByRole("tab", { name: "Positions" });
  const watchlist = page.getByRole("tab", { name: "Watchlist" });

  await positions.focus();
  await positions.press("ArrowRight");

  await expect(watchlist).toBeFocused();
  await expect(watchlist).toHaveCSS("outline-width", "3px");
  await expect(watchlist).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("tabpanel", { name: "Watchlist" })).toContainText("Synthetic Watchlist Fund");
  await expect(page.getByRole("status").filter({ hasText: "Showing 1 watchlist instrument" })).toBeAttached();
});
