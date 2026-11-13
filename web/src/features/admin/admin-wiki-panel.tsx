import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Trans, useTranslation } from "react-i18next";

import i18n from "@/i18n";
import {
  AlertTriangle,
  FilePlus2,
  History,
  Loader2,
  Pencil,
  RefreshCw,
  Save,
  Search,
  Stethoscope,
  Trash2,
  X,
} from "lucide-react";
import { toast } from "sonner";
import {
  type WikiLintReport,
  type WikiLogEntry,
  type WikiOverview,
  type WikiPageDetail,
  type WikiSearchHit,
  WikiApiError,
  deleteWikiPage,
  getWikiLog,
  getWikiOverview,
  getWikiPage,
  lintWiki,
  saveWikiPage,
  searchWiki,
} from "@/lib/api/apis/AdminWikiApi";
import { MessageResponse } from "@/components/ai-elements/message";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
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
import {
  isValidSlug,
  pageSlug,
  parseTags,
  suggestSlug,
  wikiLinks,
} from "./wiki-utils";

type View = "pages" | "health" | "history";

type Draft = {
  mode: "create" | "edit";
  category: string;
  slug: string;
  title: string;
  tags: string;
  body: string;
  /** Null when creating. */
  expectedRevision: number | null;
};

function message(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}

function formatTime(value: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString(i18n.language);
}

function draftFrom(page: WikiPageDetail): Draft {
  return {
    mode: "edit",
    category: page.path.split("/", 1)[0],
    slug: pageSlug(page.path),
    title: page.title,
    tags: page.tags.join(", "),
    body: page.body,
    expectedRevision: page.revision,
  };
}

function draftIsDirty(draft: Draft, page: WikiPageDetail | null): boolean {
  if (draft.mode === "create") {
    return Boolean(draft.slug || draft.title || draft.tags || draft.body);
  }
  if (!page) return false;
  return (
    draft.title !== page.title ||
    draft.body !== page.body ||
    parseTags(draft.tags).join("\n") !== page.tags.join("\n")
  );
}

