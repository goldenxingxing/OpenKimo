/**
 * Turning the MCP server form into an `mcp.json` entry, and back.
 *
 * Kept out of the component so it can be tested without a DOM: the fiddly
 * parts here are the textarea formats (KEY=VALUE, `Header: value`, a command
 * line with quoted arguments) and the masked-secret round trip, none of which
 * need React to go wrong.
 */

import type { McpServer } from "../../lib/api/apis/AdminMcpApi";

export type McpTransport = "stdio" | "http" | "sse";

export interface McpFormState {
  name: string;
  transport: McpTransport;
  /** stdio */
  command: string;
  args: string;
  env: string;
  /** http / sse */
  url: string;
  headers: string;
  /** "", "oauth", or a bearer token. */
  auth: string;
  /** The entry being edited, as loaded; absent when adding a server. */
  loaded?: LoadedServer;
}

/**
 * What an edit started from. The form shows only some of an entry's keys and
 * cannot express every argument (there is no escape for a literal `"`), so a
 * save keeps whatever the form did not touch rather than rebuilding the entry
 * from the fields alone.
 */
export interface LoadedServer {
  config: Record<string, unknown>;
  /** The command and args fields exactly as they were filled in. */
  commandText: string;
  argsText: string;
}

/** Keys the form edits; every other key of a loaded entry is carried over. */
const FORM_KEYS = ["command", "args", "env", "url", "transport", "headers", "auth"];

export const emptyMcpForm: McpFormState = {
  name: "",
  transport: "stdio",
  command: "",
  args: "",
  env: "",
  url: "",
  headers: "",
  auth: "",
};

/** Same rule the API enforces, so the form can say no before the round trip. */
const NAME_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;

/** Whitespace between command-line arguments. */
const WHITESPACE = /\s/;

/** Only http(s) URLs reach an MCP server. */
const HTTP_URL = /^https?:\/\//i;

/** A value the server masked. Sent back untouched, it means "unchanged". */
export function isMaskedSecret(value: string): boolean {
  return value.startsWith("***");
}

/** Quote one argument so {@link splitCommandLine} reads it back whole. */
export function quoteCommandArg(arg: string): string {
  return arg === "" || WHITESPACE.test(arg) ? `"${arg}"` : arg;
}

/**
 * Split a command line into command and arguments.
 *
 * Double quotes group a single argument so a path with spaces survives; there
 * is no escaping beyond that, because anything more elaborate belongs in the
 * raw JSON editor rather than in a one-line field.
 */
export function splitCommandLine(text: string): string[] {
  const parts: string[] = [];
  let current = "";
  let quoted = false;
  let started = false;

  for (const char of text) {
    if (char === '"') {
      quoted = !quoted;
      started = true;
      continue;
    }
    if (!quoted && WHITESPACE.test(char)) {
      if (started) {
        parts.push(current);
        current = "";
        started = false;
      }
      continue;
    }
    current += char;
    started = true;
  }
  if (started) parts.push(current);
  return parts;
}

export interface KeyValueParse {
  values: Record<string, string>;
  /** Lines that had no separator, verbatim, for the error message. */
  invalid: string[];
}

/** Parse `KEY=VALUE` or `Header: value` lines, ignoring blanks and `#` comments. */
export function parseKeyValueLines(
  text: string,
  separator: "=" | ":",
): KeyValueParse {
  const values: Record<string, string> = {};
  const invalid: string[] = [];

  for (const rawLine of text.split("\n")) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) continue;
    const index = line.indexOf(separator);
    if (index <= 0) {
      invalid.push(line);
      continue;
    }
    values[line.slice(0, index).trim()] = line.slice(index + 1).trim();
  }
  return { values, invalid };
}

/** Render a mapping back into the textarea format it came from. */
export function formatKeyValueLines(
  values: Record<string, unknown> | undefined,
  separator: "=" | ":",
): string {
  if (!values) return "";
  const join = separator === ":" ? ": " : "=";
  return Object.entries(values)
    .map(([key, value]) => `${key}${join}${String(value)}`)
    .join("\n");
}

/**
 * Why a form cannot be saved. A code rather than a sentence, so the panel can
 * say it in the user's language (`admin:mcp.formErrors.<code>`); `detail` is
 * the offending input, shown as it was typed.
 */
