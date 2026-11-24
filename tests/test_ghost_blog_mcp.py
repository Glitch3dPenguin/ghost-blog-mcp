"""Mocked-HTTP tests for ghost_blog_mcp.py.

No live Ghost, no network: every Ghost call goes through the module's
_api_get / _api_post / _api_delete wrappers, which these tests replace
with fakes. The auth middleware is exercised through its ASGI protocol,
and the fail-closed startup refusal is checked in a subprocess.
"""
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import ghost_blog_mcp as g  # noqa: E402


def run(coro):
    return asyncio.run(coro)


# --- fakes -----------------------------------------------------------------

def fake_api(reply=(True, {})):
    """Stand-in for _api_get / _api_post / _api_delete.

    call tuples: (url, args, kwargs) — for _api_post, args[0] is the
    request envelope ({"posts": [...]})."""
    calls = []

    async def fake(url, *args, **kwargs):
        calls.append((url, args, kwargs))
        return reply

    fake.calls = calls
    return fake


def _fake_resolve(raw="a" * 24):
    async def res(kind, identifier):
        return True, raw
    return res


# --- auth middleware ---------------------------------------------------------

def _auth_invoke(token, headers):
    g._auth_token = token
    app_called = []

    async def app(scope, receive, send):
        app_called.append(True)

    sent = []

    async def send(msg):
        sent.append(msg)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    wrapped = g._token_auth(app)
    scope = {"type": "http", "headers": headers}
    run(wrapped(scope, receive, send))
    return app_called, sent


def test_token_auth_accepts_correct_token():
    called, sent = _auth_invoke("sekrit", [(b"authorization", b"Bearer sekrit")])
    assert called == [True]
    assert sent == []


def test_token_auth_rejects_missing_header():
    called, sent = _auth_invoke("sekrit", [])
    assert called == []
    assert sent[0]["status"] == 401


def test_token_auth_rejects_wrong_token():
    called, sent = _auth_invoke("sekrit", [(b"authorization", b"Bearer no")])
    assert called == []
    assert sent[0]["status"] == 401


def test_token_auth_is_constant_time():
    src = open(g.__file__, "r", encoding="utf-8").read()
    assert "hmac.compare_digest" in src


def test_fail_closed_non_loopback_without_token():
    env = dict(os.environ)
    env.update({"HOST": "0.0.0.0", "MCP_TRANSPORT": "streamable-http"})
    env.pop("MCP_AUTH_TOKEN", None)
    r = subprocess.run(
        [sys.executable, "-c", "import ghost_blog_mcp"],
        cwd=str(REPO), env=env, capture_output=True, text=True,
        timeout=120)
    assert r.returncode != 0
    assert "refusing to start" in (r.stderr + r.stdout)


# --- id resolution -----------------------------------------------------------

def test_fetch_item_24hex_uses_path(monkeypatch):
    fake = fake_api((True, {"posts": []}))
    monkeypatch.setattr(g, "_api_get", fake)
    rid = "a1b2c3d4e5f6a7b8c9d0e1f2"
    run(g._fetch_item("BASE", "posts", rid, True))
    url, _, kw = fake.calls[0]
    assert url == f"BASE/posts/{rid}/"
    assert kw["params"] is None


def test_fetch_item_uuid_uses_filter(monkeypatch):
    fake = fake_api((True, {"posts": []}))
    monkeypatch.setattr(g, "_api_get", fake)
    uid = "c1aa7ade-5b32-4039-94df-2750512a9ee5"
    run(g._fetch_item("BASE", "posts", uid, True))
    url, _, kw = fake.calls[0]
    assert url == "BASE/posts/"
    assert kw["params"]["filter"] == f"uuid:{uid}"


def test_fetch_item_slug_uses_filter(monkeypatch):
    fake = fake_api((True, {"posts": []}))
    monkeypatch.setattr(g, "_api_get", fake)
    run(g._fetch_item("BASE", "posts", "my-post-slug", True))
    url, _, kw = fake.calls[0]
    assert url == "BASE/posts/"
    assert kw["params"]["filter"] == "slug:my-post-slug"


