import { getAuthHeader } from "../../auth";
import { getApiBaseUrl } from "../../../hooks/utils";

export interface WikiPageSummary {
  /** Logical path, `category/slug.md`. */
  path: string;
  category: string;
  /** Null, like every other field below, when the page does not parse. */
  title: string | null;
  tags: string[];
  created: string | null;
  updated: string | null;
  revision: number | null;
  source_count: number;
  /** Why the page could not be parsed; such a page cannot be edited here. */
  error: string | null;
}

export interface WikiOverview {
  root: string;
  global_revision: number;
  categories: string[];
  pages: WikiPageSummary[];
}

export interface WikiSource {
  kind: "workspace-file" | "conversation" | "web";
  label: string;
  content_hash: string;
}

export interface WikiPageDetail {
  path: string;
  title: string;
  tags: string[];
  created: string;
  updated: string;
  revision: number;
  sources: WikiSource[];
  body: string;
  global_revision: number;
}

export interface WikiPageWrite {
  title: string;
  tags: string[];
  body: string;
  /** The revision the edit started from; null creates a new page. */
  expected_revision: number | null;
}

export interface WikiSearchHit {
  path: string;
  title: string;
  summary: string;
  snippet: string;
  revision: number;
}

export interface WikiLintIssue {
  code: string;
  path: string;
  detail: string;
  related_paths: string[];
}

export interface WikiLintReport {
  scanned_pages: number;
  issues: WikiLintIssue[];
}

export interface WikiLogEntry {
  timestamp: string;
  operation: string;
  revision: number | null;
  pages: string[];
  summary: string;
}

/** Carries the status so a caller can tell a stale edit (409) from a refused one. */
export class WikiApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

function url(path: string): string {
  return `${getApiBaseUrl()}${path}`;
}

function pageUrl(path: string): string {
  return `/api/admin/wiki/pages/${path.split("/").map(encodeURIComponent).join("/")}`;
}

async function response<T>(value: Response): Promise<T> {
  if (!value.ok) {
    let message = `Request failed (${value.status})`;
    try {
      const body = (await value.json()) as { detail?: unknown };
      if (typeof body.detail === "string" && body.detail) {
        message = body.detail;
      }
    } catch {
      // Keep the status-based fallback.
    }
    throw new WikiApiError(message, value.status);
  }
  if (value.status === 204) {
    return undefined as T;
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

export async function getWikiOverview(): Promise<WikiOverview> {
  return response(await request("/api/admin/wiki"));
}

export async function getWikiPage(path: string): Promise<WikiPageDetail> {
  return response(await request(pageUrl(path)));
}

export async function saveWikiPage(
  path: string,
  page: WikiPageWrite,
): Promise<{ global_revision: number; pages: string[] }> {
  return response(
    await request(pageUrl(path), {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(page),
    }),
  );
}

export async function deleteWikiPage(path: string, expectedRevision: number): Promise<void> {
  return response(
    await request(`${pageUrl(path)}?expected_revision=${expectedRevision}`, {
      method: "DELETE",
    }),
  );
}

export async function searchWiki(query: string, limit = 30): Promise<WikiSearchHit[]> {
  const params = new URLSearchParams({ q: query, limit: String(limit) });
  return response(await request(`/api/admin/wiki/search?${params}`));
}

export async function lintWiki(): Promise<WikiLintReport> {
  return response(await request("/api/admin/wiki/lint"));
}

export async function getWikiLog(limit = 100): Promise<WikiLogEntry[]> {
  return response(await request(`/api/admin/wiki/log?limit=${limit}`));
}
