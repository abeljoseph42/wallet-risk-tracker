"""Address validation tests, using the test vectors from the EIP-55 spec."""

import pytest

from app.core.addresses import InvalidAddressError, normalize_address, to_checksum_address

# eips.ethereum.org/EIPS/eip-55, "Test Cases".
EIP55_VECTORS = [
    "0x52908400098527886E0F7030069857D2E4169EE7",
    "0x8617E340B3D01FA5F11F306F4090FD50E238070D",
    "0xde709f2102306220921060314715629080e2fb77",
    "0x27b1fdb04752bbc536007a920d24acb045561c26",
    "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed",
    "0xfB6916095ca1df60bB79Ce92cE3Ea74c37c5d359",
    "0xdbF03B407c01E7cD3CBea99509d93f8DDDC8C6FB",
    "0xD1220A0cf47c7B9Be7A2E6BA89F429762e7b9aDb",
]


@pytest.mark.parametrize("vector", EIP55_VECTORS)
def test_checksum_matches_eip55_vectors(vector: str) -> None:
    assert to_checksum_address(vector.lower()) == vector


@pytest.mark.parametrize("vector", EIP55_VECTORS)
def test_valid_checksums_normalize_to_lowercase(vector: str) -> None:
    assert normalize_address(vector) == vector.lower()


def test_unchecksummed_lowercase_and_uppercase_are_accepted() -> None:
    lower = EIP55_VECTORS[4].lower()

    assert normalize_address(lower) == lower
    assert normalize_address("0x" + lower[2:].upper()) == lower


def test_single_wrong_case_letter_fails_the_checksum() -> None:
    vector = EIP55_VECTORS[4]  # 0x5aAeb...
    i = next(i for i, ch in enumerate(vector) if ch.isupper())
    typo = vector[:i] + vector[i].lower() + vector[i + 1 :]

    with pytest.raises(InvalidAddressError, match="checksum"):
        normalize_address(typo)


@pytest.mark.parametrize(
    "bad", ["", "0x123", "5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed", "0x" + "g" * 40]
)
def test_malformed_addresses_are_rejected(bad: str) -> None:
    with pytest.raises(InvalidAddressError, match="Not a valid"):
        normalize_address(bad)


def test_surrounding_whitespace_is_ignored() -> None:
    assert normalize_address(f"  {EIP55_VECTORS[4]}\n") == EIP55_VECTORS[4].lower()
