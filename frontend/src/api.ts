// Typed client for the backend API (/api/v1). Wei amounts arrive as decimal strings.

export type HealthResponse = {
  status: "ok" | "degraded";
  database: "ok" | "unavailable";
};

export type RiskLabel = "sanctioned" | "malicious" | "mixer";

export type Contribution = {
  address: string;
  name: string | null;
  label: RiskLabel;
  labels: string[];
  severity: number;
  hops: number;
  path: string[];
  via: string[];
  bottleneck_eq_wei: string;
  flow_share: number;
  flow_factor: number;
  hop_weight: number;
  discount: number;
  contribution: number;
};

export type GraphNode = {
  address: string;
  role: "target" | "flagged" | "path";
  labels: string[];
  hop: number | null;
  is_hub: boolean;
};

export type GraphEdge = {
  source: string;
  target: string;
  tx_count: number;
  token_transfer_count: number;
  total_value_wei: string;
  value_eq_wei: string;
  last_seen: number | null;
};

export type ScoreResult = {
  score: number;
  bucket: "low" | "medium" | "high" | "severe";
  flagged: boolean | null;
  breakdown: Contribution[];
  graph: { nodes: GraphNode[]; edges: GraphEdge[] };
  stats: {
    nodes: number;
    edges: number;
    expanded: number;
    api_calls: number;
    duration_ms: number;
  };
};

export type JobErrorCode =
  | "upstream_rate_limited"
  | "upstream_unavailable"
  | "upstream_error"
  | "not_configured"
  | "timeout"
  | "interrupted"
  | "internal";

export type ScoreRun = {
  id: string;
  address: string;
  status: "pending" | "running" | "done" | "failed";
  params_hash: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  result: ScoreResult | null;
  error: { code: JobErrorCode; message: string } | null;
};

export type Holding = {
  token_address: string | null;
  symbol: string;
  decimals: number;
  balance: string;
  balance_raw: string;
  price_usd: number | null;
  value_usd: number | null;
  price_confidence: number | null;
};

export type Portfolio = {
  address: string;
  eth: Holding | null;
  tokens: Holding[];
  priced_token_count: number;
  unpriced_token_count: number;
  total_usd: number | null;
  warnings: string[];
  as_of: string;
};

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

// FastAPI returns {"detail": "..."} or, for validation errors, {"detail": [{"msg": ...}]}.
async function errorMessage(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as { detail?: unknown };
    if (typeof body.detail === "string") return body.detail;
    if (Array.isArray(body.detail) && body.detail[0]?.msg) {
      return String(body.detail[0].msg).replace(/^Value error, /, "");
    }
  } catch {
    // Non-JSON error body; fall through.
  }
  return `HTTP ${res.status}`;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, init);
  if (!res.ok) throw new ApiError(res.status, await errorMessage(res));
  return (await res.json()) as T;
}

export async function fetchHealth(): Promise<HealthResponse> {
  const res = await fetch("/api/v1/health");
  // A 503 still carries a useful body describing which dependency is down.
  if (!res.ok && res.status !== 503) {
    throw new Error(`Health check failed: HTTP ${res.status}`);
  }
  return (await res.json()) as HealthResponse;
}

export function submitScore(address: string): Promise<ScoreRun> {
  return request<ScoreRun>("/api/v1/scores", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ address }),
  });
}

export function getScore(id: string): Promise<ScoreRun> {
  return request<ScoreRun>(`/api/v1/scores/${id}`);
}

export function getPortfolio(address: string): Promise<Portfolio> {
  return request<Portfolio>(`/api/v1/wallets/${address}/portfolio`);
}
