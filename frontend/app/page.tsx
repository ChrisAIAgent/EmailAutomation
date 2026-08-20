'use client';
import Dashboard from "@/components/Dashboard";
import ErrorBoundary from "@/components/ErrorBoundary";
import { LangProvider } from "@/lib/i18n";

export default function Page() {
  return (
    <LangProvider>
      <ErrorBoundary>
        <Dashboard />
      </ErrorBoundary>
    </LangProvider>
  );
}