def test_fetch_item_allhex_short_slug_uses_filter(monkeypatch):
    # 'decade' is all-hex but only 6 chars: it is a slug, not a raw id.
    fake = fake_api((True, {"posts": []}))
    monkeypatch.setattr(g, "_api_get", fake)
    run(g._fetch_item("BASE", "posts", "decade", True))
    url, _, kw = fake.calls[0]
    assert url == "BASE/posts/"
    assert kw["params"]["filter"] == "slug:decade"


def test_resolve_raw_id_24hex_passthrough(monkeypatch):
    fake = fake_api((True, {"posts": []}))
    monkeypatch.setattr(g, "_api_get", fake)
    ok, raw = run(g._resolve_raw_id("posts", "f" * 24))
    assert ok and raw == "f" * 24
    assert fake.calls == []  # raw ids never hit the API


def test_resolve_raw_id_slug_looks_up(monkeypatch):
    async def fake_fetch(base, kind, identifier, admin, formats=None):
        assert identifier == "about"
        return True, {"pages": [{"id": "0123456789abcdef01234567",
                                 "slug": "about"}]}
    monkeypatch.setattr(g, "_fetch_item", fake_fetch)
    ok, raw = run(g._resolve_raw_id("pages", "about"))
    assert ok and raw == "0123456789abcdef01234567"


# --- destructive gate ----------------------------------------------------------

def test_delete_gate_refuses_by_default(monkeypatch):
    monkeypatch.setattr(g, "_allow_delete", False)
    msg = g._delete_gate()
    assert msg and "GHOST_ALLOW_DELETE=1" in msg


def test_delete_post_refused_when_disabled(monkeypatch):
    monkeypatch.setattr(g, "_allow_delete", False)
    out = run(g.delete_post("a" * 24))
    assert out.startswith("Refusing")


def test_delete_post_succeeds_when_enabled(monkeypatch):
    monkeypatch.setattr(g, "_allow_delete", True)
    monkeypatch.setattr(g, "_api_delete", fake_api((True, None)))
    out = run(g.delete_post("a" * 24))
    assert "Post deleted" in out


# --- scheduling / backdating -----------------------------------------------------

def test_create_post_draft_plus_date_schedules(monkeypatch):
    reply = (True, {"posts": [{"title": "T", "uuid": "u1", "status": "scheduled",
                               "published_at": "2030-01-01T00:00:00.000Z"}]})
    fake = fake_api(reply)
    monkeypatch.setattr(g, "_api_post", fake)
    out = run(g.create_post(title="T", published_at="2030-01-01T00:00:00.000Z"))
    payload = fake.calls[0][1][0]["posts"][0]
    assert payload["status"] == "scheduled"
    assert "Scheduled to publish at 2030-01-01T00:00:00.000Z" in out


def test_create_post_explicit_published_backdates(monkeypatch):
    reply = (True, {"posts": [{"title": "T", "uuid": "u1", "status": "published",
                               "published_at": "2020-01-01T00:00:00.000Z"}]})
    fake = fake_api(reply)
    monkeypatch.setattr(g, "_api_post", fake)
    out = run(g.create_post(title="T", status="published",
                            published_at="2020-01-01T00:00:00.000Z"))
    payload = fake.calls[0][1][0]["posts"][0]
    assert payload["status"] == "published"
    assert payload["published_at"] == "2020-01-01T00:00:00.000Z"
    assert "Scheduled to publish at" not in out


def test_create_post_explicit_scheduled_wins(monkeypatch):
    reply = (True, {"posts": [{"title": "T", "uuid": "u1", "status": "scheduled",
                               "published_at": "2030-05-05T00:00:00.000Z"}]})
    fake = fake_api(reply)
    monkeypatch.setattr(g, "_api_post", fake)
    run(g.create_post(title="T", status="scheduled",
                      published_at="2030-05-05T00:00:00.000Z"))
    payload = fake.calls[0][1][0]["posts"][0]
    assert payload["status"] == "scheduled"


def test_create_post_published_no_date_stays_published(monkeypatch):
    reply = (True, {"posts": [{"title": "T", "uuid": "u1", "status": "published"}]})
    fake = fake_api(reply)
    monkeypatch.setattr(g, "_api_post", fake)
    out = run(g.create_post(title="T", status="published"))
    payload = fake.calls[0][1][0]["posts"][0]
    assert payload["status"] == "published"
    assert "published_at" not in payload
    assert "Scheduled to publish at" not in out


