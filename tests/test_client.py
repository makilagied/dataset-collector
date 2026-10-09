import json
from collections.abc import Iterator

import httpx
import pytest
import respx

from collector.ig.client import GraphClient, GraphError, RetryableError, strip_token

URL = "https://graph.instagram.com/v25.0/me/media"


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def client(sleeps: list[float]) -> Iterator[GraphClient]:
    with httpx.Client() as http:
        yield GraphClient(
            "SECRET-TOKEN", http=http, version="v25.0", max_attempts=4, sleep=sleeps.append
        )


@respx.mock
def test_retries_429_then_succeeds(client: GraphClient, sleeps: list[float]) -> None:
    respx.get(URL).mock(
        side_effect=[
            httpx.Response(429, json={"error": {"message": "slow down"}}),
            httpx.Response(503),
            httpx.Response(200, json={"data": [1]}),
        ]
    )
    assert client.get("me/media") == {"data": [1]}
    assert len(sleeps) == 2


@respx.mock
def test_throttle_code_uses_regain_hint(client: GraphClient, sleeps: list[float]) -> None:
    buc = {
        "123": [
            {
                "type": "instagram",
                "call_count": 100,
                "total_cputime": 10,
                "total_time": 10,
                "estimated_time_to_regain_access": 2,
            }
        ]
    }
    respx.get(URL).mock(
        side_effect=[
            httpx.Response(
                400,
                json={"error": {"message": "throttled", "code": 80002}},
                headers={"X-Business-Use-Case-Usage": json.dumps(buc)},
            ),
            httpx.Response(200, json={"data": []}),
        ]
    )
    client.get("me/media")
    assert sleeps == [120.0]


@respx.mock
def test_gives_up_after_max_attempts(client: GraphClient) -> None:
    route = respx.get(URL).respond(500)
    with pytest.raises(RetryableError):
        client.get("me/media")
    assert route.call_count == 4


@respx.mock
def test_client_error_not_retried_and_hides_token(client: GraphClient) -> None:
    route = respx.get(URL).respond(400, json={"error": {"message": "bad field", "code": 100}})
    with pytest.raises(GraphError) as exc:
        client.get("me/media")
    assert route.call_count == 1
    assert not isinstance(exc.value, RetryableError)
    assert "SECRET-TOKEN" not in str(exc.value)


@respx.mock
def test_high_usage_slows_down(client: GraphClient, sleeps: list[float]) -> None:
    respx.get(URL).respond(
        200, json={"data": []}, headers={"X-App-Usage": json.dumps({"call_count": 95})}
    )
    client.get("me/media")
    assert sleeps == [60.0]


@respx.mock
def test_pages_follow_next(client: GraphClient) -> None:
    respx.get(URL, params={"after": "b"}).respond(json={"data": [2]})
    respx.get(URL).respond(
        json={"data": [1], "paging": {"next": f"{URL}?after=b&access_token=SECRET-TOKEN"}}
    )
    pages = list(client.pages("me/media"))
    assert [p.data for p in pages] == [[1], [2]]
    assert pages[0].next_url is not None and "SECRET-TOKEN" not in pages[0].next_url


def test_strip_token() -> None:
    assert (
        strip_token("https://h/x?after=1&access_token=T&limit=5") == "https://h/x?after=1&limit=5"
    )