export type McpFormError = {
  code:
    | "commandRequired"
    | "envLine"
    | "urlRequired"
    | "urlScheme"
    | "headerLine"
    | "nameRequired"
    | "nameInvalid"
    | "nameTaken";
  detail?: string;
};

export type BuildResult =
  | { ok: true; config: Record<string, unknown> }
  | { ok: false; error: McpFormError };

/**
 * Build the `mcp.json` entry a form describes.
 *
 * Only the shape is checked here — whether the server exists, and whether its
 * credentials work, is what the Test button is for.
 */
export function buildServerConfig(state: McpFormState): BuildResult {
  const built = buildFormFields(state);
  if (!built.ok) return built;

  const loaded = state.loaded;
  if (!loaded) return built;
  // Carry over what the form does not show (cwd, timeout, ...), unless the
  // entry switched between stdio and remote, where those keys stop applying.
  const wasStdio = typeof loaded.config.command === "string";
  if (wasStdio !== (state.transport === "stdio")) return built;
  const kept = Object.fromEntries(
    Object.entries(loaded.config).filter(([key]) => !FORM_KEYS.includes(key)),
  );
  return { ok: true, config: { ...kept, ...built.config } };
}

function buildFormFields(state: McpFormState): BuildResult {
  if (state.transport === "stdio") {
    const command = state.command.trim();
    if (!command) return { ok: false, error: { code: "commandRequired" } };
    const { values: env, invalid } = parseKeyValueLines(state.env, "=");
    if (invalid.length > 0) {
      return { ok: false, error: { code: "envLine", detail: invalid[0] } };
    }
    const config: Record<string, unknown> = {};
    const loaded = state.loaded;
    if (
      loaded &&
      typeof loaded.config.command === "string" &&
      state.command === loaded.commandText &&
      state.args === loaded.argsText
    ) {
      // Untouched: keep the stored command and args verbatim, since the
      // one-line fields cannot represent every argument exactly.
      config.command = loaded.config.command;
      if (Array.isArray(loaded.config.args)) config.args = loaded.config.args;
    } else {
      const [head, ...inlineArgs] = splitCommandLine(command);
      if (!head) {
        return { ok: false, error: { code: "commandRequired" } };
      }
      const args = [...inlineArgs, ...splitCommandLine(state.args)];
      config.command = head;
      if (args.length > 0) config.args = args;
    }
    if (Object.keys(env).length > 0) config.env = env;
    return { ok: true, config };
  }

  const url = state.url.trim();
  if (!url) return { ok: false, error: { code: "urlRequired" } };
  if (!HTTP_URL.test(url)) {
    return { ok: false, error: { code: "urlScheme" } };
  }
  const { values: headers, invalid } = parseKeyValueLines(state.headers, ":");
  if (invalid.length > 0) {
    return { ok: false, error: { code: "headerLine", detail: invalid[0] } };
  }

  const config: Record<string, unknown> = { url, transport: state.transport };
  if (Object.keys(headers).length > 0) config.headers = headers;
  const auth = state.auth.trim();
  if (auth) config.auth = auth;
  return { ok: true, config };
}

/** Why this name cannot be used, or null when it can. */
export function validateServerName(
  name: string,
  existingNames: string[],
): McpFormError | null {
  const trimmed = name.trim();
  if (!trimmed) return { code: "nameRequired" };
  if (!NAME_PATTERN.test(trimmed)) return { code: "nameInvalid" };
  if (existingNames.includes(trimmed)) return { code: "nameTaken", detail: trimmed };
  return null;
}

/** Fill the form from an existing server, masked secrets included. */
export function formStateFromServer(server: McpServer): McpFormState {
  const config = server.config as Record<string, unknown>;
  const args = Array.isArray(config.args) ? (config.args as unknown[]) : [];
  const auth = typeof config.auth === "string" ? config.auth : "";
  const commandText =
    typeof config.command === "string" ? quoteCommandArg(config.command) : "";
  const argsText = args.map((a) => quoteCommandArg(String(a))).join(" ");

  return {
    name: server.name,
    transport: server.transport,
    command: commandText,
    args: argsText,
    env: formatKeyValueLines(config.env as Record<string, unknown>, "="),
    url: typeof config.url === "string" ? config.url : "",
    headers: formatKeyValueLines(config.headers as Record<string, unknown>, ":"),
    auth,
    loaded: { config, commandText, argsText },
  };
}
