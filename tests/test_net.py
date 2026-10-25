from collections.abc import Callable

import httpx
import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from collector import net
from collector.admin import jobs
from collector.models import Job


def flaky_network(down_for: int) -> Callable[[str], bool]:
    """A reachability check that reports the host down for the first `down_for` probes."""
    probes = [0]

    def check(host: str) -> bool:
        probes[0] += 1
        return probes[0] > down_for

    return check


def test_wait_for_network_polls_until_back() -> None:
    sleeps: list[float] = []
    assert net.wait_for_network("x.test", sleep=sleeps.append, check=flaky_network(3)) is True
    assert sleeps == [net.POLL_S] * 3
    assert net.wait_for_network("x.test", sleep=sleeps.append, check=lambda h: True) is False


def test_call_waits_out_an_outage_however_long() -> None:
    down = flaky_network(50)  # far more probes than `attempts`
    calls = [0]

    def request() -> str:
        calls[0] += 1
        if not down("x.test"):
            raise RuntimeError("page.goto: net::ERR_INTERNET_DISCONNECTED at https://x.test/")
        return "ok"

    assert net.call(request, "x.test", sleep=lambda s: None, check=down) == "ok"


def test_call_raises_real_errors() -> None:
    def broken() -> None:
        raise ValueError("bad json")

    with pytest.raises(ValueError):
        net.call(broken, "x.test", sleep=lambda s: None)

    calls = [0]

    def refused() -> None:  # a network-looking error while the host is reachable
        calls[0] += 1
        raise RuntimeError("net::ERR_CONNECTION_RESET")

    with pytest.raises(RuntimeError):
        net.call(refused, "x.test", attempts=3, sleep=lambda s: None)
    assert calls[0] == 3


def test_http_transport_waits_for_network(monkeypatch: pytest.MonkeyPatch) -> None:
    state = {"up": False, "sent": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["sent"] += 1
        if not state["up"]:
            raise httpx.ConnectError("no route to host")
        return httpx.Response(200, json={"ok": True})

    def sleep(s: float) -> None:
        state["up"] = True  # the network comes back while we wait

    monkeypatch.setattr(net, "online", lambda host, *a, **k: bool(state["up"]))
    transport = net.WaitingTransport(httpx.MockTransport(handler), sleep=sleep)
    with httpx.Client(transport=transport) as client:
        assert client.get("https://graph.instagram.com/me").json() == {"ok": True}
    assert state["sent"] == 2

    # reachable but failing: raised, so GraphClient's own retries can handle it
    state["up"] = False
    monkeypatch.setattr(net, "online", lambda host, *a, **k: True)
    with httpx.Client(transport=transport) as client, pytest.raises(httpx.ConnectError):
        client.get("https://graph.instagram.com/me")


def test_job_shows_it_is_waiting_and_then_succeeds(
    engine: Engine, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = sessionmaker(engine, expire_on_commit=False)
    seen: list[str | None] = []

    def handler(session: Session, params: dict[str, object]) -> str:
        def sleep(s: float) -> None:
            with factory() as other:
                seen.append(other.scalars(select(Job.result)).one())

        net.wait_for_network("x.test", sleep=sleep, check=flaky_network(2))
        return "done after outage"

    monkeypatch.setitem(jobs.HANDLERS, "process", handler)
    db.add(Job(kind="process", params={}, status="queued"))
    db.commit()
    job_id = db.scalars(select(Job.id)).one()
    jobs.run_job(factory, job_id)

    assert seen and all("network down" in (s or "") for s in seen)
    job = db.get(Job, job_id)
    assert job is not None
    db.refresh(job)
    assert (job.status, job.result) == ("done", "done after outage")
