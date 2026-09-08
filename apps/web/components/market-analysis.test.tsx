import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { MarketAnalysis } from "@/components/market-analysis";
import { getSupabaseBrowserClient } from "@/lib/supabase-browser";

const chartSeries: { definition: object; setData: ReturnType<typeof vi.fn> }[] = [];
const removeChart = vi.fn();

vi.mock("lightweight-charts", () => ({
  CandlestickSeries: { kind: "candlestick" },
  ColorType: { Solid: "solid" },
  LineSeries: { kind: "line" },
  createChart: vi.fn(() => ({
    addSeries: vi.fn((definition: object) => {
      const series = { definition, setData: vi.fn() };
      chartSeries.push(series);
      return series;
    }),
    applyOptions: vi.fn(),
    remove: removeChart,
    timeScale: () => ({ fitContent: vi.fn() }),
  })),
}));

vi.mock("@/lib/supabase-browser", () => ({ getSupabaseBrowserClient: vi.fn() }));

const getClient = vi.mocked(getSupabaseBrowserClient);
const fetchMock = vi.fn();

const bars = [
  {
    market_date: "2026-08-27",
    open: "100.100000000000",
    high: "104.200000000000",
    low: "99.500000000000",
    close: "103.300000000000",
    volume: 1200,
    revision: 1,
    provider_as_of: "2026-08-27T23:00:00Z",
    retrieved_at: "2026-08-28T06:00:00Z",
    source_url: "https://data.example.test/v1/eod?symbol=SYNX.XMAD",
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
    provider_as_of: "2026-08-28T23:00:00Z",
    retrieved_at: "2026-08-29T06:00:00Z",
    source_url: "https://data.example.test/v1/eod?symbol=SYNX.XMAD",
    completeness_status: "complete",
    corrected: false,
  },
];

const indicatorBase = {
  status: "available",
  observation_count: 20,
  required_observations: 20,
  window_start: "2026-08-01",
  window_end: "2026-08-28",
  provider_as_of: "2026-08-28T23:00:00Z",
  retrieved_at: "2026-08-29T06:00:00Z",
  source_urls: ["https://data.example.test/v1/eod?symbol=SYNX.XMAD"],
  freshness_status: "fresh",
  completeness_status: "complete",
  corrected: false,
  diagnostics: [],
};

const readyItem = {
  source_kind: "portfolio",
  source_instrument_id: "US0000000002",
  state: "ready",
  instrument: {
    instrument_id: "synthetic-instrument-1",
    isin: "US0000000002",
    share_class_figi: "BBG000000001",
    instrument_kind: "common_stock",
    display_name: "Synthetic Equity",
    mic: "XMAD",
    quote_currency: "EUR",
    provider: "synthetic-eod",
    provider_symbol: "SYNX.XMAD",
  },
  bars,
  indicators: [
    { ...indicatorBase, code: "sma_20", market_date: "2026-08-27", value: "98.400000000000", window_end: "2026-08-27" },
    { ...indicatorBase, code: "sma_20", market_date: "2026-08-28", value: "99.100000000000" },
    { ...indicatorBase, code: "sma_50", market_date: "2026-08-28", value: null, status: "insufficient_history", observation_count: 2, required_observations: 50 },
    { ...indicatorBase, code: "sma_200", market_date: "2026-08-28", value: null, status: "insufficient_history", observation_count: 2, required_observations: 200 },
    { ...indicatorBase, code: "rsi_14", market_date: "2026-08-27", value: "52.250000000000", required_observations: 15 },
    { ...indicatorBase, code: "rsi_14", market_date: "2026-08-28", value: "57.750000000000", required_observations: 15 },
  ],
  source: {
    provider: "synthetic-eod",
    provider_symbol: "SYNX.XMAD",
    mic: "XMAD",
    quote_currency: "EUR",
    attribution: "Market data: synthetic-eod",
    source_urls: ["https://data.example.test/v1/eod?symbol=SYNX.XMAD"],
    provider_as_of: "2026-08-28T23:00:00Z",
    retrieved_at: "2026-08-29T06:00:00Z",
  },
  freshness: { status: "fresh" },
  completeness: { status: "complete" },
  position: {
    quantity: "2.000000000000",
    evidence_event_ids: ["synthetic-buy-1"],
    snapshot_id: "synthetic-snapshot-1",
    snapshot_as_of: "2026-08-28T12:00:00Z",
    snapshot_refreshed_at: "2026-08-28T13:00:00Z",
    snapshot_input_fingerprint: "b".repeat(64),
  },
  valuation: {
    status: "available",
    quote_currency: "EUR",
    current_price: "105.500000000000",
    current_value: "211.000000000000",
    total_basis: "180.000000000000",
    unrealized_gain: "31.000000000000",
    unrealized_return_percent: "17.222222222222",
    evidence_event_ids: ["synthetic-buy-1"],
  },
  diagnostics: [],
};

