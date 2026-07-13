import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import Home from "./page";

describe("Home", () => {
  it("renders the static foundation shell", () => {
    render(<Home />);

    expect(screen.getByRole("heading", { name: "Your financial cockpit is taking shape" })).toBeInTheDocument();
    expect(screen.getByText("Foundation")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Portfolio" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Insights" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Learning" })).toBeInTheDocument();
  });
});
