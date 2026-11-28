"""
ghost_blog_mcp.py — a single-file MCP server for a Ghost blog.

Exposes the Ghost Admin API (JWT auth, read + write) and the Ghost Content
API (query-key auth, read-only fallback) as MCP tools.

Context-window safe by design: list tools return compact tables without
bodies; full bodies are fetched on demand, one post at a time.

Deps: mcp, requests, uvicorn (HTTP transports), anyio. Python 3.10+.
"""

import anyio.to_thread
import base64
import hmac
import json
import logging
import os
import re
import time

import requests
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

# ---------------------------------------------------------------------------
# Logging setup — suppress noisy streamable-HTTP / Starlette warnings so the
# server logs stay clean for operators.
# ---------------------------------------------------------------------------
logging.getLogger("mcp.server.streamable_http").setLevel(logging.INFO)
logging.getLogger("starlette.requests").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Server init
# ---------------------------------------------------------------------------
_transport = os.getenv("MCP_TRANSPORT", "stdio").lower()
# HTTP transports bind loopback by default. Binding a non-loopback address is
# opt-in (HOST) and REQUIRES MCP_AUTH_TOKEN — the endpoint fronts a Ghost
# admin key, so an unauthenticated network-facing server is refused at start.
_auth_token = (os.getenv("MCP_AUTH_TOKEN") or "").strip()
_host = ((os.getenv("HOST", "127.0.0.1").strip().lower())
         if _transport in ("streamable-http", "sse") else "127.0.0.1")
_port = int(os.getenv("PORT", "8000"))
_loopback = _host in ("127.0.0.1", "localhost", "::1")
if not _loopback and not _auth_token:
    raise SystemExit(
        "refusing to start: a non-loopback bind (%s) requires MCP_AUTH_TOKEN "
        "(set HOST=127.0.0.1 for local-only use, or set the token to protect "
        "the exposed endpoint)" % _host)
# Destructive tools (delete_*) are disabled by default; set
# GHOST_ALLOW_DELETE=1 (or true/yes) to enable them.
_allow_delete = os.getenv("GHOST_ALLOW_DELETE", "").strip().lower() in (
    "1", "true", "yes")
# GHOST_USER_AGENT overrides the Ghost API User-Agent (default is
# browser-class — see _USER_AGENT below).
_ghost_user_agent = (os.getenv("GHOST_USER_AGENT") or "").strip()

