import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";

import { HealthStatus } from "./HealthStatus";

function renderWithClient() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <HealthStatus />
    </QueryClientProvider>,
  );
}

function mockFetch(status: number, body: unknown) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(new Response(JSON.stringify(body), { status })),
  );
}

afterEach(() => vi.unstubAllGlobals());

test("shows healthy when backend and database are up", async () => {
  mockFetch(200, { status: "ok", database: "ok" });
  renderWithClient();
  expect(await screen.findByText("healthy")).toBeInTheDocument();
});

test("shows degraded when the database is down", async () => {
  mockFetch(503, { status: "degraded", database: "unavailable" });
  renderWithClient();
  expect(await screen.findByText("degraded")).toBeInTheDocument();
});

test("shows an error when the backend is unreachable", async () => {
  mockFetch(502, {});
  renderWithClient();
  expect(await screen.findByRole("alert")).toHaveTextContent("HTTP 502");
});