function signedInClient() {
  return { auth: { getSession: vi.fn().mockResolvedValue({ data: { session: { access_token: "owner-token" } } }) } };
}

function collection(items: object[]) {
  return { state: items.length ? "ready" : "empty", items };
}

afterEach(() => {
  cleanup();
  getClient.mockReset();
  fetchMock.mockReset();
  chartSeries.length = 0;
  removeChart.mockReset();
  vi.unstubAllGlobals();
});

describe("MarketAnalysis", () => {
  it("requires authentication and never calls a market provider from the browser", async () => {
    getClient.mockReturnValue(null);
    render(<MarketAnalysis />);

    expect(await screen.findByText("Sign in to view market analysis")).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("maps API OHLC and indicator series directly into separate price and RSI charts", async () => {
    getClient.mockReturnValue(signedInClient() as never);
    fetchMock.mockResolvedValue(new Response(JSON.stringify(collection([readyItem]))));
    vi.stubGlobal("fetch", fetchMock);
    render(<MarketAnalysis />);

    expect(await screen.findByRole("heading", { name: "Synthetic Equity" })).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith("http://localhost:8000/v1/market/analysis", {
      headers: { Authorization: "Bearer owner-token" },
    });
    await waitFor(() => expect(chartSeries).toHaveLength(5));
    expect(chartSeries[0].setData).toHaveBeenCalledWith([
      { time: "2026-08-27", open: 100.1, high: 104.2, low: 99.5, close: 103.3 },
      { time: "2026-08-28", open: 103.3, high: 106, low: 102.9, close: 105.5 },
    ]);
    expect(chartSeries[1].setData).toHaveBeenCalledWith([
      { time: "2026-08-27", value: 98.4 },
      { time: "2026-08-28", value: 99.1 },
    ]);
    expect(chartSeries[2].setData).toHaveBeenCalledWith([]);
    expect(chartSeries[3].setData).toHaveBeenCalledWith([]);
    expect(chartSeries[4].setData).toHaveBeenCalledWith([
      { time: "2026-08-27", value: 52.25 },
      { time: "2026-08-28", value: 57.75 },
    ]);
  });

  it("shows native quote currency, server valuation, freshness, source, as-of time, and both attributions", async () => {
    getClient.mockReturnValue(signedInClient() as never);
    fetchMock.mockResolvedValue(new Response(JSON.stringify(collection([readyItem]))));
    vi.stubGlobal("fetch", fetchMock);
    render(<MarketAnalysis />);

    expect(await screen.findByText("211.000000000000 EUR")).toBeInTheDocument();
    expect(screen.getByText("17.222222222222%")).toBeInTheDocument();
    expect(screen.getByText("Fresh · Complete")).toBeInTheDocument();
    expect(screen.getByText("Market data: synthetic-eod")).toBeInTheDocument();
    expect(screen.getByText(/28 Aug 2026/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "View market data source" })).toHaveAttribute("href", bars[0].source_url);
    expect(screen.getByRole("link", { name: "Charts by TradingView" })).toHaveAttribute("href", "https://www.tradingview.com/");
    expect(screen.getByText("Price analysis is evidence, not advice or a guarantee of returns.")).toBeInTheDocument();
  });

  it("offers an accessible table containing API values and insufficient-history states", async () => {
    getClient.mockReturnValue(signedInClient() as never);
    fetchMock.mockResolvedValue(new Response(JSON.stringify(collection([readyItem]))));
    vi.stubGlobal("fetch", fetchMock);
    render(<MarketAnalysis />);

    const toggle = await screen.findByRole("button", { name: "Show chart data for Synthetic Equity" });
    fireEvent.click(toggle);

    const table = screen.getByRole("table", { name: "Synthetic Equity daily market data" });
    expect(table).toHaveTextContent("2026-08-28");
    expect(table).toHaveTextContent("105.500000000000");
    expect(table).toHaveTextContent("99.100000000000");
    expect(table).toHaveTextContent("Insufficient history (2/50)");
    expect(table).toHaveTextContent("57.750000000000");
  });

  it("explains pending freshness and unavailable native-currency valuation without estimating it", async () => {
    const pending = {
      ...readyItem,
      freshness: { status: "pending" },
      valuation: {
        ...readyItem.valuation,
        status: "currency_mismatch",
        current_price: null,
        current_value: null,
        total_basis: null,
        unrealized_gain: null,
        unrealized_return_percent: null,
      },
    };
    getClient.mockReturnValue(signedInClient() as never);
    fetchMock.mockResolvedValue(new Response(JSON.stringify(collection([pending]))));
    vi.stubGlobal("fetch", fetchMock);
    render(<MarketAnalysis />);

    expect(await screen.findByText("Latest EOD price is pending")).toBeInTheDocument();
    expect(screen.getByText("Native valuation unavailable")).toBeInTheDocument();
    expect(screen.getByText("Position basis and quote currency do not match, so PIA does not infer a converted value.")).toBeInTheDocument();
  });

  it("supports keyboard navigation between position and watchlist views", async () => {
    const watchlistItem = { ...readyItem, source_kind: "watchlist", source_instrument_id: "US0000000010", instrument: { ...readyItem.instrument, instrument_id: "synthetic-instrument-2", isin: "US0000000010", display_name: "Synthetic Watchlist Fund" }, position: null, valuation: null };
    getClient.mockReturnValue(signedInClient() as never);
    fetchMock.mockResolvedValue(new Response(JSON.stringify(collection([readyItem, watchlistItem]))));
    vi.stubGlobal("fetch", fetchMock);
    render(<MarketAnalysis />);

    const positions = await screen.findByRole("tab", { name: "Positions" });
    fireEvent.keyDown(positions, { key: "ArrowRight" });

    expect(screen.getByRole("tab", { name: "Watchlist" })).toHaveFocus();
    expect(screen.getByRole("tabpanel", { name: "Watchlist" })).toHaveTextContent("Synthetic Watchlist Fund");
  });

  it.each([
    ["stale", "Price history is stale"],
    ["incomplete", "Price history is incomplete"],
    ["unavailable", "Price history is unavailable"],
    ["unsupported", "Instrument is unsupported"],
    ["provider_disabled", "Market-data provider is disabled"],
    ["license_review_required", "Provider license review is required"],
  ])("renders the %s item state distinctly", async (state, expected) => {
    const item = {
      ...readyItem,
      state,
      bars: ["stale", "incomplete"].includes(state) ? readyItem.bars : [],
      indicators: ["stale", "incomplete"].includes(state) ? readyItem.indicators : [],
      source: ["stale", "incomplete"].includes(state) ? readyItem.source : null,
      freshness: { status: state === "stale" ? "stale" : "unavailable" },
      completeness: { status: state === "incomplete" ? "incomplete" : state === "stale" ? "complete" : "unavailable" },
      valuation: null,
    };
    getClient.mockReturnValue(signedInClient() as never);
    fetchMock.mockResolvedValue(new Response(JSON.stringify(collection([item]))));
    vi.stubGlobal("fetch", fetchMock);
    render(<MarketAnalysis />);

    expect(await screen.findByText(expected)).toBeInTheDocument();
  });

  it("announces loading, empty, and API error states", async () => {
    getClient.mockReturnValue(signedInClient() as never);
    let resolveRequest: (response: Response) => void;
    fetchMock.mockReturnValueOnce(new Promise<Response>((resolve) => { resolveRequest = resolve; }));
    vi.stubGlobal("fetch", fetchMock);
    const view = render(<MarketAnalysis />);

    expect(await screen.findByText("Loading market analysis")).toBeInTheDocument();
    resolveRequest!(new Response(JSON.stringify(collection([]))));
    expect(await screen.findByText("No supported positions or watchlist instruments")).toBeInTheDocument();

    view.unmount();
    fetchMock.mockRejectedValueOnce(new TypeError("Network unavailable"));
    render(<MarketAnalysis />);
    expect(await screen.findByText("Market analysis unavailable")).toBeInTheDocument();
  });
});
