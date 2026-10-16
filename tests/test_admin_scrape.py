# Fixtures imported from test_admin shadow the test parameters of the same name.
# ruff: noqa: F811
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

import collector.scrape.run as scrape_run
from collector.models import Job
from collector.scrape.common import ScrapeStats, ensure_account
from tests.test_admin import client, post, settings_env  # noqa: F401  (fixtures)


def last_job(db: Session) -> Job:
    db.expire_all()
    return db.scalars(select(Job).order_by(Job.id.desc())).first()  # type: ignore[return-value]


@pytest.fixture
def fake_scrape(monkeypatch: pytest.MonkeyPatch, engine: Engine) -> list[dict[str, Any]]:
    """Replace the browser run; records calls and what the job showed mid-run."""
    calls: list[dict[str, Any]] = []
    other = sessionmaker(engine)

    def run_scrape(
        session: Session, platform: str, targets: list[str], options: Any, progress: Any = None
    ) -> scrape_run.ScrapeRun:
        calls.append({"platform": platform, "targets": targets, "options": options})
        if progress:
            progress("mama: posts: 1 scraped")
            with other() as s:  # progress is committed, so another session sees it
                calls[-1]["mid_run"] = s.scalars(select(Job.result)).one()
        run = scrape_run.ScrapeRun()
        for t in targets:
            if t.startswith("bad"):
                run.results.append(scrape_run.TargetResult(t, error="skipped: private"))
            else:
                run.results.append(scrape_run.TargetResult(t, stats=ScrapeStats(posts_seen=1)))
        if "blockme" in targets:
            run.blocked = "Instagram blocked the request (HTTP 429)"
        return run

    monkeypatch.setattr(scrape_run, "run_scrape", run_scrape)
    monkeypatch.setattr(scrape_run, "unavailable_reason", lambda: None)
    return calls


def test_page_renders(client: TestClient, db: Session) -> None:
    ensure_account(db, "instagram", "mama", "udaku")
    html = client.get("/admin/scrape").text
    assert "Log in to Instagram" in html and "Start Facebook scrape" in html
    assert "mama" in html and "udaku" in html  # scraped sources table


def test_page_explains_when_browser_unavailable(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scrape_run, "unavailable_reason", lambda: "Playwright is not installed")
    html = client.get("/admin/scrape").text
    assert "Scraping is off in this copy" in html and "collector ui" in html
    status, loc = post(
        client,
        "/admin/scrape",
        platform="instagram",
        targets="mama",
        max_posts="5",
        max_comments="10",
        delay="4",
    )
    assert status == 303 and "kind=error" in loc


def test_scrape_job_runs_with_options(
    client: TestClient,
    db: Session,
    fake_scrape: list[dict[str, Any]],
) -> None:
    status, loc = post(
        client,
        "/admin/scrape",
        platform="instagram",
        targets="@mama\n#udaku, @mama",
        max_posts="5",
        max_comments="100",
        delay="2.5",
        since="2026-02-01",
        category="Udaku",
        process="1",
    )
    assert status == 303 and "kind=ok" in loc
    call = fake_scrape[0]
    assert call["targets"] == ["@mama", "#udaku"]  # split and de-duplicated
    o = call["options"]
    assert (o.max_posts, o.max_comments, o.delay, str(o.since)) == (5, 100, 2.5, "2026-02-01")
    assert (o.category, o.refresh) == ("udaku", False)
    assert call["mid_run"] == "mama: posts: 1 scraped"
    job = last_job(db)
    assert job.status == "done"
    assert "@mama: posts: 1 scraped" in (job.result or "")
    assert "processed 0 new comments" in (job.result or "")


def test_scrape_job_fails_when_blocked(
    client: TestClient,
    db: Session,
    fake_scrape: list[dict[str, Any]],
) -> None:
    post(
        client,
        "/admin/scrape",
        platform="instagram",
        targets="mama blockme",
        max_posts="5",
        max_comments="100",
        delay="4",
    )
    job = last_job(db)
    assert job.status == "failed"
    assert "Stopped: Instagram blocked" in (job.error or "")
    assert "mama: posts: 1 scraped" in (job.error or "")  # partial results still shown


