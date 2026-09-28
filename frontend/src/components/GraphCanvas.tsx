import { useEffect, useMemo, useRef, useState } from "react";
import ForceGraph2D from "react-force-graph-2d";

import type { GraphEdge, GraphNode } from "../api";
import { NODE_COLORS, nodeKind, type NodeKind } from "../lib/colors";
import { shortAddress, weiToEth } from "../lib/format";

type Node = { id: string; kind: NodeKind; val: number; label: string };
type Link = { source: string; target: string; eth: number };

const HEIGHT = 360;
// force-graph's default: a node's radius is sqrt(val) * nodeRelSize.
const NODE_REL_SIZE = 4;

export default function GraphCanvas({
  nodes,
  edges,
  names,
}: {
  nodes: GraphNode[];
  edges: GraphEdge[];
  names: Record<string, string>;
}) {
  const container = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(600);

  useEffect(() => {
    const el = container.current;
    if (!el) return;
    const observer = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width));
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const data = useMemo(() => {
    const volume: Record<string, number> = {};
    const links: Link[] = edges.map((e) => {
      const eth = weiToEth(e.value_eq_wei);
      volume[e.source] = (volume[e.source] ?? 0) + eth;
      volume[e.target] = (volume[e.target] ?? 0) + eth;
      return { source: e.source, target: e.target, eth };
    });
    const graphNodes: Node[] = nodes.map((n) => ({
      id: n.address,
      kind: nodeKind(n.role, n.labels, n.is_hub),
      // Node area grows with the value moved through it (log scale).
      val: n.role === "target" ? 8 : 2 + 3 * Math.log10(1 + (volume[n.address] ?? 0)),
      label: names[n.address] ?? shortAddress(n.address),
    }));
    return { nodes: graphNodes, links };
  }, [nodes, edges, names]);

  return (
    <div ref={container} className="overflow-hidden rounded-lg bg-slate-50">
      <ForceGraph2D<Node, Link>
        graphData={data}
        width={width}
        height={HEIGHT}
        nodeRelSize={NODE_REL_SIZE}
        nodeVal={(n) => n.val}
        nodeColor={(n) => NODE_COLORS[n.kind]}
        nodeLabel={(n) => `${n.label} (${n.id})`}
        linkColor={() => "#94a3b8"}
        linkWidth={(l) => 1 + Math.log10(1 + l.eth)}
        linkDirectionalArrowLength={4}
        linkDirectionalArrowRelPos={0.9}
        cooldownTicks={120}
        nodeCanvasObjectMode={() => "after"}
        nodeCanvasObject={(node, ctx, scale) => {
          if (node.kind === "path" || node.kind === "exchange") return;
          ctx.font = `${12 / scale}px system-ui, sans-serif`;
          ctx.fillStyle = "#0f172a";
          ctx.textAlign = "center";
          const radius = Math.sqrt(node.val) * NODE_REL_SIZE;
          ctx.textBaseline = "top";
          ctx.fillText(node.label, node.x ?? 0, (node.y ?? 0) + radius + 3 / scale);
        }}
      />
    </div>
  );
}
