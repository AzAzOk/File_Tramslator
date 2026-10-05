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
  $('#modal-backdrop').classList.remove('hidden');
}
function closeModal() {
  $('#modal-backdrop').classList.add('hidden');
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
    await modalSubmit();
    closeModal();
  } catch (err) {
    toast(err.message, 'err');
  }
});

function permissionGrid(selected) {
  const chosen = new Set(selected || []);
  const cells = state.permissions
    .map(
      (p) => `<label><input type="checkbox" name="perm" value="${esc(p)}" ${
        chosen.has(p) ? 'checked' : ''
      } /><span>${esc(p)}</span></label>`
    )
    .join('');
  return `<div class="perm-grid">${cells || '<span class="text-on-surface-variant">Пул прав пуст</span>'}</div>`;
}

function collectPermissions() {
  return Array.from(document.querySelectorAll('input[name="perm"]:checked')).map((i) => i.value);
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

async function renderUsers() {
  const [users, roles] = await Promise.all([api('/api/users'), api('/api/roles')]);
  const roleOptions = roles
    .map((r) => `<option value="${esc(r.name)}">${esc(r.name)}</option>`)
    .join('');

  $('#page-body').innerHTML = `
    <p class="mb-3 text-xs text-on-surface-variant">
      Назначение роли вручную помечает пользователя — следующий вход через AD её не перезапишет.
    </p>
    <div class="table-wrap"><table class="data">
      <thead><tr>
        <th>Пользователь</th><th>Роль</th><th>Источник роли</th><th>Статус</th>
        <th>AD-группы</th><th>Права (роль + свои)</th><th>Действия</th>
      </tr></thead>
      <tbody>${users
        .map((u) => {
          const effective = Array.from(new Set([...(u.role_permissions || []), ...(u.permissions || [])])).sort();
          return `<tr data-user="${esc(u.user_id)}">
            <td>
              <div class="text-on-surface">${esc(u.username || u.user_id)}</div>
              <div class="text-xs text-on-surface-variant">${esc(u.display_name || '')}</div>
            </td>
            <td><select class="input role-select">${roleOptions.replace(
              `value="${esc(u.role)}"`,
              `value="${esc(u.role)}" selected`
            )}</select></td>
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
            <td class="text-xs text-on-surface-variant">${(u.ldap_groups || []).map((g) => `<span class="badge badge-neutral mr-1">${esc(g)}</span>`).join('') || '—'}</td>
            <td class="text-xs text-on-surface-variant">
              ${effective.map((p) => `<span class="badge badge-neutral mr-1">${esc(p)}</span>`).join('') || '—'}
              ${(u.permissions || []).length ? '<div class="mt-1 text-on-surface-variant">свои: ' + esc((u.permissions || []).join(', ')) + '</div>' : ''}
            </td>
            <td class="whitespace-nowrap">
              <button class="btn js-assign" data-id="${esc(u.user_id)}">Роль</button>
              <button class="btn js-perms" data-id="${esc(u.user_id)}">Права</button>
              <button class="btn js-toggle-active" data-id="${esc(u.user_id)}" data-active="${u.is_active}">${
                u.is_active ? 'Отключить' : 'Включить'
              }</button>
              ${u.manual_role ? `<button class="btn js-reset" data-id="${esc(u.user_id)}">Сброс к AD</button>` : ''}
            </td>
          </tr>`;
        })
        .join('')}</tbody>
    </table></div>`;

  $('#page-body').querySelectorAll('.js-assign').forEach((btn) =>
    btn.addEventListener('click', async () => {
      const row = btn.closest('tr');
      const select = row.querySelector('.role-select');
      try {
        await api(`/api/users/${btn.dataset.id}/role`, {
          method: 'POST',
          body: JSON.stringify({ role: select.value }),
        });
        toast(`Роль «${select.value}» назначена, флаг manual_role установлен`);
        renderUsers();
      } catch (err) {
        toast(err.message, 'err');
      }
    })
  );

  $('#page-body').querySelectorAll('.js-perms').forEach((btn) =>
    btn.addEventListener('click', async () => {
      const user = users.find((u) => u.user_id === btn.dataset.id);
      openModal(
        `Свои права: ${user.username || user.user_id}`,
        permissionGrid(user.permissions),
        async () => {
          await api(`/api/users/${user.user_id}/permissions`, {
            method: 'POST',
            body: JSON.stringify({ permissions: collectPermissions() }),
          });
          toast('Индивидуальные права обновлены');
          renderUsers();
        }
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
    btn.addEventListener('click', async () => {
      try {
        const user = await api(`/api/users/${btn.dataset.id}/reset-role`, { method: 'POST' });
        toast(`Роль сброшена к AD: «${user.role}», manual_role снят`);
        renderUsers();
      } catch (err) {
        toast(err.message, 'err');
      }
    })
  );
}

async function renderRoles() {
  const roles = await api('/api/roles');
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

async function renderAccess() {
  const data = await api('/api/access/matrix');
  const header = data.collections
    .map((c) => `<th class="rot">${esc(c)}</th>`)
    .join('');

  const rows = data.rows
    .map((row) => {
      const cells = data.collections
        .map((collection) => {
          const flags = (row.cells || {})[collection] || { read: false, write: false };
          const key = `${row.subject_type}|${row.subject}|${collection}`;
          return `<td class="cell-pair" data-key="${esc(key)}">
            <label><input type="checkbox" data-flag="read" ${
              flags.read ? 'checked' : ''
            } /><span>R</span></label>
            <label><input type="checkbox" data-flag="write" ${
              flags.write ? 'checked' : ''
            } /><span>W</span></label>
          </td>`;
        })
        .join('');
      const kind =
        row.subject_type === 'role' ? 'badge-accent' : row.subject_type === 'group' ? 'badge-accent' : 'badge-neutral';
      return `<tr>
        <td class="row-label"><span class="badge ${kind}">${esc(row.subject_type)}</span> ${esc(row.label.replace(/^[a-z]+: /, ''))}</td>
        ${cells}
      </tr>`;
    })
    .join('');

  $('#page-body').innerHTML = `
    <p class="mb-3 text-xs text-on-surface-variant">
      Галочка сразу сохраняется и увеличивает версию конфигурации — main API применяет изменение без перезапуска.
      Чтение коллекции <span class="text-on-surface">default</span> доступно всем, запись — только по явному гранту.
    </p>
    <div class="table-wrap"><table class="data matrix">
      <thead><tr><th>Субъект</th>${header}</tr></thead>
      <tbody>${rows || '<tr><td class="text-on-surface-variant">Нет субъектов</td></tr>'}</tbody>
    </table></div>`;

  $('#page-body').querySelectorAll('.cell-pair input').forEach((input) => {
    input.addEventListener('change', async () => {
      const cell = input.closest('.cell-pair');
      const [subject_type, subject, collection] = cell.dataset.key.split('|');
      const read = cell.querySelector('[data-flag="read"]').checked;
      const write = cell.querySelector('[data-flag="write"]').checked;
      try {
        const result = await api('/api/access/grant', {
          method: 'PUT',
          body: JSON.stringify({ subject_type, subject, collection, read, write }),
        });
        setVersion(result.config_version);
        toast(`Грант сохранён: ${subject} @ ${collection} — версия ${result.config_version}`);
      } catch (err) {
        toast(err.message, 'err');
        renderAccess();
      }
    });
  });
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
