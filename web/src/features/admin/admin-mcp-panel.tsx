import { useCallback, useEffect, useMemo, useState } from "react";
import { Trans, useTranslation } from "react-i18next";

import {
  ChevronDown,
  ChevronRight,
  FileJson,
  Loader2,
  Plug,
  PlusCircle,
  RefreshCw,
  Save,
  Trash2,
  Wifi,
} from "lucide-react";
import { toast } from "sonner";

import i18n from "@/i18n";

import {
  type McpMutationResult,
  type McpProbeResult,
  type McpServer,
  type McpServerList,
  createMcpServer,
  deleteMcpServer,
  getRawMcpConfig,
  listMcpServers,
  putRawMcpConfig,
  testMcpServer,
  updateMcpServer,
} from "@/lib/api/apis/AdminMcpApi";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
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
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";

import {
  type McpFormState,
  type McpTransport,
  buildServerConfig,
  emptyMcpForm,
  formStateFromServer,
  validateServerName,
} from "./admin-mcp-form";

// Labels are i18n keys, translated at render so a language switch takes effect.
const TRANSPORTS: { value: McpTransport; labelKey: string }[] = [
  { value: "stdio", labelKey: "mcp.transports.stdio" },
  { value: "http", labelKey: "mcp.transports.http" },
  { value: "sse", labelKey: "mcp.transports.sse" },
];

function transportBadge(server: McpServer) {
  return (
    <Badge variant="outline" className="font-mono text-[10px] uppercase">
      {server.transport}
    </Badge>
  );
}

/** Tell the user which sessions took the change and which did not. */
function reportMutation(result: McpMutationResult, message: string) {
  const skipped = result.skipped_busy_session_ids?.length ?? 0;
  if (skipped > 0) {
    toast.success(i18n.t("admin:mcp.skippedBusy", { message, count: skipped }));
    return;
  }
  toast.success(message);
}

