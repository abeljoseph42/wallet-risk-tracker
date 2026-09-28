import { useQuery } from "@tanstack/react-query";

import { getScore, submitScore, type ScoreRun } from "../api";

const POLL_MS = 1500;

function isFinal(run: ScoreRun | undefined): boolean {
  return run?.status === "done" || run?.status === "failed";
}

// Submits a score for `address` (the server reuses a recent result or an in-flight job),
// then polls the job until it is done or failed.
export function useScoreJob(address: string | null) {
  const submit = useQuery({
    queryKey: ["score-submit", address],
    queryFn: () => submitScore(address!),
    enabled: address !== null,
    staleTime: Infinity,
    retry: false,
  });
  const submitted = submit.data;
  const poll = useQuery({
    queryKey: ["score", submitted?.id],
    queryFn: () => getScore(submitted!.id),
    enabled: submitted !== undefined && !isFinal(submitted),
    refetchInterval: (query) => (isFinal(query.state.data) ? false : POLL_MS),
    retry: 2,
  });
  return {
    run: poll.data ?? submitted,
    error: submit.error ?? poll.error,
    submitting: address !== null && submit.isPending,
  };
}
