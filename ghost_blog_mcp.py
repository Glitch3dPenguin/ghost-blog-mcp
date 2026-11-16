"""
ghost_blog_mcp.py — a single-file MCP server for a Ghost blog.

Exposes the Ghost Admin API (JWT auth, read + write) and the Ghost Content
API (query-key auth, read-only fallback) as MCP tools.

Context-window safe by design: list tools return compact tables without
bodies; full bodies are fetched on demand, one post at a time.

Deps: mcp[cli], requests. Python 3.10+.
"""

import base64
import hmac
import json
import logging
import os
import re
import time

import requests
from mcp.server.fastmcp import FastMCP

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
_USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


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

def _api_get(url: str, params: dict = None, admin: bool = True):
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


def _api_post(url: str, payload: dict, params: dict = None, method: str = "POST"):
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


def _api_delete(url: str):
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
        if n.get("type") == "text":
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
        if ntype == "code":
            code = node.get("text")
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

@mcp.tool()
def check_connection() -> str:
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
        ok, site = _api_get(f"{ADMIN_BASE}/site/", admin=True)
        if not ok:
            return f"Connection FAILED: {site}"
        s = site.get("site", {}) if isinstance(site, dict) else {}
        return (f"Connected to {GHOST_URL} — OK.\n"
                f"Auth mode: admin (full read/write)\n"
                f"Site title: {s.get('title', '?')}\n"
                f"Ghost version: {s.get('version', '?')}   URL: {s.get('url', '?')}\n"
                f"Use list_posts() / list_pages() / list_tags() / list_authors() to explore.")
    ok, settings = _api_get(f"{CONTENT_BASE}/settings/", admin=False)
    if not ok:
        return f"Connection FAILED: {settings}"
    s = settings.get("settings", {}) if isinstance(settings, dict) else {}
    return (f"Connected to {GHOST_URL} — OK.\n"
            f"Auth mode: content-only (read-only — set GHOST_ADMIN_KEY for writes)\n"
            f"Site title: {s.get('title', '?')}\n"
            f"Use list_posts() / list_pages() / list_tags() / list_authors() to explore.")


