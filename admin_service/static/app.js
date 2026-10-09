/* File Translator — admin UI (vanilla JS SPA). */
'use strict';

// ----------------------------------------------------------------- helpers
const $ = (sel) => document.querySelector(sel);
const state = { user: '', page: 'dashboard', version: 0, permissions: [] };

function toast(message, kind = 'ok') {
  const el = $('#toast');
  el.textContent = message;
  el.className = `toast ${kind}`;
  el.classList.remove('hidden');
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => el.classList.add('hidden'), 4000);
}

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])
  );
}

function fmtTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? esc(iso) : d.toLocaleString('ru-RU');
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: 'same-origin',
    headers: options.body ? { 'Content-Type': 'application/json' } : {},
    ...options,
  });
  if (response.status === 401) {
    showLogin();
    throw new Error('Сессия истекла');
  }
  const text = await response.text();
  const data = text ? JSON.parse(text) : null;
  if (!response.ok) {
    const detail = data && data.detail ? data.detail : `HTTP ${response.status}`;
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
  return data;
}

function setVersion(version) {
  state.version = typeof version === 'number' ? version : 0;
  $('#sidebar-version').textContent = state.version;
}

// ------------------------------------------------------------------ modal
let modalSubmit = null;
function openModal(title, bodyHtml, onConfirm, confirmLabel = 'Сохранить') {
  $('#modal-title').textContent = title;
  $('#modal-body').innerHTML = bodyHtml;
  $('#modal-confirm').textContent = confirmLabel;
  modalSubmit = onConfirm;
  // Lock the page behind the dialog and start the body at the top: wheeling
  // over a long dialog must scroll the dialog only, and a reopened dialog must
  // never resume where the previous one stopped (feedback round 10.3).
  document.body.style.overflow = 'hidden';
  $('#modal-body').scrollTop = 0;
  $('#modal-backdrop').classList.remove('hidden');
}
function closeModal() {
  $('#modal-backdrop').classList.add('hidden');
  document.body.style.overflow = '';
  modalSubmit = null;
}
$('#modal-close').addEventListener('click', closeModal);
$('#modal-cancel').addEventListener('click', closeModal);
$('#modal-backdrop').addEventListener('click', (e) => {
  if (e.target === $('#modal-backdrop')) closeModal();
});
$('#modal-confirm').addEventListener('click', async () => {
  if (!modalSubmit) return closeModal();
  try {
    // A submit may replace the dialog in place (the role offer does exactly
    // that). It returns `false` to say "the modal you see now is mine, not the
    // one I was opened for" — closing it here would swallow the new dialog.
    const replaced = await modalSubmit();
    if (replaced !== false) closeModal();
  } catch (err) {
    toast(err.message, 'err');
  }
});

function permissionGrid(selected) {
  const chosen = new Set(selected || []);
  // The row carries the effective state only: where it comes from (role,
  // personal deviation, neither) lives in the API response, not in the UI —
  // the dialog reads as a plain checklist (feedback round 10.1).
  const cells = state.permissions
    .map(
      (p) => `<label><input type="checkbox" name="perm" value="${esc(p)}" ${
        chosen.has(p) ? 'checked' : ''
      } /><span>${esc(rightLabel(p))}</span></label>`
    )
    .join('');
  return `<div class="perm-grid">${cells || '<span class="text-on-surface-variant">Пул прав пуст</span>'}</div>`;
}

function collectPermissions() {
  return Array.from(document.querySelectorAll('input[name="perm"]:checked')).map((i) => i.value);
}

// The four states of the collection control, in the cascade order the resolver
// enforces: 3 implies 2 and 1, 2 implies 1. A radio group keeps states the
// spec forbids (e.g. "may add but not view") out of the UI by construction.
const COLLECTION_LEVELS = [
  [0, '—'],
  [1, 'смотреть'],
  [2, 'добавлять'],
  [3, 'править и удалять'],
];

// The one renderer for the four-state control, shared by the rights dialog and
// the matrix popover so both present the same ordered states with the same
// labels. `floor` disables every level below it — the `default` collection is
// readable by everyone, so its control must never offer "—".
function collectionLevels(name, level, { floor = 0 } = {}) {
  const radios = COLLECTION_LEVELS.map(([value, label]) => {
    const denied = value < floor;
    return `<label${denied ? ' class="is-disabled"' : ''}>
      <input type="radio" name="${esc(name)}" value="${value}"${
        level === value ? ' checked' : ''
      }${denied ? ' disabled' : ''} /><span>${esc(label)}</span>
    </label>`;
  }).join('');
  return `<div class="coll-levels">${radios}</div>`;
}

