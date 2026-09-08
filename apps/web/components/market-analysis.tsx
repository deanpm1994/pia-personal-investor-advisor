"use client";

import {
  CandlestickSeries,
  ColorType,
  LineSeries,
  createChart,
  type CandlestickData,
  type LineData,
  type Time,
} from "lightweight-charts";
import { useEffect, useMemo, useRef, useState } from "react";

import { getSupabaseBrowserClient } from "@/lib/supabase-browser";

type AnalysisState =
  | "ready"
  | "incomplete"
  | "stale"
  | "unavailable"
  | "unsupported"
  | "provider_disabled"
  | "license_review_required";
type IndicatorCode = "sma_20" | "sma_50" | "sma_200" | "rsi_14";
type MarketView = "positions" | "watchlist";
type LoadState = "checking" | "loading" | "unauthenticated" | "ready" | "empty" | "error";

type DailyBar = {
  market_date: string;
  open: string;
  high: string;
  low: string;
  close: string;
  volume: number | null;
  revision: number;
  provider_as_of: string;
  retrieved_at: string;
  source_url: string;
  completeness_status: "complete" | "incomplete";
  corrected: boolean;
};

type Indicator = {
  code: IndicatorCode;
  market_date: string;
  value: string | null;
  status: "available" | "insufficient_history";
  observation_count: number;
  required_observations: number;
  window_start: string;
  window_end: string;
  provider_as_of: string;
  retrieved_at: string;
  source_urls: string[];
  freshness_status: "fresh" | "pending" | "stale" | "unavailable";
  completeness_status: "complete" | "incomplete" | "unavailable";
  corrected: boolean;
  diagnostics: { code: string; market_date?: string }[];
};

type Instrument = {
  instrument_id: string;
  isin: string;
  share_class_figi: string | null;
  instrument_kind: string;
  display_name: string;
  mic: string;
  quote_currency: string;
  provider: string;
  provider_symbol: string;
};

type MarketSource = {
  provider: string;
  provider_symbol: string;
  mic: string;
  quote_currency: string;
  attribution: string;
  source_urls: string[];
  provider_as_of: string;
  retrieved_at: string;
};

type Valuation = {
  status: "available" | "no_position" | "market_data_unavailable" | "basis_unavailable" | "currency_mismatch" | "quantity_mismatch";
  quote_currency: string;
  current_price: string | null;
  current_value: string | null;
  total_basis: string | null;
  unrealized_gain: string | null;
  unrealized_return_percent: string | null;
  evidence_event_ids: string[];
};

type AnalysisItem = {
  source_kind: "watchlist" | "portfolio" | "portfolio_and_watchlist";
  source_instrument_id: string;
  state: AnalysisState;
  instrument: Instrument | null;
  bars: DailyBar[];
  indicators: Indicator[];
  source: MarketSource | null;
  freshness: { status: "fresh" | "pending" | "stale" | "unavailable" };
  completeness: { status: "complete" | "incomplete" | "unavailable" };
  position: { quantity: string } | null;
  valuation: Valuation | null;
  diagnostics: { code: string; market_date?: string; evidence_event_ids: string[] }[];
};

type AnalysisCollection = { state: "ready" | "empty"; items: AnalysisItem[] };

const apiUrl = process.env.NEXT_PUBLIC_PIA_API_URL ?? "http://localhost:8000";

const stateCopy: Record<AnalysisState, { title: string; description: string }> = {
  ready: { title: "Price history is ready", description: "Daily price and indicator evidence is available." },
  incomplete: { title: "Price history is incomplete", description: "Some expected observations are missing. Values shown are not estimated." },
  stale: { title: "Price history is stale", description: "The latest stored market date is behind the expected EOD schedule." },
  unavailable: { title: "Price history is unavailable", description: "No valid stored daily price history is available for this instrument." },
  unsupported: { title: "Instrument is unsupported", description: "PIA cannot map this instrument to one supported listing without guessing." },
  provider_disabled: { title: "Market-data provider is disabled", description: "Market analysis stays unavailable while provider access is disabled." },
  license_review_required: { title: "Provider license review is required", description: "Stored provider content is hidden until the required licensing review is complete." },
};

