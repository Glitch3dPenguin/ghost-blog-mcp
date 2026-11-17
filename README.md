<p align="center">
  <img src="assets/ghost-blog-mcp.png" width="280" alt="Ghost Blog MCP logo" />
</p>

<h1 align="center">Ghost Blog MCP Server</h1>

<p align="center">
  <img src="https://img.shields.io/badge/MCP-stdio%20%C2%B7%20streamable--http%20%C2%B7%20sse-8B5CF6" alt="MCP transports" />
  <img src="https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white" alt="Python 3.10+" />
  <img src="https://img.shields.io/badge/Ghost-Admin%20%2B%20Content%20API-FF5A00" alt="Ghost Admin + Content API" />
  <img src="https://img.shields.io/badge/tools-8%20read%20%C2%B7%2010%20write-FF5A00" alt="18 tools" />
</p>

<p align="center">
  A <a href="https://modelcontextprotocol.io/">Model Context Protocol (MCP)</a> server that connects AI agents to a
  <a href="https://ghost.org/">Ghost</a> blog through the Admin and Content APIs. Designed to be
  <strong>context-window safe</strong> — post and page lists return titles and metadata only, so agents can browse the
  whole site without blowing out their context. Full bodies are fetched on demand, one post at a time.
</p>

<p align="center">
  <strong>Community project</strong> — not affiliated with or endorsed by Ghost.
</p>

---

## Features

- **Context-safe listing** — compact one-line tables (date, status, title, tags, author) for posts, pages, tags, and authors. No bodies in list output, so agents can triage the whole site without context overflow.
- **On-demand detail fetching** — metadata plus a truncated body preview via `get_post()`, or the full body via `get_post_content()` (the Lexical document parsed to readable text, or raw HTML).
- **Full mode with an admin key** — create, update, publish/schedule, and delete posts, pages, and tags through the Ghost Admin API.
- **Read-only mode with a content key** — every read tool works with just a Content API key; write tools then report that `GHOST_ADMIN_KEY` is required. No admin key needed to let an agent read your blog.
- **Stdlib-only admin auth** — the Admin API's HS256 JWT is built with `hmac`/`base64`/`json`; no extra `jwt` dependency.
- **Draft → confirm pattern** — every write tool's docstring asks the agent to confirm with the user first; posts and pages are live the moment they publish.
- **Connection check** — verify credentials and connectivity, and report which auth mode (admin / content-only / unconfigured) the server is running in.
- **Three transports** — stdio (default), Streamable HTTP, and SSE, selected with `MCP_TRANSPORT`; a Docker image is published to GHCR on every push to `main`.

---

## Tools

### Read Tools

| Tool | Description |
|---|---|
| `check_connection` | Verify credentials and connectivity, with the current auth mode (admin / content-only / unconfigured) and the site title. Run first when debugging a new setup. |
| `list_posts` | Compact one-line-per-post table: title, status, tags, author, date. Filters: status, tag, author; plus paging and ordering. Drafts are visible in admin mode only. |
| `get_post` | Metadata + a truncated body preview (default 1500 chars, adjustable) for one post by id or slug. |
| `get_post_content` | The FULL body of one post, on demand — `lexical` (the Lexical document parsed to readable plain text, default) or `html` (raw). |
| `list_pages` | Compact table of static pages: title, status, tags, date. |
| `get_page` | Metadata + a truncated body preview for one static page by id or slug. |
| `list_tags` | All tags with post/page counts where the API provides them (counts are admin-mode only). |
| `list_authors` | The site's authors — admin `/users/` in full mode, content `/authors/` in read-only mode. |

### Write Tools (admin key required)

| Tool | Description |
|---|---|
| `create_post` | Create a post: title + body as HTML (sent with `?source=html`) or a Lexical JSON string, status (default draft), tag/author name lists, optional `published_at` for scheduling. |
| `update_post` | Update a post — title, slug, excerpt, or body. `updated_at` is required (fetch latest first); tags/authors are REPLACED, not merged; `save_revision` supported. |
| `set_post_status` | Publish, unpublish, or schedule a post (`draft` / `published` / `scheduled`, optional `published_at`). |
| `delete_post` | Permanently delete a post. |
| `create_page` | Create a static page (same shape as `create_post`, no scheduling). |
| `update_page` | Update a static page (same shape as `update_post`). |
| `delete_page` | Permanently delete a page. |
| `create_tag` | Create a tag (name required; slug/description optional). |
| `update_tag` | Update a tag's name, slug, and/or description. |
| `delete_tag` | Permanently delete a tag. |

