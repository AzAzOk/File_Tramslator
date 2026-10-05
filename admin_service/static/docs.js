/* Admin guide page — navigation tree, article, and search.
 *
 * The article and its outline arrive in one response from
 * GET /api/docs/admin-guide (see admin_service/routers/docs.py): the tree is
 * built from the returned outline, never scraped out of the rendered HTML, so
 * the two cannot drift apart.
 */
'use strict';

const GUIDE_ENDPOINT = '/api/docs/admin-guide';
const $ = (sel) => document.querySelector(sel);

const state = {
  sections: [],
  matches: [],
  current: -1,
  activeId: '',
  observer: null,
};

// ------------------------------------------------------------------- status
function setStatus(message, modifier = '') {
  const status = $('#docs-status');
  status.textContent = message;
  status.className = `docs-status${modifier ? ` ${modifier}` : ''}`;
  status.classList.toggle('hidden', !message);
}

function setMatchButtons(enabled) {
  $('#docs-prev').disabled = !enabled;
  $('#docs-next').disabled = !enabled;
}

// ------------------------------------------------------------------ loading
// The page shell is static and loads for anyone, so an anonymous visitor has to
// be told to sign in. Asking the public session endpoint first keeps that case
// off the failing-request path: the guide endpoint is only called by someone who
// actually has a session.
async function hasSession() {
  try {
    const response = await fetch('/api/auth/session', { credentials: 'same-origin' });
    if (!response.ok) return false;
    return Boolean((await response.json()).authenticated);
  } catch {
    return false;
  }
}

async function loadGuide() {
  setStatus('Загрузка инструкции…');
  if (!(await hasSession())) {
    setStatus('Войдите в админку, чтобы читать инструкцию.', 'docs-status-auth');
    return;
  }
  let response;
  try {
    response = await fetch(GUIDE_ENDPOINT, { credentials: 'same-origin' });
  } catch (err) {
    setStatus(`Не удалось загрузить инструкцию: ${err.message}`, 'docs-status-error');
    return;
  }
  if (response.status === 401) {
    // The session can still lapse between the two calls.
    setStatus('Войдите в админку, чтобы читать инструкцию.', 'docs-status-auth');
    return;
  }
  if (!response.ok) {
    setStatus('Инструкция недоступна. Обратитесь к администратору.', 'docs-status-error');
    return;
  }
  const data = await response.json();
  renderGuide(data);
}

function renderGuide({ title, html, sections }) {
  state.sections = sections || [];
  if (title) $('#docs-title').textContent = title;
  // Server-rendered from the repository's own Markdown, the same trust level as
  // reading the file.
  $('#docs-content').innerHTML = html;
  buildTree();
  setStatus('');
  observeHeadings();
  restoreFromHash();
}

// ---------------------------------------------------------------------- tree
function buildTree() {
  const tree = $('#docs-tree');
  tree.innerHTML = '';
  state.sections.forEach((section) => {
    const item = document.createElement('a');
    item.className = `docs-tree-item docs-tree-lv${Math.min(section.level, 4)}`;
    item.href = `#${encodeURIComponent(section.id)}`;
    item.textContent = section.text;
    item.dataset.target = section.id;
    item.addEventListener('click', (event) => {
      event.preventDefault();
      goToSection(section.id);
    });
    tree.appendChild(item);
  });
  $('#docs-tree-count').textContent = state.sections.length ? String(state.sections.length) : '';
}

function setActive(id) {
  if (!id || state.activeId === id) return;
  state.activeId = id;
  document.querySelectorAll('.docs-tree-item.active').forEach((item) => item.classList.remove('active'));
  const item = document.querySelector(`.docs-tree-item[data-target="${CSS.escape(id)}"]`);
  if (item) {
    item.classList.add('active');
    item.scrollIntoView({ block: 'nearest' });
  }
}

function goToSection(id) {
  const target = document.getElementById(id);
  if (!target) return;
  target.scrollIntoView({ behavior: 'smooth', block: 'start' });
  setActive(id);
  // replaceState, not pushState: the hash is a shareable address, not a step the
  // back button has to walk through 27 headings to undo.
  history.replaceState(null, '', `#${encodeURIComponent(id)}`);
}