def _set_status(monkeypatch, reply, pub=""):
    monkeypatch.setattr(g, "_resolve_raw_id", _fake_resolve())
    fake = fake_api(reply)
    monkeypatch.setattr(g, "_api_post", fake)
    status = reply[1]["posts"][0]["status"]
    return run(g.set_post_status("a" * 24, status,
                                 "2026-01-01T00:00:00.000Z", pub))


def test_set_post_status_scheduled_announces_publish_date(monkeypatch):
    reply = (True, {"posts": [{"status": "scheduled",
                               "published_at": "2030-01-01T00:00:00.000Z",
                               "uuid": "u1"}]})
    out = _set_status(monkeypatch, reply, "2030-01-01T00:00:00.000Z")
    assert "Publishes at 2030-01-01T00:00:00.000Z" in out


def test_set_post_status_published_no_publish_date_line(monkeypatch):
    reply = (True, {"posts": [{"status": "published",
                               "published_at": "2020-01-01T00:00:00.000Z",
                               "uuid": "u1"}]})
    out = _set_status(monkeypatch, reply)
    assert "Publishes at" not in out


# --- tags / authors shape ---------------------------------------------------------

def test_create_post_tags_authors_are_lists(monkeypatch):
    reply = (True, {"posts": [{"title": "T", "uuid": "u1", "status": "draft"}]})
    fake = fake_api(reply)
    monkeypatch.setattr(g, "_api_post", fake)
    run(g.create_post(title="T", tags=["news", "ai"],
                      authors=["max@klabsdev.com"]))
    payload = fake.calls[0][1][0]["posts"][0]
    assert payload["tags"] == [{"name": "news"}, {"name": "ai"}]
    assert payload["authors"] == ["max@klabsdev.com"]


def test_create_post_no_tags_omits_field(monkeypatch):
    reply = (True, {"posts": [{"title": "T", "uuid": "u1", "status": "draft"}]})
    fake = fake_api(reply)
    monkeypatch.setattr(g, "_api_post", fake)
    run(g.create_post(title="T"))
    payload = fake.calls[0][1][0]["posts"][0]
    assert "tags" not in payload
    assert "authors" not in payload


def test_update_post_empty_lists_clear(monkeypatch):
    reply = (True, {"posts": [{"title": "T", "uuid": "u1",
                               "updated_at": "2026-01-02T00:00:00.000Z"}]})
    fake = fake_api(reply)
    monkeypatch.setattr(g, "_api_post", fake)
    monkeypatch.setattr(g, "_resolve_raw_id", _fake_resolve())
    run(g.update_post("a" * 24, "2026-01-01T00:00:00.000Z", tags=[], authors=[]))
    payload = fake.calls[0][1][0]["posts"][0]
    assert payload["tags"] == []
    assert payload["authors"] == []


# --- tag write resolution ------------------------------------------------------------

def test_update_tag_resolves_identifier(monkeypatch):
    reply = (True, {"tags": [{"id": "b" * 24, "name": "n"}]})
    fake = fake_api(reply)
    monkeypatch.setattr(g, "_api_post", fake)
    monkeypatch.setattr(g, "_resolve_raw_id", _fake_resolve("b" * 24))
    out = run(g.update_tag("some-slug", name="New"))
    assert fake.calls[0][0].endswith("/tags/" + "b" * 24 + "/")
    assert "Tag updated" in out


def test_delete_tag_resolves_identifier(monkeypatch):
    monkeypatch.setattr(g, "_allow_delete", True)
    monkeypatch.setattr(g, "_api_delete", fake_api((True, None)))
    monkeypatch.setattr(g, "_resolve_raw_id", _fake_resolve("c" * 24))
    out = run(g.delete_tag("some-slug"))
    assert out.startswith("Tag deleted")


# --- docstring fact-check ----------------------------------------------------------------

def test_list_posts_author_docstring_says_slug():
    doc = (g.list_posts.__doc__ or "").lower()
    assert "slug" in doc
    assert "email" not in doc

# --- check_connection (the auth probe) -------------------------------------

