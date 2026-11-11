import type { CSSProperties } from "react";

/**
 * The box a built-in animation plays in: the animation alone, over the page.
 *
 * The animations used to cover the page in dark slate. The thinking one comes
 * and goes with every thinking block, so a busy turn flickered the whole page
 * between that and the white workspace.
 */
export const OVERLAY_STYLE: CSSProperties = {
  background: "transparent",
  display: "flex",
  flexDirection: "column",
  alignItems: "center",
  justifyContent: "center",
  animation: "psFadeIn 0.2s ease-out",
  cursor: "pointer",
  border: "none",
};
