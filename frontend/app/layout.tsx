import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "TAC Email Automation",
  description: "TAC AI-assisted email outreach and follow-up",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <head>
        {/* Electron serves this synchronously before every application bundle. */}
        <script src="/runtime-config.js" />
        <script dangerouslySetInnerHTML={{ __html: `try { const p = JSON.parse(localStorage.getItem('email-automation.ui-preferences') || '{}'); document.documentElement.dataset.theme = p.theme === 'light' ? 'light' : 'dark'; document.documentElement.dataset.density = p.density === 'large' ? 'large' : 'standard'; } catch (_) {}` }} />
      </head>
      <body>
        {children}
      </body>
    </html>
  );
}
