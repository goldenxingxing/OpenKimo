import { useState } from "react";

import { Loader2, Plus, RefreshCcw, Save, Trash2 } from "lucide-react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Textarea } from "@/components/ui/textarea";
import {
  type MemoryKind,
  usePersistentMemory,
  useRecentSummaries,
  useCandidates,
} from "@/hooks/useMemory";

type Tab = "persistent" | "recent" | "suggested";

/** i18n keys (`memory` namespace) of each kind's label; translated at render. */
const KIND_LABEL_KEYS: Record<MemoryKind, string> = {
  user: "kind.user",
  feedback: "kind.feedback",
  project: "kind.project",
  reference: "kind.reference",
};

function fmtTime(ts: number, locale: string): string {
  return new Date(ts * 1000).toLocaleString(locale);
}

export function MemoryPanel() {
  const { t } = useTranslation("memory");
  const [tab, setTab] = useState<Tab>("persistent");
  // One instance, shared with the tab below. Two of them fetched the list
  // twice, and the badge's copy was never refreshed — it kept counting
  // suggestions the person had already answered.
  const candidatesState = useCandidates();
  const { candidates } = candidatesState;

  return (
    <div className="flex flex-col gap-3 p-3">
      <div className="flex gap-2">
        <Button
          size="sm"
          variant={tab === "persistent" ? "default" : "outline"}
          onClick={() => setTab("persistent")}
        >
          {t("tabs.persistent")}
        </Button>
        <Button
          size="sm"
          variant={tab === "suggested" ? "default" : "outline"}
          onClick={() => setTab("suggested")}
        >
          {t("tabs.suggested")}
          {candidates.length > 0 && (
            <span className="ml-1.5 rounded-full bg-primary/15 px-1.5 text-xs font-medium">
              {candidates.length}
            </span>
          )}
        </Button>
        <Button
          size="sm"
          variant={tab === "recent" ? "default" : "outline"}
          onClick={() => setTab("recent")}
        >
          {t("tabs.recent")}
        </Button>
      </div>
      {/* Keep tabs mounted so switching back does not refetch or lose input. */}
      <div className={tab === "persistent" ? "" : "hidden"}>
        <PersistentTab />
      </div>
      <div className={tab === "suggested" ? "" : "hidden"}>
        <SuggestedTab state={candidatesState} />
      </div>
      <div className={tab === "recent" ? "" : "hidden"}>
        <RecentTab />
      </div>
    </div>
  );
}