function restoreFromHash() {
  const id = decodeURIComponent(location.hash.replace(/^#/, ''));
  if (!id) return;
  const target = document.getElementById(id);
  if (!target) return;
  target.scrollIntoView();
  setActive(id);
}

// ------------------------------------------------------- active-section track
function observeHeadings() {
  const article = $('#docs-article');
  const headings = [...document.querySelectorAll('#docs-content h1, #docs-content h2, #docs-content h3, #docs-content h4, #docs-content h5, #docs-content h6')];
  if (state.observer) state.observer.disconnect();
  if (!headings.length) return;

  // Only the top third of the viewport decides, so the section being read is
  // the one under the eye rather than the one that merely touches the bottom.
  const visible = new Set();
  state.observer = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) visible.add(entry.target);
        else visible.delete(entry.target);
      });
      // The final sections can never reach the top band; at the bottom of the
      // article the last heading is what the reader is on.
      const atEnd = article.scrollTop + article.clientHeight >= article.scrollHeight - 2;
      const current = atEnd ? headings[headings.length - 1] : headings.find((h) => visible.has(h));
      if (current) setActive(current.id);
    },
    { root: article, rootMargin: '0px 0px -70% 0px', threshold: 0 }
  );
  headings.forEach((heading) => state.observer.observe(heading));
}

// -------------------------------------------------------------------- search
function clearDocHits() {
  const content = $('#docs-content');
  content.querySelectorAll('mark.docs-hit').forEach((mark) => {
    mark.replaceWith(document.createTextNode(mark.textContent));
  });
  content.normalize();
  state.matches = [];
  state.current = -1;
}

function gotoMatch(index) {
  const matches = state.matches;
  if (!matches.length) return;
  state.current = (index + matches.length) % matches.length;
  const active = matches[state.current];
  matches.forEach((mark) => mark.classList.toggle('docs-hit-active', mark === active));
  active.scrollIntoView({ behavior: 'smooth', block: 'center' });
  $('#docs-match-count').textContent = `${state.current + 1}/${matches.length}`;
}

function runSearch(query) {
  clearDocHits();
  const content = $('#docs-content');
  const counter = $('#docs-match-count');
  const needle = query.trim();
  if (!needle) {
    counter.textContent = '';
    setMatchButtons(false);
    return;
  }
  const pattern = new RegExp(needle.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi');
  const walker = document.createTreeWalker(content, NodeFilter.SHOW_TEXT, {
    acceptNode: (node) => {
      if (!node.nodeValue.trim()) return NodeFilter.FILTER_REJECT;
      if (node.parentElement.closest('mark')) return NodeFilter.FILTER_REJECT;
      return NodeFilter.FILTER_ACCEPT;
    },
  });
  const nodes = [];
  for (let node = walker.nextNode(); node; node = walker.nextNode()) nodes.push(node);
  nodes.forEach((node) => {
    const text = node.nodeValue;
    pattern.lastIndex = 0;
    if (!pattern.test(text)) return;
    pattern.lastIndex = 0;
    const fragment = document.createDocumentFragment();
    let last = 0;
    let match;
    while ((match = pattern.exec(text)) !== null) {
      if (match.index > last) {
        fragment.appendChild(document.createTextNode(text.slice(last, match.index)));
      }
      const mark = document.createElement('mark');
      mark.className = 'docs-hit';
      mark.textContent = match[0];
      fragment.appendChild(mark);
      state.matches.push(mark);
      last = match.index + match[0].length;
    }
    fragment.appendChild(document.createTextNode(text.slice(last)));
    node.parentNode.replaceChild(fragment, node);
  });
  if (!state.matches.length) {
    counter.textContent = 'Не найдено';
    setMatchButtons(false);
    return;
  }
  setMatchButtons(true);
  gotoMatch(0);
}

// ---------------------------------------------------------------------- init
function copySectionLink(id) {
  const url = `${location.origin}${location.pathname}#${encodeURIComponent(id)}`;
  if (!navigator.clipboard) return;
  navigator.clipboard.writeText(url).catch(() => {
    /* clipboard denied - the address bar still carries the link */
  });
}

function initDocs() {
  $('#docs-theme').addEventListener('click', () => {
    window.applyTheme(!document.documentElement.classList.contains('dark'));
  });
  $('#docs-next').addEventListener('click', () => gotoMatch(state.current + 1));
  $('#docs-prev').addEventListener('click', () => gotoMatch(state.current - 1));

  const search = $('#docs-search');
  let timer = null;
  search.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(() => runSearch(search.value), 200);
  });

  // The renderer emits a permalink per heading; it also copies the address.
  $('#docs-content').addEventListener('click', (event) => {
    const link = event.target.closest('a.headerlink');
    if (!link) return;
    event.preventDefault();
    const id = decodeURIComponent(new URL(link.href, location.origin).hash.replace(/^#/, ''));
    goToSection(id);
    copySectionLink(id);
  });

  window.addEventListener('hashchange', restoreFromHash);
  loadGuide();
}

initDocs();