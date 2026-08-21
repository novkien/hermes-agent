// File attachments for the composer.
//
// Hermes owns provider-aware attachment routing. The browser only prepares a
// bounded transport object: images are compressed, while PDFs and every other
// file type are carried as data URLs without trying to interpret their bytes.

export const ATTACHMENT_MAX_COUNT = 8;
export const ATTACHMENT_TOTAL_BYTES = 700 * 1024;

const ATTACHMENT_URL_BUDGET = 700 * 1024;
const ATTACHMENT_MAX_EDGE = 1568;

function isImageFile(file) {
  return /^image\//.test(file.type || '');
}

function isPdfFile(file) {
  return (file.type || '').toLowerCase() === 'application/pdf'
    || String(file.name || '').toLowerCase().endsWith('.pdf');
}

function dataUrlMimeType(url) {
  const match = /^data:([^;,]+)(?:;[^;,=]+=[^;,]+)*;base64,/i.exec(String(url || ''));
  return match ? match[1].toLowerCase() : '';
}

function dataUrlByteLength(url) {
  const encoded = String(url || '').slice(String(url || '').indexOf(',') + 1).replace(/\s+/g, '');
  if (!encoded) return 0;
  const padding = encoded.endsWith('==') ? 2 : encoded.endsWith('=') ? 1 : 0;
  return Math.max(0, Math.floor((encoded.length * 3) / 4) - padding);
}

function readFileDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ''));
    reader.onerror = () => reject(new Error('could not read file'));
    reader.readAsDataURL(file);
  });
}

export async function toImageAttachment(file, { budget = ATTACHMENT_URL_BUDGET } = {}) {
  const attachment = await toChatAttachment(file, { budget });
  if (attachment.kind !== 'image') {
    throw new Error('not an image attachment');
  }
  return attachment;
}

export async function toChatAttachment(file, { budget = ATTACHMENT_URL_BUDGET } = {}) {
  if (!file || typeof file !== 'object') throw new Error('no file selected');
  if (!Number.isFinite(file.size) || file.size <= 0) throw new Error('file is empty');

  let data;
  let previewUrl = '';
  if (isImageFile(file)) {
    const bitmap = await loadImage(file);
    const scale = Math.min(1, ATTACHMENT_MAX_EDGE / Math.max(bitmap.width, bitmap.height));
    const canvas = document.createElement('canvas');
    canvas.width = Math.max(1, Math.round(bitmap.width * scale));
    canvas.height = Math.max(1, Math.round(bitmap.height * scale));
    canvas.getContext('2d').drawImage(bitmap, 0, 0, canvas.width, canvas.height);

    data = canvas.toDataURL('image/png');
    for (const quality of [0.9, 0.75, 0.6, 0.45]) {
      if (data.length <= budget) break;
      data = canvas.toDataURL('image/jpeg', quality);
    }
    if (data.length > budget) throw new Error('image too large after compression');
    previewUrl = data;
  } else {
    data = await readFileDataUrl(file);
    if (!data || data.length > budget) {
      throw new Error('file is too large for the 1 MB chat request limit');
    }
  }

  const mimeType = dataUrlMimeType(data)
    || (isPdfFile(file) ? 'application/pdf' : (file.type || 'application/octet-stream'));
  const size = dataUrlByteLength(data);
  if (!size) throw new Error('file is empty');

  return {
    kind: isImageFile(file) ? 'image' : (isPdfFile(file) ? 'pdf' : 'file'),
    name: file.name || 'file',
    mime: mimeType,
    mime_type: mimeType,
    size,
    data,
    url: previewUrl,
  };
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

/** Pull files out of either a paste or a drop DataTransfer. */
export function filesFromTransfer(transfer) {
  if (!transfer) return [];
  const out = [...(transfer.files || [])];
  if (out.length) return out;
  for (const item of transfer.items || []) {
    if (item.kind === 'file') {
      const file = item.getAsFile();
      if (file) out.push(file);
    }
  }
  return out;
}