// The «Глоссарии» pane of the rights dialog. Every row opens on the *server's*
// effective level, so the selection shows what this person may do today before
// any change; the row carries no source label (feedback round 10.2) — origin
// stays in the API's `source` field. `default` never offers "—" — its
// readability is not revocable, and a control that could select a forbidden
// value would promise a lockout the resolver would ignore anyway.
function collectionPane(access) {
  if (access.unrestricted) {
    return `<div class="coll-unrestricted">
      Без ограничений — роль <strong>admin</strong> обходит проверки коллекций,
      конкретные уровни для неё не задаются.
    </div>`;
  }
  const rows = (access.collections || [])
    .map((c, index) => {
      const floor = c.collection === 'default' ? 1 : 0;
      return `<div class="coll-row" data-collection="${esc(c.collection)}">
        <span class="coll-name">${esc(c.collection)}</span>
        ${collectionLevels(`coll-level-${index}`, c.level, { floor })}
      </div>`;
    })
    .join('');
  return `<p class="mb-2 text-xs text-on-surface-variant">
      Уровень — что человек может делать с коллекцией; при сохранении меняется
      только его личный грант, ролевые и групповые не трогаются.
    </p>
    <input type="search" class="input w-full coll-search" placeholder="Найти коллекцию…" />
    <div class="coll-list">${
      rows || '<span class="text-sm text-on-surface-variant">Коллекций нет</span>'
    }</div>`;
}

// ----------------------------------------------------------------- pages
const PAGES = {
  dashboard: { title: 'Обзор', render: renderDashboard },
  users: { title: 'Пользователи', render: renderUsers },
  roles: { title: 'Роли', render: renderRoles },
  access: { title: 'Доступ к коллекциям', render: renderAccess },
  settings: { title: 'Настройки', render: renderSettings },
};

async function navigate(page) {
  if (!PAGES[page]) page = 'dashboard';
  state.page = page;
  $('#page-title').textContent = PAGES[page].title;
  document.querySelectorAll('.nav-item').forEach((b) =>
    b.classList.toggle('active', b.dataset.page === page)
  );
  await PAGES[page].render();
}

function statCard(label, value) {
  return `<div class="card p-4">
    <div class="text-xs uppercase tracking-wide text-on-surface-variant">${esc(label)}</div>
    <div class="mt-1 font-[Space_Grotesk] text-2xl font-semibold text-on-surface">${esc(value)}</div>
  </div>`;
}

async function renderDashboard() {
  const data = await api('/api/dashboard');
  setVersion(data.config_version);
  const c = data.counts || {};
  $('#page-body').innerHTML = `
    <div class="grid grid-cols-2 gap-3 md:grid-cols-4">
      ${statCard('Пользователей', c.users ?? 0)}
      ${statCard('Активных', c.active_users ?? 0)}
      ${statCard('Ролей', `${c.roles ?? 0} / ${c.custom_roles ?? 0} custom`)}
      ${statCard('Грантов', c.grants ?? 0)}
    </div>
    <div class="mt-4 grid gap-4 lg:grid-cols-2">
      <div class="card p-4">
        <div class="mb-2 text-sm font-medium text-on-surface">Коллекции глоссария</div>
        <div class="flex flex-wrap gap-1.5">
          ${(data.collections || []).map((c) => `<span class="badge badge-neutral">${esc(c)}</span>`).join('') || '<span class="text-sm text-on-surface-variant">нет данных</span>'}
        </div>
      </div>
      <div class="card p-4">
        <div class="mb-2 text-sm font-medium text-on-surface">Версия конфигурации</div>
        <div class="font-[JetBrains_Mono] text-3xl text-on-surface">${data.config_version}</div>
        <div class="mt-1 text-xs text-on-surface-variant">Меняется при каждом изменении ролей/грантов; main API подхватывает без перезапуска.</div>
      </div>
    </div>
    <h3 class="mt-6 mb-2 text-sm font-medium text-on-surface">Последние события</h3>
    <div class="table-wrap"><table class="data">
      <thead><tr><th>Время</th><th>Кто</th><th>Действие</th><th>Объект</th><th>Детали</th></tr></thead>
      <tbody>${(data.recent_events || [])
        .map(
          (e) => `<tr>
            <td class="text-on-surface-variant">${fmtTime(e.created_at)}</td>
            <td>${esc(e.actor)}</td>
            <td><span class="badge badge-accent">${esc(e.action)}</span></td>
            <td class="text-on-surface-variant">${esc(e.target_type)} ${esc(e.target)}</td>
            <td class="font-[JetBrains_Mono] text-xs text-on-surface-variant">${esc(JSON.stringify(e.details || {}))}</td>
          </tr>`
        )
        .join('') || '<tr><td colspan="5" class="text-on-surface-variant">Событий пока нет</td></tr>'}</tbody>
    </table></div>`;
}

// Right labels come from the service, next to the values they name: a right
// added there without wording cannot reappear here as a raw value.
let RIGHT_LABELS = {};
function rightLabel(value) {
  return RIGHT_LABELS[value] || value;
}

