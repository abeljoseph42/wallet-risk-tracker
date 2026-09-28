import type { Contribution, JobErrorCode, ScoreResult } from "../api";

export function shortAddress(address: string): string {
  return `${address.slice(0, 6)}…${address.slice(-4)}`;
}

export function formatUsd(value: number | null): string {
  if (value === null) return "—";
  const digits = Math.abs(value) >= 1000 ? 0 : 2;
  return value.toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: digits,
    minimumFractionDigits: digits,
  });
}

// Wei strings can exceed Number's safe range, so convert with BigInt first.
export function weiToEth(wei: string): number {
  const big = BigInt(wei);
  const whole = big / 10n ** 18n;
  const frac = big % 10n ** 18n;
  return Number(whole) + Number(frac) / 1e18;
}

export function formatEth(wei: string): string {
  const eth = weiToEth(wei);
  if (eth === 0) return "0 ETH";
  if (eth < 0.0001) return "<0.0001 ETH";
  return `${eth.toLocaleString("en-US", { maximumFractionDigits: 4 })} ETH`;
}

export function formatBalance(balance: string): string {
  const n = Number(balance);
  if (n !== 0 && n < 0.0001) return "<0.0001";
  return n.toLocaleString("en-US", { maximumFractionDigits: 4 });
}

export function formatPercent(fraction: number): string {
  if (fraction > 0 && fraction < 0.001) return "<0.1%";
  return `${(fraction * 100).toFixed(1)}%`;
}

export const LABEL_TEXT: Record<string, string> = {
  sanctioned: "OFAC-sanctioned",
  malicious: "known malicious",
  mixer: "mixer",
  exchange: "exchange",
};

function describe(item: Contribution): string {
  const who = item.name ?? shortAddress(item.address);
  return `${who} (${LABEL_TEXT[item.label]})`;
}

function distance(hops: number): string {
  if (hops === 0) return "itself";
  if (hops === 1) return "directly";
  return `${hops} hops away`;
}

// One or two plain-English sentences summarizing the score for a non-expert.
export function explain(result: ScoreResult): string {
  const [top, ...rest] = result.breakdown;
  if (!top) {
    return (
      `No sanctioned, malicious or mixer addresses were found within 3 hops of this wallet ` +
      `(${result.stats.nodes} addresses checked).`
    );
  }
  if (top.hops === 0) {
    return `This address is itself labeled ${LABEL_TEXT[top.label]}.`;
  }
  let text =
    `The strongest link is to ${describe(top)}, ${distance(top.hops)}; ` +
    `${formatPercent(top.flow_share)} of the wallet's volume moved along that path.`;
  if (top.via.length > 0) {
    text += " The path runs through an exchange or high-traffic address, so it counts for less.";
  }
  if (rest.length > 0) {
    const more = rest.length === 1 ? "address adds" : "addresses add";
    text += ` ${rest.length} more flagged ${more} to the score.`;
  }
  return text;
}

export const JOB_ERROR_TEXT: Record<JobErrorCode, string> = {
  upstream_rate_limited: "Etherscan is rate-limiting us right now. Try again in a minute.",
  upstream_unavailable: "Etherscan is unreachable right now. Try again shortly.",
  upstream_error: "Etherscan rejected the request.",
  not_configured: "The server has no Etherscan API key configured.",
  timeout: "This wallet took too long to analyze (very large history).",
  interrupted: "The server restarted while scoring. Please try again.",
  internal: "Something went wrong on our side.",
};
