"use client";

import { useEffect, useRef, useState } from "react";

import { getSupabaseBrowserClient } from "@/lib/supabase-browser";

type DashboardView = "overview" | "portfolio";
type DashboardState = "checking" | "unauthenticated" | "loading" | "ready" | "empty" | "error";

type AccountSummary = {
  account_id: string;
  archived_at: string | null;
  emergency_reserve_target_eur: string | null;
  name: string;
  role: "brokerage" | "cash" | "savings" | "emergency_reserve";
};

type CashBalance = {
  account_id: string;
  amount: string;
  currency: string;
  evidence_event_ids: string[];
};

type Position = {
  account_id?: string;
  evidence_event_ids: string[];
  instrument_id: string;
  quantity: string;
};

type OpenLot = {
  account_id: string;
  buy_event_id: string;
  evidence_event_ids: string[];
  fee_event_ids: string[];
  instrument_id: string;
  quantity: string;
  source_currency: string;
  total_basis: string;
};

type RealizedSale = {
  account_id: string;
  evidence_event_ids: string[];
  instrument_id: string;
  realized_gain: string;
  sale_event_id: string;
  source_currency: string;
};

type FinancialPicture = {
  as_of: string | null;
  account_summaries: AccountSummary[];
  cash_by_currency: { accounts: CashBalance[]; owner: Record<string, string> };
  completeness: { diagnostic_count: number; status: "complete" | "incomplete" };
  diagnostics: { account_id: string; code: string; evidence_event_ids: string[] }[];
  evidence_event_ids: string[];
  fifo: { open_lots: OpenLot[]; realized_sales: RealizedSale[] };
  freshness: { status: "fresh" | "stale" };
  positions: { accounts: Position[]; owner: Position[] };
  refreshed_at: string;
  reserve_progress: {
    available_eur_balance: string | null;
    configured_target_eur: string | null;
    status: "available" | "incomplete" | "unavailable";
  };
  snapshot_id: string;
  state: "ready" | "no_ledger_data" | "incomplete" | "stale";
};

const apiUrl = process.env.NEXT_PUBLIC_PIA_API_URL ?? "http://localhost:8000";

function displayAmount(amount: string, currency: string) {
  return `${amount} ${currency}`;
}

function displayTime(value: string | null) {
  if (!value) return "No ledger activity yet";
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(value));
}

function accountRoleLabel(role: AccountSummary["role"]) {
  return role.replaceAll("_", " ");
}

function plural(count: number, singular: string, pluralForm = `${singular}s`) {
  return count === 1 ? singular : pluralForm;
}

function accountingDiagnosticSummary(code: string, count: number) {
  if (code === "FIFO_UNKNOWN_BASIS") {
    return `${count} recorded ${plural(count, "position movement")} ${count === 1 ? "has" : "have"} no broker-reported cost basis. Holdings are shown, but FIFO cost basis and returns stay unavailable.`;
  }
  if (code === "FIFO_UNREPRESENTABLE_PRECISION") {
    return `${count} ${plural(count, "sale")} cannot be represented at the broker-reported quantity precision. ${count === 1 ? "Its realized result stays unavailable." : "Their realized results stay unavailable."}`;
  }
  if (code === "SNAPSHOT_RESERVE_NON_EUR_BALANCE") {
    return `${count} non-EUR reserve ${plural(count, "balance")} cannot be included in EUR reserve progress.`;
  }
  return `${count} ${plural(count, "accounting item")} need attention. Affected values stay unavailable rather than estimated.`;
}

function groupAccountingDiagnostics(diagnostics: FinancialPicture["diagnostics"]) {
  const counts = new Map<string, number>();
  for (const diagnostic of diagnostics) {
    counts.set(diagnostic.code, (counts.get(diagnostic.code) ?? 0) + 1);
  }
  return [...counts].map(([code, count]) => ({ code, count }));
}

function ariaRangeValue(decimal: string) {
  // ARIA numeric tokens accept Decimal strings; retain the API's exact value rather than converting money to a float.
  return decimal as unknown as number;
}

