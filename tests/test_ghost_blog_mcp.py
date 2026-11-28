"""Mocked-HTTP tests for ghost_blog_mcp.py.

No live Ghost, no network: every Ghost call goes through the module's
_api_get / _api_post / _api_delete wrappers, which these tests replace
with fakes. The auth middleware is exercised through its ASGI protocol,
and the fail-closed startup refusal is checked in a subprocess.
"""
import asyncio
import inspect
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

def test_check_connection_admin_proves_key_via_users_list(monkeypatch):
    monkeypatch.setattr(g, "ADMIN_KEY", "f" * 24 + ":" + "a" * 64)
    calls = []

    async def fake(url, *args, **kwargs):
        calls.append(url)
        if url.endswith("/users/"):
            return True, {"users": [{"name": "Max"}]}
        return True, {"site": {"title": "T", "version": "6.77",
                               "url": "https://t.example"}}

    monkeypatch.setattr(g, "_api_get", fake)
    out = run(g.check_connection())
    assert "OK" in out
    assert "verified" in out
    assert "6.77" in out
    assert calls[0].endswith("/users/")


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



# --- admin-only groups: newsletters / tiers / offers / members / webhooks ---

ADMIN_URL = "https://x.example"
ADMIN_KEY = "f" * 24 + ":" + "a" * 64


def _admin_env(monkeypatch):
    """The new read tools check _admin_config_problem() up front, which
    reads the module globals — set both for a happy path."""
    monkeypatch.setattr(g, "GHOST_URL", ADMIN_URL)
    monkeypatch.setattr(g, "ADMIN_KEY", ADMIN_KEY)


def _fake_resolve1(raw="a" * 24):
    """The group-specific resolve helpers (_resolve_offer_id / _resolve_
    member_id / _resolve_webhook_id) take only the identifier."""
    async def res(identifier):
        return True, raw
    return res


# --- display helpers ---------------------------------------------------------

def test_price_label_cents_to_display():
    t = {"monthly_price": "1000", "yearly_price": "9600", "currency": "USD"}
    assert g._price_label(t) == "10.00 USD/mo + 96.00 USD/yr"
    assert g._price_label({"monthly_price": "0"}) == "0.00 USD/mo"
    assert g._price_label({}) == "free"


def test_discount_label_per_type():
    assert g._discount_label({"type": "percent", "amount": 10}) == "10% off"
    assert (g._discount_label({"type": "tier_free_trial",
                               "duration_in_months": 3})
            == "free trial (3 months)")
    assert (g._discount_label({"type": "tier_free_trial", "duration": 14})
            == "free trial (14 days)")
    assert (g._discount_label({"type": "amount", "amount": 1000,
                               "currency": "USD"})
            == "USD 10.00 off")


# --- resolution helpers ------------------------------------------------------

def test_fetch_offer_unknown_uses_code_filter(monkeypatch):
    fake = fake_api((True, {"offers": []}))
    monkeypatch.setattr(g, "_api_get", fake)
    run(g._fetch_offer("SAVE20"))
    url, _, kw = fake.calls[0]
    assert url.endswith("/offers/")
    assert kw["params"]["filter"] == "code:SAVE20"


def test_fetch_member_unknown_uses_email_filter(monkeypatch):
    fake = fake_api((True, {"members": []}))
    monkeypatch.setattr(g, "_api_get", fake)
    run(g._fetch_member("max@example.com"))
    url, _, kw = fake.calls[0]
    assert url.endswith("/members/")
    assert kw["params"]["filter"] == "email:max@example.com"


def test_fetch_webhook_rejects_non_id(monkeypatch):
    fake = fake_api((True, {}))
    monkeypatch.setattr(g, "_api_get", fake)
    ok, err = run(g._fetch_webhook("not-an-id"))
    assert not ok
    assert "raw id or UUID" in err
    assert fake.calls == []


# --- admin-only configuration gate -------------------------------------------

def test_read_tools_report_missing_admin_key(monkeypatch):
    monkeypatch.setattr(g, "GHOST_URL", ADMIN_URL)
    monkeypatch.setattr(g, "ADMIN_KEY", "")
    for tool in (g.list_newsletters, g.list_tiers, g.list_members):
        out = run(tool())
        assert "GHOST_ADMIN_KEY" in out


def test_read_tools_report_missing_url(monkeypatch):
    monkeypatch.setattr(g, "GHOST_URL", "")
    monkeypatch.setattr(g, "ADMIN_KEY", ADMIN_KEY)
    out = run(g.list_offers())
    assert "GHOST_URL" in out
    out = run(g.list_webhooks())
    assert "GHOST_URL" in out


# --- newsletters ---------------------------------------------------------------

