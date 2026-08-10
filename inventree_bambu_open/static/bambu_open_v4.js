/** Pass an attachment to the local InvenTree Bambu helper. */
export function openBambuAttachment(data) {
  const rawUrl = data?.serverContext?.url;
  const filename = data?.serverContext?.filename;

  if (!rawUrl || !filename) {
    throw new Error('Missing Bambu Studio attachment data');
  }

  const attachmentUrl = new URL(rawUrl, window.location.href);

  // InvenTree is behind an HTTPS reverse proxy which reports its internal HTTP
  // scheme. The public attachment endpoint is always on the current HTTPS host.
  if (window.location.protocol === 'https:' && attachmentUrl.host === window.location.host) {
    attachmentUrl.protocol = 'https:';
  }

  const helperUrl = new URL('inventree-bambu-open://open');
  helperUrl.searchParams.set('url', attachmentUrl.href);
  helperUrl.searchParams.set('filename', filename);
  window.location.href = helperUrl.href;
}
