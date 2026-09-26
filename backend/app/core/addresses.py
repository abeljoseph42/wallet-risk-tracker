"""Ethereum address validation (format + EIP-55 checksum) and canonicalization."""

import re

from Crypto.Hash import keccak

_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


class InvalidAddressError(ValueError):
    pass


def to_checksum_address(address: str) -> str:
    """EIP-55: uppercase each hex letter whose matching nibble of keccak256(lowercase hex) >= 8."""
    hex_part = address.lower().removeprefix("0x")
    digest = keccak.new(digest_bits=256, data=hex_part.encode("ascii")).hexdigest()
    return "0x" + "".join(
        ch.upper() if ch.isalpha() and int(digest[i], 16) >= 8 else ch
        for i, ch in enumerate(hex_part)
    )


def normalize_address(address: str) -> str:
    """Return the lowercase canonical form of a 0x-prefixed 20-byte hex address.

    All-lowercase and all-uppercase input carries no checksum and is accepted as-is.
    Mixed-case input is an EIP-55 checksum and must match exactly, which catches typos.
    """
    candidate = address.strip()
    if not _ADDRESS_RE.fullmatch(candidate):
        raise InvalidAddressError(f"Not a valid Ethereum address: {address!r}")
    hex_part = candidate[2:]
    is_mixed_case = hex_part != hex_part.lower() and hex_part != hex_part.upper()
    if is_mixed_case and to_checksum_address(candidate) != candidate:
        raise InvalidAddressError(f"Invalid EIP-55 checksum: {address!r}")
    return candidate.lower()