def test_list_newsletters_shape(monkeypatch):
    _admin_env(monkeypatch)
    reply = (True, {"newsletters": [
        {"name": "Weekly", "slug": "weekly", "id": "a" * 24,
         "status": "active", "count": {"subscribers": 7},
         "description": "The weekly one"}],
        "meta": {"pagination": {"page": 1, "page_count": 1, "total": 1}}})
    fake = fake_api(reply)
    monkeypatch.setattr(g, "_api_get", fake)
    out = run(g.list_newsletters())
    assert "Weekly (weekly)" in out
    assert "subscribers=7" in out
    url, _, kw = fake.calls[0]
    assert url.endswith("/newsletters/")
    assert kw["params"]["include"] == "count"
    assert kw["params"]["limit"] == 15


def test_create_newsletter_payload_shape(monkeypatch):
    fake = fake_api((True, {"newsletters": [{"name": "N", "id": "b" * 24,
                                             "slug": "n"}]}))
    monkeypatch.setattr(g, "_api_post", fake)
    out = run(g.create_newsletter(name="N", subscribe_on_signup=True))
    payload = fake.calls[0][1][0]["newsletters"][0]
    assert payload["name"] == "N"
    assert payload["status"] == "active"
    assert payload["subscribe_on_signup"] is True
    for field in ("show_badge", "show_header_icon", "description",
                  "title_font_category"):
        assert field not in payload
    assert "Newsletter created" in out


def test_create_newsletter_validates_status(monkeypatch):
    fake = fake_api((True, {}))
    monkeypatch.setattr(g, "_api_post", fake)
    out = run(g.create_newsletter(name="N", status="paused"))
    assert "Invalid status" in out
    assert fake.calls == []


def test_update_newsletter_nothing_to_update(monkeypatch):
    fake = fake_api((True, {}))
    monkeypatch.setattr(g, "_api_post", fake)
    out = run(g.update_newsletter("some-slug"))
    assert "Nothing to update" in out
    assert fake.calls == []


def test_update_newsletter_put_envelope(monkeypatch):
    reply = (True, {"newsletters": [{"name": "N", "id": "c" * 24}]})
    fake = fake_api(reply)
    monkeypatch.setattr(g, "_api_post", fake)
    monkeypatch.setattr(g, "_resolve_raw_id", _fake_resolve("c" * 24))
    out = run(g.update_newsletter("some-slug", description="new desc",
                                  sort_order=2))
    url, args, kw = fake.calls[0]
    assert url.endswith("/newsletters/" + "c" * 24 + "/")
    assert kw["method"] == "PUT"
    assert args[0]["newsletters"][0] == {"description": "new desc",
                                         "sort_order": 2}
    assert "Newsletter updated" in out


def test_delete_newsletter_gated_then_works(monkeypatch):
    monkeypatch.setattr(g, "_allow_delete", False)
    out = run(g.delete_newsletter("n"))
    assert out.startswith("Refusing")
    monkeypatch.setattr(g, "_allow_delete", True)
    monkeypatch.setattr(g, "_api_delete", fake_api((True, None)))
    monkeypatch.setattr(g, "_resolve_raw_id", _fake_resolve("d" * 24))
    out = run(g.delete_newsletter("n"))
    assert out.startswith("Newsletter deleted")


# --- tiers ----------------------------------------------------------------------

def test_create_tier_rejects_non_cents(monkeypatch):
    fake = fake_api((True, {}))
    monkeypatch.setattr(g, "_api_post", fake)
    out = run(g.create_tier(name="T", monthly_price="ten"))
    assert "cents" in out
    assert fake.calls == []


def test_create_tier_payload_shape(monkeypatch):
    fake = fake_api((True, {"tiers": [{"name": "T", "id": "e" * 24,
                                       "slug": "t"}]}))
    monkeypatch.setattr(g, "_api_post", fake)
    run(g.create_tier(name="T", monthly_price="1000", currency="usd",
                      benefits=["a", " b ", ""]))
    payload = fake.calls[0][1][0]["tiers"][0]
    assert payload["monthly_price"] == "1000"
    assert payload["currency"] == "USD"
    assert payload["visibility"] == "public"
    assert payload["benefits"] == ["a", "b"]


def test_list_tiers_renders_price_label(monkeypatch):
    _admin_env(monkeypatch)
    reply = (True, {"tiers": [
        {"name": "Pro", "slug": "pro", "id": "f" * 24, "visibility": "public",
         "monthly_price": "1000", "yearly_price": "9600", "currency": "USD",
         "count": {"members": 3}}],
        "meta": {"pagination": {"page": 1, "page_count": 1, "total": 1}}})
    fake = fake_api(reply)
    monkeypatch.setattr(g, "_api_get", fake)
    out = run(g.list_tiers())
    assert "Pro (pro)" in out
    assert "10.00 USD/mo + 96.00 USD/yr" in out
    assert "members=3" in out