const valuationCopy: Record<Exclude<Valuation["status"], "available">, string> = {
  no_position: "No recorded position is available for native-currency valuation.",
  market_data_unavailable: "No valid stored EOD close is available, so PIA does not estimate a value.",
  basis_unavailable: "Recorded cost-basis evidence is unavailable, so PIA does not estimate performance.",
  currency_mismatch: "Position basis and quote currency do not match, so PIA does not infer a converted value.",
  quantity_mismatch: "Position quantity and cost-basis lots do not reconcile, so PIA does not estimate performance.",
};

function titleCase(value: string) {
  return value.charAt(0).toUpperCase() + value.slice(1).replaceAll("_", " ");
}

function displayTime(value: string) {
  return new Intl.DateTimeFormat("en-GB", {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: "UTC",
  }).format(new Date(value));
}

function chartNumber(value: string) {
  // Lightweight Charts requires number coordinates. Exact API Decimal strings remain the displayed facts; no client-side financial arithmetic is performed.
  return Number(value);
}

function indicatorSeries(indicators: Indicator[], code: IndicatorCode): LineData<Time>[] {
  return indicators
    .filter((indicator) => indicator.code === code && indicator.status === "available" && indicator.value !== null)
    .map((indicator) => ({ time: indicator.market_date, value: chartNumber(indicator.value as string) }));
}

export function MarketAnalysis() {
  const [loadState, setLoadState] = useState<LoadState>("checking");
  const [items, setItems] = useState<AnalysisItem[]>([]);
  const [view, setView] = useState<MarketView>("positions");

  useEffect(() => {
    let active = true;
    async function load() {
      try {
        const session = await getSupabaseBrowserClient()?.auth.getSession();
        if (!active) return;
        const token = session?.data.session?.access_token;
        if (!token) {
          setLoadState("unauthenticated");
          return;
        }
        setLoadState("loading");
        const response = await fetch(`${apiUrl}/v1/market/analysis`, {
          headers: { Authorization: `Bearer ${token}` },
        });
        if (!response.ok) throw new Error("Market analysis read failed");
        const collection = (await response.json()) as AnalysisCollection;
        if (!active) return;
        setItems(collection.items);
        setLoadState(collection.state === "empty" || collection.items.length === 0 ? "empty" : "ready");
      } catch {
        if (active) setLoadState("error");
      }
    }
    void load();
    return () => {
      active = false;
    };
  }, []);

  const visibleItems = useMemo(
    () => items.filter((item) => view === "positions"
      ? item.source_kind === "portfolio" || item.source_kind === "portfolio_and_watchlist"
      : item.source_kind === "watchlist" || item.source_kind === "portfolio_and_watchlist"),
    [items, view],
  );

  return (
    <section aria-labelledby="market-analysis-heading" className="mt-6 space-y-5 sm:mt-8">
      <div className="rounded-panel border border-border bg-surface p-5 shadow-panel sm:p-7">
        <p className="text-sm font-semibold tracking-wide text-brand">DAILY EOD EVIDENCE</p>
        <h2 className="mt-2 text-2xl font-semibold tracking-tight text-ink sm:text-3xl" id="market-analysis-heading">
          Market analysis
        </h2>
        <p className="mt-2 max-w-3xl text-sm leading-6 text-ink-muted">
          Price analysis is evidence, not advice or a guarantee of returns.
        </p>
        <p className="mt-1 max-w-3xl text-sm leading-6 text-ink-muted">
          Values remain in each listing&apos;s native quote currency.
        </p>
      </div>

      {loadState === "checking" || loadState === "loading" ? (
        <AnalysisNotice title="Loading market analysis" description="Reading stored owner-only EOD evidence. No provider request is made from this page." />
      ) : loadState === "unauthenticated" ? (
        <AnalysisNotice title="Sign in to view market analysis" description="Position and private-watchlist evidence is available only to the authenticated owner." />
      ) : loadState === "empty" ? (
        <AnalysisNotice title="No supported positions or watchlist instruments" description="Market analysis will appear after an eligible instrument has stored EOD evidence." />
      ) : loadState === "error" ? (
        <AnalysisNotice title="Market analysis unavailable" description="PIA could not read stored market evidence. Accounting data is unaffected; try again when the API is available." />
      ) : (
        <>
          <div aria-label="Market analysis views" className="flex gap-2 border-b border-border" role="tablist">
            {(["positions", "watchlist"] as const).map((item) => (
              <button
                aria-controls={`market-${item}-panel`}
                aria-selected={view === item}
                className={`rounded-t-lg px-4 py-2 text-sm font-semibold ${view === item ? "bg-brand text-white" : "text-ink-muted"}`}
                id={`market-${item}-tab`}
                key={item}
                onClick={() => setView(item)}
                onKeyDown={(event) => {
                  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
                  event.preventDefault();
                  const nextView = event.key === "Home" ? "positions" : event.key === "End" ? "watchlist" : item === "positions" ? "watchlist" : "positions";
                  setView(nextView);
                  document.getElementById(`market-${nextView}-tab`)?.focus();
                }}
                role="tab"
                tabIndex={view === item ? 0 : -1}
                type="button"
              >
                {item === "positions" ? "Positions" : "Watchlist"}
              </button>
            ))}
          </div>
          <p aria-live="polite" className="sr-only" role="status">
            Showing {visibleItems.length} {view === "positions" ? `position${visibleItems.length === 1 ? "" : "s"}` : `watchlist instrument${visibleItems.length === 1 ? "" : "s"}`}
          </p>
          <div aria-labelledby={`market-${view}-tab`} className="space-y-5" id={`market-${view}-panel`} role="tabpanel">
            {visibleItems.length ? visibleItems.map((item) => (
              <AnalysisCard item={item} key={`${view}-${item.source_instrument_id}`} />
            )) : (
              <AnalysisNotice
                title={view === "positions" ? "No supported positions" : "No supported watchlist instruments"}
                description="No instrument in this view currently has an eligible market-analysis record."
              />
            )}
          </div>
        </>
      )}
    </section>
  );
}