def test_scrape_job_fails_when_every_target_fails(
    client: TestClient,
    db: Session,
    fake_scrape: list[dict[str, Any]],
) -> None:
    post(
        client,
        "/admin/scrape",
        platform="facebook",
        targets="bad1",
        max_posts="5",
        max_comments="100",
        delay="4",
    )
    assert last_job(db).status == "failed"


@pytest.mark.parametrize(
    ("form", "message"),
    [
        ({"targets": ""}, "at+least+one+target"),
        ({"targets": "not a user!"}, "Not+a+valid"),
        ({"max_posts": "0"}, "max+posts+must+be"),
        ({"delay": "0.2"}, "delay+must+be"),
        ({"max_comments": "lots"}, "Numbers+only"),
        ({"since": "yesterday"}, "Since+must+be"),
        ({"platform": "myspace"}, "Unknown+platform"),
    ],
)
def test_scrape_form_validation(
    client: TestClient,
    db: Session,
    fake_scrape: list[dict[str, Any]],
    form: dict[str, str],
    message: str,
) -> None:
    data = {
        "platform": "instagram",
        "targets": "mama",
        "max_posts": "5",
        "max_comments": "100",
        "delay": "4",
        **form,
    }
    status, loc = post(client, "/admin/scrape", **data)
    assert status == 303 and "kind=error" in loc and message in loc
    assert not fake_scrape and last_job(db) is None


def test_login_job(
    client: TestClient,
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def wait_for_login(session: Session, platform: str) -> bool:
        scrape_run.record_login(session, platform, True)
        return True

    monkeypatch.setattr(scrape_run, "unavailable_reason", lambda: None)
    monkeypatch.setattr(scrape_run, "wait_for_login", wait_for_login)
    status, loc = post(client, "/admin/scrape/login", platform="instagram")
    assert status == 303 and "browser+window+will+open" in loc
    job = last_job(db)
    assert (job.kind, job.status, job.result) == (
        "scrape_login",
        "done",
        "instagram: logged in, session saved",
    )
    assert "logged in</span>" in client.get("/admin/scrape").text


def test_accounts_page_marks_scraped_sources(client: TestClient, db: Session) -> None:
    ensure_account(db, "facebook", "somepage")
    html = client.get("/admin/accounts").text
    assert '<span class="chip">scraped</span>' in html
    assert "Scraped source" in html
    assert "disabled>Backfill" in html


def test_comments_page_marks_hit_words(client: TestClient, db: Session) -> None:
    from collector.ig.backfill import upsert_comment, upsert_media
    from collector.models import Media

    account = ensure_account(db, "instagram", "mama")
    upsert_media(db, account.id, {"id": "ig:1"})
    media_id = db.scalars(select(Media.id)).one()
    upsert_comment(db, media_id, {"id": "ig:9", "text": "kaa kwakutulia wewe malayq"}, "scrape")
    upsert_comment(db, media_id, {"id": "ig:10", "text": "<b>wewe</b> mjinga"}, "scrape")
    db.commit()
    html = client.get("/admin/comments").text
    assert 'wewe <mark title="hit word">malayq</mark>' in html
    assert "&lt;b&gt;wewe&lt;/b&gt; <mark" in html  # comment text stays escaped


def test_tiktok_scrape_job(
    client: TestClient,
    db: Session,
    fake_scrape: list[dict[str, Any]],
) -> None:
    html = client.get("/admin/scrape").text
    assert "Log in to TikTok" in html and "Start TikTok scrape" in html
    status, loc = post(
        client,
        "/admin/scrape",
        platform="tiktok",
        targets="@mama\nhttps://www.tiktok.com/@mama/video/7301234567890123456",
        max_posts="5",
        max_comments="100",
        delay="4",
        since="2026-02-01",
    )
    assert status == 303 and "kind=ok" in loc
    call = fake_scrape[0]
    assert call["platform"] == "tiktok" and str(call["options"].since) == "2026-02-01"
    status, loc = post(
        client,
        "/admin/scrape",
        platform="tiktok",
        targets="not a user!",
        max_posts="5",
        max_comments="100",
        delay="4",
    )
    assert status == 303 and "kind=error" in loc and "TikTok" in loc
    ensure_account(db, "tiktok", "mama")
    assert '<span class="chip">TikTok</span>' in client.get("/admin/scrape").text
