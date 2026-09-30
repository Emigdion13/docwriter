/* =================================================================
   TAG BOX  (frontend/src/ui/tagInput.js)
   The tags of the open note, under its title, like the "To" line of an
   email: type a tag, press Space, Enter or a comma and it becomes a
   bubble.  Click a bubble to edit it, × removes it, and Backspace in the
   empty box brings the last one back as text.
   The engine keeps the tags in the note's front matter (vaultnotes.tags);
   a "#" in the note's text is never a tag.
   ================================================================= */

import { icon } from '../icons.js';

// The same rule as vaultnotes.tags.clean_tag.
const TAG_TEXT_RE = /^[\p{L}\p{M}\p{N}_/-]+$/u;

export function cleanTag(text) {
  const tag = String(text ?? '').trim().replace(/^#/, '').replace(/[/-]+$/, '');
  if (!tag || tag.length > 64 || !TAG_TEXT_RE.test(tag) || !/[\p{L}\p{M}_]/u.test(tag)) return '';
  return tag.split('/').every(Boolean) ? tag : '';
}

const key = tag => String(tag).toLowerCase();

/**
 * Builds the tag box inside `container`.
 *   getSuggestions() → tags already used in this space
 *   onChange(tags)   → the note's new tag list, to be saved
 *   onInvalid(text)  → typed text that is not a tag
 */
export function createTagInput(container, { getSuggestions, onChange, onInvalid }) {
  let saved = [];        // what the note has, as last reported by the engine
  let tags = [];         // what the box shows (differs while one is edited)
  let editIndex = -1;    // where a bubble being edited goes back
  let disabled = false;

  container.classList.add('tag-box');
  container.innerHTML = `
    <span class="tag-bubbles"></span>
    <input class="tag-input" type="text" spellcheck="false" autocomplete="off"
           list="tag-suggestions" placeholder="Add tags…" aria-label="Add a tag">
    <datalist id="tag-suggestions"></datalist>
  `;
  const bubbles = container.querySelector('.tag-bubbles');
  const input = container.querySelector('.tag-input');
  const datalist = container.querySelector('datalist');

  function draw() {
    // Tags are note text: built with textContent, never innerHTML (rule 12a).
    bubbles.replaceChildren(...tags.map((tag, i) => {
      const bubble = document.createElement('span');
      bubble.className = 'tag-bubble';
      bubble.dataset.index = String(i);
      const label = document.createElement('button');
      label.type = 'button';
      label.className = 'tb-label';
      label.title = 'Click to edit';
      label.textContent = `#${tag}`;
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'tb-remove';
      remove.title = `Remove #${tag}`;
      remove.setAttribute('aria-label', `Remove tag ${tag}`);
      remove.innerHTML = icon('x', 10);
      bubble.append(label, remove);
      return bubble;
    }));
    input.placeholder = tags.length ? '' : 'Add tags…';
    container.classList.toggle('is-disabled', disabled);
    input.disabled = disabled;
  }

  function fillSuggestions() {
    const have = new Set(tags.map(key));
    const options = (getSuggestions?.() || []).filter(tag => !have.has(key(tag))).slice(0, 200);
    datalist.replaceChildren(...options.map(tag => {
      const option = document.createElement('option');
      option.value = tag;
      return option;
    }));
  }

  function sameAsSaved(list) {
    return list.length === saved.length && list.every((tag, i) => tag === saved[i]);
  }

  function save() {
    if (!sameAsSaved(tags)) onChange?.([...tags]);
  }

  /** Turns the typed text into bubbles; false when it is not a tag. */
  function commit() {
    const words = input.value.split(/[\s,]+/).filter(Boolean);
    const bad = words.find(word => !cleanTag(word));
    if (bad) {
      input.classList.add('invalid');
      onInvalid?.(bad);
      return false;
    }
    const at = editIndex >= 0 ? Math.min(editIndex, tags.length) : tags.length;
    const next = [...tags];
    const have = new Set(next.map(key));
    let offset = 0;
    words.map(cleanTag).forEach(tag => {
      if (have.has(key(tag))) return;
      have.add(key(tag));
      next.splice(at + offset, 0, tag);
      offset += 1;
    });
    tags = next;
    editIndex = -1;
    input.value = '';
    input.classList.remove('invalid');
    draw();
    fillSuggestions();
    save();
    return true;
  }

  function edit(index) {
    if (disabled || index < 0 || index >= tags.length) return;
    if (input.value.trim() && !commit()) return;
    const [tag] = tags.splice(index, 1);
    editIndex = index;
    draw();
    input.value = tag;
    input.focus();
    input.select();
  }

  input.addEventListener('keydown', (e) => {
    if (e.key === ' ' || e.key === 'Enter' || e.key === ',' || (e.key === 'Tab' && input.value.trim())) {
      if (!input.value.trim()) {
        if (e.key !== 'Tab') e.preventDefault();
        return;
      }
      e.preventDefault();
      commit();
    } else if (e.key === 'Backspace' && !input.value && tags.length) {
      e.preventDefault();
      edit(tags.length - 1);
      // Backspace edits the text, so the cursor goes to the end, not a selection.
      input.setSelectionRange(input.value.length, input.value.length);
    } else if (e.key === 'Escape') {
      // Leave the box as it was: an edited bubble goes back unchanged.
      input.value = '';
      input.classList.remove('invalid');
      tags = [...saved];
      editIndex = -1;
      draw();
      input.blur();
    }
  });

  input.addEventListener('input', (e) => {
    input.classList.remove('invalid');
    // Picking a suggestion from the list finishes the tag straight away.
    if (e.inputType === 'insertReplacementText' || e.inputType === undefined) commit();
  });

  input.addEventListener('focus', fillSuggestions);

  input.addEventListener('blur', () => {
    if (input.value.trim()) {
      if (!commit()) {
        // Not a tag: drop the text, but keep an edited bubble.
        input.value = '';
        input.classList.remove('invalid');
        tags = [...saved];
        editIndex = -1;
        draw();
      }
    } else if (editIndex >= 0) {
      // The edited bubble was emptied: that removes the tag.
      editIndex = -1;
      save();
    }
  });

  bubbles.addEventListener('click', (e) => {
    const bubble = e.target.closest('.tag-bubble');
    if (!bubble || disabled) return;
    const index = Number(bubble.dataset.index);
    if (e.target.closest('.tb-remove')) {
      tags.splice(index, 1);
      draw();
      save();
      return;
    }
    edit(index);
  });

  // A click on the empty part of the box puts the cursor in it.
  container.addEventListener('mousedown', (e) => {
    if (e.target === container || e.target === bubbles) {
      e.preventDefault();
      input.focus();
    }
  });

  draw();

  return {
    /** Shows a note's tags (after opening it, a save or a set_tags answer). */
    setTags(next, opts = {}) {
      saved = [...(next || [])];
      disabled = !!opts.disabled;
      // A bubble being edited stays in the box; everything else follows the note.
      if (opts.reset !== false || editIndex < 0) {
        tags = [...saved];
        if (opts.reset !== false) {
          editIndex = -1;
          input.value = '';
          input.classList.remove('invalid');
        }
      }
      draw();
    },
    focus() {
      input.focus();
    }
  };
}
