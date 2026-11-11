import {
  useState,
  useEffect,
  useCallback,
  type FormEvent,
} from "react";
import { Trans, useTranslation } from "react-i18next";

import i18n from "@/i18n";
import {
  listUsers,
  createUser,
  updateUser,
  deleteUser,
} from "@/lib/api/apis/AdminApi";
import type { AdminUser } from "@/lib/api/apis/AdminApi";
import type { UserInfo } from "@/lib/api/apis/AuthApi";
import { AdminMcpPanel } from "./admin-mcp-panel";
import { AdminPluginsPanel } from "./admin-plugins-panel";
import { AdminBrandingPanel } from "./admin-branding-panel";
import { AdminWikiPanel } from "./admin-wiki-panel";
import { AdminSkillsPanel } from "./admin-skills-panel";
import { AdminEvolutionPanel } from "./admin-evolution-panel";
import { MemoryPanel } from "../memory/memory-panel";
import { useCandidates } from "@/hooks/useMemory";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
  DialogDescription,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { ArrowLeft, Cpu, Loader2, Plus, RefreshCw, Trash2, KeyRound, ToggleLeft, ToggleRight, Users, Puzzle, Palette, WandSparkles, Brain, Plug, Library, Dna } from "lucide-react";
import { toast } from "sonner";
import { apiClient } from "@/lib/apiClient";
import type { ConfigModel, ConfigToml } from "@/lib/api/models";

type AdminTab =
  | "users"
  | "models"
  | "plugins"
  | "mcp"
  | "skills"
  | "branding"
  | "wiki"
  | "memory"
  | "evolution";

type AdminPageProps = {
  currentUser: UserInfo;
};

