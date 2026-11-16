import { getAuthHeader } from "../../auth";
import { getApiBaseUrl } from "../../../hooks/utils";

/**
 * One server as configured in `~/.kimo/mcp.json`.
 *
 * `config` is the raw entry with credentials masked — header, env and bearer
 * `auth` values arrive as `***tail`. Send a masked value back unchanged and the
 * server keeps what it already stored; that is the only way to edit a server
 * without re-typing its key.
 */
export interface McpServer {
  name: string;
  transport: "stdio" | "http" | "sse";
  /** Command line or URL, ready to print. */
  target: string;
  config: Record<string, unknown>;
  uses_oauth: boolean;
  /** Null when the entry does not use OAuth. */
  oauth_authorized: boolean | null;
}

export interface McpServerList {
  path: string;
  servers: McpServer[];
  /**
   * False when this deployment refuses stdio entries from the browser: they
   * run a command on the server, which is a different proposition on a shared
   * host than on the desktop app's own machine.
   */
  stdio_editable: boolean;
}

export interface McpMutationResult {
  servers: McpServerList;
  restarted_session_ids: string[] | null;
  /** Sessions mid-turn, still running the old config until they restart. */
  skipped_busy_session_ids: string[] | null;
}

export interface McpProbeTool {
  name: string;
  description: string;
}

export interface McpProbeResult {
  ok: boolean;
  tools: McpProbeTool[];
  error: string | null;
}

export interface RawMcpConfig {
  path: string;
  content: string;
}

function url(path: string): string {
  return `${getApiBaseUrl()}${path}`;
}

async function response<T>(value: Response): Promise<T> {
  if (!value.ok) {
    let message = `Request failed (${value.status})`;
    try {
      const body = (await value.json()) as { detail?: unknown } | null;
      const detail = body?.detail;
      if (Array.isArray(detail)) {
        // FastAPI 422 returns detail as an array of {loc, msg, type} objects.
        message = detail
          .map((item) =>
            item && typeof item === "object" && "msg" in item
              ? String((item as { msg: unknown }).msg)
              : String(item),
          )
          .join("; ");
      } else if (typeof detail === "string") {
        message = detail;
      }
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

export async function listMcpServers(): Promise<McpServerList> {
  return response(await request("/api/admin/mcp/servers"));
}

export async function createMcpServer(
  name: string,
  config: Record<string, unknown>,
): Promise<McpMutationResult> {
  return response(
    await jsonRequest("/api/admin/mcp/servers", "POST", { name, config }),
  );
}

export async function updateMcpServer(
  name: string,
  config: Record<string, unknown>,
): Promise<McpMutationResult> {
  return response(
    await jsonRequest(
      `/api/admin/mcp/servers/${encodeURIComponent(name)}`,
      "PUT",
      { config },
    ),
  );
}

export async function deleteMcpServer(
  name: string,
): Promise<McpMutationResult> {
  return response(
    await request(`/api/admin/mcp/servers/${encodeURIComponent(name)}`, {
      method: "DELETE",
    }),
  );
}

export async function testMcpServer(name: string): Promise<McpProbeResult> {
  return response(
    await request(`/api/admin/mcp/servers/${encodeURIComponent(name)}/test`, {
      method: "POST",
    }),
  );
}

export async function getRawMcpConfig(): Promise<RawMcpConfig> {
  return response(await request("/api/admin/mcp/raw"));
}

export async function putRawMcpConfig(
  content: string,
): Promise<McpMutationResult> {
  return response(await jsonRequest("/api/admin/mcp/raw", "PUT", { content }));
}