// The dropdown must show the role that is actually stored. If the registry no
// longer carries that role it is added, so the control can never contradict the
// row — and so a failed write is visible as an unchanged value rather than as
// the first option in the list.
function roleOptionsFor(user, optionsHtml) {
  const current = user.role;
  if (!current) return optionsHtml;
  const marker = `value="${esc(current)}"`;
  return optionsHtml.includes(marker)
    ? optionsHtml.replace(marker, `${marker} selected`)
    : `<option value="${esc(current)}" selected>${esc(current)}</option>${optionsHtml}`;
}

// Second confirmation after a save whose result equals some other role
// exactly (rights *and* collection levels). Accepting trades the deviations
// for the role itself: what the person can do does not change — only where
// the rights live. Several roles may fit, so the dialog names them all and
// lets the administrator pick instead of the server choosing silently.
function openRoleOffer(user, res) {
  const names = res.suggestions || [];
  const name = user.username || user.user_id;
  const parts = [];
  if (res.raised) parts.push(`повышенных — ${res.raised}`);
  if (res.lowered) parts.push(`пониженных — ${res.lowered}`);
  const picker =
    names.length > 1
      ? `<select id="offer-role" class="input mt-3 w-full">${names
          .map((r) => `<option value="${esc(r)}">${esc(r)}</option>`)
          .join('')}</select>`
      : '';
  openModal(
    'Права совпадают с ролью',
    `<p class="text-sm">Права <strong>«${esc(name)}»</strong> полностью совпадают с ${
      names.length > 1 ? 'ролями' : 'ролью'
    }: ${names.map((r) => `<strong>«${esc(r)}»</strong>`).join(', ')}.</p>
     <p class="mt-2 text-sm">Назначить роль${names.length > 1 ? ' (выберите)' : ''} и убрать личные
     отклонения (${parts.join(', ') || 'нет'})? Доступ не изменится — изменится только то,
     где эти права хранятся.</p>
     ${picker}`,
    async () => {
      const role = names.length > 1 ? $('#offer-role').value : names[0];
      const saved = await api(`/api/users/${user.user_id}/role`, {
        method: 'POST',
        body: JSON.stringify({ role, clear_deviations: true }),
      });
      toast(`Роль «${saved.role}» назначена, личные отклонения сняты`);
      renderUsers();
    },
    'Назначить роль'
  );
}

