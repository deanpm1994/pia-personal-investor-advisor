import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ImportReview } from "@/components/import-review";
import { getSupabaseBrowserClient } from "@/lib/supabase-browser";

vi.mock("@/lib/supabase-browser", () => ({ getSupabaseBrowserClient: vi.fn() }));

const getClient = vi.mocked(getSupabaseBrowserClient);
const fetchMock = vi.fn();

const validReview = {
  id: "import-1",
  status: "review_ready",
  row_count: 1,
  event_count: 1,
  diagnostic_count: 0,
  confirmation_eligible: true,
  rows: [
    {
      row_number: 2,
      events: [{ event_type: "buy", source_identity: { event_reference: "TR-1" } }],
      diagnostics: [],
    },
  ],
};

function signedInClient() {
  return {
    auth: {
      getSession: vi.fn().mockResolvedValue({
        data: { session: { access_token: "owner-token" } },
      }),
    },
  };
}

function chooseCsv() {
  fireEvent.change(screen.getByLabelText("CSV file"), {
    target: { files: [new File(["synthetic csv"], "history.csv", { type: "text/csv" })] },
  });
}

afterEach(() => {
  cleanup();
  getClient.mockReset();
  fetchMock.mockReset();
  vi.unstubAllGlobals();
});

describe("ImportReview", () => {
  it("shows an empty state", () => {
    getClient.mockReturnValue(null);
    render(<ImportReview />);

    expect(screen.getByText("No staged import selected.")).toBeInTheDocument();
  });

  it("requires a signed-in owner before uploading", async () => {
    getClient.mockReturnValue(null);
    render(<ImportReview />);

    chooseCsv();

    expect(await screen.findByRole("alert")).toHaveTextContent("Sign in before uploading an import.");
  });

  it("shows normalized review data and enables the next step only for valid batches", async () => {
    getClient.mockReturnValue(signedInClient() as never);
    fetchMock.mockResolvedValue(new Response(JSON.stringify(validReview), { status: 201 }));
    vi.stubGlobal("fetch", fetchMock);
    render(<ImportReview />);

    chooseCsv();

    expect(await screen.findByText("buy: TR-1")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Confirmation available in the next step" })).toBeEnabled();
    expect(screen.queryByText("synthetic csv")).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      "http://localhost:8000/v1/imports",
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: "Bearer owner-token" }),
        method: "POST",
      }),
    );
  });

  it("shows row diagnostics and blocks confirmation for invalid batches", async () => {
    getClient.mockReturnValue(signedInClient() as never);
    fetchMock.mockResolvedValue(
      new Response(
        JSON.stringify({
          ...validReview,
          confirmation_eligible: false,
          diagnostic_count: 1,
          rows: [
            {
              row_number: 2,
              events: [],
              diagnostics: [{ code: "TRCSV007", message: "Invalid amount" }],
            },
          ],
        }),
        { status: 201 },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<ImportReview />);

    chooseCsv();

    expect(await screen.findByText("TRCSV007: Invalid amount")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Confirmation available in the next step" })).toBeDisabled();
  });

  it("renders every diagnostic when a row has repeated diagnostic codes", async () => {
    getClient.mockReturnValue(signedInClient() as never);
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
    fetchMock.mockResolvedValue(
      new Response(
        JSON.stringify({
          ...validReview,
          confirmation_eligible: false,
          diagnostic_count: 2,
          rows: [
            {
              row_number: 2,
              events: [],
              diagnostics: [
                { code: "TRCSV008_INVALID_SIGN", message: "First sign mismatch" },
                { code: "TRCSV008_INVALID_SIGN", message: "Second sign mismatch" },
              ],
            },
          ],
        }),
        { status: 201 },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<ImportReview />);

    chooseCsv();

    expect(await screen.findByText("TRCSV008_INVALID_SIGN: First sign mismatch")).toBeInTheDocument();
    expect(screen.getByText("TRCSV008_INVALID_SIGN: Second sign mismatch")).toBeInTheDocument();
    expect(consoleError.mock.calls.flat().join(" ")).not.toContain("same key");
    consoleError.mockRestore();
  });

  it("shows a staging failure when the upload request cannot complete", async () => {
    getClient.mockReturnValue(signedInClient() as never);
    fetchMock.mockRejectedValue(new TypeError("Network unavailable"));
    vi.stubGlobal("fetch", fetchMock);
    render(<ImportReview />);

    chooseCsv();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Unable to stage this CSV. Make sure the PIA API is running and try again.",
    );
  });

  it("shows loading and stale-data feedback when a review refresh fails", async () => {
    getClient.mockReturnValue(signedInClient() as never);
    let rejectRefresh: (reason?: unknown) => void;
    const refresh = new Promise<Response>((_resolve, reject) => {
      rejectRefresh = reject;
    });
    fetchMock
      .mockResolvedValueOnce(new Response(JSON.stringify(validReview), { status: 201 }))
      .mockReturnValueOnce(refresh);
    vi.stubGlobal("fetch", fetchMock);
    render(<ImportReview />);

    chooseCsv();
    await screen.findByText("buy: TR-1");
    fireEvent.click(screen.getByRole("button", { name: "Refresh status" }));

    expect(await screen.findByRole("status")).toHaveTextContent("Refreshing import status…");
    rejectRefresh!(new TypeError("Network unavailable"));
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent("Import status is stale. Refresh after reconnecting."),
    );
  });
});
