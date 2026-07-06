/**
 * Client-side photo compression (2026-07-06 — field-test round 2).
 *
 * Phone cameras produce 4–12MB images; uploading those over site
 * connectivity made "Add photo" feel stuck for a long time. Downscaling to a
 * documentation-grade size (max 1920px long edge, JPEG q0.82 ≈ 300–800KB)
 * makes uploads near-instant and shrinks the offline IndexedDB queue too.
 *
 * EVIDENTIARY METADATA (review fix, same day): a bare canvas re-encode
 * strips EXIF — and this platform's backend extracts DateTimeOriginal + GPS
 * on upload for delay documentation/exhibits. So:
 *   - Only ORIGINAL JPEGs are compressed, and the original's entire EXIF
 *     APP1 segment is copied verbatim into the compressed file (GPS + all
 *     IFD offsets are relative to the TIFF header inside the segment, so a
 *     verbatim copy stays self-consistent). The Orientation tag is patched
 *     to 1 because the pixels are re-encoded upright.
 *   - Non-JPEG sources (HEIC/PNG/WebP) are uploaded UNTOUCHED — no size win
 *     is worth losing their metadata, and HEIC is already well-compressed.
 *
 * Safety properties:
 *   - NEVER throws and NEVER loses a photo: any failure at any stage
 *     returns the ORIGINAL file untouched.
 *   - Skips files already smaller than the target output.
 *   - createImageBitmap(..., {imageOrientation:"from-image"}) bakes the EXIF
 *     rotation into the pixels (tag patched to 1 to match). If that options
 *     form is unsupported (older Safari), compression is skipped entirely
 *     rather than risking a double/missing rotation.
 */

const MAX_DIM = 1920;
const JPEG_QUALITY = 0.82;
// Below this size compression can't win enough to matter — skip the work.
const SKIP_UNDER_BYTES = 700 * 1024;

/** The original JPEG's APP1(Exif) segment bytes (marker included), or null. */
async function extractJpegExifSegment(file) {
  try {
    const head = new Uint8Array(await file.slice(0, 256 * 1024).arrayBuffer());
    if (head.length < 4 || head[0] !== 0xFF || head[1] !== 0xD8) return null;
    let o = 2;
    while (o + 4 <= head.length) {
      if (head[o] !== 0xFF) return null;
      const marker = head[o + 1];
      if (marker === 0xDA || marker === 0xD9) return null; // image data starts
      const len = (head[o + 2] << 8) | head[o + 3];
      if (len < 2 || o + 2 + len > head.length) return null;
      if (marker === 0xE1
          && head[o + 4] === 0x45 && head[o + 5] === 0x78   // "Ex"
          && head[o + 6] === 0x69 && head[o + 7] === 0x66   // "if"
          && head[o + 8] === 0x00 && head[o + 9] === 0x00) {
        return head.slice(o, o + 2 + len);
      }
      o += 2 + len;
    }
    return null;
  } catch {
    return null;
  }
}

/** Set the EXIF Orientation tag (0x0112, IFD0) to 1 in-place — the pixels
 *  were re-encoded upright, so the original rotation flag must not apply
 *  twice. Unparseable structures are left untouched (harmless). */
function patchOrientationUpright(app1) {
  try {
    const tiff = 10; // FF E1 LL LL "Exif\0\0" -> TIFF header
    if (app1.length < tiff + 8) return app1;
    const little = app1[tiff] === 0x49 && app1[tiff + 1] === 0x49;      // "II"
    const big = app1[tiff] === 0x4D && app1[tiff + 1] === 0x4D;          // "MM"
    if (!little && !big) return app1;
    const u16 = (off) => (little
      ? app1[off] | (app1[off + 1] << 8)
      : (app1[off] << 8) | app1[off + 1]);
    const u32 = (off) => (little
      ? (app1[off] | (app1[off + 1] << 8) | (app1[off + 2] << 16) | (app1[off + 3] << 24)) >>> 0
      : ((app1[off] << 24) | (app1[off + 1] << 16) | (app1[off + 2] << 8) | app1[off + 3]) >>> 0);
    const ifd0 = tiff + u32(tiff + 4);
    if (ifd0 + 2 > app1.length) return app1;
    const n = u16(ifd0);
    for (let i = 0; i < n; i++) {
      const e = ifd0 + 2 + i * 12;
      if (e + 12 > app1.length) break;
      if (u16(e) === 0x0112) {
        const v = e + 8; // SHORT value lives in the first 2 value bytes
        if (little) { app1[v] = 1; app1[v + 1] = 0; }
        else { app1[v] = 0; app1[v + 1] = 1; }
        break;
      }
    }
    return app1;
  } catch {
    return app1;
  }
}

/** Splice the EXIF APP1 segment into a canvas-produced JPEG (after any APP0). */
async function injectExif(jpegBlob, app1) {
  try {
    const out = new Uint8Array(await jpegBlob.arrayBuffer());
    if (out.length < 4 || out[0] !== 0xFF || out[1] !== 0xD8) return jpegBlob;
    let at = 2;
    if (out[2] === 0xFF && out[3] === 0xE0) { // keep JFIF APP0 first
      const len = (out[4] << 8) | out[5];
      at = 4 + len;
    }
    const merged = new Uint8Array(out.length + app1.length);
    merged.set(out.subarray(0, at), 0);
    merged.set(app1, at);
    merged.set(out.subarray(at), at + app1.length);
    return new Blob([merged], { type: "image/jpeg" });
  } catch {
    return jpegBlob;
  }
}

export async function compressImage(file, {
  maxDim = MAX_DIM,
  quality = JPEG_QUALITY,
} = {}) {
  try {
    if (!file || !(file instanceof Blob)) return file;
    // JPEG-only (see header): other formats keep their bytes + metadata.
    if ((file.type || "") !== "image/jpeg") return file;
    if (file.size < SKIP_UNDER_BYTES) return file;

    // Grab the EXIF FIRST — it must survive the re-encode.
    const exif = await extractJpegExifSegment(file);

    let bmp;
    try {
      bmp = await createImageBitmap(file, { imageOrientation: "from-image" });
    } catch {
      // Options form unsupported (older Safari): skip compression rather
      // than risk wrong rotation. The original uploads untouched.
      return file;
    }
    try {
      const scale = Math.min(1, maxDim / Math.max(bmp.width, bmp.height));
      const w = Math.max(1, Math.round(bmp.width * scale));
      const h = Math.max(1, Math.round(bmp.height * scale));
      const canvas = document.createElement("canvas");
      canvas.width = w;
      canvas.height = h;
      const ctx = canvas.getContext("2d");
      if (!ctx) return file;
      ctx.drawImage(bmp, 0, 0, w, h);
      let blob = await new Promise((resolve) =>
        canvas.toBlob(resolve, "image/jpeg", quality),
      );
      if (!blob) return file;
      if (exif) blob = await injectExif(blob, patchOrientationUpright(exif));
      // Only swap when compression genuinely won.
      if (blob.size >= file.size) return file;
      const base = (file.name || "photo").replace(/\.[a-z0-9]+$/i, "");
      return new File([blob], `${base}.jpg`, {
        type: "image/jpeg",
        lastModified: file.lastModified || Date.now(),
      });
    } finally {
      try { bmp.close(); } catch { /* older browsers */ }
    }
  } catch {
    return file; // any failure: upload the original, never block the user
  }
}

/** Compress a batch in sequence (phones dislike parallel full-res decodes). */
export async function compressImages(files, opts) {
  const out = [];
  for (const f of files) out.push(await compressImage(f, opts));
  return out;
}
