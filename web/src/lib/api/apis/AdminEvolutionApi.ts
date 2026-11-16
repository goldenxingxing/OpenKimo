import { getAuthHeader } from "../../auth";
import { getApiBaseUrl } from "../../../hooks/utils";

/**
 * Self-evolution admin API: evidence collection (free), the master switch,
 * proposals → double-run verification → human adoption, and generation-based
 * versioning with rollback. Long-running operations (propose, verify) run as a
 * single mutually-exclusive background job; poll `getJob()` until terminal.
 */

export interface Overview {
  enabled: boolean;
  evidence_turns: number;
  evidence_tasks: number;
  evidence_total_tokens: number;
  proposals_pending: number;
  adopted_total: number;
  generation_current: number;
  generation_count: number;
  disabled_ids: string[];
}

export interface EvidenceGroup {
  task: string;
  runs: number;
  failed: number;
  avg_steps: number;
  avg_total_tokens: number;
}

export interface EntrySummary {
  id: string;
  status: string;
  created_at: number | null;
  rationale: string;
  source_tasks: string[];
  editor_model: string | null;
  has_evidence: boolean;
  enabled: boolean;
}

/** One run of a verification pair, as recorded in evidence.json. */
export interface RunRecord {
  outcome?: string;
  steps?: number;
  input_tokens?: number;
  output_tokens?: number;
  reasoning_tokens?: number;
  note?: string | null;
}

export interface TaskEvidence {
  task: string;
  baseline: RunRecord;
  candidate: RunRecord;
  token_delta_pct: number | null;
  steps_delta: number | null;
  verdict_suggestion: string;
}

export interface EvidenceJson {
  proposal_id: string;
  overall_verdict_suggestion: string;
  caveat: string;
  tasks: TaskEvidence[];
}

export interface JobStatus {
  status: "idle" | "running" | "succeeded" | "failed";
  kind: string | null;
  proposal_id: string | null;
  started_at: number | null;
  finished_at: number | null;
  error: string | null;
  result: Record<string, unknown> | null;
}

export interface ActionEntry {
  ts: number;
  actor: string;
  action: string;
  detail: unknown;
}

function url(path: string): string {
  return `${getApiBaseUrl()}${path}`;
}

async function response<T>(value: Response): Promise<T> {
  if (!value.ok) {
    let message = `Request failed (${value.status})`;
    try {
      const body = (await value.json()) as { detail?: string };
      message = body.detail || message;
    } catch {
      // Keep the status-based fallback.
    }
    throw new Error(message);
  }
  return value.json() as Promise<T>;
}

function request(path: string, init?: RequestInit): Promise<Response> {
  return fetch(url(path), {
    credentials: "include",
    ...init,
    headers: { ...getAuthHeader(), ...init?.headers },
  });
}

function jsonRequest(
  path: string,
  method: string,
  body: unknown,
): Promise<Response> {
  return request(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

const BASE = "/api/admin/evolution";

export async function getOverview(): Promise<Overview> {
  return response(await request(`${BASE}/overview`));
}

export async function getEvidence(pattern?: string): Promise<EvidenceGroup[]> {
  const query = pattern ? `?pattern=${encodeURIComponent(pattern)}` : "";
  return response(await request(`${BASE}/evidence${query}`));
}

export async function getProposals(): Promise<EntrySummary[]> {
  return response(await request(`${BASE}/proposals`));
}

export async function getAdopted(): Promise<EntrySummary[]> {
  return response(await request(`${BASE}/adopted`));
}

export async function getProposalEvidence(id: string): Promise<EvidenceJson> {
  return response(
    await request(`${BASE}/proposals/${encodeURIComponent(id)}/evidence`),
  );
}

export async function getActions(limit?: number): Promise<ActionEntry[]> {
  const query = limit === undefined ? "" : `?limit=${limit}`;
  return response(await request(`${BASE}/actions${query}`));
}

export async function setSwitch(enabled: boolean): Promise<Overview> {
  return response(await jsonRequest(`${BASE}/switch`, "POST", { enabled }));
}

export async function runPropose(taskPattern?: string): Promise<JobStatus> {
  return response(
    await jsonRequest(`${BASE}/proposals`, "POST", {
      task_pattern: taskPattern || null,
    }),
  );
}

export async function runVerify(
  proposalId: string,
  limit: number,
): Promise<JobStatus> {
  return response(
    await jsonRequest(
      `${BASE}/proposals/${encodeURIComponent(proposalId)}/verify`,
      "POST",
      { limit },
    ),
  );
}

export async function adoptProposal(
  proposalId: string,
): Promise<{ adopted: string; generation_current: number }> {
  return response(
    await request(
      `${BASE}/proposals/${encodeURIComponent(proposalId)}/adopt`,
      { method: "POST" },
    ),
  );
}

export async function rollbackGeneration(): Promise<{
  generation_current: number;
}> {
  return response(
    await request(`${BASE}/generations/rollback`, { method: "POST" }),
  );
}

export async function advanceGeneration(): Promise<{
  generation_current: number;
}> {
  return response(
    await request(`${BASE}/generations/advance`, { method: "POST" }),
  );
}

export async function toggleAdopted(
  id: string,
  enabled: boolean,
): Promise<{ adopted_id: string; enabled: boolean }> {
  return response(
    await jsonRequest(
      `${BASE}/adopted/${encodeURIComponent(id)}/toggle`,
      "POST",
      { enabled },
    ),
  );
}

export async function getJob(): Promise<JobStatus> {
  return response(await request(`${BASE}/jobs/current`));
}
