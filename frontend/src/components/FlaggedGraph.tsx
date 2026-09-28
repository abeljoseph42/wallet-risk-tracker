import { lazy, Suspense } from "react";

import type { ScoreResult } from "../api";
import { NODE_COLORS, NODE_KIND_TEXT, nodeKind, type NodeKind } from "../lib/colors";

// The graph library is the largest dependency; load it only when there's a graph to draw.
const GraphCanvas = lazy(() => import("./GraphCanvas"));

export function FlaggedGraph({ result }: { result: ScoreResult }) {
  const { nodes, edges } = result.graph;
  if (nodes.length === 0) return null;
  const names = Object.fromEntries(
    result.breakdown.filter((b) => b.name).map((b) => [b.address, b.name as string]),
  );
  const kinds = [...new Set(nodes.map((n) => nodeKind(n.role, n.labels, n.is_hub)))] as NodeKind[];

  return (
    <section aria-labelledby="graph-heading" className="rounded-xl bg-white p-5 shadow-sm ring-1 ring-slate-200">
      <h2 id="graph-heading" className="mb-1 text-lg font-semibold text-slate-900">
        Flagged paths
      </h2>
      <p className="mb-3 text-sm text-slate-600">
        Only the paths behind the score are drawn. Larger nodes and thicker links moved more
        value. The table above lists the same paths as text.
      </p>
      <div role="img" aria-label={`Graph of ${nodes.length} addresses on flagged paths`}>
        <Suspense fallback={<p className="text-sm text-slate-600">Loading graph…</p>}>
          <GraphCanvas nodes={nodes} edges={edges} names={names} />
        </Suspense>
      </div>
      <ul className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-sm text-slate-700" aria-label="Legend">
        {kinds.map((kind) => (
          <li key={kind} className="flex items-center gap-1.5">
            <span aria-hidden className="inline-block size-3 rounded-full" style={{ background: NODE_COLORS[kind] }} />
            {NODE_KIND_TEXT[kind]}
          </li>
        ))}
      </ul>
    </section>
  );
}
