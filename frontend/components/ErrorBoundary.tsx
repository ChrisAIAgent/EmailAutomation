"use client";

import { Component, ReactNode } from "react";
import { getT } from "@/lib/i18n";

export default class ErrorBoundary extends Component<
  { children: ReactNode },
  { error: Error | null }
> {
  constructor(props: { children: ReactNode }) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error) {
    console.error("Dashboard crashed:", error);
  }

  handleRetry = () => this.setState({ error: null });

  render() {
    if (this.state.error) {
      const t = getT();
      return (
        <div className="min-h-screen flex items-center justify-center p-6">
          <div className="card max-w-md w-full">
            <h2 className="text-lg font-semibold text-danger mb-2">{t('common_error')}</h2>
            <p className="text-muted text-xs mb-1">{t('err_loading')}:</p>
            <p className="text-muted text-sm mb-4 break-words">
              {this.state.error.message || t('common_error')}
            </p>
            <div className="flex gap-2">
              <button className="btn-primary" onClick={this.handleRetry}>
                {t('common_retry')}
              </button>
              {/* Pure navigation only: the diagnostics page never runs a check
                  on mount, so opening it performs zero diagnostic work. */}
              <button className="btn" onClick={() => window.location.assign("/diagnostics")}>
                {t('diag_open')}
              </button>
            </div>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
