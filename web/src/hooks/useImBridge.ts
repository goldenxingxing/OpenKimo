import { useState, useCallback, useEffect } from "react";
import { getAuthHeader } from "../lib/auth";
import { getApiBaseUrl } from "./utils";

/** Config knobs stored per channel (mirrors the backend contract). */
export type ImChannelConfig = {
  show_thinking: boolean;
  group_chat_enabled: boolean;
  timeout_seconds: number;
};

export type ImChannelStatus = {
  running: boolean;
  last_error: string | null;
};

export type ImChannel = {
  id: number;
  type: "feishu" | "dingtalk";
  name: string;
  enabled: boolean;
  app_id: string;
  /** True when a secret is stored; the secret itself is never returned. */
  has_secret: boolean;
  config: ImChannelConfig;
  status: ImChannelStatus;
};

export type ImChannelBinding = {
  external_id: string;
  display_name: string;
  session_id: string;
};

/** A stranger who messaged the bot and is waiting for whitelist approval. */
export type ImPendingUser = {
  external_id: string;
  display_name: string;
  /** Epoch seconds (float) of the most recent message. */
  last_seen: number;
  message_count: number;
};

export type CreateChannelInput = {
  type: "feishu" | "dingtalk";
  name: string;
  app_id: string;
  app_secret: string;
  config?: ImChannelConfig;
};

export type UpdateChannelInput = {
  name?: string;
  app_id?: string;
  /** Empty string means "leave unchanged" per the backend contract. */
  app_secret?: string;
  enabled?: boolean;
  config?: ImChannelConfig;
};

export const DEFAULT_IM_CHANNEL_CONFIG: ImChannelConfig = {
  show_thinking: false,
  group_chat_enabled: false,
  timeout_seconds: 300,
};

async function imFetch(path: string, init?: RequestInit): Promise<Response> {
  const basePath = getApiBaseUrl();
  const response = await fetch(`${basePath}/api/im${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...getAuthHeader(),
      ...init?.headers,
    },
  });
  if (!response.ok) {
    let detail = "";
    try {
      const data = await response.json();
      detail = typeof data?.detail === "string" ? data.detail : "";
    } catch {
      // Non-JSON error body; fall through to the status-based message.
    }
    throw new Error(detail || `Request failed with status ${response.status}`);
  }
  return response;
}

export type UseImChannelsReturn = {
  channels: ImChannel[];
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
  createChannel: (input: CreateChannelInput) => Promise<void>;
  updateChannel: (id: number, input: UpdateChannelInput) => Promise<void>;
  deleteChannel: (id: number) => Promise<void>;
  getWhitelist: (id: number) => Promise<string[]>;
  setWhitelist: (id: number, openIds: string[]) => Promise<void>;
  getBindings: (id: number) => Promise<ImChannelBinding[]>;
  getPending: (id: number) => Promise<ImPendingUser[]>;
  approvePending: (id: number, externalId: string) => Promise<void>;
  dismissPending: (id: number, externalId: string) => Promise<void>;
};

/**
 * Manages IM bridge channels (Feishu etc.) against /api/im, following the
 * same fetch conventions as useSessions: getApiBaseUrl() base, getAuthHeader()
 * auth, JSON bodies, throwing on non-OK so callers can toast the failure.
 */
export function useImChannels(): UseImChannelsReturn {
  const [channels, setChannels] = useState<ImChannel[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await imFetch("/channels");
      const data: ImChannel[] = await response.json();
      setChannels(data);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to load channels";
      setError(message);
      console.error("Failed to load IM channels:", err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const createChannel = useCallback(
    async (input: CreateChannelInput) => {
      await imFetch("/channels", {
        method: "POST",
        body: JSON.stringify(input),
      });
      await refresh();
    },
    [refresh],
  );

  const updateChannel = useCallback(
    async (id: number, input: UpdateChannelInput) => {
      await imFetch(`/channels/${id}`, {
        method: "PATCH",
        body: JSON.stringify(input),
      });
      await refresh();
    },
    [refresh],
  );

  const deleteChannel = useCallback(
    async (id: number) => {
      await imFetch(`/channels/${id}`, { method: "DELETE" });
      await refresh();
    },
    [refresh],
  );

  const getWhitelist = useCallback(async (id: number): Promise<string[]> => {
    const response = await imFetch(`/channels/${id}/whitelist`);
    const data: { open_ids: string[] } = await response.json();
    return data.open_ids ?? [];
  }, []);

  const setWhitelist = useCallback(
    async (id: number, openIds: string[]) => {
      await imFetch(`/channels/${id}/whitelist`, {
        method: "PUT",
        body: JSON.stringify({ open_ids: openIds }),
      });
      await refresh();
    },
    [refresh],
  );

  const getBindings = useCallback(async (id: number): Promise<ImChannelBinding[]> => {
    const response = await imFetch(`/channels/${id}/bindings`);
    const data: ImChannelBinding[] = await response.json();
    return data;
  }, []);

  const getPending = useCallback(async (id: number): Promise<ImPendingUser[]> => {
    const response = await imFetch(`/channels/${id}/pending`);
    const data: ImPendingUser[] = await response.json();
    return data;
  }, []);

  const approvePending = useCallback(
    async (id: number, externalId: string) => {
      await imFetch(
        `/channels/${id}/pending/${encodeURIComponent(externalId)}/approve`,
        { method: "POST" },
      );
      await refresh();
    },
    [refresh],
  );

  const dismissPending = useCallback(
    async (id: number, externalId: string) => {
      await imFetch(`/channels/${id}/pending/${encodeURIComponent(externalId)}`, {
        method: "DELETE",
      });
      await refresh();
    },
    [refresh],
  );

  return {
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
  };
}
