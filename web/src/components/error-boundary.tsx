import { useState } from "react";
import { ErrorBoundary as ReactErrorBoundary, type FallbackProps } from "react-error-boundary";
import { AlertTriangle, RefreshCw, Copy, Check } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Button } from "./ui/button";

function ErrorFallback({ error, resetErrorBoundary }: FallbackProps) {
  const { t } = useTranslation("errors");
  const [copied, setCopied] = useState(false);

  const copyError = async () => {
    const errorObj = error instanceof Error ? error : new Error(String(error));
    const text = `${errorObj.name}: ${errorObj.message}\n\n${errorObj.stack ?? ""}`;
    await navigator.clipboard.writeText(text);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const errorMessage = error instanceof Error ? error.message : String(error);
  const errorStack = error instanceof Error ? error.stack : undefined;

  return (
    <div className="flex h-screen w-full items-center justify-center bg-background">
      <div className="flex max-w-md flex-col items-center gap-4 rounded-lg border border-destructive/20 bg-destructive/5 p-8 text-center">
        <AlertTriangle className="h-12 w-12 text-destructive" />
        <h2 className="text-xl font-semibold text-foreground">
          {t("generic")}
        </h2>
        <p className="text-sm text-muted-foreground">
          {errorMessage || t("unexpected")}
        </p>
        {import.meta.env.DEV && errorStack && (
          <pre className="max-h-40 w-full overflow-auto rounded bg-muted p-2 text-left text-xs">
            {errorStack}
          </pre>
        )}
        <div className="flex gap-2">
          <Button onClick={copyError} variant="outline">
            {copied ? (
              <Check className="mr-2 h-4 w-4" />
            ) : (
              <Copy className="mr-2 h-4 w-4" />
            )}
            {copied ? t("copied") : t("copyError")}
          </Button>
          <Button onClick={resetErrorBoundary} variant="outline">
            <RefreshCw className="mr-2 h-4 w-4" />
            {t("tryAgain")}
          </Button>
        </div>
      </div>
    </div>
  );
}

export function ErrorBoundary({ children }: { children: React.ReactNode }) {
  return (
    <ReactErrorBoundary
      FallbackComponent={ErrorFallback}
      onReset={() => {
        // Optionally reload the page or reset app state
        window.location.reload();
      }}
      onError={(error, info) => {
        // Log error to console in development
        console.error("ErrorBoundary caught an error:", error, info);
        // TODO: Send to error tracking service in production
      }}
    >
      {children}
    </ReactErrorBoundary>
  );
}
