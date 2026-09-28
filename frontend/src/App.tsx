import { useEffect, useState } from "react";

import { ApiError } from "./api";
import { AddressForm } from "./components/AddressForm";
import { BreakdownTable } from "./components/BreakdownTable";
import { FlaggedGraph } from "./components/FlaggedGraph";
import { HoldingsPanel } from "./components/HoldingsPanel";
import { RiskSummary } from "./components/RiskSummary";
import { ScoreProgress } from "./components/ScoreProgress";
import { HealthStatus } from "./HealthStatus";
import { useScoreJob } from "./hooks/useScoreJob";
import { checkAddress } from "./lib/address";
import { JOB_ERROR_TEXT, shortAddress } from "./lib/format";

function addressFromUrl(): string | null {
  const param = new URLSearchParams(window.location.search).get("address");
  if (!param) return null;
  const check = checkAddress(param);
  return check.ok ? check.address : null;
}

function ErrorBox({ children }: { children: React.ReactNode }) {
  return (
    <div role="alert" className="rounded-xl bg-red-50 p-5 text-red-900 ring-1 ring-red-200">
      {children}
    </div>
  );
}

export default function App() {
  const [address, setAddress] = useState<string | null>(addressFromUrl);
  const { run, error, submitting } = useScoreJob(address);

  // Keep the URL shareable: ?address=0x… reproduces the view.
  useEffect(() => {
    const url = new URL(window.location.href);
    if (address) url.searchParams.set("address", address);
    else url.searchParams.delete("address");
    window.history.replaceState(null, "", url);
  }, [address]);

  const inProgress = submitting || run?.status === "pending" || run?.status === "running";
  const result = run?.status === "done" ? run.result : null;

  return (
    <div className="min-h-screen bg-slate-50 text-slate-800">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:rounded focus:bg-white focus:px-3 focus:py-2"
      >
        Skip to content
      </a>
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto max-w-5xl px-4 py-5">
          <h1 className="text-2xl font-semibold text-slate-900">Wallet Risk Tracker</h1>
          <p className="text-sm text-slate-600">
            How close is an Ethereum wallet to sanctioned, malicious or mixer addresses?
          </p>
        </div>
      </header>

      <main id="main" className="mx-auto max-w-5xl space-y-5 px-4 py-6">
        <AddressForm initial={address ?? ""} busy={inProgress} onSubmit={setAddress} />

        {address && (
          <p className="text-sm text-slate-600">
            Wallet <span className="font-mono text-slate-900" title={address}>{shortAddress(address)}</span>
          </p>
        )}

        {error && (
          <ErrorBox>
            {error instanceof ApiError && error.status === 422
              ? error.message
              : error instanceof ApiError && error.status === 503
                ? "Scoring is unavailable: the server has no Etherscan API key configured."
                : `Couldn't reach the server: ${error.message}`}
          </ErrorBox>
        )}

        {inProgress && !error && <ScoreProgress key={address} run={run} />}

        {run?.status === "failed" && run.error && (
          <ErrorBox>
            <p className="font-medium">Couldn't score this wallet.</p>
            <p>{JOB_ERROR_TEXT[run.error.code]}</p>
          </ErrorBox>
        )}

        {result && (
          <div className="space-y-5">
            <RiskSummary result={result} />
            <div className="grid gap-5 lg:grid-cols-2">
              <div className="space-y-5">
                <BreakdownTable items={result.breakdown} />
                <FlaggedGraph result={result} />
              </div>
              {address && <HoldingsPanel address={address} />}
            </div>
            <p className="text-xs text-slate-500">
              Checked {result.stats.nodes} addresses ({result.stats.expanded} expanded) with{" "}
              {result.stats.api_calls} Etherscan calls in {(result.stats.duration_ms / 1000).toFixed(1)}s ·
              params {run?.params_hash}
            </p>
          </div>
        )}
      </main>

      <footer className="mx-auto max-w-5xl px-4 pb-8 text-xs text-slate-500">
        <HealthStatus />
        <p className="mt-1">
          Proximity to labeled addresses is a signal, not proof of wrongdoing. Labels: OFAC SDN list
          and Etherscan name tags.
        </p>
      </footer>
    </div>
  );
}