All write tools require `GHOST_ADMIN_KEY` — in read-only mode they return a clear configuration error naming it.
Each tool's docstring asks the agent to confirm with the user first; published posts and pages are live immediately.

---

## Deployment

### Option 1 — Container (Recommended)

Running as a container means every client always uses the latest version.
The image is built automatically on every push to `main` via GitHub Actions
and published to GitHub Container Registry.

#### docker-compose / Portainer

```yaml
services:
  ghost-blog-mcp:
    image: ghcr.io/glitch3dpenguin/ghost-blog-mcp:latest
    container_name: ghost-blog-mcp
    restart: unless-stopped
    # Loopback-only by default: the endpoint fronts a Ghost admin key. To
    # expose it beyond localhost, set HOST + MCP_AUTH_TOKEN in .env
    # (non-loopback binds are refused without a token) and put a reverse
    # proxy with its own auth in front.
    ports:
      - "127.0.0.1:8000:8000"
    env_file:
      - .env
    environment:
      # Transport: streamable-http recommended for container deployments.
      # Options: streamable-http | sse | stdio
      MCP_TRANSPORT: "streamable-http"
      PORT: "8000"
      # The container listens on all interfaces (docker forwards the
      # published port to the container's eth0); the non-loopback bind
      # requires MCP_AUTH_TOKEN, set in .env
      HOST: "0.0.0.0"
      MCP_AUTH_TOKEN: "${MCP_AUTH_TOKEN:?set MCP_AUTH_TOKEN in .env}"
```

```sh
cp .env.example .env   # then fill in GHOST_URL / GHOST_ADMIN_KEY / MCP_AUTH_TOKEN
docker compose up -d
```

