// Client-side format check. EIP-55 checksums need Keccak-256, so mixed-case checksums
// are verified by the server, which returns a clear 422 message if one is wrong.
const ADDRESS_RE = /^0x[0-9a-fA-F]{40}$/;

export type AddressCheck = { ok: true; address: string } | { ok: false; error: string };

export function checkAddress(input: string): AddressCheck {
  const address = input.trim();
  if (!address) return { ok: false, error: "Enter an Ethereum address." };
  if (!address.startsWith("0x")) return { ok: false, error: "Addresses start with 0x." };
  if (address.length !== 42) {
    return { ok: false, error: `Addresses are 42 characters; this one is ${address.length}.` };
  }
  if (!ADDRESS_RE.test(address)) {
    return { ok: false, error: "Only hexadecimal characters (0-9, a-f) are allowed after 0x." };
  }
  return { ok: true, address };
}
