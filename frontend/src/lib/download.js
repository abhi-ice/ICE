// File name from a Content-Disposition header: the accented UTF-8 form
// (filename*) when present, else the plain one.
export function dispositionFilename(cd) {
  const s = String(cd || "");
  const star = s.match(/filename\*\s*=\s*(?:UTF-8|utf-8)''([^;]+)/i);
  if (star) {
    try { return decodeURIComponent(star[1].trim().replace(/^"|"$/g, "")).replace(/[\\/]/g, ""); } catch { /* fall through */ }
  }
  const m = s.match(/filename\s*=\s*"?([^";]+)"?/i);
  return m ? m[1] : null;
}
