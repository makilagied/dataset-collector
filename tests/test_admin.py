from collections.abc import Iterator

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from collector.admin import csrf_token, jobs
from collector.api import app, get_sessionmaker
from collector.config import get_settings
from collector.demo import load_demo
from collector.ig.backfill import upsert_comment
from collector.models import Account, Comment, CommentFeatures, Job, Media, Sample
from collector.nlp.langid import HeuristicDetector
from collector.pipeline.process import process
from collector.security import encrypt_token
from collector.storage_filter import is_hit
from tests.fake_graph import FakeGraph

AUTH = ("admin", "pw-123")


@pytest.fixture
def settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_PASSWORD", AUTH[1])
    get_settings.cache_clear()


@pytest.fixture
def client(
    engine: Engine, db: Session, settings_env: None, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    import collector.nlp.langid as langid

    factory = sessionmaker(engine, expire_on_commit=False)
    app.dependency_overrides[get_sessionmaker] = lambda: factory
    # run jobs inline so tests can assert on their outcome
    monkeypatch.setattr(jobs.runner, "submit", jobs.run_job)
    monkeypatch.setattr(langid, "get_detector", HeuristicDetector)
    yield make_client(AUTH)
    app.dependency_overrides.clear()


def make_client(auth: tuple[str, str] | None) -> TestClient:
    client = TestClient(app)
    client.auth = auth
    return client


def csrf() -> str:
    return csrf_token(get_settings())


def post(client: TestClient, url: str, **data: str) -> tuple[int, str]:
    resp = client.post(url, data={"csrf": csrf(), **data}, follow_redirects=False)
    return resp.status_code, resp.headers.get("location", "")


@pytest.fixture
def account(db: Session) -> Account:
    acct = Account(
        ig_user_id="17841400000000001",
        username="duka_la_mama",
        access_token_enc=encrypt_token("TOKEN"),
        active=True,
    )
    db.add(acct)
    db.commit()
    return acct


# --- access control -------------------------------------------------------------------


def test_disabled_without_password(engine: Engine) -> None:
    get_settings.cache_clear()
    resp = make_client(AUTH).get("/admin")
    assert resp.status_code == 503
    assert "ADMIN_PASSWORD" in resp.text


def test_anonymous_get_redirects_to_login(client: TestClient) -> None:
    anon = make_client(None)
    resp = anon.get("/admin/comments?show=hits", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/login?next=%2Fadmin%2Fcomments%3Fshow%3Dhits"
    assert make_client(("admin", "wrong")).get("/admin", follow_redirects=False).status_code == 303
    assert anon.post("/admin/process", data={"csrf": csrf()}).status_code == 401
    assert client.get("/admin").status_code == 200  # Basic auth still works for scripts


def test_login_flow(client: TestClient) -> None:
    browser = make_client(None)
    page = browser.get("/admin/login?next=/admin/jobs")
    assert page.status_code == 200 and 'name="password"' in page.text
    assert csrf() not in page.text  # the CSRF secret is never shown before login

    bad = browser.post("/admin/login", data={"username": "admin", "password": "nope"})
    assert bad.status_code == 401 and "Wrong username or password" in bad.text

    ok = browser.post(
        "/admin/login",
        data={"username": "admin", "password": AUTH[1], "next": "/admin/jobs"},
        follow_redirects=False,
    )
    assert ok.status_code == 303 and ok.headers["location"] == "/admin/jobs"
    cookie = ok.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "path=/admin" in cookie

    assert browser.get("/admin/jobs").status_code == 200  # cookie alone is enough
    assert "Log out" in browser.get("/admin").text
    assert browser.get("/admin/login", follow_redirects=False).status_code == 303

    out = browser.post("/admin/logout", data={"csrf": csrf()}, follow_redirects=False)
    assert out.headers["location"] == "/admin/login"
    browser.cookies.clear()
    assert browser.get("/admin", follow_redirects=False).status_code == 303


def test_login_rejects_open_redirect(client: TestClient) -> None:
    resp = make_client(None).post(
        "/admin/login",
        data={"username": "admin", "password": AUTH[1], "next": "//evil.example/x"},
        follow_redirects=False,
    )
    assert resp.headers["location"] == "/admin"


def test_session_tokens(settings_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from collector.admin import SESSION_SECONDS, make_session, valid_session

    s = get_settings()
    token = make_session(s, now=1000)
    assert valid_session(s, token, now=1000 + SESSION_SECONDS - 1)
    assert not valid_session(s, token, now=1000 + SESSION_SECONDS + 1)  # expired
    assert not valid_session(s, token.replace(".", ".0", 1), now=1000)  # tampered
    assert not valid_session(s, "garbage", now=1000)
    monkeypatch.setenv("ADMIN_PASSWORD", "changed")
    get_settings.cache_clear()
    assert not valid_session(get_settings(), token, now=1000)  # password change logs out


def test_refused_through_tunnel_unless_allowed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    tunnel = {"X-Forwarded-For": "203.0.113.9"}
    assert client.get("/admin", headers=tunnel).status_code == 403
    monkeypatch.setenv("ADMIN_ALLOW_REMOTE", "true")
    get_settings.cache_clear()
    assert client.get("/admin", headers=tunnel).status_code == 200


def test_csrf_required_on_posts(client: TestClient, account: Account) -> None:
    resp = client.post(
        f"/admin/accounts/{account.id}/category", data={"category": "x"}, follow_redirects=False
    )
    assert resp.status_code == 403
    resp = client.post(
        f"/admin/accounts/{account.id}/category",
        data={"category": "x", "csrf": "forged"},
        follow_redirects=False,
    )
    assert resp.status_code == 403


def test_security_headers(client: TestClient) -> None:
    resp = client.get("/admin")
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["cache-control"] == "no-store"
    assert "default-src 'none'" in resp.headers["content-security-policy"]


def test_no_open_redirect(client: TestClient, account: Account) -> None:
    status, location = post(
        client,
        f"/admin/accounts/{account.id}/category",
        category="media",
        next="https://evil.example/x",
    )
    assert status == 303
    assert location.startswith("/admin?")


# --- pages ----------------------------------------------------------------------------


def test_all_pages_render_with_demo_data(client: TestClient, db: Session) -> None:
    load_demo(db)
    process(db, detector=HeuristicDetector())
    for url, expected in [
        ("/admin", "Lexicon hits"),
        ("/admin/accounts", "@demo_account"),
        ("/admin/comments", "wewe ni mjinga kabisa"),
        ("/admin/comments?show=hits", "mjinga"),
        ("/admin/batches", "Draw a new batch"),
        ("/admin/jobs", "No jobs yet"),
    ]:
        resp = client.get(url)
        assert resp.status_code == 200, url
        assert expected in resp.text, url


def test_comment_text_is_escaped(client: TestClient, db: Session, account: Account) -> None:
    media = Media(ig_media_id="M1", account_id=account.id, permalink="javascript:alert(1)")
    db.add(media)
    db.flush()
    upsert_comment(
        db, media.id, {"id": "x1", "text": "<script>alert('x')</script> mjinga"}, "webhook"
    )
    db.commit()
    page = client.get("/admin/comments").text
    assert "<script>alert" not in page
    assert "&lt;script&gt;" in page
    assert "javascript:alert" not in page  # non-https permalinks are not linked


def test_comment_filters(client: TestClient, db: Session) -> None:
    load_demo(db)
    process(db, detector=HeuristicDetector())
    hits = client.get("/admin/comments?show=hits").text
    assert "malaya tu huyu" in hits and "habari za asubuhi dada" not in hits
    search = client.get("/admin/comments?q=nguruwe").text
    assert "unakula kama nguruwe" in search and "malaya" not in search
    excluded = client.get("/admin/comments?show=excluded").text
    assert "emoji_only" in excluded
    assert "Page 1 of 1" in client.get("/admin/comments").text


# --- account management -----------------------------------------------------------------


def test_set_category_and_pause(client: TestClient, db: Session, account: Account) -> None:
    status, location = post(
        client,
        f"/admin/accounts/{account.id}/category",
        category=" Football ",
        next="/admin/accounts",
    )
    assert status == 303 and location.startswith("/admin/accounts?msg=")
    post(client, f"/admin/accounts/{account.id}/active", active="0")
    db.refresh(account)
    assert (account.category, account.active) == ("football", False)
    assert "paused" in client.get("/admin/accounts").text

    post(client, f"/admin/accounts/{account.id}/backfill")
    job = db.scalars(select(Job)).one()
    assert job.status == "failed" and "paused" in (job.error or "")

    post(client, f"/admin/accounts/{account.id}/active", active="1")
    db.refresh(account)
    assert account.active


def test_backfill_job_runs(client: TestClient, db: Session, account: Account) -> None:
    fake = FakeGraph()
    with respx.mock:
        respx.get(url__startswith="https://graph.instagram.com/").mock(side_effect=fake)
        status, location = post(
            client, f"/admin/accounts/{account.id}/backfill", since="2026-06-01"
        )
    assert status == 303 and "started" in location
    job = db.scalars(select(Job)).one()
    assert job.status == "done", job.error
    assert job.params == {"account_id": account.id, "since": "2026-06-01"}
    assert "new" in (job.result or "")
    assert db.scalars(select(Comment)).first() is not None
    assert "TOKEN" not in (job.result or "")


def test_bad_since_date(client: TestClient, account: Account) -> None:
    status, location = post(client, f"/admin/accounts/{account.id}/backfill", since="31/01/26")
    assert status == 303 and "kind=error" in location


def test_demo_account_cannot_backfill(client: TestClient, db: Session) -> None:
    acct, _ = load_demo(db)
    post(client, f"/admin/accounts/{acct.id}/backfill")
    job = db.scalars(select(Job)).one()
    assert job.status == "failed" and "demo" in (job.error or "")


def test_subscribe_job(client: TestClient, db: Session, account: Account) -> None:
    with respx.mock:
        respx.post("https://graph.instagram.com/v25.0/me/subscribed_apps").respond(
            json={"success": True}
        )
        post(client, f"/admin/accounts/{account.id}/subscribe")
    assert db.scalars(select(Job)).one().status == "done"


def test_connect_link(client: TestClient) -> None:
    page = client.get("/admin/accounts?consent_ref=CF-7").text
    assert "https://example.test/connect?consent_ref=CF-7" in page


# --- jobs --------------------------------------------------------------------------------


def test_process_job_and_duplicate_guard(client: TestClient, db: Session) -> None:
    load_demo(db)
    post(client, "/admin/process")
    job = db.scalars(select(Job)).one()
    assert job.status == "done" and "45 processed" in (job.result or "")
    assert db.scalars(select(CommentFeatures)).first() is not None

    db.add(Job(kind="process", params={"all": False}, status="running"))
    db.commit()
    status, location = post(client, "/admin/process")
    assert "already" in location and "kind=warn" in location


def test_prune_job(client: TestClient, db: Session) -> None:
    load_demo(db)
    total = db.scalar(select(func.count()).select_from(Comment))
    post(client, "/admin/prune", dry_run="1")
    job = db.scalars(select(Job).order_by(Job.id.desc())).first()
    assert job is not None and job.status == "done" and "would be deleted" in (job.result or "")
    assert db.scalar(select(func.count()).select_from(Comment)) == total

    status, location = post(client, "/admin/prune")
    assert "kind=error" in location  # not confirmed

    post(client, "/admin/prune", confirm="1")
    texts = list(db.scalars(select(Comment.text_raw)))
    assert 0 < len(texts) < total and all(is_hit(t or "") for t in texts)


def test_refresh_tokens_job(client: TestClient, db: Session) -> None:
    post(client, "/admin/tokens/refresh")
    job = db.scalars(select(Job)).one()
    assert job.status == "done" and "no tokens expire" in (job.result or "")


def test_unexpected_errors_are_recorded_and_redacted(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(session: Session, params: dict[str, object]) -> str:
        raise RuntimeError("GET https://x/y?access_token=SECRET failed")

    monkeypatch.setitem(jobs.HANDLERS, "process", boom)
    post(client, "/admin/process")
    job = db.scalars(select(Job)).one()
    assert job.status == "failed"
    assert "SECRET" not in (job.error or "") and "RuntimeError" in (job.error or "")
    assert "failed" in client.get("/admin/jobs").text


def test_retry_failed_job(client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    load_demo(db)
    calls: list[dict[str, object]] = []

    def flaky(session: Session, params: dict[str, object]) -> str:
        calls.append(params)
        if len(calls) == 1:
            raise jobs.JobError("temporary failure")
        return "ok now"

    monkeypatch.setitem(jobs.HANDLERS, "process", flaky)
    post(client, "/admin/process", all="1")
    failed = db.scalars(select(Job)).one()
    assert failed.status == "failed"
    assert f"/admin/jobs/{failed.id}/retry" in client.get("/admin/jobs").text

    status, location = post(client, f"/admin/jobs/{failed.id}/retry")
    assert status == 303 and location.startswith("/admin/jobs?") and "kind=ok" in location
    old, new = db.scalars(select(Job).order_by(Job.id)).all()
    db.refresh(old)
    assert old.status == "failed"  # history kept
    assert (new.kind, new.params, new.status, new.result) == (
        "process",
        {"all": True},
        "done",
        "ok now",
    )
    assert calls == [{"all": True}, {"all": True}]

    # only failed / interrupted jobs can be retried
    status, location = post(client, f"/admin/jobs/{new.id}/retry")
    assert "kind=error" in location
    assert "kind=error" in post(client, "/admin/jobs/999999/retry")[1]
    assert f"/admin/jobs/{new.id}/retry" not in client.get("/admin/jobs").text


def test_mark_interrupted(db: Session) -> None:
    db.add_all(
        [
            Job(kind="process", params={}, status="running"),
            Job(kind="process", params={}, status="done"),
        ]
    )
    db.commit()
    assert jobs.mark_interrupted(db) == 1
    assert sorted(j.status for j in db.scalars(select(Job))) == ["done", "interrupted"]


def test_unknown_job_kind(db: Session) -> None:
    with pytest.raises(jobs.JobError):
        jobs.enqueue(db, sessionmaker(), "nope", {})


# --- batches -------------------------------------------------------------------------------


def test_create_and_download_batch(client: TestClient, db: Session) -> None:
    load_demo(db)
    process(db, detector=HeuristicDetector())
    status, location = post(
        client,
        "/admin/batches",
        batch="ui-1",
        size="10",
        mix="50/30/20",
        seed="3",
        scorer="stub",
        next="/admin/batches",
    )
    assert status == 303 and "kind=ok" in location
    assert len(db.scalars(select(Sample)).all()) == 10

    resp = client.get("/admin/batches/ui-1/export?fmt=csv")
    assert resp.status_code == 200
    assert resp.headers["content-disposition"] == 'attachment; filename="ui-1.csv"'
    text = resp.content.decode("utf-8-sig")
    assert text.startswith("sample_id,text_raw")
    assert "demo_user_" not in text
    assert "ui-1" in client.get("/admin/batches").text

    assert client.get("/admin/batches/ui-1/export?fmt=jsonl").status_code == 200
    assert client.get("/admin/batches/nope/export").status_code == 404


@pytest.mark.parametrize(
    "form",
    [
        {"batch": "bad name!", "size": "5"},
        {"batch": "b", "size": "lots"},
        {"batch": "b", "size": "5", "mix": "90/20/0"},
    ],
)
def test_batch_validation(client: TestClient, db: Session, form: dict[str, str]) -> None:
    status, location = post(client, "/admin/batches", **form)
    assert status == 303 and "kind=error" in location


# --- posts ---------------------------------------------------------------------------------


def test_posts_page_shows_reported_vs_collected(client: TestClient, db: Session) -> None:
    acct, _ = load_demo(db)
    process(db, detector=HeuristicDetector())
    # Like a real Development-mode app: Instagram says 3 comments, none were returned.
    db.add(
        Media(
            ig_media_id="M-hidden",
            account_id=acct.id,
            caption="Picha yangu mpya",
            comments_count=3,
            media_type="IMAGE",
        )
    )
    db.commit()

    page = client.get(f"/admin/posts?account={acct.id}").text
    assert "Picha yangu mpya" in page and "Mechi ya leo" in page
    assert ">4</div>" in page  # posts card: 3 demo posts + 1
    assert "−3" in page  # per-row gap chip for the post with hidden comments
    assert "Live mode" in page  # hint shown when comments are missing
    assert 'href="/admin/comments?media=' in page


def test_posts_page_empty_and_nav(client: TestClient) -> None:
    page = client.get("/admin/posts").text
    assert "No posts yet" in page
    assert 'href="/admin/posts"' in client.get("/admin").text


def test_comments_filtered_by_post(client: TestClient, db: Session) -> None:
    load_demo(db)
    process(db, detector=HeuristicDetector())
    m3 = db.scalars(select(Media).where(Media.ig_media_id == "demo-M3")).one()

    page = client.get(f"/admin/comments?media={m3.id}").text
    assert "Post: " in page
    assert "unakula kama nguruwe" in page and "malaya tu huyu" not in page  # M2 comment
    assert f'name="media" value="{m3.id}"' in page  # filter survives re-filtering

    csv_text = client.get(f"/admin/comments/export?fmt=csv&media={m3.id}").content.decode()
    assert "unakula kama nguruwe" in csv_text and "malaya tu huyu" not in csv_text


def test_empty_filters_in_links_mean_all(client: TestClient, db: Session) -> None:
    load_demo(db)
    for url in [
        "/admin/comments?account=&show=all&q=&media=&root=&page=2",
        "/admin/posts?account=&page=1",
        "/admin/comments/export?account=&show=all&q=&media=&root=&fmt=csv",
    ]:
        assert client.get(url).status_code == 200, url
    assert client.get("/admin/comments?account=abc").status_code == 422