export function AdminWikiPanel() {
  const { t } = useTranslation("wiki");
  // Unknown directories (an install may add its own) show under their own name.
  const categoryLabel = (category: string) =>
    t(`categories.${category}`, { defaultValue: category });
  const [overview, setOverview] = useState<WikiOverview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [view, setView] = useState<View>("pages");

  const [category, setCategory] = useState<string>("all");
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<WikiSearchHit[] | null>(null);
  const [searching, setSearching] = useState(false);

  const [selected, setSelected] = useState<string | null>(null);
  const [page, setPage] = useState<WikiPageDetail | null>(null);
  const [pageLoading, setPageLoading] = useState(false);
  const [pageError, setPageError] = useState<string | null>(null);

  const [draft, setDraft] = useState<Draft | null>(null);
  const [saving, setSaving] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);

  const [lint, setLint] = useState<WikiLintReport | null>(null);
  const [lintLoading, setLintLoading] = useState(false);
  const [log, setLog] = useState<WikiLogEntry[] | null>(null);
  const [logLoading, setLogLoading] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setOverview(await getWikiOverview());
    } catch (err) {
      setError(message(err, t("errors.loadWiki")));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // Only the latest request may fill the page: clicking A then B, with A
  // slower, must not leave B's title over A's content — and an edit saved
  // from that view would write A's text into B.
  const pageRequest = useRef(0);
  const loadPage = useCallback(async (path: string) => {
    const request = ++pageRequest.current;
    setPageLoading(true);
    setPageError(null);
    try {
      const loaded = await getWikiPage(path);
      if (request === pageRequest.current) setPage(loaded);
    } catch (err) {
      if (request !== pageRequest.current) return;
      setPage(null);
      setPageError(message(err, t("errors.loadPage")));
    } finally {
      if (request === pageRequest.current) setPageLoading(false);
    }
  }, [t]);

  useEffect(() => {
    if (!selected) {
      pageRequest.current++;
      setPage(null);
      setPageLoading(false);
      return;
    }
    const summary = overview?.pages.find((item) => item.path === selected);
    if (summary?.error) {
      pageRequest.current++;
      // Nothing to fetch: the server cannot parse it either.
      setPage(null);
      setPageError(null);
      setPageLoading(false);
      return;
    }
    loadPage(selected);
  }, [selected, overview, loadPage]);

  // Full-text search runs on the server; an empty query shows the whole list.
  useEffect(() => {
    const trimmed = query.trim();
    if (!trimmed) {
      setHits(null);
      setSearching(false);
      return;
    }
    let cancelled = false;
    setSearching(true);
    const timer = window.setTimeout(() => {
      searchWiki(trimmed)
        .then((result) => {
          if (!cancelled) setHits(result);
        })
        .catch((err) => {
          if (!cancelled) toast.error(message(err, t("errors.search")));
        })
        .finally(() => {
          if (!cancelled) setSearching(false);
        });
    }, 250);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [query, t]);

  const loadLint = useCallback(async () => {
    setLintLoading(true);
    try {
      setLint(await lintWiki());
    } catch (err) {
      toast.error(message(err, t("errors.health")));
    } finally {
      setLintLoading(false);
    }
  }, [t]);

  const loadLog = useCallback(async () => {
    setLogLoading(true);
    try {
      setLog(await getWikiLog(200));
    } catch (err) {
      toast.error(message(err, t("errors.history")));
    } finally {
      setLogLoading(false);
    }
  }, [t]);

  useEffect(() => {
    if (view === "health" && lint === null) loadLint();
    if (view === "history" && log === null) loadLog();
  }, [view, lint, log, loadLint, loadLog]);

  const dirty = draft !== null && draftIsDirty(draft, page);

  const open = useCallback(
    (path: string) => {
      if (dirty) {
        toast.warning(t("errors.finishEditFirst"));
        return;
      }
      setDraft(null);
      setView("pages");
      setSelected(path);
    },
    [dirty, t],
  );

  const startCreate = useCallback(() => {
    if (dirty) {
      toast.warning(t("errors.finishEditFirst"));
      return;
    }
    setView("pages");
    setSelected(null);
    setDraft({
      mode: "create",
      category: category === "all" ? "concepts" : category,
      slug: "",
      title: "",
      tags: "",
      body: "",
      expectedRevision: null,
    });
  }, [category, dirty, t]);

  const afterWrite = useCallback(() => {
    // History and health describe the Wiki as it was; drop them so they reload.
    setLog(null);
    setLint(null);
    return refresh();
  }, [refresh]);

  const handleSave = useCallback(async () => {
    if (!draft) return;
    const slug = draft.slug.trim();
    if (!isValidSlug(slug)) {
      toast.error(t("errors.invalidSlug"));
      return;
    }
    const path = `${draft.category}/${slug}.md`;
    setSaving(true);
    try {
      await saveWikiPage(path, {
        title: draft.title.trim(),
        tags: parseTags(draft.tags),
        body: draft.body.endsWith("\n") ? draft.body : `${draft.body}\n`,
        expected_revision: draft.expectedRevision,
      });
      toast.success(draft.mode === "create" ? t("toasts.created") : t("toasts.saved"));
      setDraft(null);
      // Refreshing the list reloads the open page; a new one is opened here.
      await afterWrite();
      setSelected(path);
    } catch (err) {
      if (err instanceof WikiApiError && err.status === 409) {
        toast.error(
          draft.mode === "create"
            ? t("errors.pageExists")
            : t("errors.editConflict"),
          { duration: 10000 },
        );
      } else {
        toast.error(message(err, t("errors.save")));
      }
    } finally {
      setSaving(false);
    }
  }, [draft, afterWrite, t]);

  const handleDelete = useCallback(async () => {
    if (!page) return;
    setDeleting(true);
    try {
      await deleteWikiPage(page.path, page.revision);
      toast.success(t("toasts.deleted"));
      setConfirmDelete(false);
      setSelected(null);
      await afterWrite();
    } catch (err) {
      toast.error(
        err instanceof WikiApiError && err.status === 409
          ? t("errors.deleteConflict")
          : message(err, t("errors.delete")),
      );
    } finally {
      setDeleting(false);
    }
  }, [page, afterWrite, t]);

  const pages = useMemo(() => {
    const all = overview?.pages ?? [];
    return category === "all" ? all : all.filter((item) => item.category === category);
  }, [overview, category]);

  const counts = useMemo(() => {
    const result: Record<string, number> = {};
    for (const item of overview?.pages ?? []) {
      result[item.category] = (result[item.category] ?? 0) + 1;
    }
    return result;
  }, [overview]);

  const known = useMemo(() => new Set(overview?.pages.map((item) => item.path)), [overview]);
  const broken = overview?.pages.filter((item) => item.error).length ?? 0;
  const selectedSummary = overview?.pages.find((item) => item.path === selected) ?? null;

  const viewButton = (value: View, label: string, Icon: typeof History) => (
    <Button
      size="sm"
      variant={view === value ? "default" : "outline"}
      onClick={() => setView(value)}
      className="gap-2"
    >
      <Icon className="size-4" />
      {label}
    </Button>
  );

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader className="flex flex-row items-start justify-between gap-2">
          <div>
            <CardTitle className="text-base">{t("header.title")}</CardTitle>
            <p className="mt-1 text-xs text-muted-foreground">
              <Trans i18nKey="wiki:header.description" components={{ code: <code /> }} />
            </p>
            {overview && (
              <p className="mt-1 font-mono text-[11px] text-muted-foreground">
                {overview.root} · {t("header.revision", { revision: overview.global_revision })}{" "}
                · {t("header.pages", { count: overview.pages.length })}
                {broken > 0 && (
                  <span className="text-destructive">
                    {" · "}
                    {t("header.unreadable", { count: broken })}
                  </span>
                )}
              </p>
            )}
          </div>
          <div className="flex shrink-0 gap-2">
            <Button size="sm" variant="outline" onClick={refresh} disabled={loading} className="gap-2">
              <RefreshCw className={loading ? "size-4 animate-spin" : "size-4"} />
              {t("header.refresh")}
            </Button>
            <Button size="sm" onClick={startCreate} disabled={!overview} className="gap-2">
              <FilePlus2 className="size-4" />
              {t("header.newPage")}
            </Button>
          </div>
        </CardHeader>
        <CardContent className="flex flex-wrap gap-2">
          {viewButton("pages", t("views.pages"), Search)}
          {viewButton("health", t("views.health"), Stethoscope)}
          {viewButton("history", t("views.history"), History)}
        </CardContent>
      </Card>

      {error && (
        <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {error}
        </div>
      )}

      {view === "pages" && overview && (
        <div className="grid gap-4 lg:grid-cols-[minmax(260px,1fr)_2fr]">
          {/* Page list */}
          <Card className="min-h-0">
            <CardContent className="flex flex-col gap-3 pt-6">
              <div className="relative">
                <Search className="absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" />
                <Input
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder={t("list.searchPlaceholder")}
                  className="pl-8"
                />
              </div>
              {hits === null && (
                <div className="flex flex-wrap gap-1.5">
                  {["all", ...overview.categories].map((item) => (
                    <button
                      key={item}
                      type="button"
                      onClick={() => setCategory(item)}
                      className={[
                        "rounded-full border px-2.5 py-0.5 text-xs transition-colors",
                        category === item
                          ? "border-primary bg-primary text-primary-foreground"
                          : "text-muted-foreground hover:text-foreground",
                      ].join(" ")}
                    >
                      {item === "all" ? t("list.all") : categoryLabel(item)}{" "}
                      {item === "all" ? overview.pages.length : (counts[item] ?? 0)}
                    </button>
                  ))}
                </div>
              )}
              <div className="flex max-h-[60vh] flex-col gap-1 overflow-y-auto">
                {hits !== null ? (
                  searching && hits.length === 0 ? (
                    <p className="px-2 py-4 text-sm text-muted-foreground">{t("list.searching")}</p>
                  ) : hits.length === 0 ? (
                    <p className="px-2 py-4 text-sm text-muted-foreground">{t("list.noMatches")}</p>
                  ) : (
                    hits.map((hit) => (
                      <button
                        key={hit.path}
                        type="button"
                        onClick={() => open(hit.path)}
                        className={[
                          "rounded-md px-2 py-1.5 text-left transition-colors hover:bg-muted",
                          selected === hit.path ? "bg-muted" : "",
                        ].join(" ")}
                      >
                        <div className="truncate text-sm font-medium">{hit.title}</div>
                        <div className="truncate font-mono text-[11px] text-muted-foreground">
                          {hit.path}
                        </div>
                        {hit.snippet && (
                          <div className="line-clamp-2 text-xs text-muted-foreground">
                            {hit.snippet}
                          </div>
                        )}
                      </button>
                    ))
                  )
                ) : pages.length === 0 ? (
                  <p className="px-2 py-4 text-sm text-muted-foreground">
                    {overview.pages.length === 0
                      ? t("list.empty")
                      : t("list.emptyCategory")}
                  </p>
                ) : (
                  pages.map((item) => (
                    <button
                      key={item.path}
                      type="button"
                      onClick={() => open(item.path)}
                      className={[
                        "rounded-md px-2 py-1.5 text-left transition-colors hover:bg-muted",
                        selected === item.path ? "bg-muted" : "",
                      ].join(" ")}
                    >
                      <div className="flex items-center gap-1.5">
                        {item.error && (
                          <AlertTriangle className="size-3.5 shrink-0 text-destructive" />
                        )}
                        <span className="truncate text-sm font-medium">
                          {item.title ?? pageSlug(item.path)}
                        </span>
                      </div>
                      <div className="flex items-center gap-2 font-mono text-[11px] text-muted-foreground">
                        <span className="truncate">{item.path}</span>
                        {item.revision !== null && <span className="shrink-0">r{item.revision}</span>}
                      </div>
                    </button>
                  ))
                )}
              </div>
            </CardContent>
          </Card>

          {/* Page detail / editor */}
          <Card>
            <CardContent className="pt-6">
              {draft ? (
                <div className="flex flex-col gap-3">
                  <div className="flex items-center justify-between gap-2">
                    <h3 className="text-sm font-semibold">
                      {draft.mode === "create"
                        ? t("editor.newPage")
                        : t("editor.editTitle", { path: `${draft.category}/${draft.slug}.md` })}
                    </h3>
                    <div className="flex gap-2">
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() => setDraft(null)}
                        disabled={saving}
                        className="gap-2"
                      >
                        <X className="size-4" />
                        {t("editor.discard")}
                      </Button>
                      <Button
                        size="sm"
                        onClick={handleSave}
                        disabled={saving || !draft.title.trim() || !draft.body.trim() || !dirty}
                        className="gap-2"
                      >
                        {saving ? (
                          <Loader2 className="size-4 animate-spin" />
                        ) : (
                          <Save className="size-4" />
                        )}
                        {t("editor.save")}
                      </Button>
                    </div>
                  </div>
                  <div className="flex flex-col gap-1 text-xs text-muted-foreground">
                    <label htmlFor="wiki-title">{t("editor.title")}</label>
                    <Input
                      id="wiki-title"
                      value={draft.title}
                      onChange={(e) => {
                        const title = e.target.value;
                        setDraft((current) => {
                          if (!current) return current;
                          // Follow the title until the name has been typed by hand.
                          const followsTitle =
                            current.mode === "create" &&
                            (current.slug === "" || current.slug === suggestSlug(current.title));
                          return {
                            ...current,
                            title,
                            slug: followsTitle ? suggestSlug(title) : current.slug,
                          };
                        });
                      }}
                      placeholder={t("editor.titlePlaceholder")}
                    />
                  </div>
                  {draft.mode === "create" && (
                    <div className="grid gap-3 sm:grid-cols-[180px_1fr]">
                      <div className="flex flex-col gap-1 text-xs text-muted-foreground">
                        <label htmlFor="wiki-category">{t("editor.category")}</label>
                        <Select
                          value={draft.category}
                          onValueChange={(value) =>
                            setDraft((current) => current && { ...current, category: value })
                          }
                        >
                          <SelectTrigger id="wiki-category">
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent>
                            {overview.categories.map((item) => (
                              <SelectItem key={item} value={item}>
                                {categoryLabel(item)}
                              </SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                      </div>
                      <div className="flex flex-col gap-1 text-xs text-muted-foreground">
                        <label htmlFor="wiki-slug">{t("editor.pageName")}</label>
                        <Input
                          id="wiki-slug"
                          value={draft.slug}
                          onChange={(e) =>
                            setDraft((current) => current && { ...current, slug: e.target.value })
                          }
                          placeholder="release-train"
                          className="font-mono"
                          aria-invalid={draft.slug !== "" && !isValidSlug(draft.slug)}
                        />
                        <span className="font-mono text-[11px]">
                          {draft.category}/{draft.slug || "…"}.md
                        </span>
                      </div>
                    </div>
                  )}
                  <div className="flex flex-col gap-1 text-xs text-muted-foreground">
                    <label htmlFor="wiki-tags">{t("editor.tags")}</label>
                    <Input
                      id="wiki-tags"
                      value={draft.tags}
                      onChange={(e) =>
                        setDraft((current) => current && { ...current, tags: e.target.value })
                      }
                      placeholder={t("editor.tagsPlaceholder")}
                    />
                  </div>
                  <div className="flex flex-col gap-1 text-xs text-muted-foreground">
                    <label htmlFor="wiki-body">{t("editor.body")}</label>
                    <Textarea
                      id="wiki-body"
                      value={draft.body}
                      onChange={(e) =>
                        setDraft((current) => current && { ...current, body: e.target.value })
                      }
                      rows={20}
                      className="font-mono text-sm"
                    />
                  </div>
                  <p className="text-[11px] text-muted-foreground">
                    {t("editor.rules")}
                  </p>
                </div>
              ) : selectedSummary?.error ? (
                <div className="flex flex-col gap-2">
                  <div className="flex items-center gap-2 text-sm font-semibold text-destructive">
                    <AlertTriangle className="size-4" />
                    {t("page.unreadableTitle")}
                  </div>
                  <p className="font-mono text-xs">{selectedSummary.path}</p>
                  <p className="text-sm text-muted-foreground">{selectedSummary.error}</p>
                  <p className="text-xs text-muted-foreground">
                    <Trans
                      i18nKey="wiki:page.unreadableHint"
                      values={{ root: overview.root }}
                      components={{ code: <code /> }}
                    />
                  </p>
                </div>
              ) : pageLoading && !page ? (
                <div className="flex items-center gap-2 py-8 text-sm text-muted-foreground">
                  <Loader2 className="size-4 animate-spin" />
                  {t("page.loading")}
                </div>
              ) : pageError ? (
                <p className="text-sm text-destructive">{pageError}</p>
              ) : page ? (
                <div className="flex flex-col gap-4">
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0">
                      <h3 className="text-lg font-semibold">{page.title}</h3>
                      <p className="font-mono text-[11px] text-muted-foreground">
                        {t("page.meta", {
                          path: page.path,
                          revision: page.revision,
                          updated: formatTime(page.updated),
                          created: formatTime(page.created),
                        })}
                      </p>
                      {page.tags.length > 0 && (
                        <div className="mt-2 flex flex-wrap gap-1">
                          {page.tags.map((tag) => (
                            <Badge key={tag} variant="secondary">
                              {tag}
                            </Badge>
                          ))}
                        </div>
                      )}
                    </div>
                    <div className="flex shrink-0 gap-2">
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() => setDraft(draftFrom(page))}
                        className="gap-2"
                      >
                        <Pencil className="size-4" />
                        {t("page.edit")}
                      </Button>
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() => setConfirmDelete(true)}
                        className="gap-2 text-destructive hover:text-destructive"
                      >
                        <Trash2 className="size-4" />
                        {t("page.delete")}
                      </Button>
                    </div>
                  </div>
                  <div className="rounded-md border p-4 text-sm">
                    <MessageResponse>{page.body}</MessageResponse>
                  </div>
                  {wikiLinks(page.body).length > 0 && (
                    <div className="flex flex-wrap items-center gap-1.5 text-xs">
                      <span className="text-muted-foreground">{t("page.links")}</span>
                      {wikiLinks(page.body).map((link) =>
                        known.has(link) ? (
                          <button
                            key={link}
                            type="button"
                            onClick={() => open(link)}
                            className="rounded border px-1.5 py-0.5 font-mono hover:bg-muted"
                          >
                            {link}
                          </button>
                        ) : (
                          <span
                            key={link}
                            title={t("page.noSuchPage")}
                            className="rounded border border-dashed px-1.5 py-0.5 font-mono text-muted-foreground line-through"
                          >
                            {link}
                          </span>
                        ),
                      )}
                    </div>
                  )}
                  <div className="text-xs">
                    <div className="mb-1 text-muted-foreground">
                      {t("page.sources", { count: page.sources.length })}
                    </div>
                    {page.sources.length === 0 ? (
                      <p className="text-muted-foreground">{t("page.noSources")}</p>
                    ) : (
                      <ul className="flex flex-col gap-1">
                        {page.sources.map((source) => (
                          <li
                            key={`${source.kind}:${source.label}:${source.content_hash}`}
                            className="flex items-center gap-2"
                          >
                            <Badge variant="outline">{source.kind}</Badge>
                            <span className="truncate font-mono">{source.label}</span>
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                </div>
              ) : (
                <p className="py-8 text-center text-sm text-muted-foreground">
                  {t("page.selectPrompt")}
                </p>
              )}
            </CardContent>
          </Card>
        </div>
      )}

      {view === "health" && (
        <Card>
          <CardHeader className="flex flex-row items-center justify-between gap-2">
            <div>
              <CardTitle className="text-base">{t("health.title")}</CardTitle>
              <p className="mt-1 text-xs text-muted-foreground">{t("health.description")}</p>
            </div>
            <Button size="sm" variant="outline" onClick={loadLint} disabled={lintLoading} className="gap-2">
              <RefreshCw className={lintLoading ? "size-4 animate-spin" : "size-4"} />
              {t("health.recheck")}
            </Button>
          </CardHeader>
          <CardContent>
            {lint === null ? (
              <p className="text-sm text-muted-foreground">{t("health.checking")}</p>
            ) : lint.issues.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                {t("health.noIssues", { count: lint.scanned_pages })}
              </p>
            ) : (
              <ul className="flex flex-col divide-y">
                {lint.issues.map((issue) => (
                  <li
                    key={`${issue.code}:${issue.path}:${issue.detail}`}
                    className="flex flex-wrap items-center gap-2 py-2 text-sm"
                  >
                    <Badge variant={issue.code === "malformed_page" ? "destructive" : "outline"}>
                      {t(`health.issues.${issue.code}`, {
                        defaultValue: issue.code.replace(/_/g, " "),
                      })}
                    </Badge>
                    <button
                      type="button"
                      onClick={() => open(issue.path)}
                      className="font-mono text-xs underline-offset-2 hover:underline"
                    >
                      {issue.path}
                    </button>
                    <span className="text-xs text-muted-foreground">{issue.detail}</span>
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
      )}

      {view === "history" && (
        <Card>
          <CardHeader className="flex flex-row items-center justify-between gap-2">
            <div>
              <CardTitle className="text-base">{t("history.title")}</CardTitle>
              <p className="mt-1 text-xs text-muted-foreground">
                <Trans i18nKey="wiki:history.description" components={{ code: <code /> }} />
              </p>
            </div>
            <Button size="sm" variant="outline" onClick={loadLog} disabled={logLoading} className="gap-2">
              <RefreshCw className={logLoading ? "size-4 animate-spin" : "size-4"} />
              {t("history.refresh")}
            </Button>
          </CardHeader>
          <CardContent>
            {log === null ? (
              <p className="text-sm text-muted-foreground">{t("history.loading")}</p>
            ) : log.length === 0 ? (
              <p className="text-sm text-muted-foreground">{t("history.empty")}</p>
            ) : (
              <ul className="flex flex-col divide-y">
                {log.map((entry) => (
                  <li
                    key={`${entry.timestamp}:${entry.revision}`}
                    className="flex flex-col gap-1 py-2 text-sm"
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      <Badge variant={entry.operation.startsWith("admin") ? "secondary" : "outline"}>
                        {entry.operation}
                      </Badge>
                      {entry.revision !== null && (
                        <span className="font-mono text-xs text-muted-foreground">
                          r{entry.revision}
                        </span>
                      )}
                      <span className="text-xs text-muted-foreground">
                        {formatTime(entry.timestamp)}
                      </span>
                    </div>
                    {entry.summary && <div>{entry.summary}</div>}
                    <div className="flex flex-wrap gap-1.5">
                      {entry.pages.map((path) =>
                        known.has(path) ? (
                          <button
                            key={path}
                            type="button"
                            onClick={() => open(path)}
                            className="font-mono text-xs underline-offset-2 hover:underline"
                          >
                            {path}
                          </button>
                        ) : (
                          <span key={path} className="font-mono text-xs text-muted-foreground">
                            {path}
                          </span>
                        ),
                      )}
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
      )}

      <AlertDialog open={confirmDelete} onOpenChange={setConfirmDelete}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("deleteDialog.title")}</AlertDialogTitle>
            <AlertDialogDescription>
              <Trans
                i18nKey="wiki:deleteDialog.description"
                values={{ path: page?.path ?? "" }}
                components={{ path: <span className="font-mono" /> }}
              />
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleting}>{t("deleteDialog.cancel")}</AlertDialogCancel>
            <AlertDialogAction
              onClick={(event) => {
                event.preventDefault();
                handleDelete();
              }}
              disabled={deleting}
              className="bg-destructive text-white hover:bg-destructive/90"
            >
              {deleting && <Loader2 className="size-4 animate-spin" />}
              {t("deleteDialog.delete")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
