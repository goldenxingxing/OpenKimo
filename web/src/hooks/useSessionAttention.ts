import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  type AttentionState,
  EMPTY_ATTENTION,
  type SessionActivity,
  applyActivity,
  applySnapshot,
  attentionTitle,
  markSeen,
} from "@/features/sessions/session-attention";
import { getAuthToken } from "@/lib/auth";
import { getApiBaseUrl } from "./utils";

type ActivityEvent =
  | { type: "snapshot"; sessions: SessionActivity[] }
  | ({ type: "activity" } & SessionActivity);

const FLASH_INTERVAL_MS = 1000;
/** After the server refused the stream, e.g. a 401 or a restart. */
const RECONNECT_DELAY_MS = 5000;

/** In front of the person: the tab is showing and its window has focus. */
function isPresent(): boolean {
  return document.visibilityState === "visible" && document.hasFocus();
}

/**
 * Put sessions that stopped to wait for the person into the tab title, and
 * return them so the page can mark them too.
 *
 * The title names the one session, or counts several, and counts down as each
 * is opened. It flashes while the page is out of sight and holds still once the
 * person is back, since they are looking at it then. No permission is needed:
 * a system notification would ask for one.
 */
export function useSessionAttention(
  selectedSessionId: string | null,
  titleOf: (sessionId: string) => string | undefined,
  /** Whether someone is signed in; the stream only serves their sessions. */
  enabled: boolean,
): readonly string[] {
  const { t } = useTranslation("sessions");
  const [attention, setAttention] = useState<AttentionState>(EMPTY_ATTENTION);
  const [present, setPresent] = useState(isPresent);

  const viewing = present ? selectedSessionId : null;
  const viewingRef = useRef(viewing);
  viewingRef.current = viewing;

  useEffect(() => {
    const update = () => setPresent(isPresent());
    document.addEventListener("visibilitychange", update);
    window.addEventListener("focus", update);
    window.addEventListener("blur", update);
    return () => {
      document.removeEventListener("visibilitychange", update);
      window.removeEventListener("focus", update);
      window.removeEventListener("blur", update);
    };
  }, []);

  // Opening a session, or coming back to one left open, is seeing it.
  useEffect(() => {
    setAttention((state) => markSeen(state, viewing));
  }, [viewing]);

  // Bumped to open the stream again after the server closed it.
  const [connection, setConnection] = useState(0);

  // biome-ignore lint/correctness/useExhaustiveDependencies: `connection` only exists to reopen the stream
  useEffect(() => {
    if (!enabled) {
      // Signed out: another person may sign in next, and what was waiting
      // for this one is not theirs to see.
      setAttention(EMPTY_ATTENTION);
      return;
    }
    const token = getAuthToken();
    const base = `${getApiBaseUrl()}/api/sessions/activity`;
    const url = token ? `${base}?token=${encodeURIComponent(token)}` : base;
    const source = new EventSource(url, { withCredentials: true });
    let retry: number | undefined;
    // EventSource retries a dropped connection by itself, but a response it
    // cannot use — a 401, a 502 while the server restarts — closes it for
    // good. The stream used to open before sign-in, get that 401, and stay
    // closed, so no session was ever announced until the page was reloaded.
    source.onerror = () => {
      if (source.readyState === EventSource.CLOSED && retry === undefined) {
        retry = window.setTimeout(() => setConnection((n) => n + 1), RECONNECT_DELAY_MS);
      }
    };
    source.onmessage = (message) => {
      let event: ActivityEvent;
      try {
        event = JSON.parse(message.data) as ActivityEvent;
      } catch {
        return;
      }
      if (event.type === "snapshot") {
        setAttention((state) => applySnapshot(state, event.sessions, viewingRef.current));
      } else if (event.type === "activity") {
        setAttention((state) => applyActivity(state, event, viewingRef.current));
      }
    };
    // A reconnect, by either route, starts with a snapshot that re-baselines.
    return () => {
      window.clearTimeout(retry);
      source.close();
    };
  }, [enabled, connection]);

  const label = attentionTitle(attention.pending, titleOf, {
    one: (title) => t("attention.one", { title }),
    many: (count) => t("attention.many", { count }),
    untitled: t("attention.untitled"),
  });

  // The title the page had before anything was waiting, to put back after.
  const baseTitle = useRef<string | null>(null);

  useEffect(
    () => () => {
      if (baseTitle.current !== null) document.title = baseTitle.current;
    },
    [],
  );

  useEffect(() => {
    if (label === null) {
      if (baseTitle.current !== null) {
        document.title = baseTitle.current;
        baseTitle.current = null;
      }
      return;
    }
    if (baseTitle.current === null) {
      baseTitle.current = document.title;
    }
    const original = baseTitle.current;
    document.title = label;
    if (present) {
      return;
    }
    let showingLabel = true;
    const timer = window.setInterval(() => {
      showingLabel = !showingLabel;
      document.title = showingLabel ? label : original;
    }, FLASH_INTERVAL_MS);
    return () => {
      window.clearInterval(timer);
    };
  }, [label, present]);

  return attention.pending;
}
