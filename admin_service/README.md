# Admin Service — File Translator

Standalone FastAPI app for runtime administration of **roles**, **users** and
**glossary collection access**. It writes directly to MongoDB and bumps the
Redis config-version key, so the main API picks changes up **without a restart**.

It is intentionally a *separate* service: admin writes never touch the main
API's lifecycle, and the admin surface is not exposed on the main API.

## Screens (vanilla HTML/CSS/JS, same visual language as the main UI)

| Screen | What it does |
|--------|--------------|
| **Login** | Single admin account, httpOnly session cookie |
| **Обзор** | Counters (users/active/roles/grants), glossary collections, config version, recent audit trail |
| **Пользователи** | Assign role (by choosing it in the row — it applies at once, marks `manual_role`), effective rights per user with the personal raises and lowered rights spelled out, a two-tab rights dialog (`Функции` + `Глоссарии`) that saves only the deviation from the role and offers the matching role when rights *and* collection levels equal another role, "return to role" (drops personal rights and personal collection levels), activate/deactivate, reset role to the LDAP-derived one behind a confirmation that names the role it will restore |
| **Роли** | Create / edit / delete custom roles with a permission pool, view members; built-ins are protected |
| **Доступ к коллекциям** | Subject × collection matrix (roles, AD groups, users) with read/write toggles; each toggle saves immediately and bumps the config version |
| **Настройки** | Current config version, password source, change the admin password |

## Admin guide

The administrator's guide lives in the repository as `docs/admin-guide.md` and
is served as a **documentation page** at `/docs`: the section tree on the left,
the article on the right, the current section tracked while scrolling, and
in-page search with match navigation. Every section has its own link, so a
specific part of the guide can be shared.

Nothing is built or cached in the repository — the service renders the Markdown
on request (`GET /api/docs/admin-guide`, behind the admin session) and keeps the
result in memory until the file's timestamp or size changes, so editing the
Markdown is visible on the next page load.

To point the service at a guide elsewhere (a mounted file, a different checkout),
set `ADMIN_GUIDE_PATH`. If the file is missing or unreadable the endpoint answers
**503** and the page says the guide is unavailable; the rest of the admin UI
keeps working.

## Access model

- Roles and grants live in MongoDB (`roles`, `grants`) — see the main change
  spec for the full model.
- Grants are four-state levels: `0` nothing, `1` view, `2` create, `3` edit and
  delete (import included). `read`/`write` flags are the legacy face of `1`/`3`;
  level `2` has no flag pair and is only set through the rights dialog or the
  API's `level` parameter.
- A user's effective level per collection is `max(role, AD-group)`; a personal
  grant with `subject_type=user` replaces that inheritance wholesale (it may
  raise or lower it). `GET /api/users/{id}/access` reports the effective level
  and where it came from (`role:x`, `group:x`, `personal`, `default`, `none`),
  or `unrestricted: true` for the built-in `admin` role.
- The matrix always includes the shared `default` collection: readable by
  everyone (its level is floored at `1`, even by a personal level-`0` grant),
  writable only through an explicit level-`3` grant (seeded to role `admin`).
- The built-in `admin` role bypasses collection checks; the machine-facing
  `api` role is hidden from the UI and cannot be assigned here.
- `manual_role` on a user means "assigned here, not from AD" — the next AD
  login will not overwrite it. «Сброс к AD» clears the flag and re-derives the
  role from the user's AD groups.

## API

All endpoints require the session cookie except `GET /api/health`.

```
POST   /api/auth/login | /api/auth/logout      GET /api/auth/session
GET    /api/health                             GET /api/dashboard | /api/audit
GET    /api/users                              POST /api/users/{id}/role
                                               POST /api/users/{id}/permissions
                                               POST /api/users/{id}/reset-rights
                                               POST /api/users/{id}/active
                                               POST /api/users/{id}/reset-role
GET    /api/users/{id}/access
GET    /api/roles                              POST /api/roles
PATCH  /api/roles/{name}                       DELETE /api/roles/{name}
GET    /api/roles/{name}/members               GET /api/roles/permissions-pool
                                               GET /api/roles/permission-labels
GET    /api/access/matrix | /api/access/collections
PUT    /api/access/grant                       DELETE /api/access/grant
GET    /api/settings                           POST /api/settings/password
GET    /api/docs/admin-guide
```

