/* =================================================================
   PREVIEW  (frontend/src/ui/preview.js)
   Renders sanitized Markdown preview, routes wikilinks and external links.
   ================================================================= */

import DOMPurify from 'dompurify';
import { bridge } from '../bridge.js';

const OPEN_PREFIX = '#vn-open/';
const NEW_PREFIX = '#vn-new/';
const MISSING_PREFIX = '#vn-missing/';

let previewContainer = null;
let linkHandlers = {
  onOpenNoteByTitle: null,
  onCreateNotePrompt: null,
  onMissingNoteInOtherSpace: null,
  onExternalLink: null
};

/**
 * Text of one `#vn-...` address part, tolerant of a malformed escape.
 */
function decodePart(text) {
  try {
    return decodeURIComponent(text);
  } catch (err) {
    return text;
  }
}

/**
 * Splits the payload of a preview address into what the app needs to act:
 *
 *   `Trip`                  → this space, note "Trip"
 *   `plain/Trip`            → the Plain space, note "Trip" (M10 cross-space)
 *   `Trip#Hotels`           → note "Trip", then scroll to the "Hotels" heading
 *
 * Titles can never contain "/" (the engine sanitizes them) and a title is
 * percent-encoded, so a literal slash here is always the space separator.
 */
export function parseLinkAddress(payload) {
  const text = String(payload ?? "");
  const hash = text.indexOf("#");
  const head = hash === -1 ? text : text.slice(0, hash);
  const heading = hash === -1 ? "" : decodePart(text.slice(hash + 1));
  const slash = head.indexOf("/");
  return {
    space: slash === -1 ? "" : decodePart(head.slice(0, slash)),
    title: decodePart(slash === -1 ? head : head.slice(slash + 1)),
    heading
  };
}

export function initPreview(container, handlers = {}) {
  previewContainer = container;
  linkHandlers = { ...linkHandlers, ...handlers };

  previewContainer.addEventListener('click', (e) => {
    // The title of an embedded note is a link to that note (M10).
    const cardTitle = e.target.closest('.vn-embed-title');
    if (cardTitle) {
      e.preventDefault();
      const note = cardTitle.closest('[data-note]')?.getAttribute('data-note') || cardTitle.textContent;
      linkHandlers.onOpenNoteByTitle?.(String(note || '').trim(), {});
      return;
    }

    const anchor = e.target.closest('a');
    if (!anchor) return;

    e.preventDefault();
    const href = anchor.getAttribute('href') || '';

    if (href.startsWith(OPEN_PREFIX)) {
      const link = parseLinkAddress(href.slice(OPEN_PREFIX.length));
      linkHandlers.onOpenNoteByTitle?.(link.title, { space: link.space, heading: link.heading });
    } else if (href.startsWith(NEW_PREFIX)) {
      const link = parseLinkAddress(href.slice(NEW_PREFIX.length));
      linkHandlers.onCreateNotePrompt?.(link.title);
    } else if (href.startsWith(MISSING_PREFIX)) {
      // A link to a note that is missing in *another* space: the app must not
      // offer to create it here, so it only reports (M10).
      const link = parseLinkAddress(href.slice(MISSING_PREFIX.length));
      linkHandlers.onMissingNoteInOtherSpace?.(link.title, { space: link.space });
    } else if (href.startsWith('http://') || href.startsWith('https://') || href.startsWith('mailto:')) {
      linkHandlers.onExternalLink?.(href);
    }
  });
}

/**
 * Renders Markdown into the preview container safely with DOMPurify.
 */
export async function renderPreview(spaceId, body) {
  if (!previewContainer) return;
  if (!body) {
    previewContainer.innerHTML = '';
    return;
  }

  const rawHtml = await bridge.render_preview(spaceId, body);
  const cleanHtml = DOMPurify.sanitize(rawHtml, {
    ADD_TAGS: [
      'div', 'span', 'code', 'pre', 'article', 'blockquote', 'table', 'thead',
      'tbody', 'tr', 'th', 'td', 'ul', 'li', 'a', 'strong', 'em',
      'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'input', 'hr'
    ],
    ADD_ATTR: [
      'class', 'href', 'target', 'rel', 'title', 'data-note', 'data-new',
      'data-ext', 'type', 'checked', 'disabled'
    ]
  });

  previewContainer.innerHTML = cleanHtml;
}

/**
 * Scrolls the preview to a heading whose text matches (M10: `[[Note#Heading]]`).
 *
 * The engine emits no id attributes on headings, so the match is on the
 * heading's own text; a prefix match is enough and copes with "1. Setup" style
 * numbering.  No match leaves the preview at the top, which is what opening a
 * note without a heading does anyway.
 */
export function scrollPreviewToHeading(headingText) {
  if (!previewContainer || !headingText) return false;
  const wanted = String(headingText).trim().toLowerCase();
  if (!wanted) return false;
  const headings = previewContainer.querySelectorAll('h1, h2, h3, h4, h5, h6');
  for (const node of headings) {
    const text = (node.textContent || '').trim().toLowerCase();
    if (text === wanted || text.startsWith(wanted)) {
      node.scrollIntoView({ block: 'start', behavior: 'smooth' });
      node.classList.add('is-targeted');
      window.setTimeout(() => node.classList.remove('is-targeted'), 1200);
      return true;
    }
  }
  return false;
}

export function scrollPreviewTo(top) {
  if (previewContainer) {
    previewContainer.scrollTop = top;
  }
}
