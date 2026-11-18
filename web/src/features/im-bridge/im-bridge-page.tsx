import { useState, useCallback, useEffect } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import {
  ArrowLeft,
  Plus,
  Pencil,
  Trash2,
  Users,
  Link2,
  Loader2,
  MessagesSquare,
  UserCheck,
  ExternalLink,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Switch } from "@/components/ui/switch";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  useImChannels,
  DEFAULT_IM_CHANNEL_CONFIG,
  type ImChannel,
  type ImChannelBinding,
  type ImPendingUser,
} from "@/hooks/useImBridge";

type ChannelFormState = {
  type: "feishu" | "dingtalk";
  name: string;
  app_id: string;
  app_secret: string;
  show_thinking: boolean;
  group_chat_enabled: boolean;
  timeout_seconds: string;
};

const EMPTY_CHANNEL_FORM: ChannelFormState = {
  type: "feishu",
  name: "",
  app_id: "",
  app_secret: "",
  show_thinking: DEFAULT_IM_CHANNEL_CONFIG.show_thinking,
  group_chat_enabled: DEFAULT_IM_CHANNEL_CONFIG.group_chat_enabled,
  timeout_seconds: String(DEFAULT_IM_CHANNEL_CONFIG.timeout_seconds),
};

function channelToForm(channel: ImChannel): ChannelFormState {
  return {
    type: channel.type === "dingtalk" ? "dingtalk" : "feishu",
    name: channel.name,
    app_id: channel.app_id,
    app_secret: "",
    show_thinking: channel.config.show_thinking,
    group_chat_enabled: channel.config.group_chat_enabled,
    timeout_seconds: String(channel.config.timeout_seconds),
  };
}

function formToConfig(form: ChannelFormState) {
  const timeout = Number.parseInt(form.timeout_seconds, 10);
  return {
    show_thinking: form.show_thinking,
    group_chat_enabled: form.group_chat_enabled,
    timeout_seconds: Number.isNaN(timeout)
      ? DEFAULT_IM_CHANNEL_CONFIG.timeout_seconds
      : timeout,
  };
}

/** Green when running, red (with the error in a tooltip) on last_error, gray when disabled. */
function ChannelStatusDot({ channel }: { channel: ImChannel }) {
  const { t } = useTranslation("imbridge");
  if (!channel.enabled) {
    return (
      <Tooltip delayDuration={300}>
        <TooltipTrigger asChild>
          <span className="size-2 rounded-full bg-muted-foreground/40" />
        </TooltipTrigger>
        <TooltipContent side="right">{t("status.disabled")}</TooltipContent>
      </Tooltip>
    );
  }
  if (channel.status.last_error) {
    return (
      <Tooltip delayDuration={300}>
        <TooltipTrigger asChild>
          <span className="size-2 rounded-full bg-red-500" />
        </TooltipTrigger>
        <TooltipContent side="right" className="max-w-md">
          {t("status.error")}: {channel.status.last_error}
        </TooltipContent>
      </Tooltip>
    );
  }
  if (channel.status.running) {
    return (
      <Tooltip delayDuration={300}>
        <TooltipTrigger asChild>
          <span className="size-2 rounded-full bg-emerald-500" />
        </TooltipTrigger>
        <TooltipContent side="right">{t("status.running")}</TooltipContent>
      </Tooltip>
    );
  }
  return (
    <Tooltip delayDuration={300}>
      <TooltipTrigger asChild>
        <span className="size-2 rounded-full bg-amber-500" />
      </TooltipTrigger>
      <TooltipContent side="right">{t("status.stopped")}</TooltipContent>
    </Tooltip>
  );
}