def test_check_connection_admin_proves_key_via_users_me(monkeypatch):
    monkeypatch.setattr(g, "ADMIN_KEY", "f" * 24 + ":" + "a" * 64)
    calls = []

    async def fake(url, *args, **kwargs):
        calls.append(url)
        if url.endswith("/users/me/"):
            return True, {"users": [{"name": "Max"}]}
        return True, {"site": {"title": "T", "version": "6.77",
                               "url": "https://t.example"}}

    monkeypatch.setattr(g, "_api_get", fake)
    out = run(g.check_connection())
    assert "OK" in out
    assert "Max" in out
    assert "6.77" in out
    assert calls[0].endswith("/users/me/")


def test_check_connection_admin_bad_key_fails(monkeypatch):
    monkeypatch.setattr(g, "ADMIN_KEY", "f" * 24 + ":" + "a" * 64)
    fake = fake_api((False, "Error 401: Unknown Admin API Key"))
    monkeypatch.setattr(g, "_api_get", fake)
    out = run(g.check_connection())
    assert out.startswith("Connection FAILED")
    assert "401" in out


def test_check_connection_admin_malformed_key_no_network(monkeypatch):
    calls = []

    async def fake(url, *args, **kwargs):
        calls.append(url)
        return True, {}

    monkeypatch.setattr(g, "ADMIN_KEY", "no-colon-key")
    monkeypatch.setattr(g, "_api_get", fake)
    out = run(g.check_connection())
    assert "Connection FAILED" in out
    assert "invalid format" in out
    assert calls == []


def test_check_connection_admin_nonhex_secret_no_network(monkeypatch):
    calls = []

    async def fake(url, *args, **kwargs):
        calls.append(url)
        return True, {}

    monkeypatch.setattr(g, "ADMIN_KEY", "f" * 24 + ":" + "z" * 64)
    monkeypatch.setattr(g, "_api_get", fake)
    out = run(g.check_connection())
    assert "Connection FAILED" in out
    assert "hex" in out
    assert calls == []


# --- get_page_content --------------------------------------------------------

def _page_reply(lexical=None, html="<p>legacy body</p>"):
    page = {"id": "a" * 24, "uuid": "u1", "html": html}
    if lexical is not None:
        page["lexical"] = lexical
    return (True, {"pages": [page]})


def test_get_page_content_lexical(monkeypatch):
    monkeypatch.setattr(g, "ADMIN_KEY", "f" * 24 + ":" + "a" * 64)
    lexical = json.dumps({"root": {"type": "root", "direction": "ltr",
                                   "format": "", "indent": 0, "version": 1,
                                   "children": [
        {"type": "paragraph", "direction": "ltr", "format": "",
         "indent": 0, "version": 1, "children": [
            {"type": "extended-text", "version": 1, "mode": "normal",
             "style": "", "detail": 0, "format": 0,
             "text": "hello page"}]}]}})
    fake = fake_api(_page_reply(lexical=lexical))
    monkeypatch.setattr(g, "_api_get", fake)
    out = run(g.get_page_content("a" * 24))
    assert "hello page" in out
    # admin lexical mode still requests html (the no-lexical fallback needs it)
    assert fake.calls[0][2]["params"] == {"formats": "html"}


def test_get_page_content_html(monkeypatch):
    monkeypatch.setattr(g, "ADMIN_KEY", "f" * 24 + ":" + "a" * 64)
    fake = fake_api(_page_reply(html="<p>raw <b>html</b></p>"))
    monkeypatch.setattr(g, "_api_get", fake)
    out = run(g.get_page_content("a" * 24, format="html"))
    assert out == "<p>raw <b>html</b></p>"


def test_get_page_content_no_lexical_falls_back_to_html(monkeypatch):
    monkeypatch.setattr(g, "ADMIN_KEY", "f" * 24 + ":" + "a" * 64)
    fake = fake_api(_page_reply())  # no lexical field
    monkeypatch.setattr(g, "_api_get", fake)
    out = run(g.get_page_content("a" * 24))
    assert "legacy body" in out


def test_get_post_content_lexical_requests_html(monkeypatch):
    monkeypatch.setattr(g, "ADMIN_KEY", "f" * 24 + ":" + "a" * 64)
    fake = fake_api((True, {"posts": [{"id": "a" * 24, "lexical": "{}",
                                       "html": "<p>p</p>"}]}))
    monkeypatch.setattr(g, "_api_get", fake)
    run(g.get_post_content("a" * 24))
    assert fake.calls[0][2]["params"] == {"formats": "html"}