function SuggestedTab({
  state,
}: {
  state: ReturnType<typeof useCandidates>;
}) {
  const { t, i18n } = useTranslation(["memory", "common"]);
  const { candidates, loading, error, promote, dismiss } = state;
  const [busy, setBusy] = useState(false);

  const decide = async (action: () => Promise<void>, done: string) => {
    if (busy) return;
    setBusy(true);
    try {
      await action();
      toast.success(done);
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (loading) {
    return (
      <p className="text-sm text-muted-foreground">{t("common:states.loading")}</p>
    );
  }
  if (error) return <p className="text-sm text-destructive">{error}</p>;
  if (candidates.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        {t("suggested.empty")}
      </p>
    );
  }

  return (
    <div className="flex flex-col gap-3">
      {candidates.map((c) => (
        <Card key={c.id}>
          <CardHeader className="flex flex-row items-center justify-between">
            <span className="text-xs text-muted-foreground">
              {t(KIND_LABEL_KEYS[c.kind])} · {fmtTime(c.created_at, i18n.language)}
            </span>
            <div className="flex gap-2">
              <Button size="sm" disabled={busy} onClick={() => decide(() => promote(c.id), t("suggested.kept"))}>
                {t("suggested.keep")}
              </Button>
              <Button
                size="sm"
                variant="outline"
                disabled={busy}
                onClick={() => decide(() => dismiss(c.id), t("suggested.discarded"))}
              >
                {t("suggested.discard")}
              </Button>
            </div>
          </CardHeader>
          <CardContent>
            <p className="whitespace-pre-wrap text-sm">{c.content}</p>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

function PersistentTab() {
  const { t } = useTranslation(["memory", "common"]);
  const { entries, loading, error, refresh, add, update, remove } = usePersistentMemory();
  const [newKind, setNewKind] = useState<MemoryKind>("user");
  const [newContent, setNewContent] = useState("");
  const [adding, setAdding] = useState(false);

  const handleAdd = async () => {
    const trimmed = newContent.trim();
    if (!trimmed || adding) return;
    setAdding(true);
    try {
      await add(newKind, trimmed);
      setNewContent("");
      toast.success(t("persistent.added"));
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setAdding(false);
    }
  };

  return (
    <div className="flex flex-col gap-3">
      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <CardTitle className="text-sm">{t("persistent.addTitle")}</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-2">
          <select
            className="rounded border px-2 py-1 text-sm"
            value={newKind}
            onChange={(e) => setNewKind(e.target.value as MemoryKind)}
          >
            {Object.entries(KIND_LABEL_KEYS).map(([k, labelKey]) => (
              <option key={k} value={k}>
                {t(labelKey)}
              </option>
            ))}
          </select>
          <Textarea
            value={newContent}
            onChange={(e) => setNewContent(e.target.value)}
            placeholder={t("persistent.placeholder")}
            rows={3}
          />
          <div className="flex justify-end">
            <Button size="sm" onClick={handleAdd} disabled={!newContent.trim() || adding}>
              <Plus className="mr-1 h-3.5 w-3.5" /> {t("persistent.add")}
            </Button>
          </div>
        </CardContent>
      </Card>

      <div className="flex items-center justify-between">
        <div className="text-sm font-medium">
          {loading
            ? t("common:states.loading")
            : t("persistent.entryCount", { count: entries.length })}
        </div>
        <Button
          size="sm"
          variant="outline"
          onClick={() => {
            refresh();
          }}
        >
          <RefreshCcw className="mr-1 h-3.5 w-3.5" /> {t("refresh")}
        </Button>
      </div>
      {error && <div className="text-sm text-red-500">{error}</div>}

      <ul className="flex flex-col gap-2">
        {entries.map((e) => (
          <li key={e.id}>
            <PersistentEntryItem
              entry={e}
              onSave={(content) => update(e.id, content)}
              onDelete={() => remove(e.id)}
            />
          </li>
        ))}
      </ul>
    </div>
  );
}

function PersistentEntryItem({
  entry,
  onSave,
  onDelete,
}: {
  entry: { id: string; kind: MemoryKind; content: string; created_at: number };
  onSave: (content: string) => Promise<void>;
  onDelete: () => Promise<void>;
}) {
  const { t, i18n } = useTranslation(["memory", "common"]);
  const [content, setContent] = useState(entry.content);
  const [busy, setBusy] = useState(false);
  const dirty = content !== entry.content;

  const handleSave = async () => {
    setBusy(true);
    try {
      await onSave(content);
      toast.success(t("persistent.updated"));
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const handleDelete = async () => {
    setBusy(true);
    try {
      await onDelete();
      toast.success(t("persistent.deleted"));
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card>
      <CardContent className="flex flex-col gap-2 p-3">
        <div className="flex items-center justify-between text-xs text-muted-foreground">
          <span className="flex items-center gap-2">
            <Badge variant="outline">{t(KIND_LABEL_KEYS[entry.kind])}</Badge>
            <span>{fmtTime(entry.created_at, i18n.language)}</span>
          </span>
          <span className="font-mono">{entry.id.slice(0, 8)}</span>
        </div>
        <Textarea value={content} onChange={(e) => setContent(e.target.value)} rows={3} />
        <div className="flex justify-end gap-2">
          <Button
            size="sm"
            variant="outline"
            onClick={handleDelete}
            disabled={busy}
          >
            <Trash2 className="mr-1 h-3.5 w-3.5" /> {t("persistent.delete")}
          </Button>
          <Button size="sm" onClick={handleSave} disabled={!dirty || busy}>
            {busy ? <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" /> : <Save className="mr-1 h-3.5 w-3.5" />}
            {t("common:actions.save")}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

function RecentTab() {
  const { t, i18n } = useTranslation(["memory", "common"]);
  const { items, loading, error, refresh } = useRecentSummaries(50);

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between">
        <div className="text-sm font-medium">
          {loading
            ? t("common:states.loading")
            : t("recent.summaryCount", { count: items.length })}
        </div>
        <Button
          size="sm"
          variant="outline"
          onClick={() => {
            refresh();
          }}
        >
          <RefreshCcw className="mr-1 h-3.5 w-3.5" /> {t("refresh")}
        </Button>
      </div>
      {error && <div className="text-sm text-red-500">{error}</div>}
      <ul className="flex flex-col gap-2">
        {items.map((s) => (
          <li key={s.id}>
            <Card>
              <CardContent className="p-3 text-sm">
                <div className="flex justify-between text-xs text-muted-foreground">
                  <span className="flex items-center gap-2">
                    <Badge variant="outline">{s.trigger}</Badge>
                    <span>{fmtTime(s.created_at, i18n.language)}</span>
                  </span>
                  <span className="font-mono">{s.session_id.slice(0, 8)}</span>
                </div>
                <pre className="mt-1 whitespace-pre-wrap font-sans">{s.summary}</pre>
              </CardContent>
            </Card>
          </li>
        ))}
      </ul>
    </div>
  );
}