function AnalysisNotice({ title, description }: { title: string; description: string }) {
  return (
    <div className="rounded-panel border border-border bg-surface p-6" role="status">
      <h3 className="text-lg font-semibold text-ink">{title}</h3>
      <p className="mt-2 max-w-2xl text-sm leading-6 text-ink-muted">{description}</p>
    </div>
  );
}

function AnalysisCard({ item }: { item: AnalysisItem }) {
  const [showTable, setShowTable] = useState(false);
  const name = item.instrument?.display_name ?? item.source_instrument_id;
  const canChart = item.bars.length > 0 && item.source !== null && ["ready", "stale", "incomplete"].includes(item.state);
  const status = stateCopy[item.state];
  const pending = item.state === "ready" && item.freshness.status === "pending";

  return (
    <article className="overflow-hidden rounded-panel border border-border bg-surface shadow-panel" aria-labelledby={`analysis-${item.source_instrument_id}`}>
      <div className="flex flex-col gap-4 border-b border-border p-5 sm:flex-row sm:items-start sm:justify-between sm:p-6">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-xl font-semibold text-ink" id={`analysis-${item.source_instrument_id}`}>{name}</h3>
            <span className="rounded-full bg-surface-muted px-2.5 py-1 text-xs font-semibold text-ink-muted">
              {item.instrument?.instrument_kind.replaceAll("_", " ") ?? "unmapped"}
            </span>
          </div>
          <p className="mt-1 text-sm text-ink-muted">
            {item.source_instrument_id}{item.instrument ? ` · ${item.instrument.mic} · ${item.instrument.quote_currency}` : ""}
          </p>
        </div>
        {item.position && item.valuation && <ValuationSummary valuation={item.valuation} />}
      </div>

      {(item.state !== "ready" || pending) && (
        <div className="border-b border-border bg-surface-muted px-5 py-4 sm:px-6" role="status">
          <p className="font-semibold text-ink">{pending ? "Latest EOD price is pending" : status.title}</p>
          <p className="mt-1 text-sm leading-6 text-ink-muted">
            {pending ? "The provider has not supplied the target market date yet. The actual stored market date remains visible." : status.description}
          </p>
        </div>
      )}

      {canChart ? (
        <div className="p-5 sm:p-6">
          <MarketCharts bars={item.bars} idBase={item.source_instrument_id} indicators={item.indicators} name={name} />
          <button
            aria-expanded={showTable}
            className="mt-4 rounded-lg border border-border px-3 py-2 text-sm font-semibold text-brand hover:bg-surface-muted"
            onClick={() => setShowTable((current) => !current)}
            type="button"
          >
            {showTable ? "Hide" : "Show"} chart data for {name}
          </button>
          {showTable && <AnalysisTable bars={item.bars} indicators={item.indicators} name={name} />}
        </div>
      ) : item.state === "ready" ? (
        <div className="px-5 py-4 text-sm text-ink-muted sm:px-6" role="status">Valid chart series are unavailable.</div>
      ) : null}

      {item.source && <SourceEvidence item={item} source={item.source} />}
    </article>
  );
}