export function AdminMcpPanel() {
  const { t } = useTranslation("admin");
  const [data, setData] = useState<McpServerList | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const [editing, setEditing] = useState<McpFormState | null>(null);
  const [editingOriginal, setEditingOriginal] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);
  const [probes, setProbes] = useState<Record<string, McpProbeResult>>({});
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  const [rawOpen, setRawOpen] = useState(false);
  const [rawContent, setRawContent] = useState("");
  const [rawPath, setRawPath] = useState("");
  const [rawSaving, setRawSaving] = useState(false);

  const names = useMemo(
    () => (data?.servers ?? []).map((server) => server.name),
    [data],
  );

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await listMcpServers());
    } catch (err) {
      setError(err instanceof Error ? err.message : t("mcp.loadFailed"));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const openCreate = useCallback(() => {
    setEditingOriginal(null);
    setEditing({
      ...emptyMcpForm,
      transport: data?.stdio_editable ? "stdio" : "http",
    });
    setFormError(null);
  }, [data]);

  const openEdit = useCallback((server: McpServer) => {
    setEditingOriginal(server.name);
    setEditing(formStateFromServer(server));
    setFormError(null);
  }, []);

  const handleSave = useCallback(async () => {
    if (!editing) return;
    const isNew = editingOriginal === null;

    if (isNew) {
      const nameError = validateServerName(editing.name, names);
      if (nameError) {
        setFormError(t(`mcp.formErrors.${nameError.code}`, { detail: nameError.detail }));
        return;
      }
    }
    const built = buildServerConfig(editing);
    if (!built.ok) {
      setFormError(t(`mcp.formErrors.${built.error.code}`, { detail: built.error.detail }));
      return;
    }

    setSaving(true);
    setFormError(null);
    try {
      const result = isNew
        ? await createMcpServer(editing.name.trim(), built.config)
        : await updateMcpServer(editingOriginal, built.config);
      setData(result.servers);
      setEditing(null);
      reportMutation(result, isNew ? t("mcp.added") : t("mcp.updated"));
    } catch (err) {
      setFormError(err instanceof Error ? err.message : t("mcp.saveFailed"));
    } finally {
      setSaving(false);
    }
  }, [editing, editingOriginal, names, t]);

  const handleDelete = useCallback(async () => {
    if (!deleteTarget) return;
    setBusy(deleteTarget);
    try {
      const result = await deleteMcpServer(deleteTarget);
      setData(result.servers);
      setProbes((current) => {
        const next = { ...current };
        delete next[deleteTarget];
        return next;
      });
      reportMutation(result, t("mcp.removed", { name: deleteTarget }));
    } catch (err) {
      toast.error(err instanceof Error ? err.message : t("mcp.removeFailed"));
    } finally {
      setBusy(null);
      setDeleteTarget(null);
    }
  }, [deleteTarget, t]);

  const handleTest = useCallback(async (name: string) => {
    setBusy(name);
    try {
      const result = await testMcpServer(name);
      setProbes((current) => ({ ...current, [name]: result }));
      setExpanded((current) => ({ ...current, [name]: result.ok }));
      if (result.ok) {
        toast.success(t("mcp.testOk", { name, count: result.tools.length }));
      } else {
        toast.error(t("mcp.testNoConnect", { name }));
      }
    } catch (err) {
      toast.error(err instanceof Error ? err.message : t("mcp.testFailed"));
    } finally {
      setBusy(null);
    }
  }, [t]);

  const openRaw = useCallback(async () => {
    try {
      const raw = await getRawMcpConfig();
      setRawContent(raw.content || '{\n  "mcpServers": {}\n}\n');
      setRawPath(raw.path);
      setRawOpen(true);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : t("mcp.rawReadFailed"));
    }
  }, [t]);

  const handleRawSave = useCallback(async () => {
    setRawSaving(true);
    try {
      const result = await putRawMcpConfig(rawContent);
      setData(result.servers);
      setRawOpen(false);
      reportMutation(result, t("mcp.rawSaved"));
    } catch (err) {
      toast.error(err instanceof Error ? err.message : t("mcp.rawSaveFailed"));
    } finally {
      setRawSaving(false);
    }
  }, [rawContent, t]);

  const stdioEditable = data?.stdio_editable ?? false;

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader className="flex flex-row items-start justify-between gap-2">
          <div>
            <CardTitle className="text-base">{t("mcp.title")}</CardTitle>
            <p className="mt-1 text-xs text-muted-foreground">
              {t("mcp.description")}
            </p>
            {data && (
              <p className="mt-1 font-mono text-[11px] text-muted-foreground">{data.path}</p>
            )}
            {data && !stdioEditable && (
              <p className="mt-1 text-[11px] text-muted-foreground">
                <Trans i18nKey="admin:mcp.remoteOnly" components={{ code: <code /> }} />
              </p>
            )}
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <Button
              size="sm"
              variant="outline"
              onClick={refresh}
              disabled={loading}
              className="gap-2"
            >
              <RefreshCw className={loading ? "size-4 animate-spin" : "size-4"} />
              {t("actions.refresh")}
            </Button>
            <Button size="sm" variant="outline" onClick={openRaw} className="gap-2">
              <FileJson className="size-4" />
              {t("mcp.editJson")}
            </Button>
            <Button size="sm" onClick={openCreate} className="gap-2">
              <PlusCircle className="size-4" />
              {t("mcp.addServer")}
            </Button>
          </div>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          {error && (
            <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
              {error}
            </div>
          )}

          {!loading && data?.servers.length === 0 && (
            <div className="flex flex-col items-center gap-2 rounded-md border border-dashed py-10 text-center">
              <Plug className="size-6 text-muted-foreground" />
              <p className="text-sm text-muted-foreground">{t("mcp.empty")}</p>
            </div>
          )}

          {data?.servers.map((server) => {
            const probe = probes[server.name];
            const isOpen = expanded[server.name] ?? false;
            return (
              <div key={server.name} className="rounded-md border px-3 py-2.5">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-medium">{server.name}</span>
                      {transportBadge(server)}
                      {server.uses_oauth && (
                        <Badge
                          variant={server.oauth_authorized ? "outline" : "destructive"}
                          className="text-[10px]"
                        >
                          {server.oauth_authorized ? t("mcp.oauthReady") : t("mcp.authNeeded")}
                        </Badge>
                      )}
                      {probe && (
                        <Badge
                          variant={probe.ok ? "outline" : "destructive"}
                          className="text-[10px]"
                        >
                          {probe.ok
                            ? t("mcp.toolCount", { count: probe.tools.length })
                            : t("mcp.unreachable")}
                        </Badge>
                      )}
                    </div>
                    <p className="mt-1 truncate font-mono text-[11px] text-muted-foreground">
                      {server.target}
                    </p>
                    {server.uses_oauth && server.oauth_authorized === false && (
                      <p className="mt-1 text-[11px] text-muted-foreground">
                        <Trans
                          i18nKey="admin:mcp.authHint"
                          values={{ name: server.name }}
                          components={{ code: <code /> }}
                        />
                      </p>
                    )}
                  </div>
                  <div className="flex shrink-0 items-center gap-1">
                    <Button
                      size="sm"
                      variant="ghost"
                      className="gap-1.5"
                      disabled={busy === server.name}
                      onClick={() => handleTest(server.name)}
                    >
                      {busy === server.name ? (
                        <Loader2 className="size-3.5 animate-spin" />
                      ) : (
                        <Wifi className="size-3.5" />
                      )}
                      {t("mcp.test")}
                    </Button>
                    <Button size="sm" variant="ghost" onClick={() => openEdit(server)}>
                      {t("mcp.edit")}
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      className="text-destructive"
                      onClick={() => setDeleteTarget(server.name)}
                    >
                      <Trash2 className="size-3.5" />
                    </Button>
                  </div>
                </div>

                {probe && !probe.ok && probe.error && (
                  <p className="mt-2 rounded bg-destructive/10 px-2 py-1 font-mono text-[11px] text-destructive">
                    {probe.error}
                  </p>
                )}

                {probe?.ok && probe.tools.length > 0 && (
                  <div className="mt-2">
                    <button
                      type="button"
                      className="flex items-center gap-1 text-[11px] text-muted-foreground hover:text-foreground"
                      onClick={() =>
                        setExpanded((current) => ({
                          ...current,
                          [server.name]: !isOpen,
                        }))
                      }
                    >
                      {isOpen ? (
                        <ChevronDown className="size-3.5" />
                      ) : (
                        <ChevronRight className="size-3.5" />
                      )}
                      {t("mcp.toolCount", { count: probe.tools.length })}
                    </button>
                    {isOpen && (
                      <ul className="mt-1 flex flex-col gap-0.5 pl-5">
                        {probe.tools.map((tool) => (
                          <li key={tool.name} className="text-[11px] text-muted-foreground">
                            <span className="font-mono text-foreground">{tool.name}</span>
                            {tool.description ? ` — ${tool.description}` : ""}
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </CardContent>
      </Card>

      <Dialog
        open={editing !== null}
        onOpenChange={(open) => {
          if (!open) setEditing(null);
        }}
      >
        <DialogContent className="max-w-xl">
          <DialogHeader>
            <DialogTitle>
              {editingOriginal
                ? t("mcp.editTitle", { name: editingOriginal })
                : t("mcp.addTitle")}
            </DialogTitle>
            <DialogDescription>
              {t("mcp.dialogDescription")}
            </DialogDescription>
          </DialogHeader>

          {editing && (
            <div className="flex flex-col gap-3">
              {editingOriginal === null && (
                <div className="flex flex-col gap-1 text-xs">
                  <label htmlFor="mcp-name">{t("mcp.name")}</label>
                  <Input
                    id="mcp-name"
                    value={editing.name}
                    onChange={(e) =>
                      setEditing({ ...editing, name: e.target.value })
                    }
                    placeholder="context7"
                  />
                </div>
              )}

              <div className="flex flex-col gap-1 text-xs">
                <label htmlFor="mcp-transport">{t("mcp.transport")}</label>
                <select
                  id="mcp-transport"
                  className="h-9 rounded-md border bg-transparent px-2 text-sm"
                  value={editing.transport}
                  onChange={(e) =>
                    setEditing({
                      ...editing,
                      transport: e.target.value as McpTransport,
                    })
                  }
                >
                  {TRANSPORTS.map((option) => (
                    <option
                      key={option.value}
                      value={option.value}
                      disabled={option.value === "stdio" && !stdioEditable}
                    >
                      {t(option.labelKey)}
                    </option>
                  ))}
                </select>
              </div>

              {editing.transport === "stdio" ? (
                <>
                  <div className="flex flex-col gap-1 text-xs">
                    <label htmlFor="mcp-command">{t("mcp.command")}</label>
                    <Input
                      id="mcp-command"
                      value={editing.command}
                      onChange={(e) =>
                        setEditing({ ...editing, command: e.target.value })
                      }
                      placeholder="npx chrome-devtools-mcp@latest"
                      className="font-mono"
                    />
                  </div>
                  <div className="flex flex-col gap-1 text-xs">
                    <label htmlFor="mcp-args">{t("mcp.args")}</label>
                    <Input
                      id="mcp-args"
                      value={editing.args}
                      onChange={(e) => setEditing({ ...editing, args: e.target.value })}
                      placeholder="--headless"
                      className="font-mono"
                    />
                  </div>
                  <div className="flex flex-col gap-1 text-xs">
                    <label htmlFor="mcp-env">{t("mcp.env")}</label>
                    <Textarea
                      id="mcp-env"
                      rows={3}
                      value={editing.env}
                      onChange={(e) => setEditing({ ...editing, env: e.target.value })}
                      placeholder="API_TOKEN=..."
                      className="font-mono text-xs"
                    />
                  </div>
                </>
              ) : (
                <>
                  <div className="flex flex-col gap-1 text-xs">
                    <label htmlFor="mcp-url">{t("mcp.url")}</label>
                    <Input
                      id="mcp-url"
                      value={editing.url}
                      onChange={(e) => setEditing({ ...editing, url: e.target.value })}
                      placeholder="https://mcp.context7.com/mcp"
                      className="font-mono"
                    />
                  </div>
                  <div className="flex flex-col gap-1 text-xs">
                    <label htmlFor="mcp-headers">
                      <Trans i18nKey="admin:mcp.headers" components={{ code: <code /> }} />
                    </label>
                    <Textarea
                      id="mcp-headers"
                      rows={3}
                      value={editing.headers}
                      onChange={(e) =>
                        setEditing({ ...editing, headers: e.target.value })
                      }
                      placeholder="Authorization: Bearer ..."
                      className="font-mono text-xs"
                    />
                  </div>
                  <div className="flex flex-col gap-1 text-xs">
                    <label htmlFor="mcp-auth">
                      <Trans i18nKey="admin:mcp.auth" components={{ code: <code /> }} />
                    </label>
                    <Input
                      id="mcp-auth"
                      value={editing.auth}
                      onChange={(e) => setEditing({ ...editing, auth: e.target.value })}
                      placeholder="oauth"
                      className="font-mono"
                    />
                  </div>
                </>
              )}

              <p className="text-[11px] text-muted-foreground">
                <Trans i18nKey="admin:mcp.secretsHint" components={{ code: <code /> }} />
              </p>

              {formError && (
                <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive">
                  {formError}
                </div>
              )}
            </div>
          )}

          <DialogFooter>
            <Button variant="outline" onClick={() => setEditing(null)} disabled={saving}>
              {t("actions.cancel")}
            </Button>
            <Button onClick={handleSave} disabled={saving} className="gap-2">
              {saving ? <Loader2 className="size-4 animate-spin" /> : <Save className="size-4" />}
              {t("actions.save")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={rawOpen} onOpenChange={setRawOpen}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>{t("mcp.rawTitle")}</DialogTitle>
            <DialogDescription className="font-mono text-[11px]">{rawPath}</DialogDescription>
          </DialogHeader>
          <Textarea
            rows={18}
            value={rawContent}
            onChange={(e) => setRawContent(e.target.value)}
            className="font-mono text-xs"
          />
          <p className="text-[11px] text-muted-foreground">
            {t("mcp.rawWarning")}
          </p>
          <DialogFooter>
            <Button variant="outline" onClick={() => setRawOpen(false)} disabled={rawSaving}>
              {t("actions.cancel")}
            </Button>
            <Button onClick={handleRawSave} disabled={rawSaving} className="gap-2">
              {rawSaving ? (
                <Loader2 className="size-4 animate-spin" />
              ) : (
                <Save className="size-4" />
              )}
              {t("actions.save")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <AlertDialog
        open={deleteTarget !== null}
        onOpenChange={(open) => {
          if (!open) setDeleteTarget(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {t("mcp.removeTitle", { name: deleteTarget ?? "" })}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {t("mcp.removeDescription")}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t("actions.cancel")}</AlertDialogCancel>
            <AlertDialogAction onClick={handleDelete}>{t("mcp.remove")}</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
