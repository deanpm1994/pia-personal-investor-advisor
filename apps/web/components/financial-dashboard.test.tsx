import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { FinancialDashboard } from "@/components/financial-dashboard";
import { getSupabaseBrowserClient } from "@/lib/supabase-browser";

vi.mock("@/lib/supabase-browser", () => ({ getSupabaseBrowserClient: vi.fn() }));

const getClient = vi.mocked(getSupabaseBrowserClient);
const fetchMock = vi.fn();

const picture = {
  snapshot_id: "snapshot-1",
  as_of: "2026-08-08T10:00:00Z",
  refreshed_at: "2026-08-09T10:00:00Z",
  state: "ready",
  freshness: { status: "fresh" },
  completeness: { status: "complete", diagnostic_count: 0 },
  account_summaries: [
    { account_id: "cash-1", name: "Everyday cash", role: "cash", archived_at: null, emergency_reserve_target_eur: null },
    { account_id: "reserve-1", name: "Reserve", role: "emergency_reserve", archived_at: null, emergency_reserve_target_eur: "2000.00" },
  ],
  cash_by_currency: { accounts: [{ account_id: "cash-1", amount: "540.00", currency: "EUR", evidence_event_ids: ["event-1"] }], owner: { EUR: "540.00" } },
  positions: { accounts: [], owner: [{ instrument_id: "IE00B4L5Y983", quantity: "2.5000", evidence_event_ids: ["event-2"] }] },
  fifo: { open_lots: [{ account_id: "cash-1", buy_event_id: "event-2", evidence_event_ids: ["event-2"], fee_event_ids: [], instrument_id: "IE00B4L5Y983", quantity: "2.5000", source_currency: "EUR", total_basis: "300.00" }], realized_sales: [{ account_id: "cash-1", sale_event_id: "event-3", evidence_event_ids: ["event-3"], instrument_id: "IE00B4L5Y983", source_currency: "EUR", realized_gain: "20.00" }] },
  reserve_progress: { status: "available", available_eur_balance: "1500.00", configured_target_eur: "2000.00" },
  diagnostics: [],
  evidence_event_ids: ["event-1", "event-2", "event-3"],
};

function signedInClient() {
  return { auth: { getSession: vi.fn().mockResolvedValue({ data: { session: { access_token: "owner-token" } } }) } };
}

afterEach(() => {
  cleanup();
  getClient.mockReset();
  fetchMock.mockReset();
  vi.unstubAllGlobals();
});

