import { useSyncExternalStore } from "react";
import i18n from "@/i18n";
import { getAuthHeader } from "@/lib/auth";

export type BackendApp =
  | "default"
  | "finder"
  | "cursor"
  | "vscode"
  | "iterm"
  | "terminal"
  | "antigravity";

export type OpenTargetDef = {
  id: string;
  label: string;
  backendApp: BackendApp;
  macOnly?: boolean;
};

/**
 * Authoritative list of all supported open targets.
 * Components filter this list based on their needs.
 */
export const ALL_OPEN_TARGETS: OpenTargetDef[] = [
  {
    id: "finder",
    // macOS names it in the user's language, so translate it too
    get label() {
      return i18n.t("chat:openIn.apps.finder");
    },
    backendApp: "finder",
    macOnly: true,
  },
  { id: "cursor", label: "Cursor", backendApp: "cursor" },
  { id: "vscode", label: "VS Code", backendApp: "vscode" },
  { id: "antigravity", label: "Antigravity", backendApp: "antigravity" },
  { id: "iterm", label: "iTerm", backendApp: "iterm", macOnly: true },
  {
    id: "terminal",
    get label() {
      return i18n.t("chat:openIn.apps.terminal");
    },
    backendApp: "terminal",
    macOnly: true,
  },
];

const LAST_OPEN_TARGET_KEY = "kimo-open-in-last-target";

/** In-memory subscribers for same-tab reactivity. */
const listeners = new Set<() => void>();

function getSnapshot(): string | null {
  try {
    return localStorage.getItem(LAST_OPEN_TARGET_KEY);
  } catch {
    return null;
  }
}

function subscribe(cb: () => void): () => void {
  listeners.add(cb);
  return () => listeners.delete(cb);
}

export function getLastOpenTargetId(): string | null {
  return getSnapshot();
}

export function setLastOpenTargetId(id: string): void {
  try {
    localStorage.setItem(LAST_OPEN_TARGET_KEY, id);
  } catch {
    /* ignore */
  }
  // Notify all subscribers in the same tab.
  for (const cb of listeners) cb();
}

/**
 * Reactive hook — returns the current last-used target ID and
 * re-renders whenever any component calls setLastOpenTargetId().
 */
export function useLastOpenTargetId(): string | null {
  return useSyncExternalStore(subscribe, getSnapshot, () => null);
}

export async function openViaBackend(
  app: BackendApp,
  path: string,
): Promise<void> {
  const response = await fetch("/api/open-in", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...getAuthHeader() },
    body: JSON.stringify({ app, path }),
  });

  if (response.ok) {
    return;
  }

  let detail = i18n.t("chat:openIn.openFailedSentence");
  try {
    const data = await response.json();
    if (data?.detail) {
      detail = String(data.detail);
    }
  } catch {
    /* ignore */
  }
  throw new Error(detail);
}

/**
 * Map a mentioned path onto a real file before opening it. An agent often
 * cites a bare file name while the file lives in a subdirectory of the work
 * directory; the backend resolves the literal path first, then searches the
 * work directory tree by basename and returns the shallowest match.
 */
export async function resolveViaBackend(
  path: string,
  workDir?: string | null,
): Promise<string> {
  const response = await fetch("/api/open-in/resolve", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...getAuthHeader() },
    body: JSON.stringify({ path, work_dir: workDir || null }),
  });

  if (!response.ok) {
    throw new Error(i18n.t("chat:openIn.openFailedSentence"));
  }
  const data = (await response.json()) as {
    found: boolean;
    resolved?: string | null;
  };
  if (!(data.found && data.resolved)) {
    throw new Error(i18n.t("chat:openIn.pathNotFound", { path }));
  }
  return data.resolved;
}
