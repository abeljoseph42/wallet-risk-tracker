import type { Contribution, ScoreResult } from "../api";
import { explain, formatEth, formatPercent, formatUsd, weiToEth } from "./format";

test("weiToEth handles values beyond Number's safe range", () => {
  expect(weiToEth("92479217800000000000000")).toBeCloseTo(92479.2178, 4);
  expect(weiToEth("1")).toBe(1e-18);
});

test("formatting helpers", () => {
  expect(formatEth("60000000000000000000")).toBe("60 ETH");
  expect(formatEth("1")).toBe("<0.0001 ETH");
  expect(formatUsd(1234567.8)).toBe("$1,234,568");
  expect(formatUsd(3.456)).toBe("$3.46");
  expect(formatUsd(null)).toBe("—");
  expect(formatPercent(0.4828)).toBe("48.3%");
  expect(formatPercent(0.00005)).toBe("<0.1%");
});

const item = (overrides: Partial<Contribution> = {}): Contribution => ({
  address: "0x910cbd523d972eb0a6f4cae4618ad62622b39dbf",
  name: "Tornado.Cash: 10 ETH",
  label: "mixer",
  labels: ["mixer"],
  severity: 0.7,
  hops: 1,
  path: [],
  via: [],
  bottleneck_eq_wei: "60000000000000000000",
  flow_share: 0.4828,
  flow_factor: 1,
  hop_weight: 1,
  discount: 1,
  contribution: 0.7,
  ...overrides,
});

const result = (breakdown: Contribution[]): ScoreResult => ({
  score: 70,
  bucket: "high",
  flagged: breakdown.length > 0,
  breakdown,
  graph: { nodes: [], edges: [] },
  stats: { nodes: 26, edges: 29, expanded: 15, api_calls: 0, duration_ms: 1000 },
});

test("explains the strongest link in plain language", () => {
  expect(explain(result([item(), item({ address: "0x2", name: null })]))).toBe(
    "The strongest link is to Tornado.Cash: 10 ETH (mixer), directly; 48.3% of the wallet's " +
      "volume moved along that path. 1 more flagged address adds to the score.",
  );
});

test("mentions discounted exchange paths and multi-hop distance", () => {
  const text = explain(result([item({ hops: 2, via: ["0xex"] })]));
  expect(text).toContain("2 hops away");
  expect(text).toContain("runs through an exchange");
});

test("explains a clean result and a directly labeled address", () => {
  expect(explain(result([]))).toContain("No sanctioned, malicious or mixer addresses");
  expect(explain(result([item({ hops: 0, label: "sanctioned" })]))).toBe(
    "This address is itself labeled OFAC-sanctioned.",
  );
});


test("ties rank the larger share of volume first", () => {
  const small = item({ address: "0xsmall", name: "1 ETH pool", flow_share: 0.016 });
  const big = item({ address: "0xbig", name: "10 ETH pool", flow_share: 0.476 });
  expect(explain(result([small, big]))).toContain("strongest link is to 10 ETH pool");
});