async function renderUsers() {
  const [users, roles, labels] = await Promise.all([
    api('/api/users'),
    api('/api/roles'),
    api('/api/roles/permission-labels'),
  ]);
  RIGHT_LABELS = labels;
  const roleOptions = roles
    .map((r) => `<option value="${esc(r.name)}">${esc(r.name)}</option>`)
    .join('');

  $('#page-body').innerHTML = `
    <p class="mb-3 text-xs text-on-surface-variant">
      Роль применяется сразу при выборе и помечает пользователя — следующий вход через AD её не перезапишет.
    </p>
    <div class="table-wrap"><table class="data">
      <thead><tr>
        <th>Пользователь</th><th>Роль</th><th>Источник роли</th><th>Статус</th>
        <th>AD-группы</th><th>Права (фактические)</th><th>Действия</th>
      </tr></thead>
      <tbody>${users
        .map((u) => {
          const own = u.permissions || [];
          const lowered = u.denied || [];
          // The server's formula — (роль ∖ denied) ∪ permissions — is the one
          // the API enforces; the union below would keep counting a right the
          // user was personally denied as if they still had it.
          const effective = u.effective
            ? u.effective
            : Array.from(new Set([...(u.role_permissions || []), ...own])).sort();
          const deviations = own.length + lowered.length;
          return `<tr data-user="${esc(u.user_id)}">
            <td>
              <div class="text-on-surface">${esc(u.username || u.user_id)}</div>
              <div class="text-xs text-on-surface-variant">${esc(u.display_name || '')}</div>
            </td>
            <td><select class="input role-select" data-id="${esc(u.user_id)}">${roleOptionsFor(u, roleOptions)}</select></td>
            <td>${
              u.manual_role
                ? '<span class="badge badge-accent">вручную</span>'
                : '<span class="badge badge-neutral">из AD</span>'
            }</td>
            <td>${
              u.is_active
                ? '<span class="badge badge-success">активен</span>'
                : '<span class="badge badge-danger">отключён</span>'
            }</td>
            <td class="text-xs text-on-surface-variant">
              ${
                (u.ldap_groups || []).length
                  ? `<div class="chip-list">${u.ldap_groups
                      .map((g) => `<span class="badge badge-neutral">${esc(g)}</span>`)
                      .join('')}</div>`
                  : '—'
              }
            </td>
            <td class="text-xs text-on-surface-variant">
              ${
                effective.length
                  ? `<div class="chip-list">${effective
                      .map((p) => `<span class="badge badge-neutral">${esc(rightLabel(p))}</span>`)
                      .join('')}</div>`
                  : '—'
              }
              ${
                own.length
                  ? `<div class="mt-1 text-on-surface-variant">свои: ${esc(own.map(rightLabel).join(', '))}</div>`
                  : ''
              }
              ${
                lowered.length
                  ? `<div class="mt-1 text-on-surface-variant">понижено: ${esc(lowered.map(rightLabel).join(', '))}</div>`
                  : ''
              }
            </td>
            <td class="user-actions-cell">
              <div class="user-actions">
                <button class="btn js-perms" data-id="${esc(u.user_id)}">Права</button>
                <button class="btn js-toggle-active" data-id="${esc(u.user_id)}" data-active="${u.is_active}">${
                  u.is_active ? 'Отключить' : 'Включить'
                }</button>
              </div>
              ${
                u.manual_role || deviations
                  ? `<div class="user-actions-sub">${
                      u.manual_role
                        ? `<button class="btn js-reset" data-id="${esc(u.user_id)}">Сброс к AD</button>`
                        : ''
                    }${
                      deviations
                        ? `<button class="btn js-reset-rights" data-id="${esc(u.user_id)}">Вернуть к роли</button>`
                        : ''
                    }</div>`
                  : ''
              }
            </td>
          </tr>`;
        })
        .join('')}</tbody>
    </table></div>`;

  // Choosing a role is the action. There is no second "Роль" button to find:
  // it looked live but nothing left the browser until it was pressed.
  $('#page-body').querySelectorAll('.role-select').forEach((select) =>
    select.addEventListener('change', async () => {
      const wanted = select.value;
      try {
        const saved = await api(`/api/users/${select.dataset.id}/role`, {
          method: 'POST',
          body: JSON.stringify({ role: wanted }),
        });
        toast(`Роль «${saved.role}» сохранена`);
      } catch (err) {
        toast(`Роль не изменена: ${err.message}`, 'err');
      }
      // Re-render either way: the dropdown must end up showing what is stored,
      // not what was clicked. The endpoint answers from storage, so a write
      // that did not land shows up here as an unchanged role.
      renderUsers();
    })
  );

  $('#page-body').querySelectorAll('.js-perms').forEach((btn) =>
    btn.addEventListener('click', async () => {
      const user = users.find((u) => u.user_id === btn.dataset.id);
      if (!user) return;
      // The collection tab reads from its own endpoint: the matrix would drag
      // every subject in the system into a dialog about one person.
      let access;
      try {
        access = await api(`/api/users/${user.user_id}/access`);
      } catch (err) {
        toast(`Коллекции не загружены: ${err.message}`, 'err');
        return;
      }
      openModal(
        `Права: ${user.username || user.user_id}`,
        `<div class="dlg-tabs">
           <button type="button" class="dlg-tab active" data-pane="perms">Функции</button>
           <button type="button" class="dlg-tab" data-pane="colls">Глоссарии</button>
         </div>
         <div class="dlg-pane" data-pane="perms">
           <p class="mb-3 text-xs text-on-surface-variant">
             Галочки — фактические права человека: роль плюс личные отклонения;
             при сохранении пишется только разница.
           </p>
           ${permissionGrid(user.effective)}
         </div>
         <div class="dlg-pane hidden" data-pane="colls">${collectionPane(access)}</div>`,
        async () => {
          // Levels first and only where the selection differs from what the
          // server reported: the role offer below compares the resulting
          // per-collection levels, so it must see them already saved.
          const changed = [];
          document.querySelectorAll('#modal-body .coll-row').forEach((row) => {
            const checked = row.querySelector('input:checked');
            if (!checked) return;
            const level = Number(checked.value);
            const before = (access.collections || []).find(
              (c) => c.collection === row.dataset.collection
            );
            if (!before || before.level !== level) {
              changed.push({ collection: row.dataset.collection, level });
            }
          });
          for (const change of changed) {
            const saved = await api('/api/access/grant', {
              method: 'PUT',
              body: JSON.stringify({
                subject_type: 'user',
                subject: user.user_id,
                collection: change.collection,
                level: change.level,
              }),
            });
            setVersion(saved.config_version);
          }
          const res = await api(`/api/users/${user.user_id}/permissions`, {
            method: 'POST',
            body: JSON.stringify({ permissions: collectPermissions() }),
          });
          toast(
            res.raised + res.lowered || changed.length
              ? `Сохранено: +${res.raised} своих, −${res.lowered} понижено${
                  changed.length ? `, уровней коллекций: ${changed.length}` : ''
                }`
              : 'Права сохранены — отклонений от роли нет'
          );
          renderUsers();
          if (res.suggestions && res.suggestions.length) {
            openRoleOffer(user, res);
            return false;
          }
          return undefined;
        }
      );
      // One Save covers both tabs, so the sections only switch what is on
      // screen; neither is discarded when the other is edited.
      document.querySelectorAll('#modal-body .dlg-tab').forEach((tab) =>
        tab.addEventListener('click', () => {
          document.querySelectorAll('#modal-body .dlg-tab').forEach((t) =>
            t.classList.toggle('active', t === tab)
          );
          document.querySelectorAll('#modal-body .dlg-pane').forEach((pane) =>
            pane.classList.toggle('hidden', pane.dataset.pane !== tab.dataset.pane)
          );
        })
      );
      const search = document.querySelector('#modal-body .coll-search');
      if (search) {
        search.addEventListener('input', () => {
          const query = search.value.trim().toLowerCase();
          document.querySelectorAll('#modal-body .coll-row').forEach((row) =>
            row.classList.toggle('hidden', !row.dataset.collection.toLowerCase().includes(query))
          );
        });
      }
    })
  );

  $('#page-body').querySelectorAll('.js-reset-rights').forEach((btn) =>
    btn.addEventListener('click', () => {
      const user = users.find((u) => u.user_id === btn.dataset.id);
      if (!user) return;
      const raised = (user.permissions || []).length;
      const lowered = (user.denied || []).length;
      // The button only exists while there is something to undo, and the text
      // names both halves so nobody has to guess what «к роли» will touch.
      openModal(
        'Вернуть права к роли?',
        `<p class="text-sm">Личные отклонения пользователя
           <strong>«${esc(user.username || user.user_id)}»</strong>:
           повышенных прав — ${raised}, пониженных — ${lowered}.
           Они будут сняты, и доступ вернётся к тому, что даёт роль
           <strong>«${esc(user.role)}»</strong>. Личные уровни коллекций
           удалятся тоже; сама роль не меняется.</p>`,
        async () => {
          const res = await api(`/api/users/${user.user_id}/reset-rights`, { method: 'POST' });
          toast(`Права возвращены к роли «${res.user.role}»`);
          renderUsers();
        },
        'Вернуть к роли'
      );
    })
  );

  $('#page-body').querySelectorAll('.js-toggle-active').forEach((btn) =>
    btn.addEventListener('click', async () => {
      try {
        await api(`/api/users/${btn.dataset.id}/active`, {
          method: 'POST',
          body: JSON.stringify({ is_active: btn.dataset.active !== 'true' }),
        });
        renderUsers();
      } catch (err) {
        toast(err.message, 'err');
      }
    })
  );

  $('#page-body').querySelectorAll('.js-reset').forEach((btn) =>
    btn.addEventListener('click', () => {
      const user = users.find((u) => u.user_id === btn.dataset.id);
      if (!user) return;
      // This action undoes the manual assignment next to it, so it says what it
      // discards and what takes its place before anything happens.
      openModal(
        'Сбросить роль к AD?',
        `<p class="text-sm">Роль <strong>«${esc(user.role)}»</strong> будет заменена на
         <strong>«${esc(user.ldap_role || 'user')}»</strong> — её выведет членство
         в AD-группах. После сброса роль снова меняется при каждом входе через AD.</p>`,
        async () => {
          try {
            const saved = await api(`/api/users/${user.user_id}/reset-role`, { method: 'POST' });
            toast(`Роль сброшена к AD: «${saved.role}»`);
          } catch (err) {
            toast(`Роль не сброшена: ${err.message}`, 'err');
          }
          renderUsers();
        },
        'Сбросить к AD'
      );
    })
  );
}

