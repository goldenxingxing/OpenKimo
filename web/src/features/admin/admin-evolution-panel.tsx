import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  Activity,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  ClipboardList,
  Dna,
  FlaskConical,
  GitCompareArrows,
  Loader2,
  RefreshCw,
  Sparkles,
} from "lucide-react";
import { toast } from "sonner";

import i18n from "@/i18n";

import {
  type ActionEntry,
  type EntrySummary,
  type EvidenceGroup,
  type EvidenceJson,
  type JobStatus,
  type Overview,
  type RunRecord,
  adoptProposal,
  advanceGeneration,
  getActions,
  getAdopted,
  getEvidence,
  getJob,
  getOverview,
  getProposalEvidence,
  getProposals,
  rollbackGeneration,
  runPropose,
  runVerify,
  setSwitch,
  toggleAdopted,
} from "@/lib/api/apis/AdminEvolutionApi";
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";

type EvolutionTab = "overview" | "evidence" | "improvements" | "versions";

const VERIFY_LIMITS = [1, 3, 5];

function formatTime(ts: number | null): string {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString(i18n.language, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function runTokens(run: RunRecord): number {
  return (run.input_tokens ?? 0) + (run.output_tokens ?? 0) + (run.reasoning_tokens ?? 0);
}

function formatDelta(value: number | null, unit: string): string {
  if (value === null || Number.isNaN(value)) return "—";
  const sign = value > 0 ? "+" : "";
  return `${sign}${value}${unit}`;
}

function verdictBadgeClass(verdict: string): string {
  if (verdict === "candidate_looks_better") {
    return "border-green-500/40 bg-green-500/10 text-green-600 dark:text-green-400";
  }
  if (verdict === "baseline_looks_better") {
    return "border-red-500/40 bg-red-500/10 text-red-600 dark:text-red-400";
  }
  return "text-muted-foreground";
}

function VerdictBadge({ verdict }: { verdict: string }) {
  const { t } = useTranslation("admin");
  const key =
    verdict === "candidate_looks_better"
      ? "evolution.improvements.verdictCandidate"
      : verdict === "baseline_looks_better"
        ? "evolution.improvements.verdictBaseline"
        : "evolution.improvements.verdictInconclusive";
  return (
    <Badge variant="outline" className={`text-[10px] ${verdictBadgeClass(verdict)}`}>
      {t(key)}
    </Badge>
  );
}

function detailSummary(detail: unknown): string {
  if (detail === null || detail === undefined) return "";
  if (typeof detail === "string") return detail;
  try {
    return JSON.stringify(detail);
  } catch {
    return "";
  }
}

function JobBanner({ job }: { job: JobStatus | null }) {
  const { t } = useTranslation("admin");
  if (job?.status !== "running") return null;
  return (
    <div className="flex items-center gap-2 rounded-md border border-primary/30 bg-primary/10 px-3 py-2 text-sm">
      <Loader2 className="size-4 animate-spin" />
      {t("evolution.jobRunning", { kind: job.kind ?? "?" })}
    </div>
  );
}

function OverviewSection({
  overview,
  onRefresh,
}: {
  overview: Overview | null;
  onRefresh: () => void;
}) {
  const { t } = useTranslation("admin");
  const [actions, setActions] = useState<ActionEntry[]>([]);
  const [switchBusy, setSwitchBusy] = useState(false);
  const [generationDialog, setGenerationDialog] = useState<"rollback" | "advance" | null>(null);
  const [generationBusy, setGenerationBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    getActions(10)
      .then((list) => {
        if (!cancelled) setActions(list);
      })
      .catch(() => {
        // Audit is best-effort; a failure here is surfaced by the other loads.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const handleSwitch = useCallback(
    async (enabled: boolean) => {
      setSwitchBusy(true);
      try {
        await setSwitch(enabled);
        toast.success(
          enabled ? t("evolution.overview.switchOn") : t("evolution.overview.switchOff"),
        );
        onRefresh();
      } catch (err) {
        toast.error(err instanceof Error ? err.message : t("evolution.loadFailed"));
      } finally {
        setSwitchBusy(false);
      }
    },
    [onRefresh, t],
  );

  const handleGeneration = useCallback(async () => {
    if (!generationDialog) return;
    setGenerationBusy(true);
    try {
      const result =
        generationDialog === "rollback"
          ? await rollbackGeneration()
          : await advanceGeneration();
      toast.success(
        t(
          generationDialog === "rollback"
            ? "evolution.versions.rolledBack"
            : "evolution.versions.advanced",
          { generation: result.generation_current },
        ),
      );
      setGenerationDialog(null);
      onRefresh();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : t("evolution.loadFailed"));
    } finally {
      setGenerationBusy(false);
    }
  }, [generationDialog, onRefresh, t]);

  const stats = overview
    ? [
        { label: t("evolution.overview.statEvidenceTurns"), value: overview.evidence_turns },
        { label: t("evolution.overview.statEvidenceTasks"), value: overview.evidence_tasks },
        {
          label: t("evolution.overview.statEvidenceTokens"),
          value: overview.evidence_total_tokens.toLocaleString(i18n.language),
        },
        { label: t("evolution.overview.statPendingProposals"), value: overview.proposals_pending },
      ]
    : [];

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader className="flex flex-row items-start justify-between gap-2">
          <div>
            <CardTitle className="text-base">{t("evolution.overview.switchTitle")}</CardTitle>
            <p className="mt-1 text-xs text-muted-foreground">
              {t("evolution.overview.switchHint1")}
            </p>
            <p className="mt-1 text-xs text-muted-foreground">
              {t("evolution.overview.switchHint2")}
            </p>
          </div>
          {overview && (
            <Switch
              checked={overview.enabled}
              disabled={switchBusy}
              onCheckedChange={handleSwitch}
              aria-label={t("evolution.overview.switchTitle")}
            />
          )}
        </CardHeader>
      </Card>

      <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        {stats.map((stat) => (
          <Card key={stat.label}>
            <CardHeader className="pb-1">
              <CardTitle className="text-sm font-medium text-muted-foreground">
                {stat.label}
              </CardTitle>
            </CardHeader>
            <CardContent>
              <p className="text-2xl font-bold">{stat.value}</p>
            </CardContent>
          </Card>
        ))}
      </div>

      {overview && (
        <Card>
          <CardHeader className="flex flex-row items-start justify-between gap-2">
            <div>
              <CardTitle className="text-base">{t("evolution.overview.generationTitle")}</CardTitle>
              <p className="mt-1 text-sm font-medium">
                {t("evolution.overview.generationCurrent", {
                  current: overview.generation_current,
                  count: overview.generation_count,
                })}
              </p>
              <p className="mt-1 text-xs text-muted-foreground">
                {t("evolution.overview.generationAdopted", { count: overview.adopted_total })}
              </p>
              <p className="mt-1 text-[11px] text-muted-foreground">
                {t("evolution.overview.generationHint")}
              </p>
            </div>
            <div className="flex shrink-0 items-center gap-2">
              <Button
                size="sm"
                variant="outline"
                className="gap-1.5"
                disabled={switchBusy || generationBusy}
                onClick={() => setGenerationDialog("rollback")}
              >
                <ChevronLeft className="size-3.5" />
                {t("evolution.overview.rollback")}
              </Button>
              <Button
                size="sm"
                variant="outline"
                className="gap-1.5"
                disabled={switchBusy || generationBusy}
                onClick={() => setGenerationDialog("advance")}
              >
                {t("evolution.overview.advance")}
                <ChevronRight className="size-3.5" />
              </Button>
            </div>
          </CardHeader>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle className="text-base">{t("evolution.overview.auditTitle")}</CardTitle>
        </CardHeader>
        <CardContent>
          {actions.length === 0 ? (
            <p className="text-sm text-muted-foreground">{t("evolution.overview.auditEmpty")}</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b text-left text-xs font-medium uppercase tracking-wide text-muted-foreground">
                    <th className="px-2 py-2">{t("evolution.overview.auditTime")}</th>
                    <th className="px-2 py-2">{t("evolution.overview.auditActor")}</th>
                    <th className="px-2 py-2">{t("evolution.overview.auditAction")}</th>
                    <th className="px-2 py-2">{t("evolution.overview.auditDetail")}</th>
                  </tr>
                </thead>
                <tbody className="divide-y">
                  {actions.map((action) => (
                    <tr key={`${action.ts}-${action.action}-${action.actor}`}>
                      <td className="whitespace-nowrap px-2 py-2 text-muted-foreground">
                        {formatTime(action.ts)}
                      </td>
                      <td className="px-2 py-2">{action.actor}</td>
                      <td className="px-2 py-2">
                        <Badge variant="outline" className="text-[10px]">
                          {action.action}
                        </Badge>
                      </td>
                      <td className="max-w-64 truncate px-2 py-2 font-mono text-[11px] text-muted-foreground">
                        {detailSummary(action.detail)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>

      <AlertDialog
        open={generationDialog !== null}
        onOpenChange={(open) => {
          if (!open) setGenerationDialog(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {t(
                generationDialog === "rollback"
                  ? "evolution.versions.rollbackTitle"
                  : "evolution.versions.advanceTitle",
              )}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {t(
                generationDialog === "rollback"
                  ? "evolution.versions.rollbackDescription"
                  : "evolution.versions.advanceDescription",
              )}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={generationBusy}>{t("actions.cancel")}</AlertDialogCancel>
            <AlertDialogAction onClick={handleGeneration} disabled={generationBusy}>
              {generationBusy ? <Loader2 className="size-4 animate-spin" /> : null}
              {t("actions.save")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

function EvidenceSection({
  onImproveFrom,
}: {
  onImproveFrom: (task: string) => void;
}) {
  const { t } = useTranslation("admin");
  const [pattern, setPattern] = useState("");
  const [appliedPattern, setAppliedPattern] = useState<string | undefined>(undefined);
  const [groups, setGroups] = useState<EvidenceGroup[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setGroups(await getEvidence(appliedPattern));
    } catch (err) {
      setError(err instanceof Error ? err.message : t("evolution.loadFailed"));
    } finally {
      setLoading(false);
    }
  }, [appliedPattern, t]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  return (
    <Card>
      <CardHeader className="flex flex-row items-start justify-between gap-2">
        <div className="flex items-center gap-2">
          <Input
            value={pattern}
            onChange={(e) => setPattern(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") setAppliedPattern(pattern.trim() || undefined);
            }}
            placeholder={t("evolution.evidence.filterPlaceholder")}
            className="w-64"
          />
          <Button
            size="sm"
            variant="outline"
            onClick={() => setAppliedPattern(pattern.trim() || undefined)}
            disabled={loading}
            className="gap-2"
          >
            <RefreshCw className={loading ? "size-4 animate-spin" : "size-4"} />
            {t("evolution.evidence.filter")}
          </Button>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {error && (
          <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
            {error}
          </div>
        )}
        {!loading && groups?.length === 0 && (
          <p className="text-sm text-muted-foreground">{t("evolution.evidence.empty")}</p>
        )}
        {groups && groups.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b text-left text-xs font-medium uppercase tracking-wide text-muted-foreground">
                  <th className="px-2 py-2">{t("evolution.evidence.colTask")}</th>
                  <th className="px-2 py-2">{t("evolution.evidence.colRuns")}</th>
                  <th className="px-2 py-2">{t("evolution.evidence.colFailed")}</th>
                  <th className="px-2 py-2">{t("evolution.evidence.colAvgSteps")}</th>
                  <th className="px-2 py-2">{t("evolution.evidence.colAvgTokens")}</th>
                  <th className="px-2 py-2 text-right">{t("users.columns.actions")}</th>
                </tr>
              </thead>
              <tbody className="divide-y">
                {groups.map((group) => (
                  <tr key={group.task} className="transition-colors hover:bg-muted/30">
                    <td className="max-w-72 truncate px-2 py-2 font-mono text-xs" title={group.task}>
                      {group.task.length > 60 ? `${group.task.slice(0, 60)}…` : group.task}
                    </td>
                    <td className="px-2 py-2">{group.runs}</td>
                    <td className="px-2 py-2">{group.failed}</td>
                    <td className="px-2 py-2">{group.avg_steps.toFixed(1)}</td>
                    <td className="px-2 py-2">{Math.round(group.avg_total_tokens)}</td>
                    <td className="px-2 py-2 text-right">
                      <Button
                        size="sm"
                        variant="ghost"
                        className="gap-1.5"
                        onClick={() => onImproveFrom(group.task)}
                      >
                        <Sparkles className="size-3.5" />
                        {t("evolution.evidence.improve")}
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function EntryCard({
  entry,
  onVerify,
  onViewEvidence,
  onAdopt,
  busy,
}: {
  entry: EntrySummary;
  onVerify: (entry: EntrySummary) => void;
  onViewEvidence: (entry: EntrySummary) => void;
  onAdopt: (entry: EntrySummary) => void;
  busy: boolean;
}) {
  const { t } = useTranslation("admin");
  return (
    <div className="rounded-md border px-3 py-2.5">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="font-mono text-xs">{entry.id}</span>
            {entry.has_evidence && (
              <Badge
                variant="outline"
                className="border-green-500/40 bg-green-500/10 text-[10px] text-green-600 dark:text-green-400"
              >
                {t("evolution.improvements.verifiedBadge")}
              </Badge>
            )}
            {entry.editor_model && (
              <Badge variant="outline" className="font-mono text-[10px]">
                {entry.editor_model}
              </Badge>
            )}
          </div>
          {entry.rationale && (
            <p className="mt-1 line-clamp-2 text-xs text-muted-foreground">{entry.rationale}</p>
          )}
          {entry.source_tasks.length > 0 && (
            <p className="mt-1 truncate text-[11px] text-muted-foreground">
              {t("evolution.improvements.sourceTasks")}:{" "}
              {entry.source_tasks.map((task) => (task.length > 40 ? `${task.slice(0, 40)}…` : task)).join(", ")}
            </p>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-1">
          <Button
            size="sm"
            variant="ghost"
            className="gap-1.5"
            disabled={busy}
            onClick={() => onVerify(entry)}
          >
            <GitCompareArrows className="size-3.5" />
            {t("evolution.improvements.verify")}
          </Button>
          {entry.has_evidence && (
            <>
              <Button
                size="sm"
                variant="ghost"
                className="gap-1.5"
                disabled={busy}
                onClick={() => onViewEvidence(entry)}
              >
                <FlaskConical className="size-3.5" />
                {t("evolution.improvements.viewEvidence")}
              </Button>
              <Button
                size="sm"
                variant="ghost"
                className="gap-1.5 text-green-600 dark:text-green-400"
                disabled={busy}
                onClick={() => onAdopt(entry)}
              >
                <CheckCircle2 className="size-3.5" />
                {t("evolution.improvements.adopt")}
              </Button>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function ImprovementsSection({
  job,
  setJob,
  ensureIdle,
  proposePrefill,
  onProposePrefillConsumed,
  onMutated,
}: {
  job: JobStatus | null;
  setJob: (job: JobStatus) => void;
  ensureIdle: () => Promise<boolean>;
  proposePrefill: string | null;
  onProposePrefillConsumed: () => void;
  onMutated: () => void;
}) {
  const { t } = useTranslation("admin");
  const [proposals, setProposals] = useState<EntrySummary[] | null>(null);
  const [adopted, setAdopted] = useState<EntrySummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const [proposeOpen, setProposeOpen] = useState(false);
  const [proposePattern, setProposePattern] = useState("");
  const [proposeSubmitting, setProposeSubmitting] = useState(false);

  const [verifyTarget, setVerifyTarget] = useState<EntrySummary | null>(null);
  const [verifyLimit, setVerifyLimit] = useState("3");
  const [verifySubmitting, setVerifySubmitting] = useState(false);

  const [evidenceTarget, setEvidenceTarget] = useState<EntrySummary | null>(null);
  const [evidence, setEvidence] = useState<EvidenceJson | null>(null);
  const [evidenceLoading, setEvidenceLoading] = useState(false);

  const [adoptTarget, setAdoptTarget] = useState<EntrySummary | null>(null);
  const [adoptSubmitting, setAdoptSubmitting] = useState(false);

  const jobRunning = job?.status === "running";

  const refresh = useCallback(async () => {
    setError(null);
    try {
      const [proposalList, adoptedList] = await Promise.all([getProposals(), getAdopted()]);
      setProposals(proposalList);
      setAdopted(adoptedList);
    } catch (err) {
      setError(err instanceof Error ? err.message : t("evolution.loadFailed"));
    }
  }, [t]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    if (proposePrefill !== null) {
      setProposePattern(proposePrefill);
      setProposeOpen(true);
      onProposePrefillConsumed();
    }
  }, [proposePrefill, onProposePrefillConsumed]);

  const openEvidence = useCallback(
    async (entry: EntrySummary) => {
      setEvidenceTarget(entry);
      setEvidence(null);
      setEvidenceLoading(true);
      try {
        setEvidence(await getProposalEvidence(entry.id));
      } catch (err) {
        toast.error(err instanceof Error ? err.message : t("evolution.loadFailed"));
        setEvidenceTarget(null);
      } finally {
        setEvidenceLoading(false);
      }
    },
    [t],
  );

  const handlePropose = useCallback(async () => {
    if (!(await ensureIdle())) return;
    setProposeSubmitting(true);
    try {
      setJob(await runPropose(proposePattern.trim() || undefined));
      setProposeOpen(false);
      onMutated();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : t("evolution.loadFailed"));
    } finally {
      setProposeSubmitting(false);
    }
  }, [ensureIdle, proposePattern, onMutated, t, setJob]);

  const handleVerify = useCallback(async () => {
    if (!verifyTarget) return;
    if (!(await ensureIdle())) {
      setVerifyTarget(null);
      return;
    }
    setVerifySubmitting(true);
    try {
      setJob(await runVerify(verifyTarget.id, Number(verifyLimit)));
      setVerifyTarget(null);
      onMutated();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : t("evolution.loadFailed"));
    } finally {
      setVerifySubmitting(false);
    }
  }, [verifyTarget, verifyLimit, ensureIdle, onMutated, t, setJob]);

  const handleAdopt = useCallback(async () => {
    if (!adoptTarget) return;
    setAdoptSubmitting(true);
    try {
      const result = await adoptProposal(adoptTarget.id);
      toast.success(
        t("evolution.improvements.adopted", {
          id: result.adopted,
          generation: result.generation_current,
        }),
      );
      setAdoptTarget(null);
      onMutated();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : t("evolution.loadFailed"));
    } finally {
      setAdoptSubmitting(false);
    }
  }, [adoptTarget, onMutated, t]);

  const handleToggle = useCallback(
    async (entry: EntrySummary, enabled: boolean) => {
      setBusyId(entry.id);
      try {
        await toggleAdopted(entry.id, enabled);
        setAdopted((current) =>
          (current ?? []).map((item) => (item.id === entry.id ? { ...item, enabled } : item)),
        );
        toast.success(
          enabled
            ? t("evolution.improvements.enabledToast")
            : t("evolution.improvements.disabledToast"),
        );
      } catch (err) {
        toast.error(err instanceof Error ? err.message : t("evolution.improvements.toggleFailed"));
      } finally {
        setBusyId(null);
      }
    },
    [t],
  );

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold">{t("evolution.improvements.proposalsTitle")}</h3>
        <Button
          size="sm"
          onClick={() => {
            setProposePattern("");
            setProposeOpen(true);
          }}
          disabled={jobRunning}
          className="gap-2"
        >
          <Sparkles className="size-4" />
          {t("evolution.improvements.propose")}
        </Button>
      </div>

      {error && (
        <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {error}
        </div>
      )}

      <div className="flex flex-col gap-2">
        {proposals?.length === 0 && (
          <p className="text-sm text-muted-foreground">
            {t("evolution.improvements.proposalsEmpty")}
          </p>
        )}
        {proposals?.map((entry) => (
          <EntryCard
            key={entry.id}
            entry={entry}
            busy={jobRunning || busyId === entry.id}
            onVerify={setVerifyTarget}
            onViewEvidence={openEvidence}
            onAdopt={setAdoptTarget}
          />
        ))}
      </div>

      <h3 className="text-sm font-semibold">{t("evolution.improvements.adoptedTitle")}</h3>
      <div className="flex flex-col gap-2">
        {adopted?.length === 0 && (
          <p className="text-sm text-muted-foreground">
            {t("evolution.improvements.adoptedEmpty")}
          </p>
        )}
        {adopted?.map((entry) => (
          <div key={entry.id} className="flex items-center justify-between gap-3 rounded-md border px-3 py-2.5">
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <span className="font-mono text-xs">{entry.id}</span>
                {!entry.enabled && (
                  <Badge variant="outline" className="text-[10px] text-muted-foreground">
                    {t("evolution.improvements.disabledBadge")}
                  </Badge>
                )}
              </div>
              <p className="mt-1 text-[11px] text-muted-foreground">
                {t("evolution.improvements.takesEffect")}
              </p>
            </div>
            <Switch
              checked={entry.enabled}
              disabled={busyId === entry.id}
              onCheckedChange={(enabled) => handleToggle(entry, enabled)}
              aria-label={entry.id}
            />
          </div>
        ))}
      </div>

      {/* Propose dialog */}
      <Dialog
        open={proposeOpen}
        onOpenChange={(open) => {
          if (!open) setProposeOpen(false);
        }}
      >
        <DialogContent className="max-w-xl">
          <DialogHeader>
            <DialogTitle>{t("evolution.improvements.proposeTitle")}</DialogTitle>
            <DialogDescription>
              {t("evolution.improvements.proposeDescription")}
            </DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-1 text-xs">
            <label htmlFor="evolution-propose-pattern">
              {t("evolution.improvements.proposePatternLabel")}
            </label>
            <Input
              id="evolution-propose-pattern"
              value={proposePattern}
              onChange={(e) => setProposePattern(e.target.value)}
              placeholder={t("evolution.improvements.proposePatternPlaceholder")}
              className="font-mono"
            />
          </div>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setProposeOpen(false)}
              disabled={proposeSubmitting}
            >
              {t("actions.cancel")}
            </Button>
            <Button onClick={handlePropose} disabled={proposeSubmitting} className="gap-2">
              {proposeSubmitting ? <Loader2 className="size-4 animate-spin" /> : null}
              {t("evolution.improvements.proposeSubmit")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Verify confirm dialog */}
      <AlertDialog
        open={verifyTarget !== null}
        onOpenChange={(open) => {
          if (!open) setVerifyTarget(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("evolution.improvements.verifyTitle")}</AlertDialogTitle>
            <AlertDialogDescription>
              {t("evolution.improvements.verifyDescription", {
                count: verifyTarget?.source_tasks.length || 1,
              })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <div className="flex flex-col gap-1 text-xs">
            {/* biome-ignore lint/a11y/noLabelWithoutControl: Select component wraps the control */}
            <label className="text-sm font-medium text-foreground">
              {t("evolution.improvements.verifyLimit")}
            </label>
            <Select value={verifyLimit} onValueChange={setVerifyLimit} disabled={verifySubmitting}>
              <SelectTrigger className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {VERIFY_LIMITS.map((limit) => (
                  <SelectItem key={limit} value={String(limit)}>
                    {limit}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={verifySubmitting}>
              {t("actions.cancel")}
            </AlertDialogCancel>
            <AlertDialogAction onClick={handleVerify} disabled={verifySubmitting}>
              {verifySubmitting ? <Loader2 className="size-4 animate-spin" /> : null}
              {t("evolution.improvements.verifySubmit")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Evidence viewer dialog */}
      <Dialog
        open={evidenceTarget !== null}
        onOpenChange={(open) => {
          if (!open) setEvidenceTarget(null);
        }}
      >
        <DialogContent className="sm:max-w-3xl">
          <DialogHeader>
            <DialogTitle>{t("evolution.improvements.evidenceTitle")}</DialogTitle>
            {evidence && (
              <DialogDescription className="flex items-center gap-2">
                <span>{t("evolution.improvements.evidenceOverall")}:</span>
                <VerdictBadge verdict={evidence.overall_verdict_suggestion} />
              </DialogDescription>
            )}
          </DialogHeader>
          {evidenceLoading ? (
            <div className="flex items-center justify-center gap-2 py-8 text-muted-foreground">
              <Loader2 className="size-4 animate-spin" />
            </div>
          ) : evidence ? (
            <div className="flex min-w-0 flex-col gap-3">
              <div className="min-w-0 overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b text-left text-xs font-medium uppercase tracking-wide text-muted-foreground">
                      <th className="px-2 py-2">{t("evolution.evidence.colTask")}</th>
                      <th className="px-2 py-2">{t("evolution.improvements.colBaseline")}</th>
                      <th className="px-2 py-2">{t("evolution.improvements.colCandidate")}</th>
                      <th className="px-2 py-2">{t("evolution.improvements.colDelta")}</th>
                      <th className="px-2 py-2">{t("evolution.improvements.colStepsDelta")}</th>
                      <th className="px-2 py-2">{t("evolution.improvements.colVerdict")}</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y">
                    {evidence.tasks.map((taskEvidence) => (
                      <tr key={taskEvidence.task}>
                        <td className="max-w-48 truncate px-2 py-2 font-mono text-xs" title={taskEvidence.task}>
                          {taskEvidence.task.length > 40
                            ? `${taskEvidence.task.slice(0, 40)}…`
                            : taskEvidence.task}
                        </td>
                        <td className="px-2 py-2 text-xs">
                          <div>{taskEvidence.baseline.outcome ?? "—"}</div>
                          <div className="text-muted-foreground">
                            {taskEvidence.baseline.steps ?? 0} · {runTokens(taskEvidence.baseline)}
                          </div>
                        </td>
                        <td className="px-2 py-2 text-xs">
                          <div>{taskEvidence.candidate.outcome ?? "—"}</div>
                          <div className="text-muted-foreground">
                            {taskEvidence.candidate.steps ?? 0} · {runTokens(taskEvidence.candidate)}
                          </div>
                        </td>
                        <td className="px-2 py-2 text-xs">
                          {formatDelta(taskEvidence.token_delta_pct, "%")}
                        </td>
                        <td className="px-2 py-2 text-xs">
                          {formatDelta(taskEvidence.steps_delta, "")}
                        </td>
                        <td className="px-2 py-2">
                          <VerdictBadge verdict={taskEvidence.verdict_suggestion} />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {evidence.caveat && (
                <p className="text-[11px] text-muted-foreground">
                  {t("evolution.improvements.evidenceCaveat")}: {evidence.caveat}
                </p>
              )}
            </div>
          ) : null}
        </DialogContent>
      </Dialog>

      {/* Adopt confirm dialog */}
      <AlertDialog
        open={adoptTarget !== null}
        onOpenChange={(open) => {
          if (!open) setAdoptTarget(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("evolution.improvements.adoptTitle")}</AlertDialogTitle>
            <AlertDialogDescription>
              {t("evolution.improvements.adoptDescription")}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={adoptSubmitting}>{t("actions.cancel")}</AlertDialogCancel>
            <AlertDialogAction onClick={handleAdopt} disabled={adoptSubmitting}>
              {adoptSubmitting ? <Loader2 className="size-4 animate-spin" /> : null}
              {t("evolution.improvements.adopt")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

function VersionsSection({
  overview,
  onRefresh,
}: {
  overview: Overview | null;
  onRefresh: () => void;
}) {
  const { t } = useTranslation("admin");
  const [dialog, setDialog] = useState<"rollback" | "advance" | null>(null);
  const [busy, setBusy] = useState(false);

  const handleConfirm = useCallback(async () => {
    if (!dialog) return;
    setBusy(true);
    try {
      const result =
        dialog === "rollback" ? await rollbackGeneration() : await advanceGeneration();
      toast.success(
        t(
          dialog === "rollback" ? "evolution.versions.rolledBack" : "evolution.versions.advanced",
          { generation: result.generation_current },
        ),
      );
      setDialog(null);
      onRefresh();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : t("evolution.loadFailed"));
    } finally {
      setBusy(false);
    }
  }, [dialog, onRefresh, t]);

  const disabledIds = overview?.disabled_ids ?? [];

  return (
    <div className="flex flex-col gap-4">
      {overview && (
        <Card>
          <CardContent className="flex flex-col items-start gap-4 pt-6">
            <div className="flex items-center gap-3">
              <Dna className="size-8 text-muted-foreground" />
              <div>
                <p className="text-2xl font-bold">
                  {t("evolution.versions.current", {
                    current: overview.generation_current,
                    count: overview.generation_count,
                  })}
                </p>
                <p className="text-xs text-muted-foreground">
                  {t("evolution.versions.sessionHint")}
                </p>
              </div>
            </div>
            <div className="flex items-center gap-2">
              <Button
                size="sm"
                variant="outline"
                className="gap-1.5"
                disabled={busy}
                onClick={() => setDialog("rollback")}
              >
                <ChevronLeft className="size-3.5" />
                {t("evolution.overview.rollback")}
              </Button>
              <Button
                size="sm"
                variant="outline"
                className="gap-1.5"
                disabled={busy}
                onClick={() => setDialog("advance")}
              >
                {t("evolution.overview.advance")}
                <ChevronRight className="size-3.5" />
              </Button>
            </div>
          </CardContent>
        </Card>
      )}

      {disabledIds.length > 0 && (
        <div className="rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm">
          <p>{t("evolution.versions.disabledWarning", { count: disabledIds.length })}</p>
          <ul className="mt-1 flex flex-col gap-0.5 pl-4 font-mono text-[11px] text-muted-foreground">
            {disabledIds.map((id) => (
              <li key={id}>{id}</li>
            ))}
          </ul>
        </div>
      )}

      <AlertDialog
        open={dialog !== null}
        onOpenChange={(open) => {
          if (!open) setDialog(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {t(
                dialog === "rollback" ? "evolution.versions.rollbackTitle" : "evolution.versions.advanceTitle",
              )}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {t(
                dialog === "rollback"
                  ? "evolution.versions.rollbackDescription"
                  : "evolution.versions.advanceDescription",
              )}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={busy}>{t("actions.cancel")}</AlertDialogCancel>
            <AlertDialogAction onClick={handleConfirm} disabled={busy}>
              {busy ? <Loader2 className="size-4 animate-spin" /> : null}
              {t("actions.save")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

export function AdminEvolutionPanel() {
  const { t } = useTranslation("admin");
  const [tab, setTab] = useState<EvolutionTab>("overview");
  const [job, setJob] = useState<JobStatus | null>(null);
  const [reloadTick, setReloadTick] = useState(0);
  const [overview, setOverview] = useState<Overview | null>(null);
  const [overviewError, setOverviewError] = useState<string | null>(null);
  const [proposePrefill, setProposePrefill] = useState<string | null>(null);
  const prevJobStatus = useRef<string | null>(null);

  const refreshOverview = useCallback(async () => {
    try {
      setOverview(await getOverview());
      setOverviewError(null);
    } catch (err) {
      setOverviewError(err instanceof Error ? err.message : t("evolution.loadFailed"));
    }
  }, [t]);

  useEffect(() => {
    refreshOverview();
  }, [refreshOverview]);

  // Resume polling a job that was already running when the panel mounted.
  useEffect(() => {
    let cancelled = false;
    getJob()
      .then((current) => {
        if (!cancelled) setJob(current);
      })
      .catch(() => {
        // A failed probe is fine; the next poll or submit will retry.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Poll every 2s while a job runs.
  useEffect(() => {
    if (job?.status !== "running") return;
    const timer = setInterval(() => {
      getJob()
        .then((current) => setJob(current))
        .catch(() => {
          // Keep polling; a transient failure must not stop the banner.
        });
    }, 2000);
    return () => clearInterval(timer);
  }, [job?.status]);

  // Toast and refresh when a running job reaches a terminal state.
  useEffect(() => {
    if (
      job &&
      prevJobStatus.current === "running" &&
      (job.status === "succeeded" || job.status === "failed")
    ) {
      if (job.status === "succeeded") {
        toast.success(t("evolution.jobDone"));
      } else {
        toast.error(t("evolution.jobFailed", { error: job.error ?? "—" }));
      }
      setReloadTick((n) => n + 1);
      refreshOverview();
    }
    prevJobStatus.current = job?.status ?? null;
  }, [job, t, refreshOverview]);

  const handleMutated = useCallback(() => {
    setReloadTick((n) => n + 1);
    refreshOverview();
  }, [refreshOverview]);

  const ensureIdle = useCallback(async (): Promise<boolean> => {
    try {
      const current = await getJob();
      setJob(current);
      if (current.status === "running") {
        toast.error(t("evolution.jobBusy"));
        return false;
      }
    } catch {
      // Let the mutation itself surface the error.
    }
    return true;
  }, [t]);

  const handleImproveFrom = useCallback((task: string) => {
    setProposePrefill(task);
    setTab("improvements");
  }, []);

  const TABS: { value: EvolutionTab; labelKey: string; icon: typeof Sparkles }[] = [
    { value: "overview", labelKey: "evolution.tabs.overview", icon: Activity },
    { value: "evidence", labelKey: "evolution.tabs.evidence", icon: ClipboardList },
    { value: "improvements", labelKey: "evolution.tabs.improvements", icon: Sparkles },
    { value: "versions", labelKey: "evolution.tabs.versions", icon: Dna },
  ];

  return (
    <div className="flex flex-col gap-4">
      <div className="flex gap-1 rounded-lg border bg-muted/40 p-1 w-fit">
        {TABS.map((item) => (
          <button
            key={item.value}
            type="button"
            onClick={() => setTab(item.value)}
            className={[
              "inline-flex items-center gap-2 rounded-md px-3 py-1.5 text-sm font-medium transition-colors",
              tab === item.value
                ? "bg-background text-foreground shadow-sm"
                : "text-muted-foreground hover:text-foreground",
            ].join(" ")}
          >
            <item.icon className="size-3.5" />
            {t(item.labelKey)}
          </button>
        ))}
      </div>

      {overviewError && (
        <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {overviewError}
        </div>
      )}

      <JobBanner job={job} />

      {tab === "overview" && (
        <OverviewSection key={`evolution-overview-${reloadTick}`} overview={overview} onRefresh={handleMutated} />
      )}
      {tab === "evidence" && (
        <EvidenceSection
          key={`evolution-evidence-${reloadTick}`}
          onImproveFrom={handleImproveFrom}
        />
      )}
      {tab === "improvements" && (
        <ImprovementsSection
          key={`evolution-improvements-${reloadTick}`}
          job={job}
          setJob={setJob}
          ensureIdle={ensureIdle}
          proposePrefill={proposePrefill}
          onProposePrefillConsumed={() => setProposePrefill(null)}
          onMutated={handleMutated}
        />
      )}
      {tab === "versions" && (
        <VersionsSection key={`evolution-versions-${reloadTick}`} overview={overview} onRefresh={handleMutated} />
      )}
    </div>
  );
}
