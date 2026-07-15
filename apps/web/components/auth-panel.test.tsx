import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AuthPanel } from "@/components/auth-panel";

const getSupabaseBrowserClient = vi.hoisted(() => vi.fn());

vi.mock("@/lib/supabase-browser", () => ({ getSupabaseBrowserClient }));

function makeClient(session: object | null = null) {
  const unsubscribe = vi.fn();
  return {
    auth: {
      getSession: vi.fn().mockResolvedValue({ data: { session } }),
      onAuthStateChange: vi
        .fn()
        .mockReturnValue({ data: { subscription: { unsubscribe } } }),
      signInWithOtp: vi.fn().mockResolvedValue({ error: null }),
      signOut: vi.fn().mockResolvedValue({ error: null }),
    },
  };
}

describe("AuthPanel", () => {
  beforeEach(() => {
    getSupabaseBrowserClient.mockReset();
  });

  it("sends a magic-link request and restores a signed-out session", async () => {
    const client = makeClient();
    getSupabaseBrowserClient.mockReturnValue(client);
    render(<AuthPanel />);

    const email = await screen.findByLabelText("Email");
    fireEvent.change(email, { target: { value: "investor@example.test" } });
    fireEvent.submit(screen.getByRole("form", { name: "Magic link sign in" }));

    await waitFor(() => {
      expect(client.auth.signInWithOtp).toHaveBeenCalledWith({
        email: "investor@example.test",
        options: { emailRedirectTo: window.location.origin },
      });
    });
    expect(screen.getByRole("status")).toHaveTextContent("Check your email");
  });

  it("restores a signed-in session and signs out", async () => {
    const client = makeClient({ user: { id: "user-1" } });
    getSupabaseBrowserClient.mockReturnValue(client);
    render(<AuthPanel />);

    const signOut = await screen.findByRole("button", { name: "Sign out" });
    fireEvent.click(signOut);

    await waitFor(() => expect(client.auth.signOut).toHaveBeenCalledOnce());
  });

  it("does not offer authentication without public Supabase configuration", async () => {
    getSupabaseBrowserClient.mockReturnValue(null);
    render(<AuthPanel />);

    expect(await screen.findByText("Local auth setup required")).toBeInTheDocument();
  });
});
