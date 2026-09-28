import { useQuery } from "@tanstack/react-query";

import { fetchHealth } from "./api";

export function HealthStatus() {
  const { data, error, isPending } = useQuery({
    queryKey: ["health"],
    queryFn: fetchHealth,
    refetchInterval: 10_000,
  });

  if (isPending) return <p role="status">Checking backend…</p>;
  if (error) {
    return (
      <p role="alert" className="text-red-700">
        Backend unreachable: {error.message}
      </p>
    );
  }

  const healthy = data.status === "ok";
  return (
    <p role="status">
      Backend:{" "}
      <strong className={healthy ? "text-emerald-700" : "text-red-700"}>
        {healthy ? "healthy" : "degraded"}
      </strong>{" "}
      (database: {data.database})
    </p>
  );
}
