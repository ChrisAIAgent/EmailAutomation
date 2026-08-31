'use client';
import Dashboard from "@/components/Dashboard";
import ErrorBoundary from "@/components/ErrorBoundary";
import { LangProvider } from "@/lib/i18n";
import { UiPreferencesProvider } from "@/lib/ui-preferences";

export default function Page() {
  return (
    <UiPreferencesProvider>
      <LangProvider>
        <ErrorBoundary>
          <Dashboard />
        </ErrorBoundary>
      </LangProvider>
    </UiPreferencesProvider>
  );
}