async function renderRoles() {
  const [roles, labels] = await Promise.all([api('/api/roles'), api('/api/roles/permission-labels')]);
  RIGHT_LABELS = labels;
  $('#page-body').innerHTML = `
    <div class="mb-4 flex justify-end">
      <button id="new-role" class="btn btn-primary">+ Создать роль</button>
    </div>
    <div class="table-wrap"><table class="data">
      <thead><tr><th>Роль</th><th>Описание</th><th>Пользователей</th><th>Прав</th><th>Статус</th><th>Действия</th></tr></thead>
      <tbody>${roles
        .map(
          (r) => `<tr>
            <td class="font-medium text-on-surface">${esc(r.name)}</td>
            <td class="text-on-surface-variant">${esc(r.description)}</td>
            <td>${r.member_count}</td>
            <td class="text-xs text-on-surface-variant">${(r.permissions || []).length}</td>
            <td>${
              r.protected
                ? '<span class="badge badge-neutral">встроенная · защищена</span>'
                : '<span class="badge badge-accent">своя</span>'
            }</td>
            <td class="whitespace-nowrap">
              <button class="btn js-edit" data-name="${esc(r.name)}" ${
                r.protected ? 'disabled' : ''
              }>Изменить</button>
              <button class="btn js-members" data-name="${esc(r.name)}">Участники</button>
              <button class="btn btn-danger js-del" data-name="${esc(r.name)}" ${
                r.protected ? 'disabled' : ''
              }>Удалить</button>
            </td>
          </tr>`
        )
        .join('')}</tbody>
    </table></div>`;

  $('#new-role').addEventListener('click', () => {
    openModal(
      'Новая роль',
      `<div class="space-y-3">
        <input id="role-name" class="input w-full" placeholder="Имя роли" />
        <input id="role-desc" class="input w-full" placeholder="Описание" />
        ${permissionGrid([])}
      </div>`,
      async () => {
        const name = $('#role-name').value.trim();
        if (!name) throw new Error('Укажите имя роли');
        await api('/api/roles', {
          method: 'POST',
          body: JSON.stringify({
            name,
            description: $('#role-desc').value.trim(),
            permissions: collectPermissions(),
            grants: [],
          }),
        });
        toast(`Роль «${name}» создана`);
        renderRoles();
      },
      'Создать'
    );
  });

  $('#page-body').querySelectorAll('.js-edit').forEach((btn) =>
    btn.addEventListener('click', async () => {
      const role = roles.find((r) => r.name === btn.dataset.name);
      openModal(
        `Роль: ${role.name}`,
        `<div class="space-y-3">
          <input id="role-desc" class="input w-full" value="${esc(role.description)}" />
          ${permissionGrid(role.permissions)}
        </div>`,
        async () => {
          await api(`/api/roles/${encodeURIComponent(role.name)}`, {
            method: 'PATCH',
            body: JSON.stringify({
              description: $('#role-desc').value.trim(),
              permissions: collectPermissions(),
            }),
          });
          toast('Роль обновлена, версия конфигурации увеличена');
          renderRoles();
        }
      );
    })
  );

  $('#page-body').querySelectorAll('.js-members').forEach((btn) =>
    btn.addEventListener('click', async () => {
      const members = await api(`/api/roles/${encodeURIComponent(btn.dataset.name)}/members`);
      openModal(
        `Участники роли ${btn.dataset.name}`,
        members.length
          ? members
              .map((m) => `<div class="border-b border-outline-variant py-1.5 text-sm">${esc(m.username || m.user_id)}</div>`)
              .join('')
          : '<span class="text-sm text-on-surface-variant">Нет пользователей</span>',
        async () => {},
        'Закрыть'
      );
    })
  );

  $('#page-body').querySelectorAll('.js-del').forEach((btn) =>
    btn.addEventListener('click', async () => {
      try {
        const result = await api(`/api/roles/${encodeURIComponent(btn.dataset.name)}`, { method: 'DELETE' });
        toast(`Роль удалена, грантов удалено: ${result.grants_removed}`);
        renderRoles();
      } catch (err) {
        toast(err.message, 'err');
      }
    })
  );
}

