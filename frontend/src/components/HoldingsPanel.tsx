import { useQuery } from "@tanstack/react-query";

import { getPortfolio, type Holding } from "../api";
import { formatBalance, formatUsd, shortAddress } from "../lib/format";

const WARNING_TEXT: Record<string, string> = {
  eth_balance_unavailable: "ETH balance is temporarily unavailable.",
  eth_balance_not_configured: "ETH balance isn't configured on the server.",
  token_balances_unavailable: "Token balances are temporarily unavailable.",
  token_balances_not_configured: "Token balances aren't configured on the server.",
  prices_unavailable: "Prices are temporarily unavailable, so USD values are missing.",
};

function Row({ holding }: { holding: Holding }) {
  return (
    <tr>
      <td className="py-2 pr-4">
        <div className="font-medium text-slate-900">{holding.symbol}</div>
        {holding.token_address && (
          <div className="font-mono text-xs text-slate-500" title={holding.token_address}>
            {shortAddress(holding.token_address)}
          </div>
        )}
      </td>
      <td className="py-2 pr-4 text-right tabular-nums whitespace-nowrap">{formatBalance(holding.balance)}</td>
      <td className="py-2 pr-4 text-right tabular-nums whitespace-nowrap">{formatUsd(holding.price_usd)}</td>
      <td className="py-2 text-right font-medium tabular-nums whitespace-nowrap">{formatUsd(holding.value_usd)}</td>
    </tr>
  );
}

export function HoldingsPanel({ address }: { address: string }) {
  const { data, error, isPending } = useQuery({
    queryKey: ["portfolio", address],
    queryFn: () => getPortfolio(address),
    staleTime: 60_000,
    retry: 1,
  });

  return (
    <section aria-labelledby="holdings-heading" className="rounded-xl bg-white p-5 shadow-sm ring-1 ring-slate-200">
      <div className="mb-3 flex items-baseline justify-between gap-4">
        <h2 id="holdings-heading" className="text-lg font-semibold text-slate-900">
          Holdings
        </h2>
        {data && <p className="text-lg font-semibold tabular-nums">{formatUsd(data.total_usd)}</p>}
      </div>
      {isPending && <p role="status" className="text-sm text-slate-600">Loading holdings…</p>}
      {error && (
        <p role="alert" className="text-sm text-red-700">
          Couldn't load holdings: {error.message}
        </p>
      )}
      {data && (
        <>
          {data.warnings.map((w) => (
            <p key={w} role="note" className="mb-2 rounded bg-amber-50 px-3 py-2 text-sm text-amber-900">
              {WARNING_TEXT[w] ?? w}
            </p>
          ))}
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <caption className="sr-only">Wallet holdings, largest USD value first</caption>
              <thead className="border-b border-slate-200 text-slate-600">
                <tr>
                  <th scope="col" className="py-2 pr-4 font-medium">Asset</th>
                  <th scope="col" className="py-2 pr-4 text-right font-medium">Balance</th>
                  <th scope="col" className="py-2 pr-4 text-right font-medium">Price</th>
                  <th scope="col" className="py-2 text-right font-medium">Value</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {data.eth && <Row holding={data.eth} />}
                {data.tokens.map((t) => (
                  <Row key={t.token_address} holding={t} />
                ))}
              </tbody>
            </table>
          </div>
          <p className="mt-3 text-xs text-slate-500">
            {data.priced_token_count > data.tokens.length &&
              `Showing ${data.tokens.length} of ${data.priced_token_count} priced tokens. `}
            {data.unpriced_token_count > 0 &&
              `${data.unpriced_token_count} tokens without a reliable price (mostly airdropped spam) are hidden. `}
            Prices: DefiLlama.
          </p>
        </>
      )}
    </section>
  );
}