function ValuationSummary({ valuation }: { valuation: Valuation }) {
  if (valuation.status !== "available") {
    return (
      <div className="max-w-sm text-sm sm:text-right">
        <p className="font-semibold text-ink">Native valuation unavailable</p>
        <p className="mt-1 leading-5 text-ink-muted">{valuationCopy[valuation.status]}</p>
      </div>
    );
  }
  return (
    <dl className="grid grid-cols-2 gap-x-5 gap-y-2 text-right text-sm">
      <div><dt className="text-ink-muted">Market value</dt><dd className="font-semibold text-ink">{valuation.current_value} {valuation.quote_currency}</dd></div>
      <div><dt className="text-ink-muted">Unrealized return</dt><dd className="font-semibold text-ink">{valuation.unrealized_return_percent}%</dd></div>
    </dl>
  );
}

function MarketCharts({ bars, idBase, indicators, name }: { bars: DailyBar[]; idBase: string; indicators: Indicator[]; name: string }) {
  const priceContainer = useRef<HTMLDivElement>(null);
  const rsiContainer = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!priceContainer.current || !rsiContainer.current) return;
    const sharedOptions = {
      layout: { background: { type: ColorType.Solid, color: "#ffffff" }, textColor: "#5f7178" },
      grid: { vertLines: { color: "#eaf1f1" }, horzLines: { color: "#eaf1f1" } },
      rightPriceScale: { borderColor: "#d4e0e0" },
      timeScale: { borderColor: "#d4e0e0", timeVisible: false },
    };
    const priceChart = createChart(priceContainer.current, { ...sharedOptions, height: 320, width: priceContainer.current.clientWidth || 720 });
    const candles = priceChart.addSeries(CandlestickSeries, {
      upColor: "#0f766e", downColor: "#b45309", borderVisible: false, wickUpColor: "#0f766e", wickDownColor: "#b45309",
    });
    const sma20 = priceChart.addSeries(LineSeries, { color: "#2563eb", lineWidth: 2, title: "SMA 20" });
    const sma50 = priceChart.addSeries(LineSeries, { color: "#7c3aed", lineWidth: 2, title: "SMA 50" });
    const sma200 = priceChart.addSeries(LineSeries, { color: "#b45309", lineWidth: 2, title: "SMA 200" });
    candles.setData(bars.map((bar) => ({
      time: bar.market_date as Time,
      open: chartNumber(bar.open),
      high: chartNumber(bar.high),
      low: chartNumber(bar.low),
      close: chartNumber(bar.close),
    })) as CandlestickData<Time>[]);
    sma20.setData(indicatorSeries(indicators, "sma_20"));
    sma50.setData(indicatorSeries(indicators, "sma_50"));
    sma200.setData(indicatorSeries(indicators, "sma_200"));
    priceChart.timeScale().fitContent();

    const rsiChart = createChart(rsiContainer.current, { ...sharedOptions, height: 180, width: rsiContainer.current.clientWidth || 720 });
    const rsi = rsiChart.addSeries(LineSeries, { color: "#0f766e", lineWidth: 2, title: "RSI 14" });
    rsi.setData(indicatorSeries(indicators, "rsi_14"));
    rsiChart.timeScale().fitContent();

    const resize = () => {
      if (priceContainer.current) priceChart.applyOptions({ width: priceContainer.current.clientWidth });
      if (rsiContainer.current) rsiChart.applyOptions({ width: rsiContainer.current.clientWidth });
    };
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(resize);
    if (observer) {
      observer.observe(priceContainer.current);
      observer.observe(rsiContainer.current);
    }
    return () => {
      observer?.disconnect();
      priceChart.remove();
      rsiChart.remove();
    };
  }, [bars, indicators]);

  return (
    <div className="space-y-5">
      <section aria-labelledby={`price-chart-${idBase}`}>
        <h4 className="text-sm font-semibold text-ink" id={`price-chart-${idBase}`}>Daily price with SMA 20, 50, and 200</h4>
        <div aria-label={`${name} daily price chart. A data table is available below.`} className="mt-3 min-h-80 w-full overflow-hidden rounded-lg border border-border" ref={priceContainer} role="img" tabIndex={0} />
      </section>
      <section aria-labelledby={`rsi-chart-${idBase}`}>
        <h4 className="text-sm font-semibold text-ink" id={`rsi-chart-${idBase}`}>RSI 14</h4>
        <div aria-label={`${name} RSI-14 chart. A data table is available below.`} className="mt-3 min-h-45 w-full overflow-hidden rounded-lg border border-border" ref={rsiContainer} role="img" tabIndex={0} />
      </section>
    </div>
  );
}

