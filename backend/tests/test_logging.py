"""API keys travel in request URLs, so request-URL logging must stay off."""

import logging

from app.main import create_app


def test_httpx_request_logs_are_suppressed_so_url_keys_never_reach_logs() -> None:
    create_app()

    assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)