// ------------------------------------------------------------------ matrix
// Collections are the horizontal axis and there are ~26 of them, so the
// subject list is split by kind and each cell shows a compact level badge
// instead of a control; the four-state radio opens only on demand.
const MATRIX_VIEWS = [
  ['role', 'Роли'],
  ['user', 'Пользователи'],
  ['group', 'Группы'],
];
const MATRIX_LEVEL_GLYPHS = { 0: '—', 1: 'R', 2: 'R+', 3: 'RW' };
const MATRIX_LEVEL_TITLES = {
  0: 'нет доступа',
  1: 'смотреть',
  2: 'добавлять',
  3: 'править и удалять',
};

const matrixState = { data: null, view: 'role', filter: '' };

function matrixLevel(subjectType, subject, collection) {
  const row = (matrixState.data.rows || []).find(
    (r) => r.subject_type === subjectType && r.subject === subject
  );
  const cell = row && (row.cells || {})[collection];
  return cell ? Number(cell.level) || 0 : 0;
}

function matrixTableHtml() {
  const { data, view, filter } = matrixState;
  const query = filter.trim().toLowerCase();
  const collections = data.collections.filter((c) => c.toLowerCase().includes(query));
  const header = collections.map((c) => `<th class="rot">${esc(c)}</th>`).join('');
  const rows = (data.rows || [])
    .filter((row) => row.subject_type === view)
    .map((row) => {
      const kind = row.subject_type === 'user' ? 'badge-neutral' : 'badge-accent';
      const label = `<td class="row-label"><span class="badge ${kind}">${esc(
        row.subject_type
      )}</span> ${esc(row.label.replace(/^[a-z]+: /, ''))}</td>`;
      if (row.unrestricted) {
        return `<tr>${label}
          <td class="coll-unrestricted-cell" colspan="${collections.length || 1}">
            Без ограничений — роль admin обходит проверки коллекций.
          </td></tr>`;
      }
      const cells = collections
        .map((collection) => {
          const level = Number((row.cells || {})[collection]?.level) || 0;
          const title = MATRIX_LEVEL_TITLES[level] || MATRIX_LEVEL_TITLES[0];
          return `<td class="cell-level-cell">
            <button type="button" class="cell-level level-${level}"
              data-subject-type="${esc(row.subject_type)}" data-subject="${esc(row.subject)}"
              data-collection="${esc(collection)}" title="${esc(title)}"
              aria-label="${esc(row.label)} · ${esc(collection)}: ${esc(title)}"
            >${esc(MATRIX_LEVEL_GLYPHS[level] || MATRIX_LEVEL_GLYPHS[0])}</button>
          </td>`;
        })
        .join('');
      return `<tr>${label}${cells}</tr>`;
    })
    .join('');
  return `<table class="data matrix">
    <thead><tr><th class="subject-col">Субъект</th>${header}</tr></thead>
    <tbody>${
      rows ||
      `<tr><td class="text-on-surface-variant" colspan="${collections.length + 1}">Нет субъектов</td></tr>`
    }</tbody>
  </table>`;
}

