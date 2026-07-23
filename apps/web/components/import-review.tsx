"use client";

import { useState } from "react";

import { getSupabaseBrowserClient } from "@/lib/supabase-browser";

type ReviewEvent = {
  event_type: string;
  source_identity: { event_reference: string };
};

type ReviewRow = {
  row_number: number;
  events: ReviewEvent[];
  diagnostics: { code: string; message: string }[];
};

type Review = {
  id: string;
  status: string;
  row_count: number;
  event_count: number;
  diagnostic_count: number;
  confirmation_eligible: boolean;
  rows: ReviewRow[];
};

type ReviewState = "empty" | "loading" | "error";

export function ImportReview() {
  const [review, setReview] = useState<Review>();
  const [state, setState] = useState<ReviewState>("empty");
  const [message, setMessage] = useState("");
  const [loadingMessage, setLoadingMessage] = useState("");
  const apiUrl = process.env.NEXT_PUBLIC_PIA_API_URL ?? "http://localhost:8000";

  async function accessToken(): Promise<string | undefined> {
    const session = await getSupabaseBrowserClient()?.auth.getSession();
    return session?.data.session?.access_token;
  }

  async function upload(file: File) {
    setState("loading");
    setMessage("");
    setLoadingMessage("Staging import…");
    const token = await accessToken();
    if (!token) {
      setState("error");
      setMessage("Sign in before uploading an import.");
      return;
    }

    try {
      const response = await fetch(`${apiUrl}/v1/imports`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "text/csv",
          "X-Import-Filename": file.name,
        },
        body: file,
      });
      if (!response.ok) {
        throw new Error("Import staging failed");
      }

      setReview((await response.json()) as Review);
      setState("empty");
    } catch {
      setState("error");
      setMessage("Unable to stage this CSV. Make sure the PIA API is running and try again.");
    }
  }

  async function refresh() {
    if (!review) return;
    setState("loading");
    setMessage("");
    setLoadingMessage("Refreshing import status…");
    const token = await accessToken();
    if (!token) {
      setState("error");
      setMessage("Import status is stale. Refresh after reconnecting.");
      return;
    }

    try {
      const response = await fetch(`${apiUrl}/v1/imports/${review.id}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!response.ok) {
        throw new Error("Import refresh failed");
      }

      setReview((await response.json()) as Review);
      setState("empty");
    } catch {
      setState("error");
      setMessage("Import status is stale. Refresh after reconnecting.");
    }
  }

  return (
    <section
      aria-labelledby="import-heading"
      className="mt-6 rounded-panel border border-border bg-surface p-5"
    >
      <h2 className="text-lg font-semibold text-ink" id="import-heading">
        Import Trade Republic CSV
      </h2>
      <p className="mt-1 text-sm text-ink-muted">
        Upload a CSV for private staged review. Raw file contents are never shown here.
      </p>
      <label className="mt-4 block text-sm font-medium text-ink">
        CSV file
        <input
          accept=".csv,text/csv"
          className="mt-2 block w-full text-sm"
          disabled={state === "loading"}
          onChange={(event) => {
            const file = event.target.files?.[0];
            if (file) void upload(file);
          }}
          type="file"
        />
      </label>
      {state === "loading" && (
        <p className="mt-3 text-sm text-ink-muted" role="status">
          {loadingMessage}
        </p>
      )}
      {state === "error" && (
        <p className="mt-3 text-sm text-red-700" role="alert">
          {message}
        </p>
      )}
      {!review && state === "empty" && (
        <p className="mt-3 text-sm text-ink-muted">No staged import selected.</p>
      )}
      {review && (
        <div className="mt-4 space-y-3">
          <div className="flex justify-between text-sm">
            <span>
              {review.row_count} rows · {review.event_count} events · {review.diagnostic_count} diagnostics
            </span>
            <button className="text-brand" onClick={() => void refresh()} type="button">
              Refresh status
            </button>
          </div>
          {review.rows.map((row) => (
            <div className="rounded border border-border p-3 text-sm" key={row.row_number}>
              <strong>Row {row.row_number}</strong>
              {row.events.map((event) => (
                <p key={event.source_identity.event_reference}>
                  {event.event_type}: {event.source_identity.event_reference}
                </p>
              ))}
              {row.diagnostics.map((diagnostic, index) => (
                <p className="text-red-700" key={`${diagnostic.code}-${index}`}>
                  {diagnostic.code}: {diagnostic.message}
                </p>
              ))}
            </div>
          ))}
          <button
            className="rounded bg-brand px-3 py-2 text-sm font-medium text-white disabled:opacity-50"
            disabled={!review.confirmation_eligible}
            type="button"
          >
            Confirmation available in the next step
          </button>
        </div>
      )}
    </section>
  );
}