`POST /api/users/{id}/permissions` takes the desired *effective* rights set,
diffs it against the role at save time and writes only the deviation
(`permissions` for raised, `denied` for lowered); the `RightsSaveResponse`
carries `raised`/`lowered` counts and `suggestions` — role names whose
permissions **and collection levels** now equal the user's, offered so the
deviations can be replaced by the role itself. `POST /api/users/{id}/reset-rights`
drops both personal rights and personal collection levels in one call.
`PUT /api/access/grant` accepts `level` (0–3) alongside `read`/`write`.

`GET /api/health` is public and reports Mongo counts plus the MySQL
`glossary%` table list; it returns `degraded` if either datastore is
unreachable (the matrix then hides the collection list but stays usable).
`GET /api/docs/admin-guide` returns the guide as `{title, html, sections}`, where
`sections` is the outline of its headings — the page builds its tree from that
outline rather than scraping the HTML, so the two cannot disagree.
`GET /api/roles/permission-labels` maps each stored right to its wording, served
from the domain enum, so the UI renders rights as words and keeps no second list
of labels that could drift.

## Configuration

| Env | Default | Notes |
|-----|---------|-------|
| `ADMIN_UI_USERNAME` | `admin` | Admin account name |
| `ADMIN_UI_PASSWORD` | `admin123` | **Set this before deploying** |
| `ADMIN_SESSION_SECRET` | `JWT_SECRET` → random | Cookie signing key; random per restart if unset |
| `ADMIN_SESSION_TTL_HOURS` | `12` | Session lifetime |
| `MONGO_URI` / `MONGO_DB_NAME` | `mongodb://mongo:27017` / `file_translator_auth` | roles, grants, users, `admin_events` |
| `REDIS_HOST` / `REDIS_PORT` / `REDIS_PASSWORD` | `redis` / `6379` / empty | `admin_config_version` bump |
| `GLOSSARY_DB_*` | `dbserver:3306/glossary` | **read-only** account, only lists `glossary%` tables |
| `LDAP_GROUP_ADMIN` | empty | AD group that maps to `admin` on reset |
| `ADMIN_GUIDE_PATH` | `docs/admin-guide.md` | The administrator's guide, rendered on demand at `/docs` |
| `ADMIN_HOST` / `ADMIN_PORT` | `0.0.0.0` / `8011` | Internal listen address |

Admin password storage: the password changed in the UI is stored as a bcrypt
hash in MongoDB (`admin_settings`, `_id: admin_credentials`). `ADMIN_UI_PASSWORD`
is the bootstrap/fallback value; the settings screen shows which source is in
use (`env` or `mongo`).

Password changes require **at least 6 characters**, so the shipped default
(`admin123`) can be typed into the UI, but any *shorter* password can only be
supplied through `ADMIN_UI_PASSWORD` in the environment.

## Deployment

The service listens on `8011` and is published to the host on **loopback
only** — open the UI at `http://127.0.0.1:8011` in a browser on the Docker
host. Binding `127.0.0.1` keeps it off the LAN; nothing else on the network
can reach it.

```yaml
container_name: file-translator-admin
ports:
  - "127.0.0.1:8011:8011"
expose:
  - "8011"
```

Port `8011` was chosen because host port `8010` is already used on some
deployments by the `first_pdf_converter` service.

To reach the UI from another workstation, forward the loopback port over SSH
rather than widening the bind address:

```bash
ssh -L 8011:127.0.0.1:8011 user@docker-host   # then open http://127.0.0.1:8011
```

If the UI must be reachable from the LAN, change the bind address explicitly
(`"8011:8011"`), and put it behind a firewall rule — the service has a single
shared account and no per-user auth.

## Local development

```bash
python tools/admin_demo.py      # http://127.0.0.1:8011, admin/admin123
```

The demo server uses in-memory repositories (real bcrypt) and needs no
MongoDB/Redis/MySQL. It defaults to the same port 8011, so either stop the
container first or set `ADMIN_PORT` to something else when running the demo
alongside `file-translator-admin`.

## Tests

```bash
python -m pytest file_translator/tests/integration/test_admin_service.py -q
```

Covers login/session, health, users, roles, access matrix + grants, settings
and password change, and audit records for every mutation.

## Frontend assets

`static/tailwind.css` and `static/fonts.css` are vendored on purpose — the UI
must render without internet access (the main UI does the same). Regenerate
the utility layer after changing markup classes:

```bash
python tools/gen_admin_tailwind.py
```

The script copies the matching rules from the main UI's Tailwind build and
fills the gaps with the same Tailwind v3 defaults, then verifies that every
class used by `index.html`/`app.js` has a rule.
