/** Pass a preferred 3D attachment and its InvenTree context to the Windows helper. */
export function openBambuAttachment(data) {
  const context = data?.serverContext;
  const rawUrl = context?.url;
  const filename = context?.filename;
  const partId = context?.partId;
  const attachmentId = context?.attachmentId;

  if (!rawUrl || !filename || !partId || !attachmentId) {
    throw new Error('Missing Bambu Studio attachment data');
  }

  const attachmentUrl = new URL(rawUrl, window.location.href);

  // Reverse proxies can report their internal HTTP scheme to InvenTree.
  if (window.location.protocol === 'https:' && attachmentUrl.host === window.location.host) {
    attachmentUrl.protocol = 'https:';
  }

  const helperUrl = new URL('inventree-bambu-open://open');
  helperUrl.searchParams.set('url', attachmentUrl.href);
  helperUrl.searchParams.set('filename', filename);
  helperUrl.searchParams.set('partId', String(partId));
  helperUrl.searchParams.set('attachmentId', String(attachmentId));
  helperUrl.searchParams.set('instanceUrl', window.location.origin);
  window.location.href = helperUrl.href;
}
