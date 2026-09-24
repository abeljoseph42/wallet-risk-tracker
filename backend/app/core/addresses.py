"""Ethereum address validation and canonicalization."""

import re

_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


class InvalidAddressError(ValueError):
    pass


def normalize_address(address: str) -> str:
    """Return the lowercase canonical form of a 0x-prefixed 20-byte hex address.

    EIP-55 checksum verification for mixed-case input is not done here yet; it needs a
    Keccak-256 implementation, which is a new dependency (see Phase 1 open questions).
    """
    candidate = address.strip()
    if not _ADDRESS_RE.fullmatch(candidate):
        raise InvalidAddressError(f"Not a valid Ethereum address: {address!r}")
    return candidate.lower()
