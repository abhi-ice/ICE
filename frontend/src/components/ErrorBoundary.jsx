/**
 * ErrorBoundary — day-1 audit finding 43 (2026-10-25).
 *
 * Without this, any error while a screen renders makes React 19 unmount
 * the whole app and the user sees a blank white page with no way out.
 * This catches render-time errors and shows a calm full-page message
 * with a Reload button and a plain link home (a plain <a>, not a router
 * Link, so it works even when the router itself is what crashed).
 *
 * Saved and queued work lives on the server or in the device's offline
 * queue, which this component never touches.
 *
 * Errors in click handlers / async code (save, submit, upload) do not
 * reach an error boundary; those keep their existing toast handling.
 */
import React from "react";

export default class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { hasError: false };
  }

  static getDerivedStateFromError() {
    return { hasError: true };
  }

  componentDidCatch(error, info) {
    // eslint-disable-next-line no-console
    console.error("[ErrorBoundary] A screen failed to render:", error, info?.componentStack);
  }

  render() {
    if (!this.state.hasError) return this.props.children;
    return (
      <div
        role="alert"
        data-testid="error-boundary"
        className="min-h-screen bg-background text-foreground flex items-center justify-center px-4 py-8"
      >
        <div className="surface w-full max-w-md p-6 sm:p-8 text-center space-y-4">
          <h1 className="text-xl font-semibold tracking-tight">Something went wrong on this screen.</h1>
          <p className="text-sm text-muted-foreground">
            Your saved work is safe. Tap Reload to try again. If this keeps happening,
            go to the home screen and tell your project manager or admin.
          </p>
          <div className="flex flex-col sm:flex-row gap-3 justify-center pt-2">
            <button
              type="button"
              data-testid="error-boundary-reload"
              onClick={() => window.location.reload()}
              className="h-11 px-5 rounded-md text-sm font-medium bg-primary text-primary-foreground hover:opacity-90"
            >
              Reload
            </button>
            <a
              href="/"
              data-testid="error-boundary-home"
              className="h-11 px-5 rounded-md text-sm font-medium border border-border text-foreground hover:bg-muted inline-flex items-center justify-center"
            >
              Go to home
            </a>
          </div>
        </div>
      </div>
    );
  }
}
