"""Wallet holdings endpoint."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Request, status

from app.api.schemas import ErrorDetail, HoldingOut, PortfolioOut
from app.core.addresses import InvalidAddressError
from app.services.portfolio import Holding, PortfolioService

router = APIRouter(prefix="/api/v1", tags=["portfolio"])


def _holding(h: Holding) -> HoldingOut:
    return HoldingOut(
        token_address=h.token_address,
        symbol=h.symbol,
        decimals=h.decimals,
        balance=format(h.balance.normalize(), "f"),
        balance_raw=str(h.balance_raw),
        price_usd=h.price_usd,
        value_usd=h.value_usd,
        price_confidence=h.price_confidence,
    )


@router.get(
    "/wallets/{address}/portfolio",
    response_model=PortfolioOut,
    summary="Get a wallet's holdings",
    description=(
        "ETH plus the wallet's priced ERC-20 tokens with USD values (cached for a few "
        "minutes). If a data source is down, the response still returns 200 with what is "
        "known and names the missing source in `warnings`."
    ),
    responses={422: {"model": ErrorDetail, "description": "Invalid address."}},
)
async def get_portfolio(
    address: Annotated[str, Path(description="Ethereum address.")], request: Request
) -> PortfolioOut:
    service: PortfolioService | None = request.app.state.portfolio
    if service is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Portfolio is not configured")
    try:
        portfolio = await service.get(address)
    except InvalidAddressError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    return PortfolioOut(
        address=portfolio.address,
        eth=_holding(portfolio.eth) if portfolio.eth else None,
        tokens=[_holding(t) for t in portfolio.tokens],
        priced_token_count=portfolio.priced_token_count,
        unpriced_token_count=portfolio.unpriced_token_count,
        total_usd=portfolio.total_usd,
        warnings=portfolio.warnings,
        as_of=portfolio.as_of,
    )