@mcp.tool()
def list_posts(
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
        author: Filter by author email (or author id, admin mode).
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
    ok, data = _api_get(f"{base}/posts/", params=params, admin=admin)
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


def _fetch_item(base, kind, identifier, admin):
    """Fetch one post/page by UUID, numeric id, or slug. Ghost 6 path
    params only accept raw numeric ids (24-hex); UUIDs and slugs are
    resolved via ?filter= queries on the list endpoint."""
    if _UUID_RE.match(identifier):
        return _api_get(f"{base}/{kind}/",
                        params={"filter": f"uuid:{identifier}"}, admin=admin)
    if re.fullmatch(r"[0-9a-f]+", identifier, flags=re.IGNORECASE):
        return _api_get(f"{base}/{kind}/{identifier}/", admin=admin)
    return _api_get(f"{base}/{kind}/",
                    params={"filter": f"slug:{identifier}"}, admin=admin)


def _resolve_raw_id(kind: str, identifier: str):
    """Resolve a post/page identifier to the RAW ObjectID that Ghost 6 path
    params require. Raw 24-hex ids pass through unchanged; UUIDs and slugs
    are looked up on the list endpoint (same routing as _fetch_item).

    Returns (True, raw_id) or (False, error_message)."""
    if re.fullmatch(r"[0-9a-f]+", identifier, flags=re.IGNORECASE):
        return True, identifier
    ok, data = _fetch_item(ADMIN_BASE, kind, identifier, True)
    if not ok:
        return False, data
    item = _first_item(data, kind)
    if not item or not item.get("id"):
        return False, f"'{identifier}' not found."
    return True, item["id"]


@mcp.tool()
def get_post(identifier: str, chars: int = 1500) -> str:
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
    ok, data = _fetch_item(base, "posts", identifier, admin)
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


@mcp.tool()
def get_post_content(identifier: str, format: str = "lexical") -> str:
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
    ok, data = _fetch_item(base, "posts", identifier, admin)
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


@mcp.tool()
def list_pages(
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
    ok, data = _api_get(f"{base}/pages/", params=params, admin=admin)
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
    lines += ["", "Use get_page(<slug>) for metadata + a truncated body preview."]
    if pag.get("page_count", 1) > page:
        lines.append(f"Use list_pages(page={page + 1}) for the next page.")
    return "\n".join(lines)


@mcp.tool()
def get_page(identifier: str, chars: int = 1500) -> str:
    """
    Metadata + a truncated body preview for ONE static page (by id or slug).

    Args:
        identifier: Page id (UUID), numeric id, or slug — from list_pages.
        chars:      Max body preview length in characters (default 1500).
    """
    chars = max(200, int(chars))
    admin = _mode() == "admin"
    base  = ADMIN_BASE if admin else CONTENT_BASE
    ok, data = _fetch_item(base, "pages", identifier, admin)
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
    ]
    return "\n".join(lines)


@mcp.tool()
def list_tags(limit: int = _DEFAULT_LIMIT, page: int = 1, order: str = "name ASC") -> str:
    """
    List all tags with post/page counts where the API provides them (counts
    are admin-mode only). Tag names from here filter list_posts(tag=...) and
    feed create_post(tags=...) / create_tag().
    """
    limit = max(1, min(int(limit), 100))
    page  = max(1, int(page))

    admin = _mode() == "admin"
    base  = ADMIN_BASE if admin else CONTENT_BASE
    ok, data = _api_get(f"{base}/tags/",
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
        count = t.get("count") if isinstance(t.get("count"), dict) else None
        cnt = ""
        if count:
            cnt = f"  posts={count.get('posts', 0)}, pages={count.get('pages', 0)}"
        desc = _truncate(t.get("description") or "", 60)
        lines.append(f"  {name} ({slug}){cnt}" + (f"  — {desc}" if desc else ""))
    lines += ["", "Use these names with list_posts(tag=...) or create_post(tags=...)."]
    if pag.get("page_count", 1) > page:
        lines.append(f"Use list_tags(page={page + 1}) for the next page.")
    return "\n".join(lines)


@mcp.tool()
def list_authors(limit: int = _DEFAULT_LIMIT, page: int = 1) -> str:
    """
    List the site's authors. Uses the Admin API /users/ in full mode (with
    status and post counts) and the Content API /authors/ in read-only mode.
    Author emails from here work with create_post(authors=...) and
    update_post(authors=...).
    """
    limit = max(1, min(int(limit), 100))
    page  = max(1, int(page))

    if _mode() == "admin":
        ok, data = _api_get(f"{ADMIN_BASE}/users/",
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

    ok, data = _api_get(f"{CONTENT_BASE}/authors/",
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

@mcp.tool()
def create_post(
    title: str,
    html: str = "",
    lexical: str = "",
    status: str = "draft",
    tags: str = "",
    authors: str = "",
    publish_at: str = "",
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
        status:     'draft' (default) or 'published'.
        tags:       Comma-separated tag names, e.g. 'news,ai'. Unmatched
                    names are auto-created by Ghost.
        authors:    Comma-separated author emails or ids.
        publish_at: ISO 8601 datetime for scheduling
                    (e.g. 2026-09-15T12:00:00.000Z).
    """
    if not title.strip():
        return "title is required."
    payload = {"title": title.strip()}
    if html:
        payload["html"] = html
    if lexical:
        payload["lexical"] = lexical
    if status:
        payload["status"] = status
    if tags.strip():
        payload["tags"] = [{"name": t.strip()} for t in tags.split(",") if t.strip()]
    if authors.strip():
        payload["authors"] = [a.strip() for a in authors.split(",") if a.strip()]
    if publish_at.strip():
        payload["publish_at"] = publish_at.strip()

    params = {"source": "html"} if html else None
    ok, data = _api_post(f"{ADMIN_BASE}/posts/", {"posts": [payload]}, params=params)
    if not ok:
        return data
    post = _first_item(data, "posts") or {}
    result = (f"Post created: '{post.get('title', title)}' "
              f"(id={post.get('uuid', '?')}, status={post.get('status', status)}).")
    if post.get("publish_at"):
        result += f"  Scheduled to publish at {post['publish_at']}."
    return result


@mcp.tool()
def update_post(
    post_id: str,
    updated_at: str,
    html: str = "",
    lexical: str = "",
    tags: str = "",
    authors: str = "",
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
        tags:          Comma-separated tag names — REPLACES all existing tags.
        authors:       Comma-separated author emails or ids — REPLACES all
                       existing authors.
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
    if tags.strip():
        payload["tags"] = [{"name": t.strip()} for t in tags.split(",") if t.strip()]
    if authors.strip():
        payload["authors"] = [a.strip() for a in authors.split(",") if a.strip()]

    params = {}
    if html:
        params["source"] = "html"
    if save_revision:
        params["save_revision"] = "true"

    ok, raw_id = _resolve_raw_id("posts", post_id)
    if not ok:
        return f"Error resolving post id '{post_id}': {raw_id}"
    ok, data = _api_post(f"{ADMIN_BASE}/posts/{raw_id}/", {"posts": [payload]},
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


@mcp.tool()
def set_post_status(post_id: str, status: str, updated_at: str, publish_at: str = "") -> str:
    """
    Change a post's status (publish, unpublish to draft, or schedule).

    IMPORTANT: Confirm with the user before calling — publishing is
    immediately visible on the site.

    Args:
        post_id:    Post id - UUID, numeric id, or slug (from get_post / list_posts).
        status:     'published' or 'draft'.
        updated_at: The Updated timestamp from get_post, verbatim
                    (optimistic lock — fetch the latest first).
        publish_at: Optional ISO 8601 datetime — a future date schedules the
                    post (it publishes itself at that time).
    """
    if not post_id:
        return "post_id is required."
    if status not in ("published", "draft"):
        return "Invalid status. Use 'published' or 'draft'."
    if not updated_at:
        return ("updated_at is required (optimistic lock). Fetch the latest post "
                "first with get_post() and pass its Updated value.")

    payload = {"status": status, "updated_at": updated_at}
    if publish_at.strip():
        payload["publish_at"] = publish_at.strip()

    ok, raw_id = _resolve_raw_id("posts", post_id)
    if not ok:
        return f"Error resolving post id '{post_id}': {raw_id}"
    ok, data = _api_post(f"{ADMIN_BASE}/posts/{raw_id}/", {"posts": [payload]}, method="PUT")
    if not ok:
        return data
    post = _first_item(data, "posts") or {}
    result = f"Post status set to {post.get('status', status)} (id={post.get('uuid', post_id)})."
    if post.get("publish_at"):
        result += f"  Publishes at {post['publish_at']}."
    return result


@mcp.tool()
def delete_post(post_id: str) -> str:
    """
    Permanently delete a post. This cannot be undone.

    IMPORTANT: Confirm the post id and the deletion with the user before
    calling.

    Args:
        post_id: Post id - UUID, numeric id, or slug (from get_post / list_posts).
    """
    if not post_id:
        return "post_id is required."
    ok, raw_id = _resolve_raw_id("posts", post_id)
    if not ok:
        return f"Error resolving post id '{post_id}': {raw_id}"
    ok, data = _api_delete(f"{ADMIN_BASE}/posts/{raw_id}/")
    if not ok:
        return data
    return f"Post deleted (id={post_id}). This cannot be undone."


@mcp.tool()
def create_page(
    title: str,
    html: str = "",
    lexical: str = "",
    status: str = "draft",
    tags: str = "",
    authors: str = "",
) -> str:
    """
    Create a new static page. (Pages cannot be scheduled — no publish_at.)

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
        tags:    Comma-separated tag names (unmatched names are auto-created).
        authors: Comma-separated author emails or ids.
    """
    if not title.strip():
        return "title is required."
    payload = {"title": title.strip()}
    if html:
        payload["html"] = html
    if lexical:
        payload["lexical"] = lexical
    if status:
        payload["status"] = status
    if tags.strip():
        payload["tags"] = [{"name": t.strip()} for t in tags.split(",") if t.strip()]
    if authors.strip():
        payload["authors"] = [a.strip() for a in authors.split(",") if a.strip()]

    params = {"source": "html"} if html else None
    ok, data = _api_post(f"{ADMIN_BASE}/pages/", {"pages": [payload]}, params=params)
    if not ok:
        return data
    post = _first_item(data, "pages") or {}
    return (f"Page created: '{post.get('title', title)}' "
            f"(id={post.get('uuid', '?')}, status={post.get('status', status)}).")


@mcp.tool()
def update_page(
    page_id: str,
    updated_at: str,
    html: str = "",
    lexical: str = "",
    tags: str = "",
    authors: str = "",
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
        tags:          Comma-separated tag names — REPLACES all existing tags.
        authors:       Comma-separated author emails or ids — REPLACES all
                       existing authors.
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
    if tags.strip():
        payload["tags"] = [{"name": t.strip()} for t in tags.split(",") if t.strip()]
    if authors.strip():
        payload["authors"] = [a.strip() for a in authors.split(",") if a.strip()]

    params = {}
    if html:
        params["source"] = "html"
    if save_revision:
        params["save_revision"] = "true"

    ok, raw_id = _resolve_raw_id("pages", page_id)
    if not ok:
        return f"Error resolving page id '{page_id}': {raw_id}"
    ok, data = _api_post(f"{ADMIN_BASE}/pages/{raw_id}/", {"pages": [payload]},
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


@mcp.tool()
def delete_page(page_id: str) -> str:
    """
    Permanently delete a static page. This cannot be undone.

    IMPORTANT: Confirm the page id and the deletion with the user before
    calling.

    Args:
        page_id: Page id - UUID, numeric id, or slug (from get_page / list_pages).
    """
    if not page_id:
        return "page_id is required."
    ok, raw_id = _resolve_raw_id("pages", page_id)
    if not ok:
        return f"Error resolving page id '{page_id}': {raw_id}"
    ok, data = _api_delete(f"{ADMIN_BASE}/pages/{raw_id}/")
    if not ok:
        return data
    return f"Page deleted (id={page_id}). This cannot be undone."


@mcp.tool()
def create_tag(name: str, slug: str = "", description: str = "") -> str:
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

    ok, data = _api_post(f"{ADMIN_BASE}/tags/", {"tags": [tag]})
    if not ok:
        return data
    created = _first_item(data, "tags") or {}
    return (f"Tag created: '{created.get('name', name)}' "
            f"(id={created.get('id', '?')}, slug={created.get('slug', '?')}).")


@mcp.tool()
def update_tag(tag_id: str, name: str = "", slug: str = "", description: str = "") -> str:
    """
    Update an existing tag (name, slug, and/or description).

    IMPORTANT: Confirm with the user before calling.

    Args:
        tag_id:      Tag id (from list_tags).
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

    ok, data = _api_post(f"{ADMIN_BASE}/tags/{tag_id}/", {"tags": [tag]}, method="PUT")
    if not ok:
        return data
    updated = _first_item(data, "tags") or {}
    return f"Tag updated: '{updated.get('name', '?')}' (id={updated.get('id', tag_id)})."


@mcp.tool()
def delete_tag(tag_id: str) -> str:
    """
    Permanently delete a tag (the tag is removed from posts/pages that use
    it). This cannot be undone.

    IMPORTANT: Confirm the tag id and the deletion with the user before
    calling.

    Args:
        tag_id: Tag id (from list_tags).
    """
    if not tag_id:
        return "tag_id is required."
    ok, data = _api_delete(f"{ADMIN_BASE}/tags/{tag_id}/")
    if not ok:
        return data
    return f"Tag deleted (id={tag_id}). This cannot be undone."


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
            if supplied != expected:
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