let activePopover = null;
function closeLevelPopover() {
  if (!activePopover) return;
  activePopover.remove();
  activePopover = null;
  document.removeEventListener('click', matrixOutsideClick);
  document.removeEventListener('keydown', matrixEscape);
  window.removeEventListener('scroll', closeLevelPopover, true);
}
function matrixOutsideClick(event) {
  if (activePopover && !activePopover.contains(event.target)) closeLevelPopover();
}
function matrixEscape(event) {
  if (event.key === 'Escape') closeLevelPopover();
}

// The badge defers the full control to a deliberate click: ~26 columns of
// four radios would neither fit nor be scannable. The popover reuses the same
// `collectionLevels` renderer as the rights dialog, so both stay identical.
function openLevelPopover(button) {
  closeLevelPopover();
  const { subjectType, subject, collection } = button.dataset;
  const floor = collection === 'default' ? 1 : 0;
  const pop = document.createElement('div');
  pop.className = 'level-popover';
  pop.innerHTML = `<div class="level-popover-title">${esc(subject)} · ${esc(collection)}</div>
    ${collectionLevels('popover-level', matrixLevel(subjectType, subject, collection), { floor })}`;
  document.body.appendChild(pop);
  const rect = button.getBoundingClientRect();
  const width = pop.offsetWidth;
  pop.style.top = `${rect.bottom + 4}px`;
  pop.style.left = `${Math.min(Math.max(8, rect.left), window.innerWidth - width - 8)}px`;
  pop.querySelectorAll('input[type="radio"]').forEach((input) =>
    input.addEventListener('change', async () => {
      await saveMatrixLevel(subjectType, subject, collection, Number(input.value));
      closeLevelPopover();
    })
  );
  activePopover = pop;
  document.addEventListener('click', matrixOutsideClick);
  document.addEventListener('keydown', matrixEscape);
  window.addEventListener('scroll', closeLevelPopover, true);
}

async function saveMatrixLevel(subjectType, subject, collection, level) {
  try {
    const result = await api('/api/access/grant', {
      method: 'PUT',
      body: JSON.stringify({ subject_type: subjectType, subject, collection, level }),
    });
    setVersion(result.config_version);
    const row = (matrixState.data.rows || []).find(
      (r) => r.subject_type === subjectType && r.subject === subject
    );
    if (row) {
      row.cells = row.cells || {};
      row.cells[collection] = { read: level >= 1, write: level >= 3, level };
    }
    drawMatrixTable();
    toast(`Грант сохранён: ${subject} @ ${collection} — версия ${result.config_version}`);
  } catch (err) {
    toast(err.message, 'err');
  }
}

function drawMatrixTable() {
  closeLevelPopover();
  const mount = $('#matrix-table');
  if (!mount) return;
  mount.innerHTML = matrixTableHtml();
  mount.querySelectorAll('.cell-level').forEach((btn) =>
    btn.addEventListener('click', (event) => {
      event.stopPropagation();
      openLevelPopover(btn);
    })
  );
}

async function renderAccess() {
  matrixState.data = await api('/api/access/matrix');
  const tabs = MATRIX_VIEWS.map(
    ([id, label]) =>
      `<button type="button" class="matrix-tab${
        matrixState.view === id ? ' active' : ''
      }" data-view="${id}">${esc(label)}</button>`
  ).join('');
  $('#page-body').innerHTML = `
    <p class="mb-3 text-xs text-on-surface-variant">
      Уровень сохраняется сразу и увеличивает версию конфигурации — main API применяет изменение без перезапуска.
      Чтение коллекции <span class="text-on-surface">default</span> доступно всем, запись — только по явному гранту.
    </p>
    <div class="matrix-toolbar">
      <div class="matrix-tabs">${tabs}</div>
      <input type="search" class="input matrix-search" placeholder="Найти коллекцию…" value="${esc(
        matrixState.filter
      )}" />
    </div>
    <div class="table-wrap matrix-wrap"><div id="matrix-table"></div></div>`;

  $('#page-body').querySelectorAll('.matrix-tab').forEach((tab) =>
    tab.addEventListener('click', () => {
      matrixState.view = tab.dataset.view;
      $('#page-body')
        .querySelectorAll('.matrix-tab')
        .forEach((t) => t.classList.toggle('active', t === tab));
      drawMatrixTable();
    })
  );
  $('#page-body').querySelector('.matrix-search').addEventListener('input', (event) => {
    matrixState.filter = event.target.value;
    drawMatrixTable();
  });
  drawMatrixTable();
}