def test_update_tier_empty_benefits_clears(monkeypatch):
    reply = (True, {"tiers": [{"name": "T", "id": "e" * 24}]})
    fake = fake_api(reply)
    monkeypatch.setattr(g, "_api_post", fake)
    monkeypatch.setattr(g, "_resolve_raw_id", _fake_resolve("e" * 24))
    run(g.update_tier("t", benefits=[]))
    url, args, kw = fake.calls[0]
    assert url.endswith("/tiers/" + "e" * 24 + "/")
    assert args[0]["tiers"][0]["benefits"] == []
    assert kw["method"] == "PUT"


# --- offers -----------------------------------------------------------------------

def test_create_offer_type_and_amount_validation(monkeypatch):
    fake = fake_api((True, {}))
    monkeypatch.setattr(g, "_api_post", fake)
    out = run(g.create_offer(name="N", code="C", type="bogus",
                             tier_id="t", amount=1))
    assert "Invalid type" in out
    out = run(g.create_offer(name="N", code="C", type="percent",
                             tier_id="t", amount=0))
    assert "amount must be positive" in out
    assert fake.calls == []


def test_create_offer_free_trial_allows_zero(monkeypatch):
    fake = fake_api((True, {"offers": [{"name": "N", "code": "C",
                                        "id": "g" * 24}]}))
    monkeypatch.setattr(g, "_api_post", fake)
    out = run(g.create_offer(name="N", code="C", type="tier_free_trial",
                             tier_id="t", amount=0, duration_in_months=1))
    payload = fake.calls[0][1][0]["offers"][0]
    assert payload["amount"] == 0
    assert payload["duration_in_months"] == 1
    assert "Offer created" in out


def test_update_offer_subset_only(monkeypatch):
    reply = (True, {"offers": [{"name": "New", "code": "C", "id": "g" * 24}]})
    fake = fake_api(reply)
    monkeypatch.setattr(g, "_api_post", fake)

    async def fake_offer_resolve(identifier):
        assert identifier == "C"
        return True, "g" * 24
    monkeypatch.setattr(g, "_resolve_offer_id", fake_offer_resolve)
    run(g.update_offer("C", name="New", display_title="D"))
    url, args, kw = fake.calls[0]
    assert url.endswith("/offers/" + "g" * 24 + "/")
    assert args[0]["offers"][0] == {"name": "New", "display_title": "D"}
    assert kw["method"] == "PUT"


def test_get_offer_by_code(monkeypatch):
    _admin_env(monkeypatch)
    reply = (True, {"offers": [
        {"name": "Launch", "code": "SAVE20", "uuid": "u", "status": "active",
         "type": "percent", "cadence": "once", "amount": 20, "duration": 30,
         "tier": {"name": "Pro", "id": "h" * 24}, "id": "h" * 24}]})
    fake = fake_api(reply)
    monkeypatch.setattr(g, "_api_get", fake)
    out = run(g.get_offer("SAVE20"))
    assert fake.calls[0][2]["params"]["filter"] == "code:SAVE20"
    assert "SAVE20" in out
    assert "20% off" in out
    assert "Pro" in out


# --- members -------------------------------------------------------------------------

def test_create_member_email_required(monkeypatch):
    fake = fake_api((True, {}))
    monkeypatch.setattr(g, "_api_post", fake)
    out = run(g.create_member(email="nope"))
    assert "valid email" in out.lower()
    assert fake.calls == []


def test_create_member_shapes(monkeypatch):
    fake = fake_api((True, {"members": [{"email": "max@example.com",
                                         "id": "i" * 24}]}))
    monkeypatch.setattr(g, "_api_post", fake)
    run(g.create_member(email="max@example.com", name="Max",
                        labels=["vip"], newsletters=["n1"]))
    payload = fake.calls[0][1][0]["members"][0]
    assert payload["email"] == "max@example.com"
    assert payload["labels"] == [{"name": "vip"}]
    assert payload["newsletters"] == [{"id": "n1"}]


def test_update_member_has_no_email_param(monkeypatch):
    params = inspect.signature(g.update_member).parameters
    assert "email" not in params
    reply = (True, {"members": [{"email": "max@example.com", "id": "i" * 24}]})
    fake = fake_api(reply)
    monkeypatch.setattr(g, "_api_post", fake)
    monkeypatch.setattr(g, "_resolve_member_id", _fake_resolve1("i" * 24))
    run(g.update_member("max@example.com", labels=[]))
    assert fake.calls[0][1][0]["members"][0]["labels"] == []


