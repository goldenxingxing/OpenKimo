import { useState, useCallback, type FormEvent, type KeyboardEvent } from "react";
import { KimoCliBrand } from "@/components/kimo-cli-brand";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";

type LoginPageProps = {
  onLogin: (username: string, password: string) => Promise<void>;
};

export function LoginPage({ onLogin }: LoginPageProps) {
  const { t } = useTranslation("auth");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = useCallback(
    async (e?: FormEvent) => {
      e?.preventDefault();
      const trimmedUsername = username.trim();
      if (!(trimmedUsername && password)) {
        setError(t("login.missingCredentials"));
        return;
      }
      setError(null);
      setIsLoading(true);
      try {
        await onLogin(trimmedUsername, password);
      } catch (err) {
        setError(
          err instanceof Error ? err.message : t("login.failed"),
        );
      } finally {
        setIsLoading(false);
      }
    },
    [username, password, onLogin, t],
  );

  const handleKeyDown = useCallback(
    (e: KeyboardEvent<HTMLInputElement>) => {
      if (e.key === "Enter") {
        handleSubmit();
      }
    },
    [handleSubmit],
  );

  return (
    <div className="flex h-[100dvh] items-center justify-center bg-background px-4">
      <div className="w-full max-w-sm">
        <div className="mb-8 flex justify-center">
          <KimoCliBrand showVersion={false} />
        </div>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-center text-xl">
              {t("login.title")}
            </CardTitle>
            <CardDescription className="text-center">
              {t("login.description")}
            </CardDescription>
          </CardHeader>

          <CardContent>
            <form onSubmit={handleSubmit} className="flex flex-col gap-4">
              <div className="flex flex-col gap-1.5">
                <label
                  htmlFor="username"
                  className="text-sm font-medium text-foreground"
                >
                  {t("login.username")}
                </label>
                <Input
                  id="username"
                  type="text"
                  autoComplete="username"
                  autoFocus
                  placeholder={t("login.usernamePlaceholder")}
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  onKeyDown={handleKeyDown}
                  disabled={isLoading}
                />
              </div>

              <div className="flex flex-col gap-1.5">
                <label
                  htmlFor="password"
                  className="text-sm font-medium text-foreground"
                >
                  {t("login.password")}
                </label>
                <Input
                  id="password"
                  type="password"
                  autoComplete="current-password"
                  placeholder={t("login.passwordPlaceholder")}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  onKeyDown={handleKeyDown}
                  disabled={isLoading}
                />
              </div>

              {error && (
                <p className="text-sm text-destructive" role="alert">
                  {error}
                </p>
              )}

              <Button
                type="submit"
                className="w-full"
                disabled={isLoading}
              >
                {isLoading ? (
                  <>
                    <Loader2 className="animate-spin" />
                    {t("login.signingIn")}
                  </>
                ) : (
                  t("login.submit")
                )}
              </Button>
            </form>
          </CardContent>
        </Card>

        <p className="mt-4 text-center text-xs text-muted-foreground">
          {t("login.contactAdmin")}
        </p>
      </div>
    </div>
  );
}