The MCP endpoint will be available at the **root** path (the server is configured to serve at `/`, not FastMCP's default `/mcp`):
```
http://<host>:8000/
```

With `MCP_TRANSPORT=sse` the endpoint is `http://<host>:8000/sse`.

#### Portainer via Stack

1. In Portainer, go to **Stacks → Add stack**
2. Paste the `docker-compose.yml` contents above
3. Create the `.env` file on the host (from `.env.example`) and fill in `GHOST_URL`, `GHOST_ADMIN_KEY`, and `MCP_AUTH_TOKEN`
4. Deploy

#### Connecting your AI client to the container

| Field | Value |
|---|---|
| **Transport** | Streamable HTTP |
| **URL** | `http://<your-server-ip>:8000/` |

In `claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "ghost-blog": {
      "type": "http",
      "url": "http://<your-server-ip>:8000/"
    }
  }
}
```

When `MCP_AUTH_TOKEN` is set, clients must send the bearer header:
```json
{
  "mcpServers": {
    "ghost-blog": {
      "type": "http",
      "url": "http://<your-server-ip>:8000/",
      "headers": {
        "Authorization": "Bearer <MCP_AUTH_TOKEN>"
      }
    }
  }
}
```

> **Tip:** If you're running behind a reverse proxy (Nginx, Caddy, Traefik), you can expose this on a subdomain with TLS instead of a raw port.

---

### Option 2 — Local (stdio)

Run the server as a local process on the same machine as your AI client.
No container required, but each machine needs its own install.

#### Install

```sh
git clone https://github.com/Glitch3dPenguin/ghost-blog-mcp.git
cd ghost-blog-mcp
pip install -r requirements.txt
```

#### AI client configuration

In `claude_desktop_config.json` (or equivalent):
```json
{
  "mcpServers": {
    "ghost-blog": {
      "command": "python3",
      "args": ["/absolute/path/to/ghost_blog_mcp.py"],
      "env": {
        "GHOST_URL": "https://your-ghost-site.example",
        "GHOST_ADMIN_KEY": "<key id>:<64-char-hex-secret>",
        "GHOST_CONTENT_KEY": "optional_content_key"
      }
    }
  }
}
```

> **⚠️ The path in `args` must be absolute.** AI clients cannot resolve relative paths.
>
> - Windows: `["C:\\Users\\Name\\ghost-blog-mcp\\ghost_blog_mcp.py"]`
> - Mac/Linux: `["/home/username/ghost-blog-mcp/ghost_blog_mcp.py"]`

---

## Security

The MCP endpoint fronts a **Ghost admin key** — full read/write
access to a live blog. Treat the endpoint like an admin panel:

- **Loopback by default.** The HTTP transports bind `127.0.0.1`, and the
  container example publishes `127.0.0.1:8000:8000`. Nothing is
  reachable off-machine out of the box.
- **Token-gated exposure.** Set `HOST` to bind another address (e.g.
  `0.0.0.0` for LAN or reverse-proxy use). A non-loopback bind
  **requires** `MCP_AUTH_TOKEN` — without a token the server refuses to start
  (fail-closed). With a token set, every HTTP request must carry
  `Authorization: Bearer <MCP_AUTH_TOKEN>` or it gets a `401`.
- **Exposing it publicly?** The bearer token protects the MCP surface,
  but if the endpoint leaves your network, put a reverse proxy with TLS
  **and** its own authentication in front, and rotate the token like a
  password.
- **Credentials in `.env`.** The compose example reads `env_file: .env`
  (gitignored). Never commit real keys to the compose file or the repo.
- **Destructive tools off by default.** `delete_post` / `delete_page` / `delete_tag` refuse to run unless `GHOST_ALLOW_DELETE=1` is set. Every tool also advertises `readOnlyHint` / `destructiveHint` annotations so MCP clients can gate, confirm, or prompt natively.
- **No key leakage.** Keys are never logged or echoed, and error output
  never includes request URLs with query params (the Content API key
  rides in the query string).

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `GHOST_URL` | ✅ | Your Ghost site URL, e.g. `https://your-ghost-site.example` (a trailing `/` is fine — it is stripped in code). |
| `GHOST_ADMIN_KEY` | ✅ for writes | Admin API key, format `<key id>:<64-char-hex-secret>`, from Ghost admin → Integrations → Admin API. Enables full read/write mode. Never logged. |
| `GHOST_ALLOW_DELETE` | — | Set to `1` to enable the destructive tools (`delete_post`, `delete_page`, `delete_tag`). They are disabled by default and refuse to run until it is set. |
| `GHOST_CONTENT_KEY` | — | Content API key, from Ghost admin → Integrations → Content API. With no admin key set, the server runs in **read-only mode**. Never logged. |
| `MCP_TRANSPORT` | — | `streamable-http`, `sse`, or `stdio` (default: `stdio`) |
| `PORT` | — | Port to bind on for HTTP transports (default: `8000`) |
| `GHOST_USER_AGENT` | — | Override the User-Agent sent to Ghost (default: a browser-class string that passes Cloudflare). |
| `HOST` | — | Address to bind for HTTP transports (default: `127.0.0.1`).
  A non-loopback value **requires** `MCP_AUTH_TOKEN`; the server refuses to
  start otherwise. |
| `MCP_AUTH_TOKEN` | required for the compose deployment (and any non-loopback `HOST`) | Bearer token
  enforced on the MCP HTTP surface (`Authorization: Bearer ...`). A
  loopback bind works without one, but then the endpoint is
  unauthenticated — keep it loopback-only. Never logged. |

**Auth modes:** admin key set → full mode (reads and writes via the Admin API) · only a content key →
read-only mode (reads via the Content API) · neither → `check_connection` reports a configuration problem
naming the missing variables.

See [Ghost's API documentation](https://ghost.io/docs/api/) for credential setup — Admin and Content API keys
are created under **Integrations** in the Ghost admin panel.

---

## Other Ghost MCP options

This is a **community project**. Ghost does not ship an official MCP server; every Ghost MCP integration is
third-party, and this is not the only one:

| Option | Type | What it is |
|---|---|---|
| **This server** | Self-hosted | Context-safe MCP server for the Ghost Admin + Content APIs — stdio, Streamable HTTP, and SSE, with a Docker image on GHCR. |
| [MFYDev/ghost-mcp](https://github.com/MFYDev/ghost-mcp) | Self-hosted | Popular TypeScript server for the Ghost Admin API — post management with an OAuth-style key flow. |
| [hithereiamaliff/mcp-ghostcms](https://github.com/hithereiamaliff/mcp-ghostcms) | Self-hosted | TypeScript server for managing a Ghost instance from LLM interfaces. |
| [siva-sub/ghost-cms-mcp-server](https://github.com/siva-sub/ghost-cms-mcp-server) | Self-hosted | Node/TypeScript server with full CRUD, bulk actions, and a test suite. |
| [mtane0412/ghost-mcp-server](https://github.com/mtane0412/ghost-mcp-server) | Self-hosted | Ghost Admin API server covering posts, pages, members, and more. |
| [thenets/ghost-mcp](https://github.com/thenets/ghost-mcp) | Self-hosted | Python (FastMCP) server with read-only Content API and read/write Admin API access. |

If you just need a quick Admin-API integration, one of the above may save you the ops work. This server is for
teams that want a context-safe read model, an optional read-only mode with a content key, and their own
credentials on their own infrastructure.

---

## Branding & trademarks

Ghost is a trademark of its respective owners. This project is **not affiliated with, endorsed by, or sponsored
by** Ghost. It is a community server that talks to Ghost's public Admin and Content APIs using your own
credentials.
