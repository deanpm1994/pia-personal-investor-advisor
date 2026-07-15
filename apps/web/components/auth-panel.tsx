"use client";

import { FormEvent, useEffect, useState } from "react";

import { getSupabaseBrowserClient } from "@/lib/supabase-browser";

type AuthState = "checking" | "signed-out" | "signed-in";

export function AuthPanel() {
  const client = getSupabaseBrowserClient();
  const [state, setState] = useState<AuthState>("checking");
  const [email, setEmail] = useState("");
  const [message, setMessage] = useState("");

  useEffect(() => {
    if (!client) return;
    void client.auth
      .getSession()
      .then(({ data }) => setState(data.session ? "signed-in" : "signed-out"));
    const { data } = client.auth.onAuthStateChange((_event, session) => {
      setState(session ? "signed-in" : "signed-out");
    });
    return () => data.subscription.unsubscribe();
  }, [client]);

  async function sendMagicLink(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const client = getSupabaseBrowserClient();
    if (!client) return;
    const { error } = await client.auth.signInWithOtp({
      email,
      options: { emailRedirectTo: window.location.origin },
    });
    setMessage(
      error
        ? "Unable to send a sign-in link. Try again."
        : "Check your email for a sign-in link.",
    );
  }

  async function signOut() {
    const client = getSupabaseBrowserClient();
    if (client) await client.auth.signOut();
  }

  if (state === "signed-in") {
    return (
      <button className="text-sm font-medium text-brand" onClick={signOut} type="button">
        Sign out
      </button>
    );
  }
  if (!client) {
    return <span className="text-xs text-ink-muted">Local auth setup required</span>;
  }
  return (
    <form aria-label="Magic link sign in" className="flex items-center gap-2" onSubmit={sendMagicLink}>
      <label className="sr-only" htmlFor="email">
        Email
      </label>
      <input
        className="w-44 rounded border border-border px-2 py-1 text-sm"
        id="email"
        onChange={(event) => setEmail(event.target.value)}
        required
        type="email"
        value={email}
      />
      <button
        className="rounded bg-brand px-3 py-1 text-sm font-medium text-white"
        disabled={state === "checking"}
        type="submit"
      >
        Email sign-in link
      </button>
      {message && (
        <span className="sr-only" role="status">
          {message}
        </span>
      )}
    </form>
  );
}