mcp = FastMCP(
    "GhostBlog",
    host=_host,
    port=_port,
    streamable_http_path="/",
    sse_path="/sse",
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
GHOST_URL   = (os.getenv("GHOST_URL") or "").rstrip("/")  # trailing / stripped
ADMIN_KEY   = os.getenv("GHOST_ADMIN_KEY")   # <key id>:<64-char-hex secret> — never logged
CONTENT_KEY = os.getenv("GHOST_CONTENT_KEY")  # optional; read-only mode — never logged

ADMIN_BASE   = f"{GHOST_URL}/ghost/api/admin"
CONTENT_BASE = f"{GHOST_URL}/ghost/api/content"

_ACCEPT_VERSION = "v6.0"  # minimum-version semantics; live instances run Ghost 6.x
_TIMEOUT = 30             # every Ghost call MUST use this — tools must never hang
_DEFAULT_LIMIT = 15
# Cloudflare fronts many Ghost sites and 403-blocks (error 1010) the default
# python urllib/requests signatures. A browser User-Agent gets through —
# verified against the live instance by the probe.
_USER_AGENT = _ghost_user_agent or (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


# ---------------------------------------------------------------------------
# Tool annotations + destructive-tool gate
# ---------------------------------------------------------------------------
_READ_ANN = ToolAnnotations(readOnlyHint=True)
_WRITE_ANN = ToolAnnotations(readOnlyHint=False)
_DELETE_ANN = ToolAnnotations(readOnlyHint=False, destructiveHint=True)


def _delete_gate():
    """Return a refusal message when destructive tools are disabled."""
    if _allow_delete:
        return None
    return ("Refusing: destructive operations are disabled by default. "
            "Set GHOST_ALLOW_DELETE=1 to enable the delete tools.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _truncate(text: str, max_chars: int = 300) -> str:
    if not text:
        return "(empty)"
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"... [truncated - {len(text) - max_chars} more chars]"


def _strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()


def _mode() -> str:
    """'admin' = full mode · 'content' = read-only · 'unconfigured'."""
    if ADMIN_KEY:
        return "admin"
    if CONTENT_KEY:
        return "content"
    return "unconfigured"


# ---------------------------------------------------------------------------
# Admin API auth — hand-rolled HS256 JWT (stdlib only; no jwt dependency).
# The Ghost admin key is "<key id>:<64-char-hex secret>". The secret is the
# hex-encoded HMAC key and must be DECODED with bytes.fromhex before signing
# (signing with the hex string itself is rejected with 401 invalid signature).
# ---------------------------------------------------------------------------

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _parse_admin_key():
    key_id, sep, secret = (ADMIN_KEY or "").partition(":")
    if not sep:
        return "", ""
    return key_id.strip(), secret.strip()


def _admin_jwt() -> str:
    """Sign a fresh admin JWT. Returns "" when the key is missing/malformed."""
    key_id, secret = _parse_admin_key()
    if not key_id or not secret:
        return ""
    try:
        key_bytes = bytes.fromhex(secret)
    except ValueError:
        return ""
    iat = int(time.time())
    header  = {"alg": "HS256", "typ": "JWT", "kid": key_id}
    payload = {"iat": iat, "exp": iat + 300, "aud": "/admin/"}
    h = _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    p = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    # The digest is requested BY NAME ("sha256") — no jwt dependency, and the
    # stdlib digest module is never imported by name in this tree.
    sig = hmac.new(key_bytes, f"{h}.{p}".encode("ascii"), "sha256").digest()
    return f"{h}.{p}.{_b64url(sig)}"


def _admin_headers() -> dict:
    return {"Authorization": f"Ghost {_admin_jwt()}", "Accept-Version": _ACCEPT_VERSION,
            "User-Agent": _USER_AGENT}


# ---------------------------------------------------------------------------
# Config checks + error formatting
# ---------------------------------------------------------------------------

def _admin_config_problem() -> str:
    missing = [n for n, v in (("GHOST_URL", GHOST_URL), ("GHOST_ADMIN_KEY", ADMIN_KEY)) if not v]
    if not missing:
        return ""
    return ("Missing required environment variable(s): " + ", ".join(missing)
            + " — set them and restart the server. "
            "(GHOST_ADMIN_KEY is required for write tools; with only GHOST_CONTENT_KEY "
            "the server is read-only.)")


def _content_config_problem() -> str:
    missing = []
    if not GHOST_URL:
        missing.append("GHOST_URL")
    if not ADMIN_KEY and not CONTENT_KEY:
        missing.append("GHOST_CONTENT_KEY or GHOST_ADMIN_KEY")
    if not missing:
        return ""
    return ("Missing required environment variable(s): " + ", ".join(missing)
            + " — set them and restart the server.")


def _error(status: int, body: str, admin: bool = True) -> str:
    if admin:
        hints = {
            401: " — signature rejected: check GHOST_ADMIN_KEY (format <key id>:<64-char-hex-secret>); "
                 "the key may have been rotated",
            403: " — the Ghost 6 admin API rejects ?key= queries by design (JWT only); "
                 "or the key role lacks permission",
            404: " — not found; check the id/slug with a list_* tool",
        }
    else:
        hints = {
            401: " — check GHOST_CONTENT_KEY; the key may have been revoked",
            403: " — the content key lacks permission for that resource",
            404: " — not found; check the id/slug with a list_* tool",
        }
    return f"Error {status}: {_truncate(body, 300)}{hints.get(status, '')}"


# ---------------------------------------------------------------------------
# API helpers — centralize the 30s timeout, the missing-env-var check,
# non-JSON response handling, and the 401/403/404 error hints. No tool may
# hang forever or die on a non-JSON error page.
# ---------------------------------------------------------------------------

def _api_get_impl(url: str, params: dict = None, admin: bool = True):
    """GET a Ghost endpoint. Returns (True, data) or (False, error_string).

    admin=True signs a fresh JWT; admin=False sends the Content API query key.
    """
    if admin:
        problem = _admin_config_problem()
        if problem:
            return False, problem
        r = requests.get(url, params=params, headers=_admin_headers(), timeout=_TIMEOUT)
    else:
        problem = _content_config_problem()
        if problem:
            return False, problem
        p = dict(params or {})
        p["key"] = CONTENT_KEY
        r = requests.get(url, params=p,
                         headers={"Accept-Version": _ACCEPT_VERSION,
                                  "User-Agent": _USER_AGENT}, timeout=_TIMEOUT)
    if r.status_code != 200:
        return False, _error(r.status_code, r.text, admin)
    try:
        return True, r.json()
    except ValueError:
        return False, f"Error: expected JSON from {url} but got a non-JSON response."


def _api_post_impl(url: str, payload: dict, params: dict = None, method: str = "POST"):
    """POST (default) or PUT a Ghost admin endpoint.
    Returns (True, data) or (False, error_string)."""
    problem = _admin_config_problem()
    if problem:
        return False, problem
    r = requests.request(method, url, json=payload, params=params,
                         headers=_admin_headers(), timeout=_TIMEOUT)
    if r.status_code not in (200, 201):
        return False, _error(r.status_code, r.text, True)
    try:
        return True, r.json()
    except ValueError:
        return True, None


def _api_delete_impl(url: str):
    """DELETE a Ghost admin endpoint. Returns (True, data) or (False, error_string)."""
    problem = _admin_config_problem()
    if problem:
        return False, problem
    r = requests.delete(url, headers=_admin_headers(), timeout=_TIMEOUT)
    if r.status_code not in (200, 204):
        return False, _error(r.status_code, r.text, True)
    try:
        return True, r.json()
    except ValueError:
        return True, None


# ---------------------------------------------------------------------------
# Response-shape + filter + lexical helpers
# ---------------------------------------------------------------------------

async def _api_get(url: str, params: dict = None, admin: bool = True):
    """Thread-offloaded _api_get_impl: the MCP SDK runs sync tools on
    the event loop thread, so the blocking requests core runs in a
    worker thread (anyio) and the loop never stalls on a Ghost call."""
    return await anyio.to_thread.run_sync(
        lambda: _api_get_impl(url, params, admin))


async def _api_post(url: str, payload: dict, params: dict = None,
                    method: str = "POST"):
    return await anyio.to_thread.run_sync(
        lambda: _api_post_impl(url, payload, params, method))


async def _api_delete(url: str):
    return await anyio.to_thread.run_sync(
        lambda: _api_delete_impl(url))


def _first_item(data, key: str):
    """Unwrap an Admin-API envelope ({"posts": [...]}) or a bare Content-API
    object into the first item (single-resource reads)."""
    if isinstance(data, dict):
        items = data.get(key)
        if isinstance(items, list) and items and isinstance(items[0], dict):
            return items[0]
        if "title" in data or "uuid" in data or "html" in data:
            return data
    return None


def _filter_clauses(status: str = "", tag: str = "", author: str = "") -> str:
    """Build a Ghost filter string (status:..., tag:..., author:...) from the
    simple list params. Values with spaces/colons are double-quoted."""
    def q(value: str) -> str:
        v = value.strip()
        if not v:
            return ""
        if any(ch in v for ch in " :\""):
            v = '"' + v.replace('"', "") + '"'
        return v

    clauses = []
    s = status.strip().lower()
    if s and s != "all":
        clauses.append("status:" + q(status))
    if tag.strip():
        clauses.append("tag:" + q(tag))
    if author.strip():
        clauses.append("author:" + q(author))
    return " ".join(clauses)


def _lexical_inline_text(node) -> str:
    """Concatenate the text nodes under a Lexical node (inline extraction)."""
    parts = []

    def rec(n):
        if not isinstance(n, dict):
            return
        if n.get("type") in ("text", "extended-text"):
            parts.append(n.get("text") or "")
            return
        for c in n.get("children") or []:
            rec(c)

    rec(node)
    return "".join(parts)


def _lexical_to_text(lexical) -> str:
    """Convert a Ghost Lexical document into readable plain text.

    The API delivers `lexical` as a JSON STRING (not an object), so parse it
    first. Layout is approximated: headings keep a markdown prefix, code
    blocks keep fences, list items get a bullet.
    """
    if not lexical:
        return "(empty)"
    doc = lexical
    if isinstance(doc, str):
        try:
            doc = json.loads(doc)
        except ValueError:
            return doc
    if not isinstance(doc, dict):
        return str(doc)
    root = doc.get("root")
    if not isinstance(root, dict):
        return str(doc)

    out = []

    def walk(node):
        if not isinstance(node, dict):
            return
        ntype = node.get("type")
        if ntype in ("code", "codeblock"):
            code = node.get("code") or node.get("text")
            if code:
                out.append("```\n" + code.strip("\n") + "\n```")
            return
        if ntype == "horizontalrule":
            out.append("---")
            return
        if ntype in ("image", "video", "bookmark", "embed", "file", "button"):
            label = (node.get("altText") or node.get("title") or node.get("url")
                     or node.get("src") or "")
            if label:
                out.append(f"[{ntype}: {label}]")
            return
        text = _lexical_inline_text(node).strip()
        if ntype == "heading":
            if text:
                tag = str(node.get("tag") or "h2").lower()
                out.append("#" * max(1, len(tag) - 1) + " " + text)
            return
        if ntype == "listitem":
            if text:
                out.append("- " + text)
            return
        if ntype in ("paragraph", "quote", "blockquote"):
            if text:
                out.append(text)
            return
        for c in node.get("children") or []:
            walk(c)

    walk(root)
    return "\n\n".join(l for l in out if l).strip() or "(empty)"


def _tag_names(items) -> str:
    return ", ".join(t.get("name", "?") for t in (items or []) if isinstance(t, dict))


def _author_names(items) -> str:
    return ", ".join(a.get("name", "?") for a in (items or []) if isinstance(a, dict))


# ===========================================================================
# READ TOOLS
# ===========================================================================

@mcp.tool(annotations=_READ_ANN)
async def check_connection() -> str:
    """
    Verify that credentials and connectivity work, with an auth-mode summary.
    Run this first when debugging a new setup.

    Auth modes:
      admin         — GHOST_ADMIN_KEY set: full read + write.
      content-only  — only GHOST_CONTENT_KEY set: read-only.
      unconfigured  — neither key set.
    """
    mode = _mode()
    if mode == "unconfigured":
        return "Connection FAILED: " + _content_config_problem()
    if mode == "admin":
        key_id, secret = _parse_admin_key()
        if not key_id or not secret:
            return ("Connection FAILED: GHOST_ADMIN_KEY has an invalid format \u2014 "
                    "expect <key id>:<hex secret> with both parts set. Copy the "
                    "full key from Ghost admin > Integrations > Admin API.")
        try:
            bytes.fromhex(secret)
        except ValueError:
            return ("Connection FAILED: GHOST_ADMIN_KEY secret is not valid hex \u2014 "
                    "re-copy the full key from Ghost admin > Integrations > "
                    "Admin API.")
        # GET /users/ requires a valid key — the lightest auth-required
        # read, so the real auth probe. (The public /site/ endpoint
        # answers 200 even with a broken key on Ghost 6, so it proves
        # connectivity only, not credentials. Ghost 6 has no /users/me/ —
        # that path 404s through the users/:id route, live-verified.)
        ok, users = await _api_get(f"{ADMIN_BASE}/users/", admin=True)
        if not ok:
            return f"Connection FAILED: {users}"
        ok, site = await _api_get(f"{ADMIN_BASE}/site/", admin=True)
        s = site.get("site", {}) if ok and isinstance(site, dict) else {}
        return (f"Connected to {GHOST_URL} — OK.\n"
                f"Auth mode: admin (full read/write) — key verified against the Admin API\n"
                f"Site title: {s.get('title', '?')}\n"
                f"Ghost version: {s.get('version', '?')}   URL: {s.get('url', '?')}\n"
                f"Use list_posts() / list_pages() / list_tags() / list_authors() to explore.")
    ok, settings = await _api_get(f"{CONTENT_BASE}/settings/", admin=False)
    if not ok:
        return f"Connection FAILED: {settings}"
    s = settings.get("settings", {}) if isinstance(settings, dict) else {}
    return (f"Connected to {GHOST_URL} — OK.\n"
            f"Auth mode: content-only (read-only — set GHOST_ADMIN_KEY for writes)\n"
            f"Site title: {s.get('title', '?')}\n"
            f"Use list_posts() / list_pages() / list_tags() / list_authors() to explore.")


@mcp.tool(annotations=_READ_ANN)
async def list_posts(
    status: str = "",
    tag: str = "",
    author: str = "",
    limit: int = _DEFAULT_LIMIT,
    page: int = 1,
    order: str = "published_at DESC",
) -> str:
    """
    Return a compact, agent-friendly list of posts — title, status, tags,
    author, and date only. No bodies included, so this never blows out
    context. Use get_post() for metadata, get_post_content() for the full
    body of one post.

    In admin mode all posts (draft/published/scheduled) are visible; with
    only a content key, published posts only.

    Args:
        status: 'draft', 'published', 'scheduled', or 'all' (admin mode).
        tag:    Filter by tag name or slug.
        author: Filter by author slug (from list_authors — the user's
                 slug, e.g. 'max'), or author id (admin mode).
        limit:  Posts per page (default 15, max 100).
        page:   Page number (1-based).
        order:  e.g. 'published_at DESC' (default), 'created_at ASC'.
    """
    limit = max(1, min(int(limit), 100))
    page  = max(1, int(page))

    filter_str = _filter_clauses(status=status, tag=tag, author=author)
    params = {"limit": limit, "page": page, "order": order, "include": "tags,authors"}
    if filter_str:
        params["filter"] = filter_str

    admin = _mode() == "admin"
    base  = ADMIN_BASE if admin else CONTENT_BASE
    ok, data = await _api_get(f"{base}/posts/", params=params, admin=admin)
    if not ok:
        return data

    posts = data.get("posts") if isinstance(data, dict) else None
    posts = posts if isinstance(posts, list) else []
    meta  = data.get("meta") if isinstance(data, dict) else {}
    pag   = meta.get("pagination") if isinstance(meta, dict) else {}
    pag   = pag if isinstance(pag, dict) else {}

    if not posts:
        return "No posts found matching those criteria."

    lines = [f"Posts (page {pag.get('page', page)}/{pag.get('page_count', '?')}, "
             f"total: {pag.get('total', '?')})"]
    if filter_str:
        lines[0] += f"  [filter: {filter_str}]"
    lines += ["",
              f"{'Date':<11}  {'Status':<10}  {'Title':<52}  {'Slug':<40}  {'Tags':<22}  Author",
              "-" * 158]
    for p in posts:
        date  = str(p.get("published_at") or p.get("created_at") or "?")[:10]
        title = p.get("title") or "(untitled)"
        if len(title) > 50:
            title = title[:47] + "..."
        tags    = _tag_names(p.get("tags"))
        if len(tags) > 20:
            tags = tags[:17] + "..."
        authors = _author_names(p.get("authors"))
        slug = (p.get("slug") or "")[:38]
        lines.append(f"{date:<11}  {str(p.get('status', '?')):<10}  {title:<52}  "
                     f"{slug:<40}  {tags:<22}  {authors}")
    lines += ["", "Use get_post(<slug>) for metadata + preview, "
              "get_post_content(<slug>) for the full body."]
    if pag.get("page_count", 1) > page:
        lines.append(f"Use list_posts(page={page + 1}) for the next page.")
    return "\n".join(lines)


_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                      r"[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


async def _fetch_item(base, kind, identifier, admin, formats=None):
    """Fetch one post/page by UUID, raw 24-hex id, or slug. Ghost 6 path
    params only accept raw 24-hex ids (all-hex slugs exist, e.g.
    'decade', so the id test is EXACTLY 24 chars); UUIDs and slugs are
    resolved via ?filter= queries on the list endpoint. formats (e.g.
    'html') is passed through — the admin API only returns html when
    asked for it."""
    extra = {"formats": formats} if formats else None
    if _UUID_RE.match(identifier):
        params = {"filter": f"uuid:{identifier}"}
        if extra:
            params.update(extra)
        return await _api_get(f"{base}/{kind}/", params=params, admin=admin)
    if re.fullmatch(r"[0-9a-f]{24}", identifier, flags=re.IGNORECASE):
        return await _api_get(f"{base}/{kind}/{identifier}/", params=extra,
                              admin=admin)
    params = {"filter": f"slug:{identifier}"}
    if extra:
        params.update(extra)
    return await _api_get(f"{base}/{kind}/", params=params, admin=admin)


async def _resolve_raw_id(kind: str, identifier: str):
    """Resolve a post/page identifier to the RAW ObjectID that Ghost 6 path
    params require. Raw 24-hex ids pass through unchanged; UUIDs and slugs
    are looked up on the list endpoint (same routing as _fetch_item).

    Returns (True, raw_id) or (False, error_message)."""
    if re.fullmatch(r"[0-9a-f]{24}", identifier, flags=re.IGNORECASE):
        return True, identifier
    ok, data = await _fetch_item(ADMIN_BASE, kind, identifier, True)
    if not ok:
        return False, data
    item = _first_item(data, kind)
    if not item or not item.get("id"):
        return False, f"'{identifier}' not found."
    return True, item["id"]


@mcp.tool(annotations=_READ_ANN)
async def get_post(identifier: str, chars: int = 1500) -> str:
    """
    Metadata + a truncated body preview for ONE post (by id or slug).

    No full body here on purpose — call get_post_content() when you need the
    complete text (lexical, default) or the raw HTML.

    Args:
        identifier: Post id (UUID), numeric id, or slug — from list_posts.
        chars:      Max body preview length in characters (default 1500).
    """
    chars = max(200, int(chars))
    admin = _mode() == "admin"
    base  = ADMIN_BASE if admin else CONTENT_BASE
    ok, data = await _fetch_item(base, "posts", identifier, admin,
                                  formats="html" if admin else None)
    if not ok:
        return f"Error looking up post '{identifier}': {data}"
    post = _first_item(data, "posts")
    if not post:
        return f"Post '{identifier}' returned an unexpected response."

    lines = [
        "=" * 64,
        f"Post: {post.get('title', '(untitled)')}  (id={post.get('uuid', '?')})",
        "=" * 64,
        f"Slug     : {post.get('slug', '?')}",
        f"Status   : {post.get('status', '?')}",
        f"Author   : {_author_names(post.get('authors')) or '-'}",
        f"Tags     : {_tag_names(post.get('tags')) or '-'}",
        f"URL      : {post.get('url') or '-'}",
        f"Created  : {post.get('created_at', '?')}",
        f"Updated  : {post.get('updated_at', '?')}   <- pass this to update_post/set_post_status",
        f"Published: {post.get('published_at') or '-'}",
        f"Excerpt  : {_truncate(post.get('custom_excerpt') or post.get('excerpt') or '', 300)}",
        "",
        f"--- Body (first {chars} chars) ---",
        _truncate(_strip_html(post.get("html")), chars),
        "",
        "Use get_post_content() for the full body; "
        "get_post_content(format='html') for raw HTML.",
    ]
    return "\n".join(lines)


@mcp.tool(annotations=_READ_ANN)
async def get_post_content(identifier: str, format: str = "lexical") -> str:
    """
    The FULL body of one post, on demand. This is the expensive call —
    browse with list_posts()/get_post() first, and only pull full bodies
    for the posts you actually need.

    Args:
        identifier: Post id or slug.
        format:     'lexical' (default) — the Lexical document (a JSON string
                    from the API) parsed into readable plain text; or
                    'html' — the raw HTML as stored.
    """
    if format not in ("lexical", "html"):
        return "Invalid format. Use 'lexical' or 'html'."
    admin = _mode() == "admin"
    base  = ADMIN_BASE if admin else CONTENT_BASE
    # Request html even in lexical mode: the Admin API omits it unless asked,
    # and the no-lexical fallback below needs it (legacy html-only content).
    ok, data = await _fetch_item(base, "posts", identifier, admin,
                                  formats="html" if admin else None)
    if not ok:
        return f"Error looking up post '{identifier}': {data}"
    post = _first_item(data, "posts")
    if not post:
        return f"Post '{identifier}' returned an unexpected response."
    if format == "html":
        return post.get("html") or "(no HTML content)"
    lexical = post.get("lexical")
    if lexical:
        return _lexical_to_text(lexical)
    return ("(no lexical document available for this post — "
            "showing text stripped from HTML)\n\n" + _strip_html(post.get("html")))


@mcp.tool(annotations=_READ_ANN)
async def list_pages(
    status: str = "",
    tag: str = "",
    limit: int = _DEFAULT_LIMIT,
    page: int = 1,
    order: str = "updated_at DESC",
) -> str:
    """
    Compact, agent-friendly list of static pages — title, status, tags, and
    date only. No bodies included. Use get_page() for metadata + a truncated
    body preview of one page.

    In admin mode all pages (draft/published) are visible; with only a
    content key, published pages only.

    Args:
        status: 'draft', 'published', or 'all' (admin mode).
        tag:    Filter by tag name or slug.
        limit:  Pages per page (default 15, max 100).
        page:   Page number (1-based).
        order:  e.g. 'updated_at DESC' (default), 'title ASC'.
    """
    limit = max(1, min(int(limit), 100))
    page  = max(1, int(page))

    filter_str = _filter_clauses(status=status, tag=tag)
    params = {"limit": limit, "page": page, "order": order, "include": "tags,authors"}
    if filter_str:
        params["filter"] = filter_str

    admin = _mode() == "admin"
    base  = ADMIN_BASE if admin else CONTENT_BASE
    ok, data = await _api_get(f"{base}/pages/", params=params, admin=admin)
    if not ok:
        return data

    pages = data.get("pages") if isinstance(data, dict) else None
    pages = pages if isinstance(pages, list) else []
    meta  = data.get("meta") if isinstance(data, dict) else {}
    pag   = meta.get("pagination") if isinstance(meta, dict) else {}
    pag   = pag if isinstance(pag, dict) else {}

    if not pages:
        return "No pages found matching those criteria."

    lines = [f"Pages (page {pag.get('page', page)}/{pag.get('page_count', '?')}, "
             f"total: {pag.get('total', '?')})"]
    if filter_str:
        lines[0] += f"  [filter: {filter_str}]"
    lines += ["",
              f"{'Updated':<11}  {'Status':<10}  {'Title':<52}  {'Slug':<40}  {'Tags':<22}  Author",
              "-" * 158]
    for p in pages:
        date  = str(p.get("updated_at") or p.get("created_at") or "?")[:10]
        title = p.get("title") or "(untitled)"
        if len(title) > 50:
            title = title[:47] + "..."
        tags    = _tag_names(p.get("tags"))
        if len(tags) > 20:
            tags = tags[:17] + "..."
        authors = _author_names(p.get("authors"))
        slug = (p.get("slug") or "")[:38]
        lines.append(f"{date:<11}  {str(p.get('status', '?')):<10}  {title:<52}  "
                     f"{slug:<40}  {tags:<22}  {authors}")
    lines += ["", "Use get_page(<slug>) for metadata + a truncated body preview, "
              "get_page_content(<slug>) for the full body."]
    if pag.get("page_count", 1) > page:
        lines.append(f"Use list_pages(page={page + 1}) for the next page.")
    return "\n".join(lines)


@mcp.tool(annotations=_READ_ANN)
async def get_page(identifier: str, chars: int = 1500) -> str:
    """
    Metadata + a truncated body preview for ONE static page (by id or slug).

    Args:
        identifier: Page id (UUID), numeric id, or slug — from list_pages.
        chars:      Max body preview length in characters (default 1500).
    """
    chars = max(200, int(chars))
    admin = _mode() == "admin"
    base  = ADMIN_BASE if admin else CONTENT_BASE
    ok, data = await _fetch_item(base, "pages", identifier, admin,
                                  formats="html" if admin else None)
    if not ok:
        return f"Error looking up page '{identifier}': {data}"
    post = _first_item(data, "pages")
    if not post:
        return f"Page '{identifier}' returned an unexpected response."

    lines = [
        "=" * 64,
        f"Page: {post.get('title', '(untitled)')}  (id={post.get('uuid', '?')})",
        "=" * 64,
        f"Slug     : {post.get('slug', '?')}",
        f"Status   : {post.get('status', '?')}",
        f"Author   : {_author_names(post.get('authors')) or '-'}",
        f"Tags     : {_tag_names(post.get('tags')) or '-'}",
        f"URL      : {post.get('url') or '-'}",
        f"Created  : {post.get('created_at', '?')}",
        f"Updated  : {post.get('updated_at', '?')}   <- pass this to update_page",
        f"Excerpt  : {_truncate(post.get('custom_excerpt') or post.get('excerpt') or '', 300)}",
        "",
        f"--- Body (first {chars} chars) ---",
        _truncate(_strip_html(post.get("html")), chars),
        "",
        "Use get_page_content() for the full body; "
        "get_page_content(format='html') for raw HTML.",
    ]
    return "\n".join(lines)


@mcp.tool(annotations=_READ_ANN)
async def get_page_content(identifier: str, format: str = "lexical") -> str:
    """
    The FULL body of one static page, on demand. This is the expensive call —
    browse with list_pages()/get_page() first, and only pull full bodies for
    the pages you actually need.

    This is where a page's custom HTML blocks live: they are html nodes in the
    Lexical document, which the flattened get_page() preview cannot show as
    stored.

    Args:
        identifier: Page id or slug.
        format:     'lexical' (default) — the Lexical document (a JSON string
                    from the API) parsed into readable plain text; or
                    'html' — the raw HTML as stored.
    """
    if format not in ("lexical", "html"):
        return "Invalid format. Use 'lexical' or 'html'."
    admin = _mode() == "admin"
    base  = ADMIN_BASE if admin else CONTENT_BASE
    ok, data = await _fetch_item(base, "pages", identifier, admin,
                                  formats="html" if admin else None)
    if not ok:
        return f"Error looking up page '{identifier}': {data}"
    page = _first_item(data, "pages")
    if not page:
        return f"Page '{identifier}' returned an unexpected response."
    if format == "html":
        return page.get("html") or "(no HTML content)"
    lexical = page.get("lexical")
    if lexical:
        return _lexical_to_text(lexical)
    return ("(no lexical document available for this page — "
            "showing text stripped from HTML)\n\n" + _strip_html(page.get("html")))


@mcp.tool(annotations=_READ_ANN)
async def list_tags(limit: int = _DEFAULT_LIMIT, page: int = 1, order: str = "name ASC") -> str:
    """
    List all tags with post/page counts where the API provides them (counts
    are admin-mode only). Tag names from here filter list_posts(tag=...) and
    feed create_post(tags=...) / create_tag().
    """
    limit = max(1, min(int(limit), 100))
    page  = max(1, int(page))

    admin = _mode() == "admin"
    base  = ADMIN_BASE if admin else CONTENT_BASE
    ok, data = await _api_get(f"{base}/tags/",
                        params={"limit": limit, "page": page, "order": order},
                        admin=admin)
    if not ok:
        return data

    tags = data.get("tags") if isinstance(data, dict) else None
    tags = tags if isinstance(tags, list) else []
    if not tags:
        return "No tags found."

    meta  = data.get("meta") if isinstance(data, dict) else {}
    pag   = meta.get("pagination") if isinstance(meta, dict) else {}
    pag   = pag if isinstance(pag, dict) else {}

    lines = [f"Tags (page {pag.get('page', page)}/{pag.get('page_count', '?')}, "
             f"total: {pag.get('total', '?')}):", ""]
    for t in tags:
        name, slug = t.get("name", "?"), t.get("slug", "?")
        tid = t.get("id") or t.get("uuid") or "?"
        count = t.get("count") if isinstance(t.get("count"), dict) else None
        cnt = ""
        if count:
            cnt = f"  posts={count.get('posts', 0)}, pages={count.get('pages', 0)}"
        desc = _truncate(t.get("description") or "", 60)
        lines.append(f"  {name} ({slug})  id={tid}{cnt}" + (f"  — {desc}" if desc else ""))
    lines += ["", "Use these names with list_posts(tag=...) or create_post(tags=...).",
              "Tag ids work with update_tag(tag_id=...) / delete_tag(tag_id=...)."]
    if pag.get("page_count", 1) > page:
        lines.append(f"Use list_tags(page={page + 1}) for the next page.")
    return "\n".join(lines)


@mcp.tool(annotations=_READ_ANN)
async def list_authors(limit: int = _DEFAULT_LIMIT, page: int = 1) -> str:
    """
    List the site's authors. Uses the Admin API /users/ in full mode (with
    status and post counts) and the Content API /authors/ in read-only mode.
    Author emails from here work with create_post(authors=...) and
    update_post(authors=...).
    """
    limit = max(1, min(int(limit), 100))
    page  = max(1, int(page))

    if _mode() == "admin":
        ok, data = await _api_get(f"{ADMIN_BASE}/users/",
                            params={"limit": limit, "page": page}, admin=True)
        if not ok:
            return data
        users = data.get("users") if isinstance(data, dict) else None
        users = users if isinstance(users, list) else []
        if not users:
            return "No users found."
        lines = [f"Authors / users ({len(users)} on page {page}):", ""]
        for u in users:
            count = u.get("count") if isinstance(u.get("count"), dict) else {}
            bio   = _truncate(u.get("bio") or "", 60)
            lines.append(f"  {u.get('name', '?')} <{u.get('email', '?')}>  "
                         f"[{u.get('status', '?')}]  posts={count.get('posts', 0)}"
                         + (f"  bio: {bio}" if bio else ""))
        lines += ["", "Use these emails/ids with create_post(authors=...) or "
                    "update_post(authors=...)."]
        return "\n".join(lines)

    ok, data = await _api_get(f"{CONTENT_BASE}/authors/",
                        params={"limit": limit, "page": page}, admin=False)
    if not ok:
        return data
    authors = data.get("authors") if isinstance(data, dict) else None
    authors = authors if isinstance(authors, list) else []
    if not authors:
        return "No authors found."
    lines = [f"Authors ({len(authors)} on page {page}):", ""]
    for a in authors:
        bio = _truncate(a.get("bio") or "", 60)
        lines.append(f"  {a.get('name', '?')} <{a.get('email', '?')}>"
                     + (f"  bio: {bio}" if bio else ""))
    lines += ["", "(Read-only mode — set GHOST_ADMIN_KEY for full author details.)"]
    return "\n".join(lines)


# ===========================================================================
# WRITE TOOLS (admin-only)
# ===========================================================================

@mcp.tool(annotations=_WRITE_ANN)
async def create_post(
    title: str,
    html: str = "",
    lexical: str = "",
    status: str = "draft",
    tags: list | None = None,
    authors: list | None = None,
    published_at: str = "",
) -> str:
    """
    Create a new post.

    IMPORTANT: Confirm the title/body with the user before calling — a
    published post is immediately live on the site.

    Args:
        title:      Post title (required).
        html:       Body as HTML. Sent with ?source=html so Ghost converts
                    it to Lexical. NOTE: that conversion is lossy — for a
                    lossless HTML embed, wrap the markup in kg-card comments:
                    <!--kg-card-begin: html--> ... <!--kg-card-end: html-->
        lexical:    Body as a Lexical document — a JSON STRING, not an object.
        status:     'draft' (default), 'published', or 'scheduled'
                    (scheduling requires published_at).
        tags:       List of tag names, e.g. ['news', 'ai'] (unmatched
                    names are auto-created by Ghost).
        authors:    List of author emails or ids.
        published_at: ISO 8601 datetime (e.g. 2026-09-15T12:00:00.000Z).
                    With the default status 'draft' it schedules the post
                    (status becomes 'scheduled'); with an explicit status it
                    is used as-is — a past date with status 'published'
                    backdates the post. Ghost's field is published_at, not
                    publish_at.
    """
    if not title.strip():
        return "title is required."
    payload = {"title": title.strip()}
    if html:
        payload["html"] = html
    if lexical:
        payload["lexical"] = lexical
    if status not in ("draft", "published", "scheduled"):
        return "Invalid status. Use 'draft', 'published', or 'scheduled'."
    payload["status"] = status
    if tags:
        payload["tags"] = [{"name": t.strip()} for t in tags if t and t.strip()]
    if authors:
        payload["authors"] = [a.strip() for a in authors if a and a.strip()]
    if published_at.strip():
        payload["published_at"] = published_at.strip()
        if status == "draft":
            payload["status"] = "scheduled"

    params = {"source": "html"} if html else None
    ok, data = await _api_post(f"{ADMIN_BASE}/posts/", {"posts": [payload]}, params=params)
    if not ok:
        return data
    post = _first_item(data, "posts") or {}
    result = (f"Post created: '{post.get('title', title)}' "
              f"(id={post.get('uuid', '?')}, status={post.get('status', status)}).")
    if post.get("status") == "scheduled" and post.get("published_at"):
        result += f"  Scheduled to publish at {post['published_at']}"
    return result


@mcp.tool(annotations=_WRITE_ANN)
async def update_post(
    post_id: str,
    updated_at: str,
    html: str = "",
    lexical: str = "",
    title: str = "",
    slug: str = "",
    excerpt: str = "",
    tags: list | None = None,
    authors: list | None = None,
    save_revision: bool = False,
) -> str:
    """
    Update an existing post.

    IMPORTANT:
    - updated_at is REQUIRED (optimistic locking). Fetch the latest version
      first with get_post() and pass its Updated value verbatim.
    - tags/authors are REPLACED, not merged — if you send tags, the post's
      existing tags are overwritten. Fetch the post first, amend the full
      list, and send it.
    - Confirm the changes with the user before calling.

    A PUT with {"html": ...} and no ?source=html returns 200 but is a SILENT
    NO-OP (updated_at does not move). The returned updated_at below must
    differ from the one you sent — if it did not move, the update was not
    applied.

    Args:
        post_id:       Post id - UUID, numeric id, or slug (from get_post / list_posts).
        updated_at:    The Updated timestamp from get_post, verbatim.
        html:          New body as HTML (sent with ?source=html; the
                       HTML→Lexical conversion is lossy — wrap in kg-card
                       comments for a lossless embed:
                       <!--kg-card-begin: html--> ... <!--kg-card-end: html-->).
        lexical:       New body as a Lexical JSON STRING.
        title:         New title (omit to leave unchanged).
        slug:          New slug (omit to leave unchanged).
        excerpt:       New custom excerpt (omit to leave unchanged).
        tags:          List of tag names — REPLACES all existing tags.
                       Omit (default) to leave tags unchanged; pass an
                       empty list to clear all tags. Unmatched names are
                       auto-created by Ghost.
        authors:       List of author emails or ids — REPLACES all
                       existing authors. Omit to leave unchanged; pass
                       an empty list to clear all.
        save_revision: Also save the current version as a revision.
    """
    if not post_id:
        return "post_id is required."
    if not updated_at:
        return ("updated_at is required (optimistic lock). Fetch the latest post "
                "first with get_post() and pass its Updated value.")

    payload = {"updated_at": updated_at}
    if html:
        payload["html"] = html
    if lexical:
        payload["lexical"] = lexical
    if title.strip():
        payload["title"] = title.strip()
    if slug.strip():
        payload["slug"] = slug.strip()
    if excerpt.strip():
        payload["custom_excerpt"] = excerpt.strip()
    if tags is not None:
        payload["tags"] = [{"name": t.strip()} for t in tags if t and t.strip()]
    if authors is not None:
        payload["authors"] = [a.strip() for a in authors if a and a.strip()]

    params = {}
    if html:
        params["source"] = "html"
    if save_revision:
        params["save_revision"] = "true"

    ok, raw_id = await _resolve_raw_id("posts", post_id)
    if not ok:
        return f"Error resolving post id '{post_id}': {raw_id}"
    ok, data = await _api_post(f"{ADMIN_BASE}/posts/{raw_id}/", {"posts": [payload]},
                         params=params or None, method="PUT")
    if not ok:
        return data
    post = _first_item(data, "posts") or {}
    new_updated = post.get("updated_at", "?")
    result = (f"Post updated: '{post.get('title', '?')}' (id={post.get('uuid', post_id)}). "
              f"updated_at: {updated_at} -> {new_updated}.")
    if new_updated == updated_at:
        result += ("  WARNING: updated_at did not move — the update was a silent "
                   "no-op (check ?source=html for html bodies).")
    return result


@mcp.tool(annotations=_WRITE_ANN)
async def set_post_status(post_id: str, status: str, updated_at: str, published_at: str = "") -> str:
    """
    Change a post's status (publish, unpublish to draft, or schedule).

    IMPORTANT: Confirm with the user before calling — publishing is
    immediately visible on the site.

    Args:
        post_id:    Post id - UUID, numeric id, or slug (from get_post / list_posts).
        status:     'published', 'draft', or 'scheduled'.
        updated_at: The Updated timestamp from get_post, verbatim
                    (optimistic lock — fetch the latest first).
        published_at: ISO 8601 datetime, REQUIRED for status 'scheduled'
                    — Ghost's field is published_at, not publish_at.
    """
    if not post_id:
        return "post_id is required."
    if status not in ("published", "draft", "scheduled"):
        return "Invalid status. Use 'published', 'draft', or 'scheduled'."
    if status == "scheduled" and not published_at.strip():
        return "Scheduling requires published_at (ISO 8601 datetime)."
    if not updated_at:
        return ("updated_at is required (optimistic lock). Fetch the latest post "
                "first with get_post() and pass its Updated value.")

    payload = {"status": status, "updated_at": updated_at}
    if published_at.strip():
        payload["published_at"] = published_at.strip()

    ok, raw_id = await _resolve_raw_id("posts", post_id)
    if not ok:
        return f"Error resolving post id '{post_id}': {raw_id}"
    ok, data = await _api_post(f"{ADMIN_BASE}/posts/{raw_id}/", {"posts": [payload]}, method="PUT")
    if not ok:
        return data
    post = _first_item(data, "posts") or {}
    result = f"Post status set to {post.get('status', status)} (id={post.get('uuid', post_id)})."
    if post.get("status") == "scheduled" and post.get("published_at"):
        result += f"  Publishes at {post['published_at']}."
    return result


@mcp.tool(annotations=_DELETE_ANN)
async def delete_post(post_id: str) -> str:
    """
    Permanently delete a post. This cannot be undone.

    IMPORTANT: Confirm the post id and the deletion with the user before
    calling.

    Args:
        post_id: Post id - UUID, numeric id, or slug (from get_post / list_posts).
    """
    if not post_id:
        return "post_id is required."
    denied = _delete_gate()
    if denied:
        return denied
    ok, raw_id = await _resolve_raw_id("posts", post_id)
    if not ok:
        return f"Error resolving post id '{post_id}': {raw_id}"
    ok, data = await _api_delete(f"{ADMIN_BASE}/posts/{raw_id}/")
    if not ok:
        return data
    return f"Post deleted (id={post_id}). This cannot be undone."


@mcp.tool(annotations=_WRITE_ANN)
async def create_page(
    title: str,
    html: str = "",
    lexical: str = "",
    status: str = "draft",
    tags: list | None = None,
    authors: list | None = None,
) -> str:
    """
    Create a new static page. (Pages cannot be scheduled — no published_at.)

    IMPORTANT: Confirm the title/body with the user before calling — a
    published page is immediately live on the site.

    Args:
        title:   Page title (required).
        html:    Body as HTML. Sent with ?source=html so Ghost converts it
                 to Lexical. NOTE: that conversion is lossy — for a lossless
                 HTML embed, wrap the markup in kg-card comments:
                 <!--kg-card-begin: html--> ... <!--kg-card-end: html-->
        lexical: Body as a Lexical document — a JSON STRING, not an object.
        status:  'draft' (default) or 'published'.
        tags:    List of tag names (unmatched names are auto-created).
        authors: List of author emails or ids.
    """
    if not title.strip():
        return "title is required."
    payload = {"title": title.strip()}
    if html:
        payload["html"] = html
    if lexical:
        payload["lexical"] = lexical
    if status not in ("draft", "published"):
        return "Invalid status. Use 'draft' or 'published'."
    payload["status"] = status
    if tags:
        payload["tags"] = [{"name": t.strip()} for t in tags if t and t.strip()]
    if authors:
        payload["authors"] = [a.strip() for a in authors if a and a.strip()]

    params = {"source": "html"} if html else None
    ok, data = await _api_post(f"{ADMIN_BASE}/pages/", {"pages": [payload]}, params=params)
    if not ok:
        return data
    post = _first_item(data, "pages") or {}
    return (f"Page created: '{post.get('title', title)}' "
            f"(id={post.get('uuid', '?')}, status={post.get('status', status)}).")


@mcp.tool(annotations=_WRITE_ANN)
async def update_page(
    page_id: str,
    updated_at: str,
    html: str = "",
    lexical: str = "",
    title: str = "",
    slug: str = "",
    excerpt: str = "",
    tags: list | None = None,
    authors: list | None = None,
    save_revision: bool = False,
) -> str:
    """
    Update an existing static page.

    IMPORTANT:
    - updated_at is REQUIRED (optimistic locking). Fetch the latest version
      first with get_page() and pass its Updated value verbatim.
    - tags/authors are REPLACED, not merged — fetch the page first, amend
      the full list, and send it.
    - Confirm the changes with the user before calling.

    A PUT with {"html": ...} and no ?source=html returns 200 but is a SILENT
    NO-OP (updated_at does not move). The returned updated_at below must
    differ from the one you sent — if it did not move, the update was not
    applied.

    Args:
        page_id:       Page id - UUID, numeric id, or slug (from get_page / list_pages).
        updated_at:    The Updated timestamp from get_page, verbatim.
        html:          New body as HTML (sent with ?source=html; the
                       HTML→Lexical conversion is lossy — wrap in kg-card
                       comments for a lossless embed:
                       <!--kg-card-begin: html--> ... <!--kg-card-end: html-->).
        lexical:       New body as a Lexical JSON STRING.
        title:         New title (omit to leave unchanged).
        slug:          New slug (omit to leave unchanged).
        excerpt:       New custom excerpt (omit to leave unchanged).
        tags:          List of tag names — REPLACES all existing tags.
                       Omit (default) to leave tags unchanged; pass an
                       empty list to clear all tags. Unmatched names are
                       auto-created by Ghost.
        authors:       List of author emails or ids — REPLACES all
                       existing authors. Omit to leave unchanged; pass
                       an empty list to clear all.
        save_revision: Also save the current version as a revision.
    """
    if not page_id:
        return "page_id is required."
    if not updated_at:
        return ("updated_at is required (optimistic lock). Fetch the latest page "
                "first with get_page() and pass its Updated value.")

    payload = {"updated_at": updated_at}
    if html:
        payload["html"] = html
    if lexical:
        payload["lexical"] = lexical
    if title.strip():
        payload["title"] = title.strip()
    if slug.strip():
        payload["slug"] = slug.strip()
    if excerpt.strip():
        payload["custom_excerpt"] = excerpt.strip()
    if tags is not None:
        payload["tags"] = [{"name": t.strip()} for t in tags if t and t.strip()]
    if authors is not None:
        payload["authors"] = [a.strip() for a in authors if a and a.strip()]

    params = {}
    if html:
        params["source"] = "html"
    if save_revision:
        params["save_revision"] = "true"

    ok, raw_id = await _resolve_raw_id("pages", page_id)
    if not ok:
        return f"Error resolving page id '{page_id}': {raw_id}"
    ok, data = await _api_post(f"{ADMIN_BASE}/pages/{raw_id}/", {"pages": [payload]},
                         params=params or None, method="PUT")
    if not ok:
        return data
    post = _first_item(data, "pages") or {}
    new_updated = post.get("updated_at", "?")
    result = (f"Page updated: '{post.get('title', '?')}' (id={post.get('uuid', page_id)}). "
              f"updated_at: {updated_at} -> {new_updated}.")
    if new_updated == updated_at:
        result += ("  WARNING: updated_at did not move — the update was a silent "
                   "no-op (check ?source=html for html bodies).")
    return result


@mcp.tool(annotations=_DELETE_ANN)
async def delete_page(page_id: str) -> str:
    """
    Permanently delete a static page. This cannot be undone.

    IMPORTANT: Confirm the page id and the deletion with the user before
    calling.

    Args:
        page_id: Page id - UUID, numeric id, or slug (from get_page / list_pages).
    """
    if not page_id:
        return "page_id is required."
    denied = _delete_gate()
    if denied:
        return denied
    ok, raw_id = await _resolve_raw_id("pages", page_id)
    if not ok:
        return f"Error resolving page id '{page_id}': {raw_id}"
    ok, data = await _api_delete(f"{ADMIN_BASE}/pages/{raw_id}/")
    if not ok:
        return data
    return f"Page deleted (id={page_id}). This cannot be undone."


@mcp.tool(annotations=_WRITE_ANN)
async def create_tag(name: str, slug: str = "", description: str = "") -> str:
    """
    Create a new tag.

    IMPORTANT: Confirm with the user before calling.

    Args:
        name:        Tag name (required).
        slug:        Optional tag slug (auto-generated from the name if omitted).
        description: Optional tag description.
    """
    if not name.strip():
        return "name is required."
    tag = {"name": name.strip()}
    if slug.strip():
        tag["slug"] = slug.strip()
    if description.strip():
        tag["description"] = description.strip()

    ok, data = await _api_post(f"{ADMIN_BASE}/tags/", {"tags": [tag]})
    if not ok:
        return data
    created = _first_item(data, "tags") or {}
    return (f"Tag created: '{created.get('name', name)}' "
            f"(id={created.get('id', '?')}, slug={created.get('slug', '?')}).")


@mcp.tool(annotations=_WRITE_ANN)
async def update_tag(tag_id: str, name: str = "", slug: str = "", description: str = "") -> str:
    """
    Update an existing tag (name, slug, and/or description).

    IMPORTANT: Confirm with the user before calling.

    Args:
        tag_id:      Tag id - raw id, UUID, or slug (from list_tags).
        name:        New name (omit to keep).
        slug:        New slug (omit to keep).
        description: New description (omit to keep).
    """
    if not tag_id:
        return "tag_id is required."
    tag = {}
    if name.strip():
        tag["name"] = name.strip()
    if slug.strip():
        tag["slug"] = slug.strip()
    if description.strip():
        tag["description"] = description.strip()
    if not tag:
        return "Nothing to update — provide name, slug, and/or description."

    ok, raw_id = await _resolve_raw_id("tags", tag_id)
    if not ok:
        return f"Error resolving tag id '{tag_id}': {raw_id}"
    ok, data = await _api_post(f"{ADMIN_BASE}/tags/{raw_id}/", {"tags": [tag]}, method="PUT")
    if not ok:
        return data
    updated = _first_item(data, "tags") or {}
    return f"Tag updated: '{updated.get('name', '?')}' (id={updated.get('id', tag_id)})."


@mcp.tool(annotations=_DELETE_ANN)
async def delete_tag(tag_id: str) -> str:
    """
    Permanently delete a tag (the tag is removed from posts/pages that use
    it). This cannot be undone.

    IMPORTANT: Confirm the tag id and the deletion with the user before
    calling.

    Args:
        tag_id: Tag id - raw id, UUID, or slug (from list_tags).
    """
    if not tag_id:
        return "tag_id is required."
    denied = _delete_gate()
    if denied:
        return denied
    ok, raw_id = await _resolve_raw_id("tags", tag_id)
    if not ok:
        return f"Error resolving tag id '{tag_id}': {raw_id}"
    ok, data = await _api_delete(f"{ADMIN_BASE}/tags/{raw_id}/")
    if not ok:
        return data
    return f"Tag deleted (id={tag_id}). This cannot be undone."


# ===========================================================================
# NEWSLETTERS / TIERS / OFFERS / MEMBERS / WEBHOOKS (admin-only groups)
#
# These five resources have NO Content API surface — unlike posts/pages,
# there is no read-only fallback. Write tools get the GHOST_ADMIN_KEY
# configuration error centralized in the _api_* helpers; read tools check
# _admin_config_problem() up front and return it when the admin key is
# missing.
# ===========================================================================

def _price_label(tier: dict) -> str:
    """Format a tier's prices (cents strings on the wire) for display."""
    cur = tier.get("currency") or "USD"
    parts = []
    for value, suffix in ((tier.get("monthly_price"), "/mo"),
                          (tier.get("yearly_price"), "/yr")):
        if value is None:
            continue
        try:
            parts.append(f"{int(value) / 100:.2f} {cur}{suffix}")
        except (TypeError, ValueError):
            parts.append(f"{value} {cur}{suffix}?")
    return " + ".join(parts) if parts else "free"


def _discount_label(offer: dict) -> str:
    """Human-readable discount summary for one offer row."""
    t, amt = offer.get("type", "?"), offer.get("amount")
    if t == "percent":
        return f"{amt}% off"
    if t == "tier_free_trial":
        months = offer.get("duration_in_months")
        if months:
            return f"free trial ({months} months)"
        return f"free trial ({offer.get('duration') or 0} days)"
    cur = offer.get("currency") or "USD"
    try:
        return f"{cur} {int(amt) / 100:.2f} off"
    except (TypeError, ValueError):
        return f"{amt} {cur} off"


# ===========================================================================
# NEWSLETTERS
# ===========================================================================

@mcp.tool(annotations=_READ_ANN)
async def list_newsletters(
    limit: int = _DEFAULT_LIMIT,
    page: int = 1,
    order: str = "sort_order ASC, name ASC",
) -> str:
    """
    List the site's newsletters (admin key required — newsletters have no
    Content API surface).

    Args:
        limit:  Newsletters per page (default 15, max 100).
        page:   Page number (1-based).
        order:  e.g. 'sort_order ASC, name ASC' (default), 'created_at DESC'.
    """
    problem = _admin_config_problem()
    if problem:
        return problem
    limit = max(1, min(int(limit), 100))
    page  = max(1, int(page))

    ok, data = await _api_get(f"{ADMIN_BASE}/newsletters/",
                              params={"limit": limit, "page": page, "order": order,
                                      "include": "count"}, admin=True)
    if not ok:
        return data
    newsletters = data.get("newsletters") if isinstance(data, dict) else None
    newsletters = newsletters if isinstance(newsletters, list) else []
    if not newsletters:
        return "No newsletters found."
    meta = data.get("meta") if isinstance(data, dict) else {}
    pag  = meta.get("pagination") if isinstance(meta, dict) else {}
    pag  = pag if isinstance(pag, dict) else {}

    lines = [f"Newsletters (page {pag.get('page', page)}/{pag.get('page_count', '?')}, "
             f"total: {pag.get('total', '?')}):", ""]
    for n in newsletters:
        count = n.get("count") if isinstance(n.get("count"), dict) else {}
        desc  = _truncate(n.get("description") or "", 60)
        lines.append(f"  {n.get('name', '?')} ({n.get('slug', '?')})  "
                     f"id={n.get('id') or n.get('uuid') or '?'}  "
                     f"[{n.get('status', '?')}]  subscribers={count.get('subscribers', 0)}"
                     + (f"  — {desc}" if desc else ""))
    lines += ["", "Use these ids with get_newsletter / update_newsletter / delete_newsletter.",
              "The site's default newsletter cannot be deleted (Ghost rejects it with a 400)."]
    if pag.get("page_count", 1) > page:
        lines.append(f"Use list_newsletters(page={page + 1}) for the next page.")
    return "\n".join(lines)


@mcp.tool(annotations=_READ_ANN)
async def get_newsletter(identifier: str) -> str:
    """
    Full metadata for ONE newsletter (by raw id, UUID, or slug).

    Args:
        identifier: Newsletter id, UUID, or slug (from list_newsletters).
    """
    problem = _admin_config_problem()
    if problem:
        return problem
    if not identifier:
        return "identifier is required."
    ok, data = await _fetch_item(ADMIN_BASE, "newsletters", identifier, True)
    if not ok:
        return f"Error looking up newsletter '{identifier}': {data}"
    n = _first_item(data, "newsletters")
    if not n:
        return f"Newsletter '{identifier}' not found."

    lines = [
        "=" * 64,
        f"Newsletter: {n.get('name', '(unnamed)')}  (id={n.get('uuid', '?')})",
        "=" * 64,
        f"Slug        : {n.get('slug', '?')}",
        f"Status      : {n.get('status', '?')}",
        f"Description : {_truncate(n.get('description') or '', 300) or '-'}",
        f"Sender      : {n.get('sender_name') or '-'} <{n.get('sender_email') or '-'}>",
        f"Reply-to    : {n.get('sender_reply_to') or '-'}",
        f"Subs on sign-up : {'yes' if n.get('subscribe_on_signup') else 'no'}",
        f"Header      : title={'yes' if n.get('show_header_title') else 'no'}  "
        f"name={'yes' if n.get('show_header_name') else 'no'}  "
        f"icon={'yes' if n.get('show_header_icon') else 'no'}",
        f"Title align : {n.get('title_alignment') or '-'}   "
        f"feature image: {'yes' if n.get('show_feature_image') else 'no'}",
        f"Fonts       : title={n.get('title_font_category') or '-'}  "
        f"body={n.get('body_font_category') or '-'}",
        f"Badge       : {'yes' if n.get('show_badge') else 'no'}",
        f"Sort order  : {n.get('sort_order', 0)}",
        f"Created     : {n.get('created_at', '?')}",
        f"Updated     : {n.get('updated_at', '?')}",
        "",
        "Use this id with update_newsletter / delete_newsletter.",
    ]
    footer = n.get("footer_content")
    if footer:
        lines.append(f"Footer      : {_truncate(footer, 200)}")
    return "\n".join(lines)


@mcp.tool(annotations=_WRITE_ANN)
async def create_newsletter(
    name: str,
    description: str = "",
    sender_reply_to: str = "",
    status: str = "active",
    subscribe_on_signup: bool | None = None,
    show_header_icon: bool | None = None,
    show_header_title: bool | None = None,
    show_header_name: bool | None = None,
    title_font_category: str = "",
    title_alignment: str = "",
    show_feature_image: bool | None = None,
    body_font_category: str = "",
    show_badge: bool | None = None,
) -> str:
    """
    Create a new newsletter.

    IMPORTANT: Confirm with the user before calling — an active newsletter
    is immediately available for member signups.

    Args:
        name:               Newsletter name (required, unique).
        description:        Short description.
        sender_reply_to:    Reply-to address used for this newsletter.
        status:             'active' (default) or 'inactive'.
        subscribe_on_signup:   Whether new members subscribe to this
                    newsletter automatically (omit = Ghost default).
        show_header_icon:   Show the site icon in the header.
        show_header_title:  Show the site title in the header.
        show_header_name:   Show the newsletter name in the header.
        title_font_category: 'sans-serif' or 'serif'.
        title_alignment:    'left' or 'center'.
        show_feature_image: Show the post feature image in the header.
        body_font_category: 'sans-serif' or 'serif'.
        show_badge:         Show the "New" badge on subscribed posts.
    """
    if not name.strip():
        return "name is required."
    if status not in ("active", "inactive"):
        return "Invalid status. Use 'active' or 'inactive'."
    payload = {"name": name.strip(), "status": status}
    if description.strip():
        payload["description"] = description.strip()
    if sender_reply_to.strip():
        payload["sender_reply_to"] = sender_reply_to.strip()
    for field, value in (
        ("subscribe_on_signup", subscribe_on_signup),
        ("show_header_icon", show_header_icon),
        ("show_header_title", show_header_title),
        ("show_header_name", show_header_name),
        ("show_feature_image", show_feature_image),
        ("show_badge", show_badge),
    ):
        if value is not None:
            payload[field] = value
    for field, value in (
        ("title_font_category", title_font_category),
        ("title_alignment", title_alignment),
        ("body_font_category", body_font_category),
    ):
        if value.strip():
            payload[field] = value.strip()

    ok, data = await _api_post(f"{ADMIN_BASE}/newsletters/", {"newsletters": [payload]})
    if not ok:
        return data
    created = _first_item(data, "newsletters") or {}
    return (f"Newsletter created: '{created.get('name', name)}' "
            f"(id={created.get('id') or created.get('uuid') or '?'}, "
            f"slug={created.get('slug', '?')}).")


@mcp.tool(annotations=_WRITE_ANN)
async def update_newsletter(
    newsletter_id: str,
    name: str = "",
    description: str = "",
    sender_name: str = "",
    sender_email: str = "",
    sender_reply_to: str = "",
    status: str = "",
    subscribe_on_signup: bool | None = None,
    show_header_icon: bool | None = None,
    show_header_title: bool | None = None,
    show_header_name: bool | None = None,
    title_font_category: str = "",
    title_alignment: str = "",
    show_feature_image: bool | None = None,
    body_font_category: str = "",
    show_badge: bool | None = None,
    sort_order: int | None = None,
    header_image: str = "",
    footer_content: str = "",
) -> str:
    """
    Update an existing newsletter — omit a field to leave it unchanged.

    IMPORTANT: Confirm with the user before calling.

    Args:
        newsletter_id:     Newsletter id, UUID, or slug (from list_newsletters).
        name:              New name.
        description:       New description.
        sender_name:       New sender name.
        sender_email:      New sender email.
        sender_reply_to:   New reply-to address.
        status:            'active' or 'inactive'.
        subscribe_on_signup:   Member sign-up toggle (omit = unchanged).
        show_header_icon / show_header_title / show_header_name:
                    Header display toggles (omit = unchanged).
        title_font_category: 'sans-serif' or 'serif'.
        title_alignment:    'left' or 'center'.
        show_feature_image: Feature image toggle.
        body_font_category: 'sans-serif' or 'serif'.
        show_badge:         "New" badge toggle.
        sort_order:        Ordering position in the newsletter list.
        header_image:      URL of a custom header image.
        footer_content:    HTML shown in the newsletter footer.
    """
    if not newsletter_id:
        return "newsletter_id is required."
    if status and status not in ("active", "inactive"):
        return "Invalid status. Use 'active' or 'inactive'."
    payload = {}
    for field, value in (
        ("name", name), ("description", description), ("sender_name", sender_name),
        ("sender_email", sender_email), ("sender_reply_to", sender_reply_to),
        ("title_font_category", title_font_category), ("title_alignment", title_alignment),
        ("body_font_category", body_font_category),
    ):
        if value.strip():
            payload[field] = value.strip()
    if status:
        payload["status"] = status
    for field, value in (
        ("subscribe_on_signup", subscribe_on_signup),
        ("show_header_icon", show_header_icon),
        ("show_header_title", show_header_title),
        ("show_header_name", show_header_name),
        ("show_feature_image", show_feature_image),
        ("show_badge", show_badge),
    ):
        if value is not None:
            payload[field] = value
    if sort_order is not None:
        payload["sort_order"] = sort_order
    if header_image.strip():
        payload["header_image"] = header_image.strip()
    if footer_content.strip():
        payload["footer_content"] = footer_content.strip()
    if not payload:
        return "Nothing to update — provide at least one field."

    ok, raw_id = await _resolve_raw_id("newsletters", newsletter_id)
    if not ok:
        return f"Error resolving newsletter id '{newsletter_id}': {raw_id}"
    ok, data = await _api_post(f"{ADMIN_BASE}/newsletters/{raw_id}/",
                               {"newsletters": [payload]}, method="PUT")
    if not ok:
        return data
    updated = _first_item(data, "newsletters") or {}
    return (f"Newsletter updated: '{updated.get('name', '?')}' "
            f"(id={updated.get('id') or newsletter_id}).")


@mcp.tool(annotations=_DELETE_ANN)
async def delete_newsletter(newsletter_id: str) -> str:
    """
    Permanently delete a newsletter. This cannot be undone — posts that
    referenced it fall back to the site's default newsletter. (The default
    newsletter itself cannot be deleted; Ghost rejects that with a 400.)

    IMPORTANT: Confirm the newsletter id and the deletion with the user
    before calling.

    Args:
        newsletter_id: Newsletter id, UUID, or slug (from list_newsletters).
    """
    if not newsletter_id:
        return "newsletter_id is required."
    denied = _delete_gate()
    if denied:
        return denied
    ok, raw_id = await _resolve_raw_id("newsletters", newsletter_id)
    if not ok:
        return f"Error resolving newsletter id '{newsletter_id}': {raw_id}"
    ok, data = await _api_delete(f"{ADMIN_BASE}/newsletters/{raw_id}/")
    if not ok:
        return data
    return f"Newsletter deleted (id={newsletter_id}). This cannot be undone."


# ===========================================================================
# TIERS (membership plans)
# ===========================================================================

@mcp.tool(annotations=_READ_ANN)
async def list_tiers(limit: int = _DEFAULT_LIMIT, page: int = 1, order: str = "created_at ASC") -> str:
    """
    List the site's membership tiers with prices (admin key required —
    tiers have no Content API surface).

    Args:
        limit:  Tiers per page (default 15, max 100).
        page:   Page number (1-based).
        order:  e.g. 'created_at ASC' (default), 'name ASC'.
    """
    problem = _admin_config_problem()
    if problem:
        return problem
    limit = max(1, min(int(limit), 100))
    page  = max(1, int(page))

    ok, data = await _api_get(f"{ADMIN_BASE}/tiers/",
                              params={"limit": limit, "page": page, "order": order,
                                      "include": "count"}, admin=True)
    if not ok:
        return data
    tiers = data.get("tiers") if isinstance(data, dict) else None
    tiers = tiers if isinstance(tiers, list) else []
    if not tiers:
        return "No tiers found."
    meta = data.get("meta") if isinstance(data, dict) else {}
    pag  = meta.get("pagination") if isinstance(meta, dict) else {}
    pag  = pag if isinstance(pag, dict) else {}

    lines = [f"Tiers (page {pag.get('page', page)}/{pag.get('page_count', '?')}, "
             f"total: {pag.get('total', '?')}):", ""]
    for t in tiers:
        count = t.get("count") if isinstance(t.get("count"), dict) else {}
        benefits = _truncate(", ".join(t.get("benefits") or []), 60)
        lines.append(f"  {t.get('name', '?')} ({t.get('slug', '?')})  "
                     f"id={t.get('id') or t.get('uuid') or '?'}  "
                     f"[{t.get('visibility', '?')}]  {_price_label(t)}  "
                     f"members={count.get('members', 0)}"
                     + (f"  — {benefits}" if benefits else ""))
    lines += ["", "Use these ids with get_tier / update_tier / delete_tier, "
              "and create_offer(tier_id=...) for discounts."]
    if pag.get("page_count", 1) > page:
        lines.append(f"Use list_tiers(page={page + 1}) for the next page.")
    return "\n".join(lines)


@mcp.tool(annotations=_READ_ANN)
async def get_tier(identifier: str) -> str:
    """
    Full metadata for ONE membership tier (by raw id, UUID, or slug).

    Args:
        identifier: Tier id, UUID, or slug (from list_tiers).
    """
    problem = _admin_config_problem()
    if problem:
        return problem
    if not identifier:
        return "identifier is required."
    ok, data = await _fetch_item(ADMIN_BASE, "tiers", identifier, True)
    if not ok:
        return f"Error looking up tier '{identifier}': {data}"
    t = _first_item(data, "tiers")
    if not t:
        return f"Tier '{identifier}' not found."

    cur = t.get("currency") or "USD"
    lines = [
        "=" * 64,
        f"Tier: {t.get('name', '(unnamed)')}  (id={t.get('uuid', '?')})",
        "=" * 64,
        f"Slug          : {t.get('slug', '?')}",
        f"Visibility    : {t.get('visibility', '?')}   (public = offered on the site, internal = hidden)",
        f"Monthly price : {t.get('monthly_price', '?')} cents ({cur})",
        f"Yearly price  : {t.get('yearly_price', '?')} cents ({cur})",
        f"Welcome URL   : {t.get('welcome_page_url') or '-'}",
        f"Description   : {_truncate(t.get('description') or '', 300) or '-'}",
        f"Benefits      : {_truncate(', '.join(t.get('benefits') or []), 300) or '-'}",
        f"Created       : {t.get('created_at', '?')}",
        f"Updated       : {t.get('updated_at', '?')}",
        "",
        "Use this id with create_offer(tier_id=...) / update_tier / delete_tier.",
    ]
    return "\n".join(lines)


@mcp.tool(annotations=_WRITE_ANN)
async def create_tier(
    name: str,
    description: str = "",
    welcome_page_url: str = "",
    visibility: str = "public",
    monthly_price: str = "",
    yearly_price: str = "",
    currency: str = "USD",
    benefits: list | None = None,
) -> str:
    """
    Create a new membership tier.

    IMPORTANT: Confirm with the user before calling — a public tier is
    immediately offered to subscribers.

    Args:
        name:             Tier name (required, unique).
        description:      Shown to subscribers.
        welcome_page_url: URL the member is taken to after subscribing.
        visibility:       'public' (default) or 'internal' (hidden from the site).
        monthly_price:    Monthly price in CENTS as a string, e.g. '1000' = $10.00
                          (omit for a free monthly price).
        yearly_price:     Yearly price in cents as a string, e.g. '9600' = $96.00.
        currency:         ISO 4217 code, default USD.
        benefits:         List of benefit strings shown on the pricing page.
    """
    if not name.strip():
        return "name is required."
    if visibility not in ("public", "internal"):
        return "Invalid visibility. Use 'public' or 'internal'."
    for label, value in (("monthly_price", monthly_price), ("yearly_price", yearly_price)):
        if value.strip() and not value.strip().isdigit():
            return (f"{label} must be a non-negative integer in cents "
                    "(e.g. '1000' for $10.00).")
    payload = {"name": name.strip(), "visibility": visibility}
    if description.strip():
        payload["description"] = description.strip()
    if welcome_page_url.strip():
        payload["welcome_page_url"] = welcome_page_url.strip()
    if monthly_price.strip():
        payload["monthly_price"] = monthly_price.strip()
    if yearly_price.strip():
        payload["yearly_price"] = yearly_price.strip()
    if currency.strip():
        payload["currency"] = currency.strip().upper()
    if benefits:
        payload["benefits"] = [b.strip() for b in benefits if b and b.strip()]

    ok, data = await _api_post(f"{ADMIN_BASE}/tiers/", {"tiers": [payload]})
    if not ok:
        return data
    created = _first_item(data, "tiers") or {}
    return (f"Tier created: '{created.get('name', name)}' "
            f"(id={created.get('id') or created.get('uuid') or '?'}, "
            f"slug={created.get('slug', '?')}).")


@mcp.tool(annotations=_WRITE_ANN)
async def update_tier(
    tier_id: str,
    name: str = "",
    description: str = "",
    welcome_page_url: str = "",
    visibility: str = "",
    monthly_price: str = "",
    yearly_price: str = "",
    currency: str = "",
    benefits: list | None = None,
) -> str:
    """
    Update an existing membership tier — omit a field to leave it unchanged.

    IMPORTANT: Confirm with the user before calling — price changes affect
    the next billing cycle for subscribers.

    Args:
        tier_id:          Tier id, UUID, or slug (from list_tiers).
        name:             New name.
        description:      New description.
        welcome_page_url: New welcome URL.
        visibility:       'public' or 'internal'.
        monthly_price:    New monthly price in cents as a string ('' = unchanged;
                          pass '0' for free).
        yearly_price:     New yearly price in cents as a string.
        currency:         New ISO 4217 code.
        benefits:         REPLACES all benefits (omit = unchanged, empty
                          list = clear all).
    """
    if not tier_id:
        return "tier_id is required."
    if visibility and visibility not in ("public", "internal"):
        return "Invalid visibility. Use 'public' or 'internal'."
    payload = {}
    for field, value in (
        ("name", name), ("description", description), ("welcome_page_url", welcome_page_url),
    ):
        if value.strip():
            payload[field] = value.strip()
    if visibility:
        payload["visibility"] = visibility
    for label, value in (("monthly_price", monthly_price), ("yearly_price", yearly_price)):
        if value:
            v = value.strip()
            if not v.isdigit():
                return f"{label} must be a non-negative integer in cents (e.g. '0' for free)."
            payload[label] = v
    if currency.strip():
        payload["currency"] = currency.strip().upper()
    if benefits is not None:
        payload["benefits"] = [b.strip() for b in benefits if b and b.strip()]
    if not payload:
        return "Nothing to update — provide at least one field."

    ok, raw_id = await _resolve_raw_id("tiers", tier_id)
    if not ok:
        return f"Error resolving tier id '{tier_id}': {raw_id}"
    ok, data = await _api_post(f"{ADMIN_BASE}/tiers/{raw_id}/", {"tiers": [payload]},
                               method="PUT")
    if not ok:
        return data
    updated = _first_item(data, "tiers") or {}
    return f"Tier updated: '{updated.get('name', '?')}' (id={updated.get('id') or tier_id})."


@mcp.tool(annotations=_DELETE_ANN)
async def delete_tier(tier_id: str) -> str:
    """
    Permanently delete a membership tier. This cannot be undone — tiers
    with active paid subscriptions may be rejected by Ghost until the
    subscriptions are moved or cancelled.

    IMPORTANT: Confirm the tier id and the deletion with the user before
    calling.

    Args:
        tier_id: Tier id, UUID, or slug (from list_tiers).
    """
    if not tier_id:
        return "tier_id is required."
    denied = _delete_gate()
    if denied:
        return denied
    ok, raw_id = await _resolve_raw_id("tiers", tier_id)
    if not ok:
        return f"Error resolving tier id '{tier_id}': {raw_id}"
    ok, data = await _api_delete(f"{ADMIN_BASE}/tiers/{raw_id}/")
    if not ok:
        return data
    return f"Tier deleted (id={tier_id}). This cannot be undone."


# ===========================================================================
# OFFERS (membership discounts)
#
# Offers have no slugs — they are referenced by raw id, UUID, or CODE, so
# the generic slug-fallback resolution in _fetch_item/_resolve_raw_id does
# not apply. The local helpers below use filter=code: for unknowns.
# ===========================================================================

async def _fetch_offer(identifier: str):
    """Fetch one offer by raw id, UUID, or code."""
    if _UUID_RE.match(identifier):
        return await _api_get(f"{ADMIN_BASE}/offers/",
                              params={"filter": f"uuid:{identifier}"})
    if re.fullmatch(r"[0-9a-f]{24}", identifier, flags=re.IGNORECASE):
        return await _api_get(f"{ADMIN_BASE}/offers/{identifier}/")
    return await _api_get(f"{ADMIN_BASE}/offers/",
                          params={"filter": f"code:{identifier}"})


async def _resolve_offer_id(identifier: str):
    """Resolve an offer identifier to the RAW ObjectID Ghost 6 path params
    require (raw ids pass through; UUIDs and codes are looked up)."""
    if re.fullmatch(r"[0-9a-f]{24}", identifier, flags=re.IGNORECASE):
        return True, identifier
    ok, data = await _fetch_offer(identifier)
    if not ok:
        return False, data
    item = _first_item(data, "offers")
    if not item or not item.get("id"):
        return False, f"'{identifier}' not found (checked as raw id, UUID, and code)."
    return True, item["id"]


@mcp.tool(annotations=_READ_ANN)
async def list_offers(
    limit: int = _DEFAULT_LIMIT,
    page: int = 1,
    order: str = "created_at DESC",
    status: str = "",
) -> str:
    """
    List the site's offers/discounts for membership tiers (admin key
    required).

    Args:
        limit:  Offers per page (default 15, max 100).
        page:   Page number (1-based).
        order:  e.g. 'created_at DESC' (default), 'name ASC'.
        status: 'active', 'draft', or 'archived' (omit = all).
    """
    problem = _admin_config_problem()
    if problem:
        return problem
    if status and status not in ("active", "draft", "archived"):
        return "Invalid status. Use 'active', 'draft', or 'archived'."
    limit = max(1, min(int(limit), 100))
    page  = max(1, int(page))

    params = {"limit": limit, "page": page, "order": order, "include": "tiers"}
    if status:
        params["filter"] = f"status:{status}"
    ok, data = await _api_get(f"{ADMIN_BASE}/offers/", params=params, admin=True)
    if not ok:
        return data
    offers = data.get("offers") if isinstance(data, dict) else None
    offers = offers if isinstance(offers, list) else []
    if not offers:
        return "No offers found."
    meta = data.get("meta") if isinstance(data, dict) else {}
    pag  = meta.get("pagination") if isinstance(meta, dict) else {}
    pag  = pag if isinstance(pag, dict) else {}

    lines = [f"Offers (page {pag.get('page', page)}/{pag.get('page_count', '?')}, "
             f"total: {pag.get('total', '?')}):", ""]
    for o in offers:
        tier = o.get("tier") if isinstance(o.get("tier"), dict) else {}
        tier_name = tier.get("name") or tier.get("id") or o.get("tier_id") or "?"
        lines.append(f"  code={o.get('code', '?')}  {_truncate(str(o.get('name', '?')), 30)}  "
                     f"[{o.get('status', '?')}]  {o.get('type', '?')} ({o.get('cadence', '?')})  "
                     f"{_discount_label(o)}  tier={tier_name}  "
                     f"id={o.get('id') or o.get('uuid') or '?'}")
    lines += ["", "Use get_offer(<code-or-id>) for details; "
              "create_offer(tier_id=...) with a tier id from list_tiers."]
    if pag.get("page_count", 1) > page:
        lines.append(f"Use list_offers(page={page + 1}) for the next page.")
    return "\n".join(lines)


@mcp.tool(annotations=_READ_ANN)
async def get_offer(identifier: str) -> str:
    """
    Full metadata for ONE offer (by raw id, UUID, or offer CODE).

    Args:
        identifier: Offer id, UUID, or code (from list_offers).
    """
    problem = _admin_config_problem()
    if problem:
        return problem
    if not identifier:
        return "identifier is required."
    ok, data = await _fetch_offer(identifier)
    if not ok:
        return f"Error looking up offer '{identifier}': {data}"
    o = _first_item(data, "offers")
    if not o:
        return f"Offer '{identifier}' not found (checked raw id, UUID, and code)."

    tier = o.get("tier") if isinstance(o.get("tier"), dict) else {}
    lines = [
        "=" * 64,
        f"Offer: {o.get('name', '(unnamed)')}  (id={o.get('uuid', '?')})",
        "=" * 64,
        f"Code            : {o.get('code', '?')}",
        f"Status          : {o.get('status', '?')}",
        f"Type            : {o.get('type', '?')}   ({o.get('cadence', '?')})",
        f"Discount        : {_discount_label(o)}  (raw amount: {o.get('amount', '?')})",
        f"Duration        : {o.get('duration', '?')} days"
        + (f"  ({o.get('duration_in_months')} months)" if o.get("duration_in_months") else ""),
        f"Tier            : {tier.get('name') or o.get('tier_id') or '?'} "
        f"(id={tier.get('id') or o.get('tier_id') or '?'})",
        f"Display title   : {o.get('display_title') or '-'}",
        f"Display desc    : {_truncate(o.get('display_description') or '', 200) or '-'}",
        f"Created         : {o.get('created_at', '?')}",
        f"Updated         : {o.get('updated_at', '?')}",
        "",
        "Use this id/code with update_offer / delete_offer.",
    ]
    return "\n".join(lines)


@mcp.tool(annotations=_WRITE_ANN)
async def create_offer(
    name: str,
    code: str,
    type: str,
    tier_id: str,
    amount: float,
    cadence: str = "once",
    duration: int = 0,
    duration_in_months: int | None = None,
    currency: str = "",
    display_title: str = "",
    display_description: str = "",
) -> str:
    """
    Create a new offer (discount) for a membership tier.

    IMPORTANT: Confirm with the user before calling — an active offer's
    code is redeemable by members immediately.

    Args:
        name:      Offer name (required).
        code:      The coupon code members redeem (required, unique).
        type:      'percent' (percentage off), 'amount' (fixed amount off),
                   'one_time' (one-time discount), 'tier_free_trial'
                   (free trial of the tier), or 'tier_discount'.
        tier_id:   Tier the offer applies to (from list_tiers).
        amount:    The discount amount — for 'percent' offers the percent
                   value (e.g. 10 = 10%); for money offers, cents
                   (e.g. 1000 = $10.00); 0 for a free trial.
        cadence:   'once' (default), 'monthly', or 'yearly'.
        duration:  Validity / trial length in days (0 = none).
        duration_in_months: Trial length in months (free trials).
        currency:  ISO 4217 code for money offers (default: the tier's).
        display_title:       Shown to members where the offer appears.
        display_description: Shown to members where the offer appears.
    """
    if not name.strip():
        return "name is required."
    if not code.strip():
        return "code is required."
    if type not in ("one_time", "percent", "amount", "tier_free_trial", "tier_discount"):
        return ("Invalid type. Use 'one_time', 'percent', 'amount', "
                "'tier_free_trial', or 'tier_discount'.")
    if cadence not in ("once", "monthly", "yearly"):
        return "Invalid cadence. Use 'once', 'monthly', or 'yearly'."
    if not tier_id.strip():
        return "tier_id is required (from list_tiers)."
    if amount is None or amount < 0:
        return "amount must be a non-negative number."
    if type in ("percent", "amount", "tier_discount") and amount == 0:
        return "amount must be positive for this offer type."

    payload = {"name": name.strip(), "code": code.strip(), "type": type,
               "cadence": cadence, "duration": int(duration),
               "amount": amount, "tier_id": tier_id.strip()}
    if duration_in_months is not None:
        payload["duration_in_months"] = int(duration_in_months)
    if currency.strip():
        payload["currency"] = currency.strip().upper()
    if display_title.strip():
        payload["display_title"] = display_title.strip()
    if display_description.strip():
        payload["display_description"] = display_description.strip()

    ok, data = await _api_post(f"{ADMIN_BASE}/offers/", {"offers": [payload]})
    if not ok:
        return data
    created = _first_item(data, "offers") or {}
    return (f"Offer created: '{created.get('name', name)}' code={created.get('code', code)} "
            f"(id={created.get('id') or created.get('uuid') or '?'}, "
            f"status={created.get('status', 'active')}).")


@mcp.tool(annotations=_WRITE_ANN)
async def update_offer(
    offer_id: str,
    name: str = "",
    code: str = "",
    display_title: str = "",
    display_description: str = "",
) -> str:
    """
    Update an existing offer — the API exposes name, code, and the
    display fields; to change the discount itself, delete and recreate
    the offer. Omit a field to leave it unchanged.

    IMPORTANT: Confirm with the user before calling.

    Args:
        offer_id:            Offer id, UUID, or code (from list_offers).
        name:                New name.
        code:                New coupon code.
        display_title:       New display title.
        display_description: New display description.
    """
    if not offer_id:
        return "offer_id is required."
    payload = {}
    for field, value in (
        ("name", name), ("code", code),
        ("display_title", display_title), ("display_description", display_description),
    ):
        if value.strip():
            payload[field] = value.strip()
    if not payload:
        return "Nothing to update — provide name, code, and/or display fields."

    ok, raw_id = await _resolve_offer_id(offer_id)
    if not ok:
        return f"Error resolving offer '{offer_id}': {raw_id}"
    ok, data = await _api_post(f"{ADMIN_BASE}/offers/{raw_id}/", {"offers": [payload]},
                               method="PUT")
    if not ok:
        return data
    updated = _first_item(data, "offers") or {}
    return f"Offer updated: '{updated.get('name', '?')}' (code={updated.get('code') or offer_id})."


@mcp.tool(annotations=_DELETE_ANN)
async def delete_offer(offer_id: str) -> str:
    """
    Permanently delete an offer. This cannot be undone — members holding
    the code lose the discount.

    IMPORTANT: Confirm the offer and the deletion with the user before
    calling.

    Args:
        offer_id: Offer id, UUID, or code (from list_offers).
    """
    if not offer_id:
        return "offer_id is required."
    denied = _delete_gate()
    if denied:
        return denied
    ok, raw_id = await _resolve_offer_id(offer_id)
    if not ok:
        return f"Error resolving offer '{offer_id}': {raw_id}"
    ok, data = await _api_delete(f"{ADMIN_BASE}/offers/{raw_id}/")
    if not ok:
        return data
    return f"Offer deleted ({offer_id}). This cannot be undone."


# ===========================================================================
# MEMBERS (newsletter subscribers)
#
# Members have no slugs — they are referenced by raw id, UUID, or EMAIL,
# so the local helpers below use filter=email: for unknowns.
# ===========================================================================

async def _fetch_member(identifier: str):
    """Fetch one member by raw id, UUID, or email address."""
    if _UUID_RE.match(identifier):
        return await _api_get(f"{ADMIN_BASE}/members/",
                              params={"filter": f"uuid:{identifier}"})
    if re.fullmatch(r"[0-9a-f]{24}", identifier, flags=re.IGNORECASE):
        return await _api_get(f"{ADMIN_BASE}/members/{identifier}/")
    return await _api_get(f"{ADMIN_BASE}/members/",
                          params={"filter": f"email:{identifier}"})


async def _resolve_member_id(identifier: str):
    """Resolve a member identifier to the RAW ObjectID Ghost 6 path params
    require (raw ids pass through; UUIDs and emails are looked up)."""
    if re.fullmatch(r"[0-9a-f]{24}", identifier, flags=re.IGNORECASE):
        return True, identifier
    ok, data = await _fetch_member(identifier)
    if not ok:
        return False, data
    item = _first_item(data, "members")
    if not item or not item.get("id"):
        return False, f"'{identifier}' not found (checked as raw id, UUID, and email)."
    return True, item["id"]


@mcp.tool(annotations=_READ_ANN)
async def list_members(
    limit: int = _DEFAULT_LIMIT,
    page: int = 1,
    order: str = "created_at DESC",
    label: str = "",
    status: str = "",
) -> str:
    """
    List the site's newsletter members (admin key required).

    Args:
        limit:  Members per page (default 15, max 100).
        page:   Page number (1-based).
        order:  e.g. 'created_at DESC' (default), 'subscribed_at DESC'.
        label:  Filter by label name (e.g. 'customers').
        status: 'free' or 'paid'.
    """
    problem = _admin_config_problem()
    if problem:
        return problem
    if status and status not in ("free", "paid"):
        return "Invalid status. Use 'free' or 'paid'."
    limit = max(1, min(int(limit), 100))
    page  = max(1, int(page))

    clauses = []
    if label.strip():
        clauses.append(f"label:{label.strip()}")
    if status:
        clauses.append(f"status:{status}")
    params = {"limit": limit, "page": page, "order": order, "include": "labels"}
    if clauses:
        params["filter"] = " ".join(clauses)
    ok, data = await _api_get(f"{ADMIN_BASE}/members/", params=params, admin=True)
    if not ok:
        return data
    members = data.get("members") if isinstance(data, dict) else None
    members = members if isinstance(members, list) else []
    if not members:
        return "No members found."
    meta = data.get("meta") if isinstance(data, dict) else {}
    pag  = meta.get("pagination") if isinstance(meta, dict) else {}
    pag  = pag if isinstance(pag, dict) else {}

    lines = [f"Members (page {pag.get('page', page)}/{pag.get('page_count', '?')}, "
             f"total: {pag.get('total', '?')}):"]
    if clauses:
        lines[0] += f"  [filter: {' '.join(clauses)}]"
    lines.append("")
    for m in members:
        labels = _tag_names(m.get("labels"))
        lines.append(f"  {m.get('email', '?')}  {str(m.get('name') or '-')[:20]:<20}  "
                     f"[{m.get('status', '?')}]  {labels or '-'}  "
                     f"id={m.get('id') or m.get('uuid') or '?'}")
    lines += ["", "Use get_member(<email-or-id>) for details; the email works with "
              "update_member / delete_member."]
    if pag.get("page_count", 1) > page:
        lines.append(f"Use list_members(page={page + 1}) for the next page.")
    return "\n".join(lines)


@mcp.tool(annotations=_READ_ANN)
async def get_member(identifier: str) -> str:
    """
    Full details for ONE member (by EMAIL address, raw id, or UUID).

    Args:
        identifier: Member email (from list_members), raw id, or UUID.
    """
    problem = _admin_config_problem()
    if problem:
        return problem
    if not identifier:
        return "identifier is required."
    ok, data = await _fetch_member(identifier)
    if not ok:
        return f"Error looking up member '{identifier}': {data}"
    m = _first_item(data, "members")
    if not m:
        return f"Member '{identifier}' not found (checked raw id, UUID, and email)."

    lines = [
        "=" * 64,
        f"Member: {m.get('name') or '(no name)'} <{m.get('email', '?')}>  "
        f"(id={m.get('uuid', '?')})",
        "=" * 64,
        f"Status    : {m.get('status', '?')}",
        f"Subscribed: {'yes' if m.get('subscribed') else 'no'}",
        f"Labels    : {_tag_names(m.get('labels')) or '-'}",
        f"Note      : {_truncate(m.get('note') or '', 300) or '-'}",
        f"Created   : {m.get('created_at', '?')}",
        f"Updated   : {m.get('updated_at', '?')}",
        "",
        "Use the email or id with update_member / delete_member.",
    ]
    return "\n".join(lines)


@mcp.tool(annotations=_WRITE_ANN)
async def create_member(
    email: str,
    name: str = "",
    note: str = "",
    labels: list | None = None,
    newsletters: list | None = None,
) -> str:
    """
    Sign up a member (add a subscriber to the newsletter).

    IMPORTANT: Confirm with the user before calling — this is a real
    subscriber on the site.

    Args:
        email:       Member email (required).
        name:        Display name.
        note:        Internal note (only visible in the Ghost admin).
        labels:      List of label names (labels are created if they do
                     not exist yet).
        newsletters: List of newsletter ids (from list_newsletters) to
                     subscribe to — omit for the site default.
    """
    if not email.strip() or "@" not in email:
        return "A valid email address is required."
    payload = {"email": email.strip()}
    if name.strip():
        payload["name"] = name.strip()
    if note.strip():
        payload["note"] = note.strip()
    if labels:
        payload["labels"] = [{"name": l.strip()} for l in labels if l and l.strip()]
    if newsletters:
        payload["newsletters"] = [{"id": n.strip()} for n in newsletters if n and n.strip()]

    ok, data = await _api_post(f"{ADMIN_BASE}/members/", {"members": [payload]})
    if not ok:
        return data
    created = _first_item(data, "members") or {}
    return (f"Member signed up: {created.get('email', email)} "
            f"(id={created.get('id') or created.get('uuid') or '?'}, "
            f"status={created.get('status', 'free')}).")


@mcp.tool(annotations=_WRITE_ANN)
async def update_member(
    member_id: str,
    name: str = "",
    note: str = "",
    labels: list | None = None,
    newsletters: list | None = None,
) -> str:
    """
    Update an existing member. A member's EMAIL CANNOT be changed — to
    fix an email typo, delete the member and sign them up again.

    IMPORTANT: Confirm with the user before calling.

    Args:
        member_id:   Member email, raw id, or UUID (from list_members / get_member).
        name:        New display name.
        note:        New internal note.
        labels:      REPLACES all labels (omit = unchanged, empty list =
                     clear all).
        newsletters: REPLACES all newsletter subscriptions with the given
                     ids (omit = unchanged, empty list = unsubscribe from
                     all).
    """
    if not member_id:
        return "member_id is required."
    payload = {}
    if name.strip():
        payload["name"] = name.strip()
    if note.strip():
        payload["note"] = note.strip()
    if labels is not None:
        payload["labels"] = [{"name": l.strip()} for l in labels if l and l.strip()]
    if newsletters is not None:
        payload["newsletters"] = [{"id": n.strip()} for n in newsletters if n and n.strip()]
    if not payload:
        return "Nothing to update — provide name, note, labels, and/or newsletters."

    ok, raw_id = await _resolve_member_id(member_id)
    if not ok:
        return f"Error resolving member '{member_id}': {raw_id}"
    ok, data = await _api_post(f"{ADMIN_BASE}/members/{raw_id}/", {"members": [payload]},
                               method="PUT")
    if not ok:
        return data
    updated = _first_item(data, "members") or {}
    return (f"Member updated: {updated.get('email') or member_id} "
            f"(id={updated.get('id') or member_id}).")


@mcp.tool(annotations=_DELETE_ANN)
async def delete_member(member_id: str) -> str:
    """
    Remove (unsubscribe) a member. This cannot be undone — the member must
    sign up again, and any paid tier subscription ends with the deletion.

    IMPORTANT: Confirm the member and the deletion with the user before
    calling.

    Args:
        member_id: Member email, raw id, or UUID (from list_members).
    """
    if not member_id:
        return "member_id is required."
    denied = _delete_gate()
    if denied:
        return denied
    ok, raw_id = await _resolve_member_id(member_id)
    if not ok:
        return f"Error resolving member '{member_id}': {raw_id}"
    ok, data = await _api_delete(f"{ADMIN_BASE}/members/{raw_id}/")
    if not ok:
        return data
    return f"Member removed ({member_id}). This cannot be undone."


# ===========================================================================
# WEBHOOKS
#
# Webhooks have no slugs or codes — raw id or UUID only.
# ===========================================================================

async def _fetch_webhook(identifier: str):
    """Fetch one webhook by raw id or UUID."""
    if _UUID_RE.match(identifier):
        return await _api_get(f"{ADMIN_BASE}/webhooks/",
                              params={"filter": f"uuid:{identifier}"})
    if re.fullmatch(r"[0-9a-f]{24}", identifier, flags=re.IGNORECASE):
        return await _api_get(f"{ADMIN_BASE}/webhooks/{identifier}/")
    return False, ("Webhooks are referenced by their raw id or UUID (from "
                   f"list_webhooks) — '{identifier}' is neither.")


async def _resolve_webhook_id(identifier: str):
    """Resolve a webhook identifier to the RAW ObjectID Ghost 6 path params
    require (raw ids pass through; UUIDs are looked up)."""
    if re.fullmatch(r"[0-9a-f]{24}", identifier, flags=re.IGNORECASE):
        return True, identifier
    ok, data = await _fetch_webhook(identifier)
    if not ok:
        return False, data
    item = _first_item(data, "webhooks")
    if not item or not item.get("id"):
        return False, f"'{identifier}' not found (webhooks take their raw id or UUID)."
    return True, item["id"]


@mcp.tool(annotations=_READ_ANN)
async def list_webhooks(limit: int = _DEFAULT_LIMIT, page: int = 1) -> str:
    """
    List the site's webhooks (admin key required — the signing secret is
    intentionally NOT shown here; use get_webhook when you need it).

    Args:
        limit: Webhooks per page (default 15, max 100).
        page:  Page number (1-based).
    """
    problem = _admin_config_problem()
    if problem:
        return problem
    limit = max(1, min(int(limit), 100))
    page  = max(1, int(page))

    ok, data = await _api_get(f"{ADMIN_BASE}/webhooks/",
                              params={"limit": limit, "page": page}, admin=True)
    if not ok:
        return data
    webhooks = data.get("webhooks") if isinstance(data, dict) else None
    webhooks = webhooks if isinstance(webhooks, list) else []
    if not webhooks:
        return "No webhooks found."
    meta = data.get("meta") if isinstance(data, dict) else {}
    pag  = meta.get("pagination") if isinstance(meta, dict) else {}
    pag  = pag if isinstance(pag, dict) else {}

    lines = [f"Webhooks (page {pag.get('page', page)}/{pag.get('page_count', '?')}, "
             f"total: {pag.get('total', '?')}):", ""]
    for w in webhooks:
        lines.append(f"  {w.get('event', '?')}  ->  {w.get('target_url', '?')}  "
                     f"[{w.get('name') or '-'}]  "
                     f"id={w.get('id') or w.get('uuid') or '?'}"
                     + (f"  api={w.get('api_version')}" if w.get("api_version") else ""))
    lines += ["", "Use these ids with get_webhook / update_webhook / delete_webhook."]
    if pag.get("page_count", 1) > page:
        lines.append(f"Use list_webhooks(page={page + 1}) for the next page.")
    return "\n".join(lines)


@mcp.tool(annotations=_READ_ANN)
async def get_webhook(webhook_id: str) -> str:
    """
    Full details for ONE webhook, including its signing secret (verify
    incoming payloads with it; treat it like a credential — never post it
    to chat or a public place).

    Args:
        webhook_id: Webhook raw id or UUID (from list_webhooks).
    """
    problem = _admin_config_problem()
    if problem:
        return problem
    if not webhook_id:
        return "webhook_id is required."
    ok, data = await _fetch_webhook(webhook_id)
    if not ok:
        return f"Error looking up webhook '{webhook_id}': {data}"
    w = _first_item(data, "webhooks")
    if not w:
        return f"Webhook '{webhook_id}' not found (raw id or UUID from list_webhooks)."

    lines = [
        "=" * 64,
        f"Webhook: {w.get('name') or w.get('event', '?')}  (id={w.get('uuid', '?')})",
        "=" * 64,
        f"Event       : {w.get('event', '?')}",
        f"Target URL  : {w.get('target_url', '?')}",
        f"API version : {w.get('api_version') or 'default (v5)'}",
        f"Integration : {w.get('integration_id') or '-'}",
        f"Created     : {w.get('created_at', '?')}",
        f"Updated     : {w.get('updated_at', '?')}",
    ]
    secret = w.get("secret")
    if secret:
        lines.append(f"Secret      : {secret}   <- use for HMAC verification; treat like a credential")
    else:
        lines.append("Secret      : (not returned by the API — only the create response shows it)")
    lines.append("")
    lines.append("Use this id with update_webhook / delete_webhook.")
    return "\n".join(lines)


@mcp.tool(annotations=_WRITE_ANN)
async def create_webhook(
    event: str,
    target_url: str,
    name: str = "",
    secret: str = "",
    api_version: str = "",
    integration_id: str = "",
) -> str:
    """
    Create a new webhook.

    IMPORTANT: Confirm the event + target URL with the user before
    calling — the webhook will POST to that URL on every matching event.

    Args:
        event:           Webhook event, e.g. 'post.published', 'post.added',
                         'post.edited', 'post.deleted', 'page.published',
                         'member.signed_up', 'member.deleted',
                         'member.subscription.created',
                         'member.subscription.edited'.
        target_url:      URL that receives the webhook POSTs.
        name:            Optional friendly name.
        secret:          Signing secret (omit = Ghost generates one — it is
                         shown ONCE in the response below, so store it).
        api_version:     'v5' (default) or 'v4' — which API version the
                         payload follows.
        integration_id:  Only needed for user-authenticated webhooks (an
                         admin key does not need it).
    """
    if not event.strip():
        return "event is required."
    url = target_url.strip()
    if not url or not url.lower().startswith(("http://", "https://")):
        return "target_url must be a full http(s) URL."
    payload = {"event": event.strip(), "target_url": url}
    if name.strip():
        payload["name"] = name.strip()
    if secret.strip():
        payload["secret"] = secret.strip()
    if api_version.strip():
        payload["api_version"] = api_version.strip()
    if integration_id.strip():
        payload["integration_id"] = integration_id.strip()

    ok, data = await _api_post(f"{ADMIN_BASE}/webhooks/", {"webhooks": [payload]})
    if not ok:
        return data
    created = _first_item(data, "webhooks") or {}
    result = (f"Webhook created: {created.get('event', event)} -> {url} "
              f"(id={created.get('id') or created.get('uuid') or '?'})")
    sec = created.get("secret")
    if sec:
        result += f"\nSecret (shown once — store it now): {sec}"
    return result


@mcp.tool(annotations=_WRITE_ANN)
async def update_webhook(
    webhook_id: str,
    event: str = "",
    target_url: str = "",
    name: str = "",
    api_version: str = "",
) -> str:
    """
    Update an existing webhook — the signing secret CANNOT be changed,
    only the event, target URL, name, and API version. Omit a field to
    leave it unchanged.

    IMPORTANT: Confirm with the user before calling — the new target URL
    receives the webhook POSTs immediately.

    Args:
        webhook_id: Webhook raw id or UUID (from list_webhooks).
        event:      New event.
        target_url: New URL.
        name:       New name.
        api_version: 'v5' or 'v4'.
    """
    if not webhook_id:
        return "webhook_id is required."
    payload = {}
    if event.strip():
        payload["event"] = event.strip()
    if target_url.strip():
        payload["target_url"] = target_url.strip()
    if name.strip():
        payload["name"] = name.strip()
    if api_version.strip():
        payload["api_version"] = api_version.strip()
    if not payload:
        return "Nothing to update — provide event, target_url, name, and/or api_version."

    ok, raw_id = await _resolve_webhook_id(webhook_id)
    if not ok:
        return f"Error resolving webhook '{webhook_id}': {raw_id}"
    ok, data = await _api_post(f"{ADMIN_BASE}/webhooks/{raw_id}/", {"webhooks": [payload]},
                               method="PUT")
    if not ok:
        return data
    updated = _first_item(data, "webhooks") or {}
    return (f"Webhook updated: {updated.get('event', '?')} -> {updated.get('target_url', '?')} "
            f"(id={updated.get('id') or webhook_id}).")


@mcp.tool(annotations=_DELETE_ANN)
async def delete_webhook(webhook_id: str) -> str:
    """
    Permanently delete a webhook. This cannot be undone — the target stops
    receiving events.

    IMPORTANT: Confirm the webhook and the deletion with the user before
    calling.

    Args:
        webhook_id: Webhook raw id or UUID (from list_webhooks).
    """
    if not webhook_id:
        return "webhook_id is required."
    denied = _delete_gate()
    if denied:
        return denied
    ok, raw_id = await _resolve_webhook_id(webhook_id)
    if not ok:
        return f"Error resolving webhook '{webhook_id}': {raw_id}"
    ok, data = await _api_delete(f"{ADMIN_BASE}/webhooks/{raw_id}/")
    if not ok:
        return data
    return f"Webhook deleted (id={webhook_id}). This cannot be undone."


# ===========================================================================
# PROMPTS (MCP prompts — model-side helpers, listed via list_prompts)
# ===========================================================================

@mcp.prompt()
async def summarize_post(post_id: str):
    """
    Load a Ghost post and ask the model to summarize it. Takes the post
    id, UUID, or slug (the same identifiers get_post accepts).
    """
    admin = _mode() == "admin"
    base  = ADMIN_BASE if admin else CONTENT_BASE
    ok, data = await _fetch_item(base, "posts", post_id, admin,
                                 formats="html" if admin else None)
    if not ok:
        return [{"role": "user",
                 "content": f"Could not load post '{post_id}': {data}"}]
    post = _first_item(data, "posts")
    if not post:
        return [{"role": "user",
                 "content": f"Post '{post_id}' not found."}]
    excerpt = _truncate(
        _strip_html(post.get("custom_excerpt") or post.get("excerpt") or ""), 300)
    body = _truncate(_strip_html(post.get("html")), 3000)
    content = (
        "Summarize the following Ghost post in 3-5 sentences, then list the "
        "key points as bullets.\n\n"
        f"Title: {post.get('title', '(untitled)')}\n"
        f"Excerpt: {excerpt or '-'}\n"
        f"Content preview (first 3000 chars):\n{body}"
    )
    return [{"role": "user", "content": content}]


@mcp.prompt()
def newsletter_draft(topic: str, key_points: list | None = None):
    """
    Prime the model to draft a Ghost-compatible HTML post on a topic, for
    creating as a DRAFT via create_post(html=...). Takes the topic and
    optional key points to cover.
    """
    points = "\n".join(f"- {k.strip()}" for k in (key_points or []) if k and k.strip()) \
        or "- (none given — choose sensible ones from the topic)"
    return [
        {"role": "system",
         "content": (
             "You are drafting a blog post for a Ghost site. Write the body as "
             "clean Ghost-compatible HTML: <h2> section headings, <p> paragraphs, "
             "<ul>/<ol> lists, <strong>/<em> emphasis, <blockquote> pull quotes. "
             "No <script> tags, no inline event handlers. Keep it 300-600 words "
             "unless told otherwise. Output ONLY the HTML, nothing else.")},
        {"role": "user",
         "content": (
             f"Draft a post on the topic: {topic}\n\n"
             f"Key points to cover:\n{points}\n\n"
             "After drafting, show the HTML to the user for approval, then create "
             "it as a DRAFT with create_post(html=...) — never publish without "
             "the user's confirmation.")},
    ]



# ===========================================================================
# Entry point
# ===========================================================================
def _token_auth(app):
    """ASGI wrapper enforcing `Authorization: Bearer <MCP_AUTH_TOKEN>` on
    every HTTP request. Installed only when a token is configured."""
    expected = ("Bearer " + _auth_token).encode()

    async def _reject(send):
        body = json.dumps({"error": "unauthorized"}).encode()
        await send({"type": "http.response.start", "status": 401,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length",
                                 str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

    async def wrapped(scope, receive, send):
        if scope["type"] == "http":
            supplied = next((v for k, v in scope.get("headers", [])
                             if k == b"authorization"), b"")
            if not hmac.compare_digest(supplied, expected):
                await _reject(send)
                return
        await app(scope, receive, send)

    return wrapped


if __name__ == "__main__":
    if _transport == "stdio":
        mcp.run(transport="stdio")
    else:
        import uvicorn

        app = (mcp.streamable_http_app() if _transport == "streamable-http"
               else mcp.sse_app())
        if _auth_token:
            app = _token_auth(app)
        uvicorn.run(app, host=_host, port=_port, log_level="info")
