# AGENTS.md

This repository contains a single-file Python MCP (Model Context Protocol) server that
connects AI agents to a Ghost blog. It speaks to the Ghost **Admin API** (JWT auth,
read + write) and the Ghost **Content API** (query-key auth, read-only fallback).
All server logic lives in `ghost_blog_mcp.py` — there is no package structure or lint config. A mocked-HTTP test suite lives in `tests/`.

The server exposes 19 tools: 9 read (`check_connection`, `list_posts`, `get_post`,
`get_post_content`, `list_pages`, `get_page`, `get_page_content`, `list_tags`,
`list_authors`) and 10
write (`create_post`, `update_post`, `set_post_status`, `delete_post`, `create_page`,
`update_page`, `delete_page`, `create_tag`, `update_tag`, `delete_tag`).

Auth modes: `GHOST_ADMIN_KEY` set = full mode (reads AND writes, reads via the Admin
API) · only `GHOST_CONTENT_KEY` set = read-only mode (reads via the Content API; write
tools return a "GHOST_ADMIN_KEY required" configuration error) · neither set =
unconfigured (`check_connection` names the missing variables).

Per the [agents.md](https://agents.md/) convention, the README is for human users
(setup, deployment, tool tables); this file carries the agent-focused context —
build/verify steps, conventions, API gotchas, deployment, and the changelog.

## Setup commands

- Install deps: `pip install -r requirements.txt` (needs Python 3.10+; the Docker image uses 3.12)
- Credentials come from environment variables — copy from `.env.example` / README:
  - `GHOST_URL` (e.g. `https://klabsdev.com`; a trailing `/` is stripped in code)
  - `GHOST_ADMIN_KEY` (format `<key id>:<64-char-hex-secret>`, created under Ghost admin → Integrations → Admin API)
  - Optional: `GHOST_CONTENT_KEY` (Content API; read-only mode when no admin key is set)
  - Optional: `MCP_TRANSPORT` (`stdio` default | `streamable-http` | `sse`),
    `PORT` (default `8000`)
  - Optional: `HOST` (HTTP bind address, default `127.0.0.1`) and
    `MCP_AUTH_TOKEN` (bearer token — required when `HOST` is non-loopback;
    the server refuses to start otherwise)
- There is no build step and no lockfile in the repo.

## Running the server

- Local: `GHOST_URL=... GHOST_ADMIN_KEY=... python ghost_blog_mcp.py`
  (defaults to `stdio`, which is how AI clients launch it)
- HTTP transports bind `127.0.0.1` by default. `HOST` opts in to another
  address, and a non-loopback bind then REQUIRES `MCP_AUTH_TOKEN` — the module
  raises `SystemExit` at start when it is missing (fail-closed). With a
  token set, the `_token_auth` ASGI wrapper answers `401` to any request
  without `Authorization: Bearer <token>`; stdio binds `127.0.0.1`.
- Endpoints: Streamable HTTP is served at the **root** path `/` (not FastMCP's default
  `/mcp`) and SSE at `/sse`. `streamable_http_path` and `sse_path` are set in the
  `FastMCP(...)` constructor — do not "fix" them back to defaults.
- `host`/`port` are passed to the `FastMCP(...)` constructor, NOT to `.run()` — that is
  the SDK API contract; don't move them.
- Container: `docker compose up -d` (image `ghcr.io/glitch3dpenguin/ghost-blog-mcp`,
  `MCP_TRANSPORT=streamable-http` is the container default via the Dockerfile).

## Verifying changes

The gate is the mocked-HTTP test suite plus the checks below (run from the
workspace root, with the repo in `ghost-blog-mcp/`):

0. `.venv/bin/python3 -m pytest ghost-blog-mcp/tests/ -q` — all tests green.

1. `python3 -m venv .venv && .venv/bin/pip install -r ghost-blog-mcp/requirements.txt`
   (once — the gate scripts below run on `.venv/bin/python3`).
2. `python3 -m py_compile ghost-blog-mcp/ghost_blog_mcp.py` — must pass.
3. Stub-env import — no real credentials, admin-key env:
   ```sh
   GHOST_URL=https://example.invalid GHOST_ADMIN_KEY=<fake-24-hex>:<fake-64-hex> \
     .venv/bin/python3 -c "import asyncio, sys; sys.path.insert(0, 'ghost-blog-mcp'); import ghost_blog_mcp as m; print([t.name for t in asyncio.run(m.mcp.list_tools())])"
   ```
   Expect EXACTLY the 19 tool names above. Repeat with `GHOST_CONTENT_KEY` set and
   `GHOST_ADMIN_KEY` unset: `list_tools` still returns 18, and the write path's
   configuration check (`_admin_config_problem()`) reports `GHOST_ADMIN_KEY` as
   missing.
4. JWT helper check (fake key only, stdlib script): sign with `_admin_jwt()`,
   base64-decode the header and payload, assert header
   `{alg: HS256, typ: JWT, kid: <fake id>}` and payload
   `{aud: "/admin/", exp - iat == 300, iat ~ now}`, and verify the signature with
   `hmac.new(bytes.fromhex(<fake secret>), "<h>.<p>".encode(), "sha256")`.
5. `grep -ri "a""sh" ghost-blog-mcp/` — must return 0 matches (the token is split
   across string literals on this line so this file does not trip its own gate).
   (A working clone also matches in `.git/` — the local reflog hostname — and
   can match random bytes in binary assets; gate the tracked tree:
   `git grep -ri "a""sh"` should return nothing in text files.)
6. Auth smoke (when touching the entry point or `_token_auth`): start
   with `MCP_TRANSPORT=streamable-http PORT=8991 MCP_AUTH_TOKEN=<fake>
   GHOST_URL=... GHOST_ADMIN_KEY=<fake>` -> an MCP client without the
   token gets `401`; with `Authorization: Bearer <fake>` it lists all 18
   tools. Then `HOST=0.0.0.0` with no token -> the process exits at
   start with the refusal message.

CI runs both on push to `main`: the `Tests` workflow (pytest, the suite above)
and `docker-publish.yml` (image build/publish, which runs no tests). Be strict
about local verification — the image build is not a behavior check.

## Code style and architecture

- **Single file by design.** Keep all tools in `ghost_blog_mcp.py`; don't split into a
  package unless explicitly asked.
- **Every Ghost HTTP call must pass `timeout=30`** (GET, POST, PUT, DELETE). This was
  a deliberate fix — tool calls without timeouts can hang an agent forever. All calls
  go through `_api_get()` / `_api_post()` / `_api_delete()`, which centralize the
  timeout, the missing-env-var check, non-JSON response handling, and the 401/403/404
  error hints — keep new endpoints on those helpers.
- **Context-window safety is the core design goal.** Read tools return compact,
  human/agent-readable formatted strings, never raw JSON dumps. `list_posts` and
  `list_pages` must never include bodies; detail comes from `get_post()` (truncated
  preview, default 1500 chars) and `get_post_content()` (full body on demand). Use
  `_truncate()` for long text.
- **Admin auth is a hand-rolled HS256 JWT** (`_admin_jwt()`): header
  `{"alg":"HS256","typ":"JWT","kid":<key id>}`, payload
  `{"iat":<unix now>,"exp":iat+300,"aud":"/admin/"}`, signed with
  `hmac.new(bytes.fromhex(<secret>), "<h>.<p>", "sha256")` — the digest is requested
  BY NAME so the stdlib digest module is never imported by name in this tree, and no
  `jwt` dependency is added. The signing key is the DECODED hex secret
  (`bytes.fromhex`) — signing with the hex string itself is rejected with
  401 invalid signature.
- **Content API reads** go to `{GHOST_URL}/ghost/api/content/<resource>/?key=<key>`
  with `Accept-Version: v6.0` (the legacy `/ghost/api/v3/content/` path also answers
  on Ghost 6 — the live probe confirms both).
- `Accept-Version` is MINIMUM-version semantics; the live instances run Ghost 6.x, so
  the module sends `v6.0`.
- Read tools pick their API by `_mode()`: admin → Admin API, content-only → Content
  API. The two APIs differ in shape: the Admin API wraps collections in
  `{"posts": [...]}` envelopes; the Content API returns bare objects for single
  resources. `_first_item()` unwraps both.
- Log noise suppression at the top of the module (`mcp.server.streamable_http` →
  INFO, `starlette.requests` → WARNING) is intentional; it keeps HTTP-transport logs
  clean.
- Write tools are admin-only and act **immediately with no undo** — keep the "confirm
  with the user first" instruction in every write tool's docstring.

## Ghost API reference

- Admin base: `{GHOST_URL}/ghost/api/admin/` — `/posts/`, `/pages/`, `/tags/`,
  `/users/`, `/site/`. Auth: `Authorization: Ghost <JWT>` + `Accept-Version: v6.0`.
- Content base: `{GHOST_URL}/ghost/api/content/` — `/posts/`, `/posts/slug/{slug}/`,
  `/pages/`, `/pages/slug/{slug}/`, `/tags/`, `/authors/`, `/settings/`. Auth:
  `?key=<content key>` query param + `Accept-Version: v6.0`.
- Envelope: list responses are `{"<resource>": [...], "meta": {"pagination": {...}}}`;
  POST/PUT bodies wrap the object in an ARRAY: `{"posts": [{...}]}`.
- Supported list params: `filter`, `limit` (default 15 here), `page`, `order`,
  `include`, `fields`.
- Full docs: [Ghost Admin API](https://ghost.io/docs/api/admin/) /
  [Ghost Content API](https://ghost.io/docs/api/content/).

## Ghost API gotchas (learned the hard way — do not regress)

- **The Ghost 6 admin API does NOT accept v3-style `?key=` queries — it 403s by
  design even with a perfectly valid key.** The Admin API is JWT-only
  (`Authorization: Ghost <token>`).
- **The Admin API omits `html` unless you ask** — GET it with
  `formats=html`; `_fetch_item(..., formats="html")` does this for
  post/page bodies, so previews and `format="html"` work in admin mode.
- **Scheduling = `status: "scheduled"` + `published_at`** — there is no
  `publish_at` field on posts; sending one is a silent no-op.
- **PUT posts requires `updated_at`** (optimistic locking) — GET the latest version
  first and send its `updated_at` back verbatim.
- **A PUT with `{"html": ...}` and no `?source=html` returns 200 but is a SILENT
  NO-OP** (`updated_at` does not move). Always send `?source=html` for HTML bodies
  and verify `updated_at` actually moved after the PUT (the tools echo the before →
  after values).
- **HTML content is converted to Lexical lossily** on create/update. For a lossless
  HTML embed, wrap the markup in kg-card comments:
  `<!--kg-card-begin: html--> ... <!--kg-card-end: html-->`.
- **`lexical` is a JSON STRING, not an object** — the API delivers the Lexical
  document serialized as a string; parse it (`_lexical_to_text()` does).
- Admin key format is `<key id>:<64-char-hex-secret>`; the JWT is signed with
  `bytes.fromhex(<secret>)` — signing with the hex string itself = 401.
- `?key=` is rejected on Admin API endpoints (see above) but REQUIRED on Content API
  endpoints; the content key rides in the query string, so error output must never
  echo full request URLs with params.
- Ghost 6's `Accept-Version` header uses minimum-version semantics — send `v6.0`.
- The Content API only ever returns published posts/pages (drafts are admin-only).
- **A FAILED create (e.g. a 422 on malformed lexical) inserts the row and does
  NOT roll back** — orphaned drafts accumulate silently. After a failed create,
  sweep `list_posts` / `list_pages` for the stray row by title and delete it.
- `GET /ghost/api/admin/site/` is PUBLIC on Ghost 6 (200 without auth, and even
  with a broken admin key) — it proves connectivity only, never credentials.
  `GET /ghost/api/admin/users/me/` is the auth-required probe (401
  `UNKNOWN_ADMIN_API_KEY` on a bad key).
- `/images/` is deliberately not exposed (YAGNI for a blog MCP).

## Tool guidance (for agents consuming this server)

Recommended workflow:

```
1. check_connection()                      # Mode + connectivity
2. list_posts()                            # Triage — titles/metadata only
3. get_post(<id-or-slug>)                  # Metadata + short preview
4. get_post_content(<id-or-slug>)          # Full body, only when needed
5. [show draft/changes to the user for approval]
6. create_post(...) / update_post(...)     # Apply the approved change
```

- **Always** confirm with the user before calling a write tool; publishing is
  immediately visible and deletions cannot be undone.
- Before `update_post`/`update_page`/`set_post_status`: call `get_post`/`get_page`
  first for the current `updated_at` AND the full tags/authors list (they are
  REPLACED, not merged).
- Use `list_tags()` for tag names and `list_authors()` for author emails before
  creating/updating posts.

## Deployment & CI/CD

- The only CI is `.github/workflows/docker-publish.yml`: on every push to `main` and
  every published release it builds the Docker image and pushes it to
  `ghcr.io/glitch3dpenguin/ghost-blog-mcp` with tags `:latest` (default branch),
  semver tags (`:v2.1.0`, `:v2.1`, `:v2`) for tagged releases, and `:sha-<commit>`
  for traceability. The `Tests` workflow (pytest) is the behavior check on push;
  the image build is packaging only.
- The container serves Streamable HTTP at `http://<host>:8000/` and SSE at
  `http://<host>:8000/sse`.
- After user-facing changes, update the README (tool tables, endpoint docs) and the
  Changelog below in the same commit.

## Changelog

### Unreleased
- **New:** `get_page_content` — the full body of one page on demand (the Lexical document parsed to readable text, or raw HTML), mirroring `get_post_content`. Pages now have the same see-half as posts, custom HTML blocks included (19 tools: 9 read, 10 write).
- **Fix:** `check_connection` in admin mode proves the key against an auth-required endpoint (`GET /users/me/`) — `GET /site/` is public on Ghost 6, so a broken admin key reported green before. Malformed keys (missing parts, non-hex secret) fail cleanly with no network round trip, and the authenticated admin user is reported.
- **Fix:** `get_post_content` requests `formats=html` in admin mode even for the `lexical` format — the no-lexical fallback (legacy HTML-only content) returned empty text before because the Admin API omits `html` unless asked.
- **Fix:** `docker-compose.yml` sets `HOST: "0.0.0.0"` and requires `MCP_AUTH_TOKEN` — docker's published port forwards to the container's eth0, so the module's loopback default was unreachable from the host.
- **Fix:** scheduling success lines ("Scheduled to publish at" / "Publishes at") key on `status == "scheduled"` — every published post carries `published_at`, so the presence check fired for plain publishes.
- **Fix:** `create_post` only forces `status: scheduled` when `published_at` is set with the default `draft` status; an explicit status wins (backdating works).
- **Fix:** `tags` / `authors` are lists on all create/update tools (create_* took comma-strings while update_* took lists — now one shape).
- **Fix:** `update_tag` / `delete_tag` accept raw ids, UUIDs, and slugs like the post/page write tools (Ghost 6 path params are raw-id-only).
- **Fix:** `list_posts` author filter docstring corrected to author slug (live-verified: `author:<slug>` matches, `author:<email>` matches nothing).
- **Security:** bearer-token compare is constant-time (`hmac.compare_digest`).
- **New:** test suite (mocked HTTP: auth middleware, id resolution, delete gate, scheduling/backdating, tags shape) + `Tests` CI workflow on push/PR.
- **New:** Dockerfile runs as a non-root user + HEALTHCHECK; requirements declare `uvicorn` / `anyio` directly (the unused `[cli]` extra is dropped).
- **Release:** `v0.1.0` — first semver tag; the publish workflow now tags `:v0.1.0` / `:v0.1` / `:0` alongside `:latest`.
- **Fix:** `get_post_content(format="lexical")` — the text extractor only matched `text` inline nodes, but Ghost 6 delivers `extended-text` nodes, so full lexical bodies came back as "(empty)"; code blocks now read the `code` field as well.
- **Fix:** post/page fetches request `formats=html` in admin mode — the Admin API omits `html` unless asked, so `get_post` previews and `get_post_content(format="html")` came back empty.
- **Fix:** identifier matching is exactly 24 hex chars now — shorter all-hex slugs (e.g. `decade`) no longer take the raw-id path.
- **Fix:** scheduling uses Ghost's `published_at` field (there is no `publish_at` field); `set_post_status` accepts `scheduled` and requires `published_at` for it.
- **New:** `update_post` / `update_page` can change `title`, `slug`, and `custom_excerpt`; `tags` / `authors` are lists — omit to keep, empty list to clear.
- **New:** `create_post` / `create_page` validate `status` (draft / published / scheduled for posts).
- **Security:** destructive tools (`delete_*`) refuse to run unless `GHOST_ALLOW_DELETE=1`; all tools carry `readOnlyHint` / `destructiveHint` annotations.
- **New:** `GHOST_USER_AGENT` overrides the Ghost API User-Agent (default: the browser-class string that passes Cloudflare).
- **Fix:** all tools are async and the blocking requests calls run in worker threads — a slow Ghost call no longer stalls the event loop under HTTP transport.
- **Docs:** `list_tags` prints tag ids (needed by `update_tag` / `delete_tag`).
- **New:** Initial release — 18 tools (8 read, 10 write) for the Ghost Admin + Content APIs.
- **New:** Hand-rolled HS256 admin JWT (stdlib only, no `jwt` dependency).
- **New:** Read-only mode with `GHOST_CONTENT_KEY` alone; write tools require `GHOST_ADMIN_KEY`.
- **New:** `get_post_content` — full body on demand (Lexical parsed to readable text, or raw HTML).
- **Docs:** README + AGENTS.md, Dockerfile, docker-compose, GHCR publish workflow.
- **Security:** bearer-token auth for the MCP HTTP surface — `MCP_AUTH_TOKEN`
  is enforced on every request when set; HTTP transports now bind
  `127.0.0.1` by default, and a non-loopback `HOST` requires the token
  (the server refuses to start without it). The compose example
  publishes `127.0.0.1:8000:8000` and reads credentials from `.env`
  (`env_file`).

## Commit and PR guidelines

- Commit messages: imperative, concise, one subject line (repo style:
  "Add tag write tools", "Fix: ..."). A short body is fine for non-obvious changes.
- Don't commit secrets; credentials only via environment variables.
- Pushes to `main` trigger the Docker build/publish — make sure the module at least
  compiles before pushing.
- Release flow: create a semver tag (e.g. `v1.0.0`) and a GitHub release; CI tags the
  image automatically.

## Security

- Never log or echo `GHOST_ADMIN_KEY` / `GHOST_CONTENT_KEY`. The JWT carries the key
  ID (not the secret), but key values must never appear in logs, tool output, or
  error messages — and the Content API key rides in the query string, so error output
  must not echo request URLs with params.
- Write tools have real side effects on a live blog — treat them as
  production-affecting and keep the confirm-before-write contract in docstrings.
- Destructive tools are off by default: `delete_*` refuse without
  `GHOST_ALLOW_DELETE=1`, and they carry `destructiveHint` so MCP
  clients can confirm natively.
- The MCP HTTP surface is loopback-only by default; exposing it
  requires `HOST` + `MCP_AUTH_TOKEN` (fail-closed at start) and a
  reverse proxy with its own auth when it leaves the network.
