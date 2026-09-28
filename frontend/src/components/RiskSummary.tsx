import type { ScoreResult } from "../api";
import { explain } from "../lib/format";

const BUCKET_STYLE: Record<ScoreResult["bucket"], string> = {
  low: "text-emerald-800 bg-emerald-50 ring-emerald-200",
  medium: "text-amber-800 bg-amber-50 ring-amber-200",
  high: "text-orange-800 bg-orange-50 ring-orange-200",
  severe: "text-red-800 bg-red-50 ring-red-200",
};

const GAUGE_COLOR: Record<ScoreResult["bucket"], string> = {
  low: "#059669",
  medium: "#d97706",
  high: "#ea580c",
  severe: "#dc2626",
};

function Gauge({ score, bucket }: { score: number; bucket: ScoreResult["bucket"] }) {
  // Semicircle from 180° (score 0) to 0° (score 100).
  const angle = Math.PI * (1 - score / 100);
  const x = 60 + 50 * Math.cos(angle);
  const y = 60 - 50 * Math.sin(angle);
  return (
    <svg viewBox="0 0 120 70" className="w-40" role="img" aria-label={`Risk score ${score} of 100`}>
      <path d="M10 60 A50 50 0 0 1 110 60" fill="none" stroke="#e2e8f0" strokeWidth="10" />
      {score > 0 && (
        <path
          d={`M10 60 A50 50 0 0 1 ${x.toFixed(2)} ${y.toFixed(2)}`}
          fill="none"
          stroke={GAUGE_COLOR[bucket]}
          strokeWidth="10"
        />
      )}
      <text x="60" y="58" textAnchor="middle" className="fill-slate-900 text-[22px] font-semibold">
        {Math.round(score)}
      </text>
    </svg>
  );
}

export function RiskSummary({ result }: { result: ScoreResult }) {
  const flagged = result.flagged ?? result.breakdown.length > 0;
  return (
    <section aria-labelledby="summary-heading" className="rounded-xl bg-white p-5 shadow-sm ring-1 ring-slate-200">
      <h2 id="summary-heading" className="sr-only">
        Risk summary
      </h2>
      <div className="flex flex-col items-start gap-4 sm:flex-row sm:items-center">
        <Gauge score={result.score} bucket={result.bucket} />
        <div className="space-y-2">
          <p
            className={`inline-flex rounded-full px-3 py-1 text-sm font-semibold ring-1 ${
              flagged ? "bg-red-50 text-red-800 ring-red-200" : "bg-emerald-50 text-emerald-800 ring-emerald-200"
            }`}
          >
            {flagged ? "Exposure detected" : "No exposure found"}
          </p>
          <p className="text-sm text-slate-600">
            Risk score {result.score.toFixed(1)} / 100 ·{" "}
            <span className={`rounded px-1.5 py-0.5 ring-1 ${BUCKET_STYLE[result.bucket]}`}>
              {result.bucket[0].toUpperCase() + result.bucket.slice(1)}
            </span>{" "}
            strength
          </p>
          <p className="max-w-prose text-slate-800">{explain(result)}</p>
        </div>
      </div>
    </section>
  );
}