export function ImBridgePage({
  onClose,
  onOpenSession,
}: {
  onClose: () => void;
  /** When provided, bindings rows show a "view conversation" jump. */
  onOpenSession?: (sessionId: string) => void;
}) {
  const { t, i18n } = useTranslation("imbridge");
  const {
    channels,
    loading,
    error,
    refresh,
    createChannel,
    updateChannel,
    deleteChannel,
    getWhitelist,
    setWhitelist,
    getBindings,
    getPending,
    approvePending,
    dismissPending,
  } = useImChannels();

  // Add / edit dialog. mode "add" has channel === null.
  const [dialog, setDialog] = useState<{
    mode: "add" | "edit";
    channel: ImChannel | null;
    form: ChannelFormState;
    saving: boolean;
  } | null>(null);

  const [whitelistDialog, setWhitelistDialog] = useState<{
    channel: ImChannel;
    text: string;
    saving: boolean;
  } | null>(null);

  // Bindings region expanded inside a channel card.
  const [bindingsFor, setBindingsFor] = useState<number | null>(null);
  const [bindings, setBindings] = useState<ImChannelBinding[]>([]);
  const [bindingsLoading, setBindingsLoading] = useState(false);

  // Pending-authorization dialog. Polls while open.
  const [pendingDialog, setPendingDialog] = useState<{ channel: ImChannel } | null>(null);
  const [pendingItems, setPendingItems] = useState<ImPendingUser[]>([]);
  const [pendingLoading, setPendingLoading] = useState(false);
  const [pendingBusy, setPendingBusy] = useState<string | null>(null);

  const loadPending = useCallback(
    async (channelId: number) => {
      try {
        setPendingItems(await getPending(channelId));
      } catch (err) {
        // Keep the previous items on a failed poll; the next tick retries.
        console.error("Failed to load pending users:", err);
      }
    },
    [getPending],
  );

  const openPendingDialog = useCallback(
    async (channel: ImChannel) => {
      setPendingDialog({ channel });
      setPendingItems([]);
      setPendingLoading(true);
      try {
        setPendingItems(await getPending(channel.id));
      } catch (err) {
        const message = err instanceof Error ? err.message : t("pending.loadFailed");
        toast.error(t("pending.loadFailed"), { description: message });
      } finally {
        setPendingLoading(false);
      }
    },
    [getPending, t],
  );

  // Poll the pending list every 10s while the dialog is open.
  useEffect(() => {
    if (!pendingDialog) {
      return;
    }
    const channelId = pendingDialog.channel.id;
    const timer = window.setInterval(() => {
      loadPending(channelId);
    }, 10_000);
    return () => window.clearInterval(timer);
  }, [pendingDialog, loadPending]);

  const handleApprovePending = useCallback(
    async (externalId: string) => {
      if (!pendingDialog) {
        return;
      }
      setPendingBusy(externalId);
      try {
        await approvePending(pendingDialog.channel.id, externalId);
        toast.success(t("pending.approved"));
        await loadPending(pendingDialog.channel.id);
      } catch (err) {
        const message = err instanceof Error ? err.message : t("pending.actionFailed");
        toast.error(t("pending.actionFailed"), { description: message });
      } finally {
        setPendingBusy(null);
      }
    },
    [pendingDialog, approvePending, loadPending, t],
  );

  const handleDismissPending = useCallback(
    async (externalId: string) => {
      if (!pendingDialog) {
        return;
      }
      setPendingBusy(externalId);
      try {
        await dismissPending(pendingDialog.channel.id, externalId);
        toast.success(t("pending.dismissed"));
        await loadPending(pendingDialog.channel.id);
      } catch (err) {
        const message = err instanceof Error ? err.message : t("pending.actionFailed");
        toast.error(t("pending.actionFailed"), { description: message });
      } finally {
        setPendingBusy(null);
      }
    },
    [pendingDialog, dismissPending, loadPending, t],
  );

  const openAddDialog = useCallback(() => {
    setDialog({ mode: "add", channel: null, form: EMPTY_CHANNEL_FORM, saving: false });
  }, []);

  const openEditDialog = useCallback((channel: ImChannel) => {
    setDialog({
      mode: "edit",
      channel,
      form: channelToForm(channel),
      saving: false,
    });
  }, []);

  const updateFormField = useCallback(
    <K extends keyof ChannelFormState>(field: K, value: ChannelFormState[K]) => {
      setDialog((prev) =>
        prev ? { ...prev, form: { ...prev.form, [field]: value } } : prev,
      );
    },
    [],
  );

  const handleSaveDialog = useCallback(async () => {
    if (!dialog) {
      return;
    }
    const { mode, channel, form } = dialog;
    if (mode === "add" && !form.app_secret.trim()) {
      toast.error(t("channel.fields.secretRequired"));
      return;
    }
    setDialog({ ...dialog, saving: true });
    try {
      if (mode === "add") {
        await createChannel({
          type: form.type,
          name: form.name.trim(),
          app_id: form.app_id.trim(),
          app_secret: form.app_secret,
          config: formToConfig(form),
        });
        toast.success(t("toast.created"));
      } else if (channel) {
        await updateChannel(channel.id, {
          name: form.name.trim(),
          app_id: form.app_id.trim(),
          // Empty string means "leave unchanged" on the backend.
          app_secret: form.app_secret,
          config: formToConfig(form),
        });
        toast.success(t("toast.saved"));
      }
      setDialog(null);
    } catch (err) {
      const message = err instanceof Error ? err.message : t("toast.error");
      toast.error(t("toast.error"), { description: message });
      setDialog((prev) => (prev ? { ...prev, saving: false } : prev));
    }
  }, [dialog, createChannel, updateChannel, t]);

  const handleToggleEnabled = useCallback(
    async (channel: ImChannel, enabled: boolean) => {
      try {
        await updateChannel(channel.id, { enabled });
        toast.success(t("toast.saved"));
      } catch (err) {
        const message = err instanceof Error ? err.message : t("toast.error");
        toast.error(t("toast.error"), { description: message });
      }
    },
    [updateChannel, t],
  );

  const handleDelete = useCallback(
    async (channel: ImChannel) => {
      // biome-ignore lint/suspicious/noAlert: per spec, deletion uses a simple window.confirm
      if (!window.confirm(t("channel.deleteConfirm", { name: channel.name }))) {
        return;
      }
      try {
        await deleteChannel(channel.id);
        toast.success(t("toast.deleted"));
      } catch (err) {
        const message = err instanceof Error ? err.message : t("toast.error");
        toast.error(t("toast.error"), { description: message });
      }
    },
    [deleteChannel, t],
  );

  const openWhitelistDialog = useCallback(
    async (channel: ImChannel) => {
      setWhitelistDialog({ channel, text: "", saving: false });
      try {
        const openIds = await getWhitelist(channel.id);
        setWhitelistDialog({ channel, text: openIds.join("\n"), saving: false });
      } catch (err) {
        const message = err instanceof Error ? err.message : t("whitelist.loadFailed");
        toast.error(t("whitelist.loadFailed"), { description: message });
        setWhitelistDialog(null);
      }
    },
    [getWhitelist, t],
  );

  const handleSaveWhitelist = useCallback(async () => {
    if (!whitelistDialog) {
      return;
    }
    const openIds = whitelistDialog.text
      .split("\n")
      .map((line) => line.trim())
      .filter((line) => line.length > 0);
    setWhitelistDialog({ ...whitelistDialog, saving: true });
    try {
      await setWhitelist(whitelistDialog.channel.id, openIds);
      toast.success(t("whitelist.saved"));
      setWhitelistDialog(null);
    } catch (err) {
      const message = err instanceof Error ? err.message : t("whitelist.saveFailed");
      toast.error(t("whitelist.saveFailed"), { description: message });
      setWhitelistDialog((prev) => (prev ? { ...prev, saving: false } : prev));
    }
  }, [whitelistDialog, setWhitelist, t]);

  const toggleBindings = useCallback(
    async (channel: ImChannel) => {
      if (bindingsFor === channel.id) {
        setBindingsFor(null);
        return;
      }
      setBindingsFor(channel.id);
      setBindingsLoading(true);
      try {
        setBindings(await getBindings(channel.id));
      } catch (err) {
        const message = err instanceof Error ? err.message : t("bindings.loadFailed");
        toast.error(t("bindings.loadFailed"), { description: message });
        setBindingsFor(null);
      } finally {
        setBindingsLoading(false);
      }
    },
    [bindingsFor, getBindings, t],
  );

  const copySessionId = useCallback(
    async (sessionId: string) => {
      try {
        await navigator.clipboard.writeText(sessionId);
        toast.success(t("bindings.copied"));
      } catch {
        toast.error(t("toast.error"));
      }
    },
    [t],
  );

  const renderChannelCard = (channel: ImChannel) => (
    <Card key={channel.id}>
      <CardContent className="flex flex-col gap-3 p-4">
        <div className="flex flex-wrap items-center gap-2">
          <MessagesSquare className="size-4 shrink-0 text-muted-foreground" />
          <span className="truncate font-medium text-foreground">
            {channel.name}
          </span>
          <Badge variant="secondary">{channel.type}</Badge>
          <ChannelStatusDot channel={channel} />
          <div className="ml-auto flex items-center gap-1.5">
            <Switch
              checked={channel.enabled}
              onCheckedChange={(enabled) => handleToggleEnabled(channel, enabled)}
              aria-label={channel.name}
            />
            <Button
              variant="outline"
              size="sm"
              className="gap-1.5"
              onClick={() => openEditDialog(channel)}
            >
              <Pencil className="size-3.5" />
              {t("channel.edit")}
            </Button>
            <Button
              variant="outline"
              size="sm"
              className="gap-1.5"
              onClick={() => openWhitelistDialog(channel)}
            >
              <Users className="size-3.5" />
              {t("channel.whitelist")}
            </Button>
            <Button
              variant="outline"
              size="sm"
              className="gap-1.5"
              onClick={() => openPendingDialog(channel)}
            >
              <UserCheck className="size-3.5" />
              {t("pending.title")}
            </Button>
            <Button
              variant="outline"
              size="sm"
              className="gap-1.5"
              onClick={() => toggleBindings(channel)}
            >
              <Link2 className="size-3.5" />
              {t("channel.bindings")}
            </Button>
            <Button
              variant="ghost"
              size="sm"
              className="gap-1.5 text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
              onClick={() => handleDelete(channel)}
            >
              <Trash2 className="size-3.5" />
              {t("channel.delete")}
            </Button>
          </div>
        </div>
        <div className="text-xs text-muted-foreground">
          {t(`channel.types.${channel.type}`)} · {t(`channel.fields.${channel.type}AppId`)}
          : {channel.app_id}
        </div>
        {bindingsFor === channel.id && (
          <div className="rounded-lg border bg-muted/20 p-3">
            {bindingsLoading ? (
              <div className="flex items-center justify-center py-3">
                <Loader2 className="size-4 animate-spin text-muted-foreground" />
              </div>
            ) : bindings.length === 0 ? (
              <p className="text-xs text-muted-foreground">
                {t("bindings.empty")}
              </p>
            ) : (
              <div className="flex flex-col gap-2">
                {bindings.map((binding) => (
                  <div
                    key={binding.external_id}
                    className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs"
                  >
                    <span className="min-w-24 font-medium text-foreground">
                      {binding.display_name}
                    </span>
                    <span className="text-muted-foreground">
                      {binding.external_id}
                    </span>
                    <button
                      type="button"
                      className="cursor-pointer font-mono text-muted-foreground underline decoration-dotted underline-offset-2 transition-colors hover:text-foreground"
                      onClick={() => copySessionId(binding.session_id)}
                    >
                      {binding.session_id}
                    </button>
                    {onOpenSession && (
                      <Button
                        variant="secondary"
                        size="sm"
                        className="gap-1.5"
                        onClick={() => onOpenSession(binding.session_id)}
                      >
                        <ExternalLink className="size-3.5" />
                        {t("bindings.viewSession")}
                      </Button>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );

  return (
    <div className="flex h-full min-h-0 flex-col overflow-y-auto">
      <div className="mx-auto flex w-full max-w-3xl flex-col gap-4 p-4">
        <div className="flex items-center gap-3">
          <Button
            variant="ghost"
            size="sm"
            className="gap-1.5"
            onClick={onClose}
          >
            <ArrowLeft className="size-4" />
            {t("page.back")}
          </Button>
          <div className="flex min-w-0 flex-1 flex-col">
            <h1 className="truncate text-lg font-semibold text-foreground">
              {t("page.title")}
            </h1>
            <p className="text-xs text-muted-foreground">
              {t("page.description")}
            </p>
          </div>
          <Button className="gap-1.5" onClick={openAddDialog}>
            <Plus className="size-4" />
            {t("channel.add")}
          </Button>
        </div>

        {loading ? (
          <div className="flex items-center justify-center gap-2 py-16 text-muted-foreground">
            <Loader2 className="size-4 animate-spin" />
            {t("page.loading")}
          </div>
        ) : error && channels.length === 0 ? (
          <div className="flex flex-col items-center gap-3 py-16 text-muted-foreground">
            <p className="text-sm">{t("page.loadFailed")}</p>
            <Button variant="outline" size="sm" onClick={() => refresh()}>
              {t("page.back")}
            </Button>
          </div>
        ) : channels.length === 0 ? (
          <div className="flex items-center justify-center py-16 text-sm text-muted-foreground">
            {t("page.empty")}
          </div>
        ) : (
          <div className="flex flex-col gap-3">
            {channels.map(renderChannelCard)}
          </div>
        )}
      </div>

      {/* Add / Edit channel dialog */}
      <Dialog
        open={dialog !== null}
        onOpenChange={(open) => !open && setDialog(null)}
      >
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>
              {dialog?.mode === "add" ? t("channel.add") : t("channel.edit")}
            </DialogTitle>
            <DialogDescription>
              {dialog?.mode === "add" ? t("page.description") : null}
            </DialogDescription>
          </DialogHeader>
          {dialog && (
            <div className="flex flex-col gap-4">
              {dialog.mode === "add" && (
                <div className="flex flex-col gap-1.5">
                  <label
                    htmlFor="im-channel-type"
                    className="text-sm font-medium text-foreground"
                  >
                    {t("channel.fields.type")}
                  </label>
                  <select
                    id="im-channel-type"
                    className="h-9 rounded-md border bg-background px-3 text-sm"
                    value={dialog.form.type}
                    onChange={(e) =>
                      updateFormField(
                        "type",
                        e.target.value as ChannelFormState["type"],
                      )
                    }
                  >
                    <option value="feishu">{t("channel.types.feishu")}</option>
                    <option value="dingtalk">{t("channel.types.dingtalk")}</option>
                  </select>
                </div>
              )}
              <div className="flex flex-col gap-1.5">
                <label
                  htmlFor="im-channel-name"
                  className="text-sm font-medium text-foreground"
                >
                  {t("channel.fields.name")}
                </label>
                <Input
                  id="im-channel-name"
                  type="text"
                  placeholder={t("channel.fields.namePlaceholder")}
                  value={dialog.form.name}
                  onChange={(e) => updateFormField("name", e.target.value)}
                />
              </div>
              <div className="flex flex-col gap-1.5">
                <label
                  htmlFor="im-channel-app-id"
                  className="text-sm font-medium text-foreground"
                >
                  {t(`channel.fields.${dialog.form.type}AppId`)}
                </label>
                <Input
                  id="im-channel-app-id"
                  type="text"
                  placeholder={t(`channel.fields.${dialog.form.type}AppIdPlaceholder`)}
                  value={dialog.form.app_id}
                  onChange={(e) => updateFormField("app_id", e.target.value)}
                />
              </div>
              <div className="flex flex-col gap-1.5">
                <label
                  htmlFor="im-channel-app-secret"
                  className="text-sm font-medium text-foreground"
                >
                  {t(`channel.fields.${dialog.form.type}AppSecret`)}
                </label>
                <Input
                  id="im-channel-app-secret"
                  type="password"
                  placeholder={
                    dialog.mode === "edit"
                      ? t("channel.fields.secretUnchanged")
                      : t(`channel.fields.${dialog.form.type}AppSecretPlaceholder`)
                  }
                  value={dialog.form.app_secret}
                  onChange={(e) => updateFormField("app_secret", e.target.value)}
                />
                {dialog.mode === "edit" && (
                  <p className="text-xs text-muted-foreground">
                    {t("channel.fields.secretUnchanged")}
                  </p>
                )}
              </div>
              <div className="flex flex-col gap-1.5">
                <label
                  htmlFor="im-channel-timeout"
                  className="text-sm font-medium text-foreground"
                >
                  {t("channel.fields.timeout")}
                </label>
                <Input
                  id="im-channel-timeout"
                  type="number"
                  min={0}
                  value={dialog.form.timeout_seconds}
                  onChange={(e) =>
                    updateFormField("timeout_seconds", e.target.value)
                  }
                />
              </div>
              <div className="flex items-center justify-between gap-3">
                <div className="flex min-w-0 flex-col">
                  <span className="text-sm font-medium text-foreground">
                    {t("channel.fields.showThinking")}
                  </span>
                  <span className="text-xs text-muted-foreground">
                    {t("channel.fields.showThinkingHint")}
                  </span>
                </div>
                <Switch
                  checked={dialog.form.show_thinking}
                  onCheckedChange={(value) =>
                    updateFormField("show_thinking", value)
                  }
                />
              </div>
              <div className="flex items-center justify-between gap-3">
                <div className="flex min-w-0 flex-col">
                  <span className="text-sm font-medium text-foreground">
                    {t("channel.fields.groupChat")}
                  </span>
                  <span className="text-xs text-muted-foreground">
                    {t("channel.fields.groupChatHint")}
                  </span>
                </div>
                <Switch
                  checked={dialog.form.group_chat_enabled}
                  onCheckedChange={(value) =>
                    updateFormField("group_chat_enabled", value)
                  }
                />
              </div>
            </div>
          )}
          <DialogFooter className="gap-2">
            <Button
              variant="outline"
              onClick={() => setDialog(null)}
              disabled={dialog?.saving}
            >
              {t("actions.cancel")}
            </Button>
            <Button onClick={handleSaveDialog} disabled={dialog?.saving}>
              {dialog?.saving ? (
                <>
                  <Loader2 className="size-4 animate-spin" />
                  {dialog.mode === "add"
                    ? t("actions.creating")
                    : t("actions.saving")}
                </>
              ) : dialog?.mode === "add" ? (
                t("actions.create")
              ) : (
                t("actions.save")
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Whitelist dialog */}
      <Dialog
        open={whitelistDialog !== null}
        onOpenChange={(open) => !open && setWhitelistDialog(null)}
      >
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>
              {t("whitelist.title")}
              {whitelistDialog ? ` — ${whitelistDialog.channel.name}` : ""}
            </DialogTitle>
            <DialogDescription>
              {t("whitelist.description")}
            </DialogDescription>
          </DialogHeader>
          <Textarea
            rows={8}
            placeholder={t("whitelist.placeholder")}
            value={whitelistDialog?.text ?? ""}
            onChange={(e) =>
              setWhitelistDialog((prev) =>
                prev ? { ...prev, text: e.target.value } : prev,
              )
            }
          />
          <DialogFooter className="gap-2">
            <Button
              variant="outline"
              onClick={() => setWhitelistDialog(null)}
              disabled={whitelistDialog?.saving}
            >
              {t("actions.cancel")}
            </Button>
            <Button
              onClick={handleSaveWhitelist}
              disabled={whitelistDialog?.saving}
            >
              {whitelistDialog?.saving ? (
                <>
                  <Loader2 className="size-4 animate-spin" />
                  {t("whitelist.saving")}
                </>
              ) : (
                t("whitelist.save")
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Pending authorization dialog */}
      <Dialog
        open={pendingDialog !== null}
        onOpenChange={(open) => !open && setPendingDialog(null)}
      >
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>
              {t("pending.title")}
              {pendingDialog ? ` — ${pendingDialog.channel.name}` : ""}
            </DialogTitle>
            <DialogDescription>
              {t("pending.description")}
            </DialogDescription>
          </DialogHeader>
          {pendingLoading ? (
            <div className="flex items-center justify-center py-6">
              <Loader2 className="size-4 animate-spin text-muted-foreground" />
            </div>
          ) : pendingItems.length === 0 ? (
            <p className="py-4 text-sm text-muted-foreground">
              {t("pending.empty")}
            </p>
          ) : (
            <div className="flex max-h-[50vh] flex-col gap-2 overflow-y-auto">
              {pendingItems.map((item) => (
                <div
                  key={item.external_id}
                  className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border bg-muted/20 p-3"
                >
                  <div className="flex min-w-0 flex-1 flex-col gap-0.5">
                    <span className="truncate text-sm font-medium text-foreground">
                      {item.display_name}
                    </span>
                    <span className="truncate font-mono text-xs text-muted-foreground">
                      {item.external_id}
                    </span>
                    <span className="text-xs text-muted-foreground">
                      {t("pending.lastSeen")}:{" "}
                      {new Date(item.last_seen * 1000).toLocaleString(
                        i18n.language,
                      )}{" "}
                      · {t("pending.messages", { count: item.message_count })}
                    </span>
                  </div>
                  <div className="flex shrink-0 items-center gap-1.5">
                    <Button
                      size="sm"
                      className="gap-1.5"
                      disabled={pendingBusy !== null}
                      onClick={() => handleApprovePending(item.external_id)}
                    >
                      {pendingBusy === item.external_id ? (
                        <Loader2 className="size-3.5 animate-spin" />
                      ) : (
                        <UserCheck className="size-3.5" />
                      )}
                      {t("pending.approve")}
                    </Button>
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={pendingBusy !== null}
                      onClick={() => handleDismissPending(item.external_id)}
                    >
                      {t("pending.dismiss")}
                    </Button>
                  </div>
                </div>
              ))}
            </div>
          )}
          <DialogFooter className="gap-2">
            <Button variant="outline" onClick={() => setPendingDialog(null)}>
              {t("actions.close")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
