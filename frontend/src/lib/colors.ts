// Shared by the graph canvas (hex needed) and the legend. Chosen for contrast on white
// and to stay distinguishable for common color-vision deficiencies; the legend and
// tables always pair color with text.
export const NODE_COLORS = {
  target: "#2563eb",
  sanctioned: "#dc2626",
  malicious: "#9f1239",
  mixer: "#d97706",
  exchange: "#64748b",
  path: "#94a3b8",
} as const;

export type NodeKind = keyof typeof NODE_COLORS;

export const NODE_KIND_TEXT: Record<NodeKind, string> = {
  target: "Wallet being scored",
  sanctioned: "Sanctioned",
  malicious: "Malicious",
  mixer: "Mixer",
  exchange: "Exchange / hub",
  path: "Intermediate wallet",
};

export function nodeKind(role: string, labels: string[], isHub: boolean): NodeKind {
  if (role === "target") return "target";
  for (const label of ["sanctioned", "malicious", "mixer"] as const) {
    if (labels.includes(label)) return label;
  }
  if (labels.includes("exchange") || isHub) return "exchange";
  return "path";
}
