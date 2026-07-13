import type { Metadata } from "next";

import "./globals.css";

export const metadata: Metadata = {
  title: "PIA | Personal Investor Advisor",
  description: "A private personal financial cockpit in foundation mode.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
