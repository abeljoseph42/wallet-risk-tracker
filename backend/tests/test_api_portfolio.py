"""Portfolio endpoint tests: the real app with a PortfolioService over fake providers."""

import httpx

from app.clients.prices import ETH_PRICE_ID, TokenPrice, token_price_id
from app.main import create_app
from app.services.portfolio import PortfolioService
from tests.test_portfolio import BALANCES, PRICES, USDC, FakeEth, FakePrices, FakeTokens

WALLET = "0x" + "1" * 40


def _api(service: PortfolioService | None) -> httpx.AsyncClient:
    app = create_app(portfolio=service)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_portfolio_returns_holdings_with_string_amounts() -> None:
    service = PortfolioService(FakeEth(), FakeTokens(BALANCES), FakePrices(PRICES))

    async with _api(service) as api:
        response = await api.get(f"/api/v1/wallets/{WALLET}/portfolio")

    assert response.status_code == 200
    body = response.json()
    assert body["eth"]["balance"] == "2"
    assert body["eth"]["balance_raw"] == str(2 * 10**18)
    assert body["eth"]["value_usd"] == 4000.0
    usdc = body["tokens"][0]
    assert (usdc["token_address"], usdc["symbol"], usdc["balance"]) == (USDC, "USDC", "3000")
    assert body["unpriced_token_count"] == 2
    assert body["total_usd"] == 7050.0
    assert body["warnings"] == []


async def test_provider_outage_still_returns_200_with_a_warning() -> None:
    service = PortfolioService(FakeEth(), FakeTokens(fail=True), FakePrices(PRICES))

    async with _api(service) as api:
        response = await api.get(f"/api/v1/wallets/{WALLET}/portfolio")

    assert response.status_code == 200
    assert response.json()["warnings"] == ["token_balances_unavailable"]
    assert response.json()["total_usd"] == 4000.0


async def test_tiny_balances_render_without_scientific_notation() -> None:
    prices = {ETH_PRICE_ID: TokenPrice(2000.0, "ETH", None, 0.99)}
    service = PortfolioService(FakeEth(wei=1), FakeTokens({}), FakePrices(prices))

    async with _api(service) as api:
        body = (await api.get(f"/api/v1/wallets/{WALLET}/portfolio")).json()

    assert body["eth"]["balance"] == "0.000000000000000001"


async def test_invalid_address_is_422() -> None:
    service = PortfolioService(FakeEth(), FakeTokens(), FakePrices({}))

    async with _api(service) as api:
        response = await api.get("/api/v1/wallets/0x123/portfolio")

    assert response.status_code == 422


async def test_unconfigured_portfolio_is_503() -> None:
    async with _api(None) as api:
        response = await api.get(f"/api/v1/wallets/{WALLET}/portfolio")

    assert response.status_code == 503


async def test_openapi_documents_the_portfolio_endpoint() -> None:
    async with _api(None) as api:
        spec = (await api.get("/openapi.json")).json()

    assert "/api/v1/wallets/{address}/portfolio" in spec["paths"]
    assert "PortfolioOut" in spec["components"]["schemas"]


def test_price_ids_match_defillama_format() -> None:
    assert token_price_id(USDC.upper().replace("0X", "0x")) == f"ethereum:{USDC}"
