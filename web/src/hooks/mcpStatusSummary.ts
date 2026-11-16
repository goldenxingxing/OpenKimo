import type { MCPStatusSnapshot } from "./wireTypes";

export type McpStatusSummary = {
  /** One line, ready to show in the message stream. */
  text: string;
  /** True when a server failed or needs authorization. */
  problem: boolean;
};

/**
 * Describe how MCP startup went, or return null when it needs no telling.
 *
 * A session with every server connected says nothing: the tools simply work,
 * and a line reporting that would be noise in the transcript forever. What is
 * worth saying is a server the agent could not reach, since its tools are
 * quietly missing for the rest of the session and nothing else would mention
 * it.
 */
export function summarizeMcpStatus(
  snapshot: MCPStatusSnapshot | null | undefined,
): McpStatusSummary | null {
  if (!snapshot || snapshot.total === 0) return null;

  const failed = snapshot.servers.filter((s) => s.status === "failed");
  const unauthorized = snapshot.servers.filter(
    (s) => s.status === "unauthorized",
  );
  if (failed.length === 0 && unauthorized.length === 0) return null;

  const parts = [
    `MCP: ${snapshot.connected}/${snapshot.total} servers connected`,
  ];
  if (failed.length > 0) {
    parts.push(`${failed.map((s) => s.name).join(", ")} failed`);
  }
  if (unauthorized.length > 0) {
    const verb = unauthorized.length === 1 ? "needs" : "need";
    const commands = unauthorized.map((s) => `\`kimo mcp auth ${s.name}\``);
    parts.push(
      `${unauthorized.map((s) => s.name).join(", ")} ${verb} authorization — run ${commands.join(", ")} on the host`,
    );
  }
  return { text: parts.join(" · "), problem: true };
}
