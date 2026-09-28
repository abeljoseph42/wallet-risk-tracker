import { checkAddress } from "./address";

test("accepts lowercase, uppercase-hex and checksummed-looking addresses", () => {
  for (const a of [
    "0x" + "a".repeat(40),
    "0x" + "A".repeat(40),
    "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed",
  ]) {
    expect(checkAddress(a)).toEqual({ ok: true, address: a });
  }
});

test("trims surrounding whitespace", () => {
  expect(checkAddress(`  0x${"b".repeat(40)} `)).toEqual({ ok: true, address: "0x" + "b".repeat(40) });
});

test.each([
  ["", "Enter an Ethereum address."],
  ["abc", "Addresses start with 0x."],
  ["0x123", "Addresses are 42 characters; this one is 5."],
  ["0x" + "g".repeat(40), "Only hexadecimal characters"],
])("rejects %j", (input, message) => {
  const check = checkAddress(input);
  expect(check.ok).toBe(false);
  if (!check.ok) expect(check.error).toContain(message);
});
