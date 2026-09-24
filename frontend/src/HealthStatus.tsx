import { useQuery } from "@tanstack/react-query";

import { fetchHealth } from "./api";

export function HealthStatus() {
  const { data, error, isPending } = useQuery({
    queryKey: ["health"],
    queryFn: fetchHealth,
    refetchInterval: 10_000,
  });

  if (isPending) return <p role="status">Checking backend…</p>;
  if (error) return <p role="alert">Backend unreachable: {error.message}</p>;

  const healthy = data.status === "ok";
  return (
    <p role="status" className={healthy ? "ok" : "bad"}>
      Backend: <strong>{healthy ? "healthy" : "degraded"}</strong> (database: {data.database})
    </p>
  );
}
