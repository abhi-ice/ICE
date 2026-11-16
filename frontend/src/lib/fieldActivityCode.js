// Activities added from the field (not in the master schedule) carry a
// field-work code. New ones are FW-0001, FW-0002, ... per project
// (2026-11-16); older ones kept their AF-prefixed code, so both count.
export function isFieldCreatedCode(code) {
  const c = code || "";
  return c.startsWith("FW-") || c.startsWith("AF");
}
