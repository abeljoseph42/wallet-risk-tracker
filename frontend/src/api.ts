export type HealthResponse = {
  status: "ok" | "degraded";
  database: "ok" | "unavailable";
};

export async function fetchHealth(): Promise<HealthResponse> {
  const res = await fetch("/api/v1/health");
  // A 503 still carries a useful body describing which dependency is down.
  if (!res.ok && res.status !== 503) {
    throw new Error(`Health check failed: HTTP ${res.status}`);
  }
  return (await res.json()) as HealthResponse;
}
