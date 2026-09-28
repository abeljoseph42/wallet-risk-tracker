import type { Contribution } from "../api";
import { formatEth, formatPercent, LABEL_TEXT, shortAddress } from "../lib/format";

export function BreakdownTable({ items }: { items: Contribution[] }) {
  if (items.length === 0) return null;
  return (
    <section aria-labelledby="breakdown-heading" className="rounded-xl bg-white p-5 shadow-sm ring-1 ring-slate-200">
      <h2 id="breakdown-heading" className="mb-3 text-lg font-semibold text-slate-900">
        Why: flagged addresses
      </h2>
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <caption className="sr-only">
            Flagged addresses near this wallet, strongest contribution first
          </caption>
          <thead className="border-b border-slate-200 text-slate-600">
            <tr>
              <th scope="col" className="py-2 pr-4 font-medium">Flagged address</th>
              <th scope="col" className="py-2 pr-4 font-medium">Label</th>
              <th scope="col" className="py-2 pr-4 text-right font-medium">Hops</th>
              <th scope="col" className="py-2 pr-4 text-right font-medium">Value on path</th>
              <th scope="col" className="py-2 pr-4 text-right font-medium">Share of volume</th>
              <th scope="col" className="py-2 text-right font-medium">Contribution</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {items.map((item) => (
              <tr key={item.address}>
                <td className="py-2 pr-4">
                  <div className="font-medium text-slate-900">{item.name ?? "Unnamed"}</div>
                  <div className="font-mono text-xs text-slate-500" title={item.address}>
                    {shortAddress(item.address)}
                  </div>
                </td>
                <td className="py-2 pr-4">
                  {LABEL_TEXT[item.label]}
                  {item.via.length > 0 && (
                    <span className="ml-1 text-xs text-slate-500">(via exchange/hub)</span>
                  )}
                </td>
                <td className="py-2 pr-4 text-right tabular-nums">{item.hops}</td>
                <td className="py-2 pr-4 text-right tabular-nums">{formatEth(item.bottleneck_eq_wei)}</td>
                <td className="py-2 pr-4 text-right tabular-nums">{formatPercent(item.flow_share)}</td>
                <td className="py-2 text-right font-medium tabular-nums">{(item.contribution * 100).toFixed(1)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
