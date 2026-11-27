import { Component, useEffect, type ErrorInfo, type ReactNode } from "react";

import { APP_NAME } from "@/lib/config";
import type { PageMeta } from "@/lib/page-meta";
import { Button, InlineNotice } from "@/lib/components";

interface BoundaryState {
  error: Error | null;
}

class PageErrorBoundary extends Component<{ title: string; children: ReactNode }, BoundaryState> {
  state: BoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): BoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Surface the component stack in dev tools; the UI shows a friendly panel.
    console.error(`[avdfleet] Page "${this.props.title}" crashed:`, error, info.componentStack);
  }

  render(): ReactNode {
    if (this.state.error) {
      return (
        <div className="page-error" role="alert">
          <h1 className="page-error__title">{this.props.title} failed to render</h1>
          <InlineNotice tone="danger" title="Unexpected UI error">
            <p>
              {this.state.error.message || "The page threw an unexpected error."} The rest of the app is
              still available — reload the page to try again.
            </p>
          </InlineNotice>
          <div className="page-error__actions">
            <Button variant="primary" icon="refresh" onClick={() => window.location.reload()}>
              Reload GUI
            </Button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}

/**
 * Wraps every registered page: sets the document title and contains render
 * failures so one page cannot blank the whole console.
 */
export function PageBoundary({ meta, children }: { meta: PageMeta; children: ReactNode }) {
  useEffect(() => {
    document.title = `${meta.title} · ${APP_NAME}`;
  }, [meta.title]);

  return <PageErrorBoundary title={meta.title}>{children}</PageErrorBoundary>;
}
