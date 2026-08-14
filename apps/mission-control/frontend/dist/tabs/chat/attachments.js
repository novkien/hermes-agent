// Image attachments for the composer.
//
// The BFF caps request bodies at 1 MB and base64 inflates ~4/3, so an image is
// downscaled and re-encoded until its data URL fits the budget. The gateway
// accepts image parts only — files/documents are rejected upstream with
// `unsupported_content_type`, so the picker is image-only by design.

const ATTACHMENT_URL_BUDGET = 700 * 1024;
const ATTACHMENT_MAX_EDGE = 1568;

export async function toImageAttachment(file, { budget = ATTACHMENT_URL_BUDGET } = {}) {
  if (!/^image\//.test(file.type || '')) {
    throw new Error('only images can be attached');
  }
  const bitmap = await loadImage(file);
  const scale = Math.min(1, ATTACHMENT_MAX_EDGE / Math.max(bitmap.width, bitmap.height));
  const canvas = document.createElement('canvas');
  canvas.width = Math.max(1, Math.round(bitmap.width * scale));
  canvas.height = Math.max(1, Math.round(bitmap.height * scale));
  canvas.getContext('2d').drawImage(bitmap, 0, 0, canvas.width, canvas.height);

  // PNG first to keep screenshots crisp; fall back to progressively stronger
  // JPEG compression when the encoded URL is still over budget.
  let url = canvas.toDataURL('image/png');
  for (const quality of [0.9, 0.75, 0.6, 0.45]) {
    if (url.length <= budget) break;
    url = canvas.toDataURL('image/jpeg', quality);
  }
  if (url.length > budget) throw new Error('image too large after compression');
  return { name: file.name || 'image', url };
}

function loadImage(file) {
  if (typeof createImageBitmap === 'function') return createImageBitmap(file);
  return new Promise((resolve, reject) => {
    const img = new Image();
    const url = URL.createObjectURL(file);
    img.onload = () => { URL.revokeObjectURL(url); resolve(img); };
    img.onerror = () => { URL.revokeObjectURL(url); reject(new Error('could not decode image')); };
    img.src = url;
  });
}

/**
 * Pull images out of a paste or drop. Both events expose the same
 * `DataTransfer` shape, so one reader covers "⌘V a screenshot" and "drag a file
 * onto the composer" — the two ways an operator actually attaches an image,
 * neither of which the file-picker-only composer supported.
 */
export function imagesFromTransfer(transfer) {
  if (!transfer) return [];
  const out = [];
  for (const item of transfer.files || []) {
    if (/^image\//.test(item.type || '')) out.push(item);
  }
  if (out.length) return out;
  for (const item of transfer.items || []) {
    if (item.kind === 'file' && /^image\//.test(item.type || '')) {
      const file = item.getAsFile();
      if (file) out.push(file);
    }
  }
  return out;
}
