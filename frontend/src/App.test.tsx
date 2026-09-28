import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type { Portfolio, ScoreRun } from "./api";
import App from "./App";

// jsdom has no canvas; the graph is covered by its legend and the breakdown table.
vi.mock("./components/GraphCanvas", () => ({ default: () => <div data-testid="graph" /> }));

const WALLET = "0x" + "1".repeat(40);
const MIXER = "0x910cbd523d972eb0a6f4cae4618ad62622b39dbf";

const doneRun: ScoreRun = {
  id: "run-1",
  address: WALLET,
  status: "done",
  params_hash: "9df73eb74da7",
  created_at: "2026-09-28T00:00:00Z",
  started_at: "2026-09-28T00:00:00Z",
  finished_at: "2026-09-28T00:00:05Z",
  error: null,
  result: {
    score: 70,
    bucket: "high",
    flagged: true,
    breakdown: [
      {
        address: MIXER,
        name: "Tornado.Cash: 10 ETH",
        label: "mixer",
        labels: ["mixer"],
        severity: 0.7,
        hops: 1,
        path: [WALLET, MIXER],
        via: [],
        bottleneck_eq_wei: "60000000000000000000",
        flow_share: 0.48,
        flow_factor: 1,
        hop_weight: 1,
        discount: 1,
        contribution: 0.7,
      },
    ],
    graph: {
      nodes: [
        { address: WALLET, role: "target", labels: [], hop: 0, is_hub: false },
        { address: MIXER, role: "flagged", labels: ["mixer"], hop: 1, is_hub: false },
      ],
      edges: [
        {
          source: WALLET,
          target: MIXER,
          tx_count: 6,
          token_transfer_count: 0,
          total_value_wei: "60000000000000000000",
          value_eq_wei: "60000000000000000000",
          last_seen: 1790000000,
        },
      ],
    },
    stats: { nodes: 25, edges: 29, expanded: 19, api_calls: 0, duration_ms: 1200 },
  },
};

const portfolio: Portfolio = {
  address: WALLET,
  eth: {
    token_address: null,
    symbol: "ETH",
    decimals: 18,
    balance: "2",
    balance_raw: "2000000000000000000",
    price_usd: 2000,
    value_usd: 4000,
    price_confidence: 0.99,
  },
  tokens: [],
  priced_token_count: 0,
  unpriced_token_count: 12,
  total_usd: 4000,
  warnings: [],
  as_of: "2026-09-28T00:00:00Z",
};

type Route = (url: string, init?: RequestInit) => Response | undefined;

function mockApi(...routes: Route[]) {
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    for (const route of routes) {
      const response = route(url, init);
      if (response) return response;
    }
    if (url.endsWith("/health")) return json(200, { status: "ok", database: "ok" });
    if (url.includes("/portfolio")) return json(200, portfolio);
    throw new Error(`unexpected request ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function json(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status });
}

function renderApp() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <App />
    </QueryClientProvider>,
  );
}

beforeEach(() => window.history.replaceState(null, "", "/"));
afterEach(() => vi.unstubAllGlobals());

test("scores a wallet end to end: badge, explanation, breakdown, holdings, URL", async () => {
  mockApi((_url, init) => (init?.method === "POST" ? json(200, doneRun) : undefined));
  const user = userEvent.setup();
  renderApp();

  await user.type(screen.getByLabelText("Ethereum wallet address"), WALLET);
  await user.click(screen.getByRole("button", { name: "Analyze" }));

  expect(await screen.findByText("Exposure detected")).toBeInTheDocument();
  expect(screen.getByText(/strongest link is to Tornado.Cash: 10 ETH/)).toBeInTheDocument();
  const breakdown = screen.getByRole("table", { name: /Flagged addresses/ });
  expect(within(breakdown).getByText("Tornado.Cash: 10 ETH")).toBeInTheDocument();
  expect(within(breakdown).getByText("60 ETH")).toBeInTheDocument();
  const holdings = await screen.findByRole("table", { name: /Wallet holdings/ });
  expect(within(holdings).getByText("ETH")).toBeInTheDocument();
  expect(within(holdings).getByText("$4,000")).toBeInTheDocument();
  expect(screen.getByText(/12 tokens without a reliable price/)).toBeInTheDocument();
  expect(screen.getByRole("list", { name: "Legend" })).toHaveTextContent("Mixer");
  expect(window.location.search).toBe(`?address=${WALLET}`);
});

test("polls a pending job until it finishes", async () => {
  const pending: ScoreRun = { ...doneRun, status: "pending", result: null };
  let polls = 0;
  mockApi(
    (_url, init) => (init?.method === "POST" ? json(202, pending) : undefined),
    (url) => {
      if (!url.endsWith("/scores/run-1")) return undefined;
      polls += 1;
      return json(200, polls < 2 ? { ...pending, status: "running" } : doneRun);
    },
  );
  window.history.replaceState(null, "", `/?address=${WALLET}`);
  renderApp();

  expect(await screen.findByText(/Queued|Tracing/)).toBeInTheDocument();
  expect(await screen.findByText("Exposure detected", {}, { timeout: 5000 })).toBeInTheDocument();
});

test("shows a clean result without a breakdown table", async () => {
  const clean: ScoreRun = {
    ...doneRun,
    result: { ...doneRun.result!, score: 0, bucket: "low", flagged: false, breakdown: [], graph: { nodes: [], edges: [] } },
  };
  mockApi((_url, init) => (init?.method === "POST" ? json(200, clean) : undefined));
  window.history.replaceState(null, "", `/?address=${WALLET}`);
  renderApp();

  expect(await screen.findByText("No exposure found")).toBeInTheDocument();
  expect(screen.getByText(/No sanctioned, malicious or mixer addresses/)).toBeInTheDocument();
  expect(screen.queryByRole("table", { name: /Flagged addresses/ })).not.toBeInTheDocument();
});

test("client-side validation stops malformed input before any request", async () => {
  const fetchMock = mockApi();
  const user = userEvent.setup();
  renderApp();

  await user.type(screen.getByLabelText("Ethereum wallet address"), "0x123");
  await user.click(screen.getByRole("button", { name: "Analyze" }));

  expect(screen.getByRole("alert")).toHaveTextContent("42 characters");
  expect(fetchMock.mock.calls.filter(([url]) => String(url).includes("/scores"))).toHaveLength(0);
});

test("shows the server's checksum error", async () => {
  mockApi((_url, init) =>
    init?.method === "POST"
      ? json(422, { detail: [{ msg: "Value error, Invalid EIP-55 checksum: '0x…'" }] })
      : undefined,
  );
  window.history.replaceState(null, "", `/?address=0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAeD`);
  renderApp();

  expect(await screen.findByText(/Invalid EIP-55 checksum/)).toBeInTheDocument();
});

test("explains a failed job in plain language", async () => {
  const failed: ScoreRun = {
    ...doneRun,
    status: "failed",
    result: null,
    error: { code: "upstream_rate_limited", message: "..." },
  };
  mockApi((_url, init) => (init?.method === "POST" ? json(200, failed) : undefined));
  window.history.replaceState(null, "", `/?address=${WALLET}`);
  renderApp();

  expect(await screen.findByText(/rate-limiting us right now/)).toBeInTheDocument();
});