export function FinancialDashboard() {
  const [view, setView] = useState<DashboardView>("overview");
  const [state, setState] = useState<DashboardState>("checking");
  const [picture, setPicture] = useState<FinancialPicture>();
  const [message, setMessage] = useState("");
  const [isRefreshing, setIsRefreshing] = useState(false);
  const pictureRequest = useRef(0);

  async function accessToken() {
    const session = await getSupabaseBrowserClient()?.auth.getSession();
    return session?.data.session?.access_token;
  }

  async function readPicture(token: string, requestId: number) {
    const response = await fetch(`${apiUrl}/v1/financial-picture`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (requestId !== pictureRequest.current) return;
    if (response.status === 404) {
      setPicture(undefined);
      setState("empty");
      return;
    }
    if (!response.ok) throw new Error("Financial picture read failed");
    const nextPicture = (await response.json()) as FinancialPicture;
    if (requestId !== pictureRequest.current) return;
    setPicture(nextPicture);
    setState("ready");
  }

  useEffect(() => {
    let active = true;
    async function load() {
      const requestId = ++pictureRequest.current;
      try {
        const token = await accessToken();
        if (!active || requestId !== pictureRequest.current) return;
        if (!token) {
          setState("unauthenticated");
          return;
        }
        setState("loading");
        await readPicture(token, requestId);
      } catch {
        if (active && requestId === pictureRequest.current) {
          setState("error");
          setMessage("We could not load your latest financial picture. Try again when the PIA API is available.");
        }
      }
    }
    void load();
    return () => {
      active = false;
    };
  }, []);

  async function refresh() {
    const requestId = ++pictureRequest.current;
    setIsRefreshing(true);
    setMessage("");
    try {
      const token = await accessToken();
      if (requestId !== pictureRequest.current) return;
      if (!token) {
        setState("unauthenticated");
        return;
      }
      const response = await fetch(`${apiUrl}/v1/financial-picture/refresh`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!response.ok) throw new Error("Financial picture refresh failed");
      const nextPicture = (await response.json()) as FinancialPicture;
      if (requestId !== pictureRequest.current) return;
      setPicture(nextPicture);
      setState("ready");
      setMessage("Financial picture refreshed from your recorded ledger facts.");
    } catch {
      if (requestId === pictureRequest.current) {
        setState(picture ? "ready" : "error");
        setMessage("Refresh failed. Your existing snapshot was not changed; retry explicitly when the PIA API is available.");
      }
    } finally {
      if (requestId === pictureRequest.current) setIsRefreshing(false);
    }
  }

  return (
    <section aria-labelledby="dashboard-heading" className="space-y-6">
      <div className="flex flex-col gap-4 rounded-panel border border-border bg-surface p-5 shadow-panel sm:flex-row sm:items-end sm:justify-between sm:p-7">
        <div>
          <p className="text-sm font-semibold tracking-wide text-brand">FINANCIAL PICTURE</p>
          <h1 className="mt-2 text-3xl font-semibold tracking-tight text-ink sm:text-4xl" id="dashboard-heading">
            Portfolio and savings
          </h1>
          <p className="mt-2 max-w-2xl text-sm leading-6 text-ink-muted">
            Your recorded accounting picture, in native currencies. Market value and unrealized performance arrive only
            after market-data support is added.
          </p>
        </div>
        <button
          className="rounded-lg bg-brand px-4 py-2 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:opacity-60"
          disabled={isRefreshing || state === "checking"}
          onClick={() => void refresh()}
          type="button"
        >
          {isRefreshing ? "Refreshing ledger snapshot…" : "Refresh financial picture"}
        </button>
      </div>

      <div aria-label="Dashboard views" className="flex gap-2 border-b border-border" role="tablist">
        {(["overview", "portfolio"] as const).map((item) => (
          <button
            aria-controls={`${item}-panel`}
            aria-selected={view === item}
            className={`rounded-t-lg px-4 py-2 text-sm font-semibold capitalize ${view === item ? "bg-brand text-white" : "text-ink-muted"}`}
            id={`${item}-tab`}
            key={item}
            onKeyDown={(event) => {
              if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
              const nextView = item === "overview" ? "portfolio" : "overview";
              setView(nextView);
              document.getElementById(`${nextView}-tab`)?.focus();
            }}
            onClick={() => setView(item)}
            role="tab"
            type="button"
          >
            {item === "overview" ? "Overview" : "Portfolio"}
          </button>
        ))}
      </div>

      <p aria-live="polite" className="sr-only" role="status">
        {isRefreshing ? "Refreshing financial picture" : message}
      </p>

      {state === "checking" || state === "loading" ? (
        <DashboardNotice title="Loading your financial picture" />
      ) : state === "unauthenticated" ? (
        <DashboardNotice title="Sign in to view your financial picture" description="Your accounting details stay private to your signed-in owner account." />
      ) : state === "empty" ? (
        <DashboardNotice
          title="No financial snapshot yet"
          description="Record or import ledger history, then choose Refresh financial picture to create a traceable snapshot."
        />
      ) : state === "error" ? (
        <DashboardNotice title="Financial picture unavailable" description={message} />
      ) : picture ? (
        <div id={`${view}-panel`} role="tabpanel" aria-labelledby={`${view}-tab`}>
          {view === "overview" ? <Overview picture={picture} /> : <Portfolio picture={picture} />}
        </div>
      ) : null}
    </section>
  );
}

function DashboardNotice({ title, description }: { title: string; description?: string }) {
  return (
    <div className="rounded-panel border border-border bg-surface p-6" role="status">
      <h2 className="text-lg font-semibold text-ink">{title}</h2>
      {description && <p className="mt-2 max-w-xl text-sm leading-6 text-ink-muted">{description}</p>}
    </div>
  );
}

function Overview({ picture }: { picture: FinancialPicture }) {
  const cash = picture.cash_by_currency.accounts;
  const accounts = picture.account_summaries;
  const hasNonEurCash = cash.some((balance) => balance.currency !== "EUR");
  return (
    <div className="grid gap-5 lg:grid-cols-3">
      {picture.state === "no_ledger_data" && (
        <div className="rounded-panel border border-border bg-surface-muted p-5 text-sm text-ink-muted lg:col-span-3" role="status">
          This snapshot contains no ledger data yet. Add recorded history, then refresh explicitly when it changes.
        </div>
      )}
      {hasNonEurCash && (
        <div className="rounded-panel border border-border bg-surface-muted p-5 text-sm text-ink-muted lg:col-span-3" role="status">
          Non-EUR balances are shown in their recorded currencies and are not converted or included in EUR totals without source evidence.
        </div>
      )}
      <section className="rounded-panel border border-border bg-surface p-5 lg:col-span-2" aria-labelledby="cash-heading">
        <h2 className="text-lg font-semibold text-ink" id="cash-heading">Cash and savings by account</h2>
        {cash.length ? (
          <ul className="mt-4 divide-y divide-border">
            {cash.map((balance) => {
              const account = accounts.find(({ account_id }) => account_id === balance.account_id);
              return (
                <li className="flex items-center justify-between gap-4 py-3 text-sm" key={`${balance.account_id}-${balance.currency}`}>
                  <span><strong className="text-ink">{account?.name ?? "Recorded account"}</strong><span className="ml-2 capitalize text-ink-muted">{account ? accountRoleLabel(account.role) : ""}</span></span>
                  <span className="font-semibold text-ink">{displayAmount(balance.amount, balance.currency)}</span>
                </li>
              );
            })}
          </ul>
        ) : <p className="mt-3 text-sm text-ink-muted">No cash balances are recorded in this snapshot.</p>}
      </section>
      <ReserveProgress reserve={picture.reserve_progress} />
      <SnapshotStatus picture={picture} />
      <Evidence evidenceEventIds={picture.evidence_event_ids} />
    </div>
  );
}

function ReserveProgress({ reserve }: { reserve: FinancialPicture["reserve_progress"] }) {
  if (reserve.status === "unavailable") {
    return <section className="rounded-panel border border-border bg-surface p-5" aria-labelledby="reserve-heading"><h2 className="text-lg font-semibold text-ink" id="reserve-heading">Emergency reserve</h2><p className="mt-3 text-sm leading-6 text-ink-muted">No EUR emergency-reserve target is configured.</p></section>;
  }
  const available = reserve.available_eur_balance ?? "0";
  const target = reserve.configured_target_eur ?? "0";
  return (
    <section className="rounded-panel border border-border bg-surface p-5" aria-labelledby="reserve-heading">
      <h2 className="text-lg font-semibold text-ink" id="reserve-heading">Emergency reserve</h2>
      <div aria-label="Emergency reserve target progress" aria-valuemax={ariaRangeValue(target)} aria-valuemin={0} aria-valuenow={ariaRangeValue(available)} aria-valuetext={`${displayAmount(available, "EUR")} available of ${displayAmount(target, "EUR")} target`} className="mt-4" role="progressbar">
        <p className="text-sm font-semibold text-ink">{displayAmount(available, "EUR")}</p>
        <p className="mt-1 text-sm text-ink-muted">of {displayAmount(target, "EUR")} target</p>
      </div>
      {reserve.status === "incomplete" && <p className="mt-3 text-sm text-amber-800">Progress is incomplete because some reserve evidence cannot be represented in EUR.</p>}
    </section>
  );
}

function SnapshotStatus({ picture }: { picture: FinancialPicture }) {
  const incomplete = picture.completeness.status === "incomplete";
  const diagnosticSummaries = groupAccountingDiagnostics(picture.diagnostics);
  return (
    <section className="rounded-panel border border-border bg-surface p-5" aria-labelledby="snapshot-heading">
      <h2 className="text-lg font-semibold text-ink" id="snapshot-heading">Snapshot status</h2>
      <dl className="mt-4 space-y-3 text-sm">
        <div><dt className="text-ink-muted">Ledger as of</dt><dd className="font-medium text-ink">{displayTime(picture.as_of)}</dd></div>
        <div><dt className="text-ink-muted">Last explicitly refreshed</dt><dd className="font-medium text-ink">{displayTime(picture.refreshed_at)}</dd></div>
        <div><dt className="text-ink-muted">Freshness</dt><dd className="font-medium capitalize text-ink">{picture.freshness.status}</dd></div>
      </dl>
      {picture.freshness.status === "stale" && <p className="mt-4 text-sm text-amber-800">New ledger inputs exist. Refresh explicitly to produce a current snapshot.</p>}
      {incomplete && <>
        <p className="mt-4 text-sm text-amber-800">{picture.completeness.diagnostic_count} accounting diagnostic{picture.completeness.diagnostic_count === 1 ? "" : "s"} {picture.completeness.diagnostic_count === 1 ? "requires" : "require"} attention. Unavailable values are not estimated.</p>
        <ul aria-label="Accounting diagnostics" className="mt-3 space-y-1 text-sm text-amber-800">
          {diagnosticSummaries.map(({ code, count }) => <li key={code}>{accountingDiagnosticSummary(code, count)}</li>)}
        </ul>
      </>}
    </section>
  );
}

function Evidence({ evidenceEventIds }: { evidenceEventIds: string[] }) {
  return (
    <section className="rounded-panel border border-border bg-surface p-5" aria-labelledby="evidence-heading">
      <h2 className="text-lg font-semibold text-ink" id="evidence-heading">Snapshot evidence</h2>
      <details className="mt-3 text-sm text-ink-muted">
        <summary className="cursor-pointer font-medium text-brand">{evidenceEventIds.length} recorded event{evidenceEventIds.length === 1 ? "" : "s"}</summary>
        <ul className="mt-3 break-all space-y-2">{evidenceEventIds.map((id) => <li key={id}>{id}</li>)}</ul>
      </details>
    </section>
  );
}

function Portfolio({ picture }: { picture: FinancialPicture }) {
  return (
    <div className="grid gap-5 lg:grid-cols-2">
      <section className="rounded-panel border border-border bg-surface p-5" aria-labelledby="positions-heading">
        <h2 className="text-lg font-semibold text-ink" id="positions-heading">Recorded positions</h2>
        {picture.positions.owner.length ? <ul className="mt-4 divide-y divide-border">{picture.positions.owner.map((position) => <li className="flex justify-between gap-4 py-3 text-sm" key={position.instrument_id}><span className="font-medium text-ink">{position.instrument_id}</span><span className="text-ink">{position.quantity} units</span></li>)}</ul> : <p className="mt-3 text-sm text-ink-muted">No positions are recorded in this snapshot.</p>}
      </section>
      <section className="rounded-panel border border-border bg-surface p-5" aria-labelledby="basis-heading">
        <h2 className="text-lg font-semibold text-ink" id="basis-heading">FIFO cost basis</h2>
        {picture.fifo.open_lots.length ? <ul className="mt-4 divide-y divide-border">{picture.fifo.open_lots.map((lot) => <li className="py-3 text-sm" key={lot.buy_event_id}><div className="flex justify-between gap-4"><span className="font-medium text-ink">{lot.instrument_id}</span><span className="text-ink">{displayAmount(lot.total_basis, lot.source_currency)}</span></div><p className="mt-1 text-ink-muted">{lot.quantity} units · lot evidence available</p></li>)}</ul> : <p className="mt-3 text-sm text-ink-muted">No open FIFO lots are recorded.</p>}
      </section>
      <section className="rounded-panel border border-border bg-surface p-5 lg:col-span-2" aria-labelledby="realized-heading">
        <h2 className="text-lg font-semibold text-ink" id="realized-heading">Realized results</h2>
        {picture.fifo.realized_sales.length ? <ul className="mt-4 divide-y divide-border">{picture.fifo.realized_sales.map((sale) => <li className="flex justify-between gap-4 py-3 text-sm" key={sale.sale_event_id}><span className="font-medium text-ink">{sale.instrument_id}</span><span className="text-ink">{displayAmount(sale.realized_gain, sale.source_currency)}</span></li>)}</ul> : <p className="mt-3 text-sm text-ink-muted">No realized sales are recorded.</p>}
      </section>
      <Evidence evidenceEventIds={picture.evidence_event_ids} />
    </div>
  );
}