function AnalysisTable({ bars, indicators, name }: { bars: DailyBar[]; indicators: Indicator[]; name: string }) {
  const byDate = new Map<string, Map<IndicatorCode, Indicator>>();
  for (const indicator of indicators) {
    const values = byDate.get(indicator.market_date) ?? new Map<IndicatorCode, Indicator>();
    values.set(indicator.code, indicator);
    byDate.set(indicator.market_date, values);
  }
  const value = (marketDate: string, code: IndicatorCode) => {
    const indicator = byDate.get(marketDate)?.get(code);
    if (!indicator) return "Unavailable";
    return indicator.value ?? `Insufficient history (${indicator.observation_count}/${indicator.required_observations})`;
  };
  return (
    <div className="mt-4 overflow-x-auto">
      <table aria-label={`${name} daily market data`} className="min-w-full border-collapse text-left text-sm">
        <thead><tr className="border-b border-border text-ink-muted"><th className="px-2 py-2">Market date</th><th className="px-2 py-2">Open</th><th className="px-2 py-2">High</th><th className="px-2 py-2">Low</th><th className="px-2 py-2">Close</th><th className="px-2 py-2">SMA 20</th><th className="px-2 py-2">SMA 50</th><th className="px-2 py-2">SMA 200</th><th className="px-2 py-2">RSI 14</th></tr></thead>
        <tbody>{bars.map((bar) => <tr className="border-b border-border/70 text-ink" key={bar.market_date}><th className="whitespace-nowrap px-2 py-2 font-medium">{bar.market_date}</th><td className="px-2 py-2">{bar.open}</td><td className="px-2 py-2">{bar.high}</td><td className="px-2 py-2">{bar.low}</td><td className="px-2 py-2">{bar.close}</td><td className="px-2 py-2">{value(bar.market_date, "sma_20")}</td><td className="px-2 py-2">{value(bar.market_date, "sma_50")}</td><td className="px-2 py-2">{value(bar.market_date, "sma_200")}</td><td className="px-2 py-2">{value(bar.market_date, "rsi_14")}</td></tr>)}</tbody>
      </table>
    </div>
  );
}

function SourceEvidence({ item, source }: { item: AnalysisItem; source: MarketSource }) {
  return (
    <footer className="border-t border-border bg-surface-muted px-5 py-4 text-sm sm:px-6">
      <div className="flex flex-wrap gap-x-6 gap-y-2">
        <span className="font-semibold text-ink">{source.attribution}</span>
        <span className="text-ink-muted">{titleCase(item.freshness.status)} · {titleCase(item.completeness.status)}</span>
        <span className="text-ink-muted">Market as of {displayTime(source.provider_as_of)} UTC</span>
        <span className="text-ink-muted">Retrieved {displayTime(source.retrieved_at)} UTC</span>
      </div>
      <div className="mt-3 flex flex-wrap gap-x-5 gap-y-2">
        {source.source_urls[0] && <a className="font-semibold text-brand underline" href={source.source_urls[0]} rel="noreferrer" target="_blank">View market data source</a>}
        <a className="font-semibold text-brand underline" href="https://www.tradingview.com/" rel="noreferrer" target="_blank">Charts by TradingView</a>
      </div>
    </footer>
  );
}