def test_get_member_by_email(monkeypatch):
    _admin_env(monkeypatch)
    reply = (True, {"members": [
        {"email": "max@example.com", "name": "Max", "status": "paid",
         "subscribed": True, "uuid": "u", "id": "i" * 24}]})
    fake = fake_api(reply)
    monkeypatch.setattr(g, "_api_get", fake)
    out = run(g.get_member("max@example.com"))
    assert fake.calls[0][2]["params"]["filter"] == "email:max@example.com"
    assert "max@example.com" in out
    assert "paid" in out


# --- webhooks ----------------------------------------------------------------------------

def test_create_webhook_url_validation(monkeypatch):
    fake = fake_api((True, {}))
    monkeypatch.setattr(g, "_api_post", fake)
    out = run(g.create_webhook(event="post.published", target_url="ftp://x"))
    assert "http(s)" in out
    assert fake.calls == []


def test_create_webhook_generated_secret_shown_once(monkeypatch):
    fake = fake_api((True, {"webhooks": [{"event": "post.published",
                                          "id": "j" * 24,
                                          "secret": "gen-secret"}]}))
    monkeypatch.setattr(g, "_api_post", fake)
    out = run(g.create_webhook(event="post.published",
                               target_url="https://hook.example/gh"))
    payload = fake.calls[0][1][0]["webhooks"][0]
    assert "secret" not in payload  # omitted -> Ghost generates one
    assert payload["target_url"] == "https://hook.example/gh"
    assert "Secret (shown once" in out
    assert "gen-secret" in out


def test_list_webhooks_never_echoes_secret(monkeypatch):
    _admin_env(monkeypatch)
    reply = (True, {"webhooks": [
        {"event": "post.published", "target_url": "https://hook.example/gh",
         "name": "GH", "secret": "top-secret-value", "id": "j" * 24}]})
    fake = fake_api(reply)
    monkeypatch.setattr(g, "_api_get", fake)
    out = run(g.list_webhooks())
    assert "GH" in out
    assert "top-secret-value" not in out


def test_get_webhook_shows_secret_with_warning(monkeypatch):
    _admin_env(monkeypatch)
    rid = "9" * 24
    reply = (True, {"webhooks": [
        {"event": "post.published", "target_url": "https://hook.example/gh",
         "name": "GH", "secret": "top-secret-value", "uuid": "u", "id": rid}]})
    fake = fake_api(reply)
    monkeypatch.setattr(g, "_api_get", fake)
    out = run(g.get_webhook(rid))
    assert fake.calls[0][0].endswith("/webhooks/" + rid + "/")
    assert "top-secret-value" in out
    assert "credential" in out


def test_update_webhook_secret_not_changeable(monkeypatch):
    params = inspect.signature(g.update_webhook).parameters
    assert "secret" not in params
    reply = (True, {"webhooks": [{"event": "post.added",
                                  "target_url": "https://x.example/h",
                                  "id": "j" * 24}]})
    fake = fake_api(reply)
    monkeypatch.setattr(g, "_api_post", fake)
    monkeypatch.setattr(g, "_resolve_webhook_id", _fake_resolve1("j" * 24))
    run(g.update_webhook("j" * 24, event="post.added"))
    assert fake.calls[0][1][0]["webhooks"][0] == {"event": "post.added"}


# --- prompts --------------------------------------------------------------------------------

def test_summarize_post_admin_mode(monkeypatch):
    _admin_env(monkeypatch)

    async def fake_fetch(base, kind, identifier, admin, formats=None):
        assert admin is True
        assert formats == "html"
        assert base == g.ADMIN_BASE
        return True, {"posts": [{"title": "Hello", "custom_excerpt": "A post",
                                 "html": "<p>Body text</p>"}]}
    monkeypatch.setattr(g, "_fetch_item", fake_fetch)
    msgs = run(g.summarize_post("a" * 24))
    assert len(msgs) == 1
    assert msgs[0]["role"] == "user"
    assert "Summarize the following Ghost post" in msgs[0]["content"]
    assert "Title: Hello" in msgs[0]["content"]
    assert "Body text" in msgs[0]["content"]


def test_summarize_post_error_message(monkeypatch):
    _admin_env(monkeypatch)

    async def fake_fetch(base, kind, identifier, admin, formats=None):
        return False, "Error 404: not found"
    monkeypatch.setattr(g, "_fetch_item", fake_fetch)
    msgs = run(g.summarize_post("missing"))
    assert msgs[0]["role"] == "user"
    assert "Could not load post" in msgs[0]["content"]


def test_newsletter_draft_shape():
    msgs = g.newsletter_draft("Sourdough", ["starter", "schedule"])
    assert [m["role"] for m in msgs] == ["system", "user"]
    assert "Ghost-compatible HTML" in msgs[0]["content"]
    assert "Sourdough" in msgs[1]["content"]
    assert "- starter" in msgs[1]["content"]
    assert "DRAFT" in msgs[1]["content"]

