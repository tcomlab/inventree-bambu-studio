/** Pass a preferred 3D attachment and its InvenTree context to the Windows helper. */
export function openBambuAttachment(data) {
  const context = data?.serverContext;
  const rawUrl = context?.url;
  const filename = context?.filename;
  const partId = context?.partId;
  const attachmentId = context?.attachmentId;
  const configuredInstanceUrl = context?.instanceUrl;

  if (!rawUrl || !filename || !partId || !attachmentId) {
    throw new Error('Missing Bambu Studio attachment data');
  }

  const instanceUrl = new URL(configuredInstanceUrl || window.location.origin);
  const attachmentUrl = new URL(rawUrl, instanceUrl);

  // Reverse proxies can report their internal HTTP scheme to InvenTree.
  if (instanceUrl.protocol === 'https:' && attachmentUrl.host === instanceUrl.host) {
    attachmentUrl.protocol = 'https:';
  }

  const helperUrl = new URL('inventree-bambu-open://open');
  helperUrl.searchParams.set('url', attachmentUrl.href);
  helperUrl.searchParams.set('filename', filename);
  helperUrl.searchParams.set('partId', String(partId));
  helperUrl.searchParams.set('attachmentId', String(attachmentId));
  helperUrl.searchParams.set('instanceUrl', instanceUrl.origin);
  window.location.href = helperUrl.href;
}