async function renderSettings() {
  const data = await api('/api/settings');
  setVersion(data.config_version);
  const counts = data.counts || {};
  $('#page-body').innerHTML = `
    <div class="grid gap-4 lg:grid-cols-2">
      <div class="card p-5">
        <h3 class="text-sm font-medium text-on-surface">Текущая конфигурация</h3>
        <dl class="mt-3 space-y-2 text-sm">
          <div class="flex justify-between"><dt class="text-on-surface-variant">Админ</dt><dd class="text-on-surface">${esc(data.admin_username)}</dd></div>
          <div class="flex justify-between"><dt class="text-on-surface-variant">Версия конфигурации</dt><dd class="font-[JetBrains_Mono] text-on-surface" id="settings-version">${data.config_version}</dd></div>
          <div class="flex justify-between"><dt class="text-on-surface-variant">Источник пароля</dt><dd class="text-on-surface">${data.password_source === 'mongo' ? 'MongoDB (bcrypt)' : 'переменная окружения'}</dd></div>
          <div class="flex justify-between"><dt class="text-on-surface-variant">Пользователей</dt><dd class="text-on-surface">${counts.users ?? 0} (вручную: ${counts.manual_roles ?? 0})</dd></div>
          <div class="flex justify-between"><dt class="text-on-surface-variant">Ролей / грантов</dt><dd class="text-on-surface">${counts.roles ?? 0} / ${counts.grants ?? 0}</dd></div>
        </dl>
      </div>
      <div class="card p-5">
        <h3 class="text-sm font-medium text-on-surface">Смена пароля администратора</h3>
        <p class="mt-1 text-xs text-on-surface-variant">Новый пароль сохраняется как bcrypt-хеш в MongoDB и действует со следующего входа.</p>
        <form id="password-form" class="mt-4 space-y-3">
          <input id="pw-current" type="password" class="input w-full" placeholder="Текущий пароль" required />
          <input id="pw-new" type="password" class="input w-full" placeholder="Новый пароль (мин. 6 символов)" required />
          <input id="pw-repeat" type="password" class="input w-full" placeholder="Повторите новый пароль" required />
          <button class="btn btn-primary" type="submit">Изменить пароль</button>
        </form>
      </div>
    </div>`;

  $('#password-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const current = $('#pw-current').value;
    const next = $('#pw-new').value;
    if (next !== $('#pw-repeat').value) return toast('Новые пароли не совпадают', 'err');
    try {
      await api('/api/settings/password', {
        method: 'POST',
        body: JSON.stringify({ current_password: current, new_password: next }),
      });
      toast('Пароль изменён');
      renderSettings();
    } catch (err) {
      toast(err.message, 'err');
    }
  });
}

// -------------------------------------------------------------- session
function showLogin() {
  $('#app-view').classList.add('hidden');
  $('#app-view').classList.remove('flex');
  $('#login-view').classList.remove('hidden');
  $('#login-view').classList.add('flex');
  $('#login-error').classList.add('hidden');
}

async function showApp(username) {
  state.user = username;
  $('#current-admin').textContent = username;
  $('#login-view').classList.add('hidden');
  $('#login-view').classList.remove('flex');
  $('#app-view').classList.remove('hidden');
  $('#app-view').classList.add('flex');
  if (!state.permissions.length) {
    try {
      state.permissions = await api('/api/roles/permissions-pool');
    } catch {
      state.permissions = [];
    }
  }
  await navigate(location.hash.replace('#', '') || 'dashboard');
}

$('#login-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const username = $('#login-username').value.trim();
  const password = $('#login-password').value;
  const error = $('#login-error');
  error.classList.add('hidden');
  try {
    await api('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    });
    $('#login-password').value = '';
    await showApp(username);
  } catch (err) {
    error.textContent = err.message;
    error.classList.remove('hidden');
  }
});

$('#logout-btn').addEventListener('click', async () => {
  try {
    await api('/api/auth/logout', { method: 'POST' });
  } catch {
    /* session already gone */
  }
  state.user = '';
  showLogin();
});

// `applyTheme` is defined by the inline script in <head> so the stored theme is
// applied before the first paint; the button only has to flip the current one.
$('#theme-toggle').addEventListener('click', () => {
  window.applyTheme(!document.documentElement.classList.contains('dark'));
});

document.querySelectorAll('.nav-item').forEach((btn) =>
  btn.addEventListener('click', () => {
    location.hash = btn.dataset.page;
  })
);

window.addEventListener('hashchange', () => {
  if (state.user) navigate(location.hash.replace('#', '') || 'dashboard');
});

async function bootstrap() {
  try {
    const session = await api('/api/auth/session');
    if (session.authenticated) {
      await showApp(session.username);
      return;
    }
  } catch {
    /* fall through to login */
  }
  showLogin();
}

bootstrap();
