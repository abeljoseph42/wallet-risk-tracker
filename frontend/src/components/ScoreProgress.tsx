import { useEffect, useState } from "react";

import type { ScoreRun } from "../api";

export function ScoreProgress({ run }: { run: ScoreRun | undefined }) {
  const [seconds, setSeconds] = useState(0);
  useEffect(() => {
    const started = Date.now();
    const timer = setInterval(() => setSeconds(Math.floor((Date.now() - started) / 1000)), 1000);
    return () => clearInterval(timer);
  }, []);

  const phase = run?.status === "running" ? "Tracing the transaction graph" : "Queued";
  return (
    <div role="status" aria-live="polite" className="flex items-center gap-3 rounded-xl bg-white p-5 shadow-sm ring-1 ring-slate-200">
      <span aria-hidden className="size-4 animate-spin rounded-full border-2 border-blue-700 border-t-transparent" />
      <div>
        <p className="font-medium text-slate-900">
          {phase}… {seconds}s
        </p>
        <p className="text-sm text-slate-600">
          A wallet we haven't seen recently can take up to a minute: its history, and its
          counterparties', are fetched from Etherscan within the free-tier rate limit.
        </p>
      </div>
    </div>
  );
}