function formatDate(timestamp: number): string {
  return new Date(timestamp * 1000).toLocaleDateString(i18n.language, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

type CreateUserDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCreated: () => void;
};

function CreateUserDialog({ open, onOpenChange, onCreated }: CreateUserDialogProps) {
  const { t } = useTranslation("admin");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState("user");
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleClose = useCallback(() => {
    setUsername("");
    setPassword("");
    setRole("user");
    setError(null);
    onOpenChange(false);
  }, [onOpenChange]);

  const handleSubmit = useCallback(
    async (e?: FormEvent) => {
      e?.preventDefault();
      const trimmed = username.trim();
      if (!trimmed) {
        setError(t("users.create.usernameRequired"));
        return;
      }
      if (!password) {
        setError(t("users.create.passwordRequired"));
        return;
      }
      setError(null);
      setIsLoading(true);
      try {
        await createUser(trimmed, password, role);
        toast.success(t("users.create.success"));
        onCreated();
        handleClose();
      } catch (err) {
        setError(err instanceof Error ? err.message : t("users.create.failed"));
      } finally {
        setIsLoading(false);
      }
    },
    [username, password, role, onCreated, handleClose, t],
  );

  return (
    <Dialog open={open} onOpenChange={handleClose}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t("users.create.title")}</DialogTitle>
          <DialogDescription>
            {t("users.create.description")}
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit} className="flex flex-col gap-4 pt-2">
          <div className="flex flex-col gap-1.5">
            <label htmlFor="new-username" className="text-sm font-medium text-foreground">
              {t("users.create.username")}
            </label>
            <Input
              id="new-username"
              type="text"
              placeholder={t("users.create.usernamePlaceholder")}
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              disabled={isLoading}
              autoFocus
            />
          </div>

          <div className="flex flex-col gap-1.5">
            <label htmlFor="new-password" className="text-sm font-medium text-foreground">
              {t("users.create.password")}
            </label>
            <Input
              id="new-password"
              type="password"
              placeholder={t("users.create.passwordPlaceholder")}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              disabled={isLoading}
            />
          </div>

          <div className="flex flex-col gap-1.5">
            {/* biome-ignore lint/a11y/noLabelWithoutControl: Select component wraps the control */}
            <label className="text-sm font-medium text-foreground">{t("users.create.role")}</label>
            <Select value={role} onValueChange={setRole} disabled={isLoading}>
              <SelectTrigger className="w-full">
                <SelectValue placeholder={t("users.create.rolePlaceholder")} />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="user">{t("users.create.roleUser")}</SelectItem>
                <SelectItem value="admin">{t("users.create.roleAdmin")}</SelectItem>
              </SelectContent>
            </Select>
          </div>

          {error && (
            <p className="text-sm text-destructive" role="alert">
              {error}
            </p>
          )}

          <DialogFooter>
            <Button type="button" variant="outline" onClick={handleClose} disabled={isLoading}>
              {t("actions.cancel")}
            </Button>
            <Button type="submit" disabled={isLoading}>
              {isLoading ? <><Loader2 className="animate-spin" />{t("users.create.creating")}</> : t("users.create.submit")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

type ResetPasswordDialogProps = {
  user: AdminUser | null;
  onOpenChange: (open: boolean) => void;
  onUpdated: () => void;
};

function ResetPasswordDialog({ user, onOpenChange, onUpdated }: ResetPasswordDialogProps) {
  const { t } = useTranslation("admin");
  const [newPassword, setNewPassword] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleClose = useCallback(() => {
    setNewPassword("");
    setError(null);
    onOpenChange(false);
  }, [onOpenChange]);

  const handleSubmit = useCallback(
    async (e?: FormEvent) => {
      e?.preventDefault();
      if (!user) return;
      if (!newPassword) {
        setError(t("users.reset.required"));
        return;
      }
      setError(null);
      setIsLoading(true);
      try {
        await updateUser(user.id, { password: newPassword });
        toast.success(t("users.reset.success", { username: user.username }));
        onUpdated();
        handleClose();
      } catch (err) {
        setError(err instanceof Error ? err.message : t("users.reset.failed"));
      } finally {
        setIsLoading(false);
      }
    },
    [user, newPassword, onUpdated, handleClose, t],
  );

  return (
    <Dialog open={!!user} onOpenChange={handleClose}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t("users.reset.title")}</DialogTitle>
          <DialogDescription>
            <Trans
              i18nKey="admin:users.reset.description"
              values={{ username: user?.username ?? "" }}
              components={{ strong: <strong /> }}
            />
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit} className="flex flex-col gap-4 pt-2">
          <div className="flex flex-col gap-1.5">
            <label htmlFor="reset-password" className="text-sm font-medium text-foreground">
              {t("users.reset.newPassword")}
            </label>
            <Input
              id="reset-password"
              type="password"
              placeholder={t("users.reset.placeholder")}
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              disabled={isLoading}
              autoFocus
            />
          </div>

          {error && (
            <p className="text-sm text-destructive" role="alert">
              {error}
            </p>
          )}

          <DialogFooter>
            <Button type="button" variant="outline" onClick={handleClose} disabled={isLoading}>
              {t("actions.cancel")}
            </Button>
            <Button type="submit" disabled={isLoading}>
              {isLoading ? <><Loader2 className="animate-spin" />{t("actions.resetting")}</> : t("users.reset.submit")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export function AdminPage({ currentUser }: AdminPageProps) {
  const { t } = useTranslation("admin");
  const [activeTab, setActiveTab] = useState<AdminTab>("users");
  // Read here so the count sits on the tab itself. Suggestions expire after a
  // fortnight and the agent raises one only when its subject comes up, so a
  // number you must open a tab to discover is the state this replaces.
  const { candidates } = useCandidates();

  const [users, setUsers] = useState<AdminUser[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [showCreateDialog, setShowCreateDialog] = useState(false);
  const [resetPasswordUser, setResetPasswordUser] = useState<AdminUser | null>(null);
  const [deleteTargetUser, setDeleteTargetUser] = useState<AdminUser | null>(null);
  const [isDeleting, setIsDeleting] = useState(false);
  const [togglingUserId, setTogglingUserId] = useState<string | null>(null);

  // Models tab state
  const [models, setModels] = useState<ConfigModel[]>([]);
  const [defaultModel, setDefaultModel] = useState<string>("");
  const [isLoadingModels, setIsLoadingModels] = useState(false);
  const [tomlContent, setTomlContent] = useState<string>("");
  const [tomlPath, setTomlPath] = useState<string>("");
  const [isEditingToml, setIsEditingToml] = useState(false);
  const [isSavingToml, setIsSavingToml] = useState(false);
  const [tomlError, setTomlError] = useState<string | null>(null);

  const loadUsers = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    try {
      const data = await listUsers();
      setUsers(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : t("users.loadFailed"));
    } finally {
      setIsLoading(false);
    }
  }, [t]);

  const loadModels = useCallback(async () => {
    setIsLoadingModels(true);
    try {
      const globalConfig = await apiClient.config.getGlobalConfigApiConfigGet();
      setModels(globalConfig.models);
      setDefaultModel(globalConfig.defaultModel);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : t("models.loadFailed"));
    } finally {
      setIsLoadingModels(false);
    }
  }, [t]);

  const loadToml = useCallback(async () => {
    try {
      const toml: ConfigToml = await apiClient.config.getConfigTomlApiConfigTomlGet();
      setTomlContent(toml.content);
      setTomlPath(toml.path);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : t("models.tomlLoadFailed"));
    }
  }, [t]);

  const handleSaveToml = useCallback(async () => {
    setIsSavingToml(true);
    setTomlError(null);
    try {
      const resp = await apiClient.config.updateConfigTomlApiConfigTomlPut({
        updateConfigTomlRequest: { content: tomlContent },
      });
      if (resp.success) {
        toast.success(t("models.saved"));
        setIsEditingToml(false);
        loadModels();
      } else {
        setTomlError(resp.error ?? t("models.saveFailed"));
        toast.error(resp.error ?? t("models.saveFailed"));
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : t("models.saveFailed");
      setTomlError(msg);
      toast.error(msg);
    } finally {
      setIsSavingToml(false);
    }
  }, [tomlContent, loadModels, t]);

  useEffect(() => {
    loadUsers();
  }, [loadUsers]);

  useEffect(() => {
    if (activeTab === "models") {
      loadModels();
      loadToml();
    }
  }, [activeTab, loadModels, loadToml]);

  const handleToggleActive = useCallback(
    async (user: AdminUser) => {
      setTogglingUserId(user.id);
      try {
        const updated = await updateUser(user.id, { is_active: !user.is_active });
        setUsers((prev) => prev.map((u) => (u.id === updated.id ? updated : u)));
        toast.success(
          updated.is_active
            ? t("users.enabled", { username: user.username })
            : t("users.disabled", { username: user.username }),
        );
      } catch (err) {
        toast.error(err instanceof Error ? err.message : t("users.updateFailed"));
      } finally {
        setTogglingUserId(null);
      }
    },
    [t],
  );

  const handleDeleteConfirm = useCallback(async () => {
    if (!deleteTargetUser) return;
    setIsDeleting(true);
    try {
      await deleteUser(deleteTargetUser.id);
      setUsers((prev) => prev.filter((u) => u.id !== deleteTargetUser.id));
      toast.success(t("users.deleted", { username: deleteTargetUser.username }));
      setDeleteTargetUser(null);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : t("users.deleteFailed"));
    } finally {
      setIsDeleting(false);
    }
  }, [deleteTargetUser, t]);

  const totalUsers = users.length;
  const activeUsers = users.filter((u) => u.is_active).length;

  return (
    <div className="min-h-[100dvh] bg-background text-foreground overflow-y-auto">
      <div className="mx-auto max-w-5xl px-4 py-6">
        {/* Header */}
        <div className="mb-4 flex items-center gap-4">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => { window.location.href = "/"; }}
            className="gap-2"
          >
            <ArrowLeft className="size-4" />
            {t("page.back")}
          </Button>
          <h1 className="text-xl font-semibold">{t("page.title")}</h1>
          {activeTab === "users" && (
            <div className="ml-auto flex items-center gap-2">
              <Button
                variant="outline"
                size="sm"
                onClick={loadUsers}
                disabled={isLoading}
                className="gap-2"
              >
                <RefreshCw className={isLoading ? "animate-spin size-4" : "size-4"} />
                {t("actions.refresh")}
              </Button>
              <Button size="sm" onClick={() => setShowCreateDialog(true)} className="gap-2">
                <Plus className="size-4" />
                {t("page.newUser")}
              </Button>
            </div>
          )}
        </div>

        {/* Tab nav */}
        <div className="mb-6 flex gap-1 rounded-lg border bg-muted/40 p-1 w-fit">
          <button
            type="button"
            onClick={() => setActiveTab("skills")}
            className={[
              "inline-flex items-center gap-2 rounded-md px-3 py-1.5 text-sm font-medium transition-colors",
              activeTab === "skills"
                ? "bg-background text-foreground shadow-sm"
                : "text-muted-foreground hover:text-foreground",
            ].join(" ")}
          >
            <WandSparkles className="size-3.5" />
            {t("page.tabs.skills")}
          </button>
          <button
            type="button"
            onClick={() => setActiveTab("users")}
            className={[
              "inline-flex items-center gap-2 rounded-md px-3 py-1.5 text-sm font-medium transition-colors",
              activeTab === "users"
                ? "bg-background text-foreground shadow-sm"
                : "text-muted-foreground hover:text-foreground",
            ].join(" ")}
          >
            <Users className="size-3.5" />
            {t("page.tabs.users")}
          </button>
          <button
            type="button"
            onClick={() => setActiveTab("models")}
            className={[
              "inline-flex items-center gap-2 rounded-md px-3 py-1.5 text-sm font-medium transition-colors",
              activeTab === "models"
                ? "bg-background text-foreground shadow-sm"
                : "text-muted-foreground hover:text-foreground",
            ].join(" ")}
          >
            <Cpu className="size-3.5" />
            {t("page.tabs.models")}
          </button>
          <button
            type="button"
            onClick={() => setActiveTab("plugins")}
            className={[
              "inline-flex items-center gap-2 rounded-md px-3 py-1.5 text-sm font-medium transition-colors",
              activeTab === "plugins"
                ? "bg-background text-foreground shadow-sm"
                : "text-muted-foreground hover:text-foreground",
            ].join(" ")}
          >
            <Puzzle className="size-3.5" />
            {t("page.tabs.plugins")}
          </button>
          <button
            type="button"
            onClick={() => setActiveTab("branding")}
            className={[
              "inline-flex items-center gap-2 rounded-md px-3 py-1.5 text-sm font-medium transition-colors",
              activeTab === "branding"
                ? "bg-background text-foreground shadow-sm"
                : "text-muted-foreground hover:text-foreground",
            ].join(" ")}
          >
            <Palette className="size-3.5" />
            {t("page.tabs.branding")}
          </button>
          <button
            type="button"
            onClick={() => setActiveTab("mcp")}
            className={[
              "inline-flex items-center gap-2 rounded-md px-3 py-1.5 text-sm font-medium transition-colors",
              activeTab === "mcp"
                ? "bg-background text-foreground shadow-sm"
                : "text-muted-foreground hover:text-foreground",
            ].join(" ")}
          >
            <Plug className="size-3.5" />
            {t("page.tabs.mcp")}
          </button>
          <button
            type="button"
            onClick={() => setActiveTab("wiki")}
            className={[
              "inline-flex items-center gap-2 rounded-md px-3 py-1.5 text-sm font-medium transition-colors",
              activeTab === "wiki"
                ? "bg-background text-foreground shadow-sm"
                : "text-muted-foreground hover:text-foreground",
            ].join(" ")}
          >
            <Library className="size-3.5" />
            {t("page.tabs.wiki")}
          </button>
          <button
            type="button"
            onClick={() => setActiveTab("memory")}
            className={[
              "inline-flex items-center gap-2 rounded-md px-3 py-1.5 text-sm font-medium transition-colors",
              activeTab === "memory"
                ? "bg-background text-foreground shadow-sm"
                : "text-muted-foreground hover:text-foreground",
            ].join(" ")}
          >
            <Brain className="size-3.5" />
            {t("page.tabs.memory")}
            {candidates.length > 0 && (
              <span className="rounded-full bg-primary/15 px-1.5 text-xs font-medium">
                {candidates.length}
              </span>
            )}
          </button>
          <button
            type="button"
            onClick={() => setActiveTab("evolution")}
            className={[
              "inline-flex items-center gap-2 rounded-md px-3 py-1.5 text-sm font-medium transition-colors",
              activeTab === "evolution"
                ? "bg-background text-foreground shadow-sm"
                : "text-muted-foreground hover:text-foreground",
            ].join(" ")}
          >
            <Dna className="size-3.5" />
            {t("page.tabs.evolution")}
          </button>
        </div>

        {/* Plugins tab */}
        {activeTab === "plugins" && <AdminPluginsPanel />}

        {/* Skills tab */}
        {activeTab === "skills" && <AdminSkillsPanel />}

        {/* MCP tab */}
        {activeTab === "mcp" && <AdminMcpPanel />}

        {/* Branding tab */}
        {activeTab === "branding" && <AdminBrandingPanel />}

        {/* Wiki tab */}
        {activeTab === "wiki" && <AdminWikiPanel />}

        {/* Memory tab */}
        {activeTab === "memory" && <MemoryPanel />}

        {/* Evolution tab */}
        {activeTab === "evolution" && <AdminEvolutionPanel />}

        {/* Models tab */}
        {activeTab === "models" && (
          <div className="flex flex-col gap-6">
            <div className="flex items-center justify-between">
              <div>
                <h2 className="text-lg font-semibold">{t("models.title")}</h2>
                <p className="text-sm text-muted-foreground">
                  {t("models.description")}
                </p>
              </div>
              <div className="flex items-center gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => { loadModels(); loadToml(); }}
                  disabled={isLoadingModels}
                  className="gap-2"
                >
                  <RefreshCw className={isLoadingModels ? "animate-spin size-4" : "size-4"} />
                  {t("actions.refresh")}
                </Button>
                <Button size="sm" onClick={() => setIsEditingToml(true)} className="gap-2">
                  {t("models.editConfig")}
                </Button>
              </div>
            </div>

            {isLoadingModels && models.length === 0 ? (
              <div className="flex items-center justify-center py-16 text-muted-foreground gap-2">
                <Loader2 className="animate-spin size-4" />
                {t("models.loading")}
              </div>
            ) : models.length === 0 ? (
              <div className="rounded-xl border bg-card shadow-sm p-8 text-center text-muted-foreground">
                {t("models.empty")}
              </div>
            ) : (
              <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
                {models.map((m) => (
                  <Card key={m.name} className={m.name === defaultModel ? "border-primary/50" : ""}>
                    <CardHeader className="pb-2">
                      <div className="flex items-center justify-between">
                        <CardTitle className="text-base font-semibold">{m.name}</CardTitle>
                        {m.name === defaultModel && (
                          <Badge variant="default" className="text-[10px]">{t("models.default")}</Badge>
                        )}
                      </div>
                    </CardHeader>
                    <CardContent className="flex flex-col gap-2 text-sm">
                      <div className="flex items-center justify-between">
                        <span className="text-muted-foreground">{t("models.protocol")}</span>
                        <Badge variant="secondary">{m.providerType}</Badge>
                      </div>
                      <div className="flex items-center justify-between">
                        <span className="text-muted-foreground">{t("models.modelId")}</span>
                        <span className="font-mono text-xs">{m.model}</span>
                      </div>
                      <div className="flex items-center justify-between">
                        <span className="text-muted-foreground">{t("models.context")}</span>
                        <span>
                          {t("models.contextTokens", {
                            size: m.maxContextSize?.toLocaleString() ?? "",
                          })}
                        </span>
                      </div>
                      {m.capabilities && m.capabilities.size > 0 && (
                        <div className="flex flex-wrap gap-1 pt-1">
                          {Array.from(m.capabilities).map((cap) => (
                            <Badge key={cap} variant="outline" className="text-[10px]">
                              {cap}
                            </Badge>
                          ))}
                        </div>
                      )}
                    </CardContent>
                  </Card>
                ))}
              </div>
            )}

            {/* Config.toml editor dialog */}
            <Dialog open={isEditingToml} onOpenChange={setIsEditingToml}>
              <DialogContent className="max-w-3xl">
                <DialogHeader>
                  <DialogTitle>{t("models.editConfig")}</DialogTitle>
                  <DialogDescription>
                    {tomlPath ? <span className="font-mono text-xs">{tomlPath}</span> : t("models.rawToml")}
                  </DialogDescription>
                </DialogHeader>
                <div className="flex flex-col gap-3 pt-2">
                  <textarea
                    className="min-h-[320px] w-full rounded-md border bg-muted/30 p-3 font-mono text-xs leading-relaxed focus:outline-none focus:ring-2 focus:ring-ring"
                    value={tomlContent}
                    onChange={(e) => setTomlContent(e.target.value)}
                    disabled={isSavingToml}
                  />
                  {tomlError && (
                    <p className="text-sm text-destructive" role="alert">
                      {tomlError}
                    </p>
                  )}
                </div>
                <DialogFooter>
                  <Button type="button" variant="outline" onClick={() => setIsEditingToml(false)} disabled={isSavingToml}>
                    {t("actions.cancel")}
                  </Button>
                  <Button type="button" onClick={handleSaveToml} disabled={isSavingToml}>
                    {isSavingToml ? <><Loader2 className="animate-spin size-4" />{t("actions.saving")}</> : t("models.saveConfig")}
                  </Button>
                </DialogFooter>
              </DialogContent>
            </Dialog>
          </div>
        )}

        {/* Users tab */}
        {activeTab === "users" && (
          <div className="flex flex-col gap-6">
            {/* Stats */}
            <div className="grid grid-cols-2 gap-4 sm:grid-cols-3">
              <Card>
                <CardHeader className="pb-1">
                  <CardTitle className="text-sm font-medium text-muted-foreground">
                    {t("users.totalUsers")}
                  </CardTitle>
                </CardHeader>
                <CardContent>
                  <p className="text-3xl font-bold">{totalUsers}</p>
                </CardContent>
              </Card>
              <Card>
                <CardHeader className="pb-1">
                  <CardTitle className="text-sm font-medium text-muted-foreground">
                    {t("users.activeUsers")}
                  </CardTitle>
                </CardHeader>
                <CardContent>
                  <p className="text-3xl font-bold text-green-600 dark:text-green-400">
                    {activeUsers}
                  </p>
                </CardContent>
              </Card>
              <Card>
                <CardHeader className="pb-1">
                  <CardTitle className="text-sm font-medium text-muted-foreground">
                    {t("users.inactiveUsers")}
                  </CardTitle>
                </CardHeader>
                <CardContent>
                  <p className="text-3xl font-bold text-muted-foreground">
                    {totalUsers - activeUsers}
                  </p>
                </CardContent>
              </Card>
            </div>

            {/* Error */}
            {error && (
              <div className="rounded-md border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive">
                {error}
              </div>
            )}

            {/* Users table */}
            <div className="rounded-xl border bg-card shadow-sm overflow-hidden">
          {isLoading && users.length === 0 ? (
            <div className="flex items-center justify-center py-16 text-muted-foreground gap-2">
              <Loader2 className="animate-spin size-4" />
              {t("users.loading")}
            </div>
          ) : users.length === 0 ? (
            <div className="flex items-center justify-center py-16 text-muted-foreground">
              {t("users.empty")}
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b bg-muted/40 text-left text-xs font-medium uppercase tracking-wide text-muted-foreground">
                    <th className="px-4 py-3">{t("users.columns.username")}</th>
                    <th className="px-4 py-3">{t("users.columns.role")}</th>
                    <th className="px-4 py-3">{t("users.columns.status")}</th>
                    <th className="px-4 py-3">{t("users.columns.sessions")}</th>
                    <th className="px-4 py-3">{t("users.columns.created")}</th>
                    <th className="px-4 py-3 text-right">{t("users.columns.actions")}</th>
                  </tr>
                </thead>
                <tbody className="divide-y">
                  {users.map((user) => {
                    const isSelf = user.id === currentUser.user_id;
                    const isToggling = togglingUserId === user.id;

                    return (
                      <tr
                        key={user.id}
                        className="transition-colors hover:bg-muted/30"
                      >
                        <td className="px-4 py-3 font-medium">
                          {user.username}
                          {isSelf && (
                            <span className="ml-2 text-xs text-muted-foreground">{t("users.you")}</span>
                          )}
                        </td>
                        <td className="px-4 py-3">
                          <Badge
                            variant={user.role === "admin" ? "default" : "secondary"}
                          >
                            {user.role === "admin"
                              ? t("users.roleBadge.admin")
                              : user.role === "user"
                                ? t("users.roleBadge.user")
                                : user.role}
                          </Badge>
                        </td>
                        <td className="px-4 py-3">
                          {user.is_active ? (
                            <Badge variant="outline" className="border-green-500/40 text-green-600 dark:text-green-400 bg-green-500/10">
                              {t("users.active")}
                            </Badge>
                          ) : (
                            <Badge variant="outline" className="text-muted-foreground">
                              {t("users.inactive")}
                            </Badge>
                          )}
                        </td>
                        <td className="px-4 py-3 text-muted-foreground">
                          {user.session_count}
                        </td>
                        <td className="px-4 py-3 text-muted-foreground">
                          {formatDate(user.created_at)}
                        </td>
                        <td className="px-4 py-3">
                          <div className="flex items-center justify-end gap-1">
                            {/* Reset password */}
                            <Button
                              variant="ghost"
                              size="icon-sm"
                              title={t("users.resetPassword")}
                              onClick={() => setResetPasswordUser(user)}
                            >
                              <KeyRound className="size-3.5" />
                            </Button>

                            {/* Toggle active/inactive */}
                            <Button
                              variant="ghost"
                              size="icon-sm"
                              title={user.is_active ? t("users.disableUser") : t("users.enableUser")}
                              disabled={isSelf || isToggling}
                              onClick={() => handleToggleActive(user)}
                            >
                              {isToggling ? (
                                <Loader2 className="size-3.5 animate-spin" />
                              ) : user.is_active ? (
                                <ToggleRight className="size-3.5 text-green-600 dark:text-green-400" />
                              ) : (
                                <ToggleLeft className="size-3.5 text-muted-foreground" />
                              )}
                            </Button>

                            {/* Delete */}
                            <Button
                              variant="ghost"
                              size="icon-sm"
                              title={t("users.deleteUser")}
                              disabled={isSelf}
                              className="text-muted-foreground hover:text-destructive"
                              onClick={() => setDeleteTargetUser(user)}
                            >
                              <Trash2 className="size-3.5" />
                            </Button>
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
            </div>
          </div>
        )}
      </div>

      {/* Create user dialog */}
      <CreateUserDialog
        open={showCreateDialog}
        onOpenChange={setShowCreateDialog}
        onCreated={loadUsers}
      />

      {/* Reset password dialog */}
      <ResetPasswordDialog
        user={resetPasswordUser}
        onOpenChange={(open) => {
          if (!open) setResetPasswordUser(null);
        }}
        onUpdated={loadUsers}
      />

      {/* Delete confirm dialog */}
      <AlertDialog
        open={!!deleteTargetUser}
        onOpenChange={(open) => {
          if (!open) setDeleteTargetUser(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("users.delete.title")}</AlertDialogTitle>
            <AlertDialogDescription>
              <Trans
                i18nKey="admin:users.delete.description"
                values={{ username: deleteTargetUser?.username ?? "" }}
                components={{ strong: <strong /> }}
              />
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={isDeleting}>{t("actions.cancel")}</AlertDialogCancel>
            <AlertDialogAction
              className="bg-destructive text-white hover:bg-destructive/90"
              disabled={isDeleting}
              onClick={handleDeleteConfirm}
            >
              {isDeleting ? (
                <><Loader2 className="animate-spin size-4" />{t("users.delete.deleting")}</>
              ) : (
                t("users.delete.confirm")
              )}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
