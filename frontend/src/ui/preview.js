/* =================================================================
   PREVIEW  (frontend/src/ui/preview.js)
   Renders sanitized Markdown preview, routes wikilinks and external links.
   ================================================================= */

import DOMPurify from 'dompurify';
import { bridge } from '../bridge.js';

let previewContainer = null;
let linkHandlers = {
  onOpenNoteByTitle: null,
  onCreateNotePrompt: null,
  onExternalLink: null
};

export function initPreview(container, handlers = {}) {
  previewContainer = container;
  linkHandlers = { ...linkHandlers, ...handlers };

  previewContainer.addEventListener('click', (e) => {
    const anchor = e.target.closest('a');
    if (!anchor) return;

    e.preventDefault();
    const href = anchor.getAttribute('href') || '';

    if (href.startsWith('#vn-open/')) {
      const encodedTitle = href.slice('#vn-open/'.length);
      const title = decodeURIComponent(encodedTitle);
      linkHandlers.onOpenNoteByTitle?.(title);
    } else if (href.startsWith('#vn-new/')) {
      const encodedTitle = href.slice('#vn-new/'.length);
      const title = decodeURIComponent(encodedTitle);
      linkHandlers.onCreateNotePrompt?.(title);
    } else if (href.startsWith('http://') || href.startsWith('https://') || href.startsWith('mailto:')) {
      linkHandlers.onExternalLink?.(href);
    }
  });
}

/**
 * Renders Markdown into the preview container safely.
 */
export async function renderPreview(spaceId, body) {
  if (!previewContainer) return;
  if (!body) {
    previewContainer.innerHTML = '';
    return;
  }

  const rawHtml = await bridge.render_preview(spaceId, body);
  const cleanHtml = DOMPurify.sanitize(rawHtml, {
    ADD_TAGS: ['span', 'code', 'pre', 'article', 'blockquote', 'table', 'thead', 'tbody', 'tr', 'th', 'td', 'ul', 'li', 'a', 'strong', 'em', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p'],
    ADD_ATTR: ['class', 'href', 'target', 'rel', 'title', 'data-note', 'data-new', 'data-ext']
  });

  previewContainer.innerHTML = cleanHtml;
}

export function scrollPreviewTo(top) {
  if (previewContainer) {
    previewContainer.scrollTop = top;
  }
}