describe("FinancialDashboard", () => {
  it("requires an authenticated owner and does not refresh on page load", async () => {
    getClient.mockReturnValue(null);
    render(<FinancialDashboard />);

    expect(await screen.findByText("Sign in to view your financial picture")).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("renders native-currency cash, reserve semantics, snapshot status, and evidence from the API", async () => {
    getClient.mockReturnValue(signedInClient() as never);
    fetchMock.mockResolvedValue(new Response(JSON.stringify(picture)));
    vi.stubGlobal("fetch", fetchMock);
    render(<FinancialDashboard />);

    expect(await screen.findByText("540.00 EUR")).toBeInTheDocument();
    const progress = screen.getByRole("progressbar", { name: "Emergency reserve target progress" });
    expect(progress).toHaveAttribute("aria-valuemin", "0");
    expect(progress).toHaveAttribute("aria-valuemax", "2000.00");
    expect(progress).toHaveAttribute("aria-valuenow", "1500.00");
    expect(progress).toHaveAttribute("aria-valuetext", "1500.00 EUR available of 2000.00 EUR target");
    expect(screen.getByText("Ledger as of")).toBeInTheDocument();
    expect(screen.getByText("3 recorded events")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith("http://localhost:8000/v1/financial-picture", { headers: { Authorization: "Bearer owner-token" } });
    expect(fetchMock.mock.calls.flat().join(" ")).not.toContain("refresh");
  });

  it("provides the portfolio view with positions, FIFO basis, and realized results", async () => {
    getClient.mockReturnValue(signedInClient() as never);
    fetchMock.mockResolvedValue(new Response(JSON.stringify(picture)));
    vi.stubGlobal("fetch", fetchMock);
    render(<FinancialDashboard />);

    await screen.findByText("540.00 EUR");
    fireEvent.click(screen.getByRole("tab", { name: "Portfolio" }));

    expect(screen.getByText("2.5000 units")).toBeInTheDocument();
    expect(screen.getByText("300.00 EUR")).toBeInTheDocument();
    expect(screen.getByText("20.00 EUR")).toBeInTheDocument();
  });

  it("creates a snapshot only after the owner explicitly refreshes", async () => {
    getClient.mockReturnValue(signedInClient() as never);
    fetchMock
      .mockResolvedValueOnce(new Response("missing", { status: 404 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ...picture, state: "no_ledger_data" })));
    vi.stubGlobal("fetch", fetchMock);
    render(<FinancialDashboard />);

    expect(await screen.findByText("No financial snapshot yet")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Refresh financial picture" }));

    await waitFor(() => expect(screen.getByText("Financial picture refreshed from your recorded ledger facts.")).toBeInTheDocument());
    expect(fetchMock).toHaveBeenLastCalledWith("http://localhost:8000/v1/financial-picture/refresh", { method: "POST", headers: { Authorization: "Bearer owner-token" } });
  });

  it("keeps the explicitly refreshed snapshot when the initial read finishes later", async () => {
    getClient.mockReturnValue(signedInClient() as never);
    let resolveInitialRead: (response: Response) => void;
    const initialRead = new Promise<Response>((resolve) => {
      resolveInitialRead = resolve;
    });
    fetchMock
      .mockReturnValueOnce(initialRead)
      .mockResolvedValueOnce(new Response(JSON.stringify({ ...picture, cash_by_currency: { ...picture.cash_by_currency, accounts: [{ ...picture.cash_by_currency.accounts[0], amount: "600.00" }] } })));
    vi.stubGlobal("fetch", fetchMock);
    render(<FinancialDashboard />);

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("button", { name: "Refresh financial picture" }));
    expect(await screen.findByText("600.00 EUR")).toBeInTheDocument();

    resolveInitialRead!(new Response(JSON.stringify(picture)));
    await waitFor(() => expect(screen.queryByText("540.00 EUR")).not.toBeInTheDocument());
  });

  it("announces no-ledger and non-EUR states without converting recorded values", async () => {
    getClient.mockReturnValue(signedInClient() as never);
    fetchMock.mockResolvedValue(
      new Response(
        JSON.stringify({
          ...picture,
          state: "no_ledger_data",
          cash_by_currency: {
            ...picture.cash_by_currency,
            accounts: [...picture.cash_by_currency.accounts, { account_id: "cash-1", amount: "50.00", currency: "USD", evidence_event_ids: ["event-4"] }],
          },
        }),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<FinancialDashboard />);

    expect(await screen.findByText("This snapshot contains no ledger data yet. Add recorded history, then refresh explicitly when it changes.")).toBeInTheDocument();
    expect(screen.getByText("Non-EUR balances are shown in their recorded currencies and are not converted or included in EUR totals without source evidence.")).toBeInTheDocument();
    expect(screen.getByText("50.00 USD")).toBeInTheDocument();
  });

  it("supports keyboard navigation between dashboard views", async () => {
    getClient.mockReturnValue(signedInClient() as never);
    fetchMock.mockResolvedValue(new Response(JSON.stringify(picture)));
    vi.stubGlobal("fetch", fetchMock);
    render(<FinancialDashboard />);

    const overview = await screen.findByRole("tab", { name: "Overview" });
    fireEvent.keyDown(overview, { key: "ArrowRight" });

    expect(screen.getByRole("tab", { name: "Portfolio" })).toHaveFocus();
    expect(screen.getByRole("tabpanel", { name: "Portfolio" })).toBeInTheDocument();
  });

  it("makes stale and incomplete accounting evidence visible without estimating unavailable values", async () => {
    getClient.mockReturnValue(signedInClient() as never);
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ ...picture, state: "stale", freshness: { status: "stale" }, completeness: { status: "incomplete", diagnostic_count: 2 }, reserve_progress: { status: "incomplete", available_eur_balance: "1500.00", configured_target_eur: "2000.00" } })));
    vi.stubGlobal("fetch", fetchMock);
    render(<FinancialDashboard />);

    expect(await screen.findByText("New ledger inputs exist. Refresh explicitly to produce a current snapshot.")).toBeInTheDocument();
    expect(screen.getByText("2 accounting diagnostics require attention. Unavailable values are not estimated.")).toBeInTheDocument();
    expect(screen.getByText("Progress is incomplete because some reserve evidence cannot be represented in EUR.")).toBeInTheDocument();
  });

  it("explains unavailable reserve targets and failures clearly", async () => {
    getClient.mockReturnValue(signedInClient() as never);
    fetchMock
      .mockResolvedValueOnce(new Response(JSON.stringify({ ...picture, reserve_progress: { status: "unavailable", available_eur_balance: null, configured_target_eur: null } })))
      .mockRejectedValueOnce(new TypeError("Network unavailable"));
    vi.stubGlobal("fetch", fetchMock);
    render(<FinancialDashboard />);

    expect(await screen.findByText("No EUR emergency-reserve target is configured.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Refresh financial picture" }));
    expect(await screen.findByText("Refresh failed. Your existing snapshot was not changed; retry explicitly when the PIA API is available.")).toBeInTheDocument();
  });

  it("shows a failure state when session lookup fails during loading or refresh", async () => {
    const getSession = vi
      .fn()
      .mockResolvedValueOnce({ data: { session: { access_token: "owner-token" } } })
      .mockRejectedValueOnce(new Error("Session unavailable"));
    getClient.mockReturnValue({ auth: { getSession } } as never);
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify(picture)));
    vi.stubGlobal("fetch", fetchMock);
    render(<FinancialDashboard />);

    expect(await screen.findByText("540.00 EUR")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Refresh financial picture" }));
    expect(await screen.findByText("Refresh failed. Your existing snapshot was not changed; retry explicitly when the PIA API is available.")).toBeInTheDocument();

    getClient.mockReturnValue({ auth: { getSession: vi.fn().mockRejectedValue(new Error("Session unavailable")) } } as never);
    render(<FinancialDashboard />);
    expect(await screen.findByText("Financial picture unavailable")).toBeInTheDocument();
  });
});
