"""Persistent Chromium profile for the scrapers.

You log in by hand once (`collector scrape login instagram|facebook`); the cookies stay in
the profile directory and later runs reuse them. Never commit or share that directory: it
holds live sessions for your accounts.
"""

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from collector.scrape import instagram
from collector.scrape.common import BlockedError, Pacer, ScrapeError

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Page, Response

LOGIN_URLS = {
    "instagram": "https://www.instagram.com/accounts/login/",
    "facebook": "https://www.facebook.com/login/",
}
SESSION_COOKIES = {"instagram": "sessionid", "facebook": "c_user"}

_FETCH_JS = """
async ({url, appId}) => {
  const csrf = (document.cookie.match(/(?:^|; )csrftoken=([^;]+)/) || [])[1] || "";
  const r = await fetch(url, {
    credentials: "include",
    headers: {"X-IG-App-ID": appId, "X-Requested-With": "XMLHttpRequest", "X-CSRFToken": csrf},
  });
  return {status: r.status, body: await r.text()};
}
"""


@contextmanager
def open_browser(profile_dir: Path, headless: bool = False) -> Iterator["BrowserContext"]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise ScrapeError(
            'Playwright is not installed: pip install -e ".[scrape]" '
            "&& python -m playwright install chromium"
        ) from e
    profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(profile_dir),
            headless=headless,
            locale="en-US",
            viewport={"width": 1280, "height": 900},
        )
        try:
            yield context
        finally:
            context.close()


def first_page(context: "BrowserContext") -> "Page":
    return context.pages[0] if context.pages else context.new_page()


def logged_in(context: "BrowserContext", platform: str) -> bool:
    return any(c.get("name") == SESSION_COOKIES[platform] for c in context.cookies())


def instagram_fetch(page: "Page") -> instagram.Fetch:
    """A Fetch that runs inside the logged-in instagram.com page (same cookies, same origin)."""
    if not page.url.startswith(instagram.BASE):
        page.goto(instagram.BASE + "/", wait_until="domcontentloaded")

    def fetch(path: str) -> dict[str, Any]:
        result = page.evaluate(_FETCH_JS, {"url": instagram.BASE + path, "appId": instagram.APP_ID})
        body = result["body"] if isinstance(result.get("body"), str) else json.dumps(result)
        return instagram.check_response(int(result["status"]), body)

    return fetch


def instagram_post_lister(
    page: "Page", pacer: Pacer, max_scrolls: int = 100
) -> instagram.ListPosts:
    """A ListPosts that scrolls the profile grid and collects the posts the page loads."""

    def list_posts(
        username: str, enough: Callable[[list[dict[str, Any]]], bool]
    ) -> list[dict[str, Any]]:
        texts: list[str] = []

        def on_response(response: "Response") -> None:
            if "graphql" not in response.url:
                return
            try:
                body = response.text()
            except Exception:  # redirects and aborted requests have no readable body
                return
            if "user_timeline_graphql_connection" in body:
                texts.append(body)

        page.on("response", on_response)
        try:
            page.goto(f"{instagram.BASE}/{quote(username)}/", wait_until="domcontentloaded")
            page.wait_for_timeout(4000)
            if any(s in page.url for s in ("/accounts/login", "/challenge")):
                raise BlockedError(f"Instagram sent us to {page.url}; log in again")
            posts: dict[str, dict[str, Any]] = {}
            idle = 0
            for _ in range(max_scrolls):
                before = len(posts)
                while texts:
                    try:
                        found = instagram.find_media(json.loads(texts.pop(0)))
                    except ValueError:
                        continue
                    for m in found:
                        posts.setdefault(str(m["pk"]), m)
                if enough(list(posts.values())):
                    break
                idle = idle + 1 if len(posts) == before else 0
                if idle >= 3:  # end of the grid (or nothing visible)
                    break
                page.mouse.wheel(0, 4000)
                pacer.wait(0.6)
                page.wait_for_timeout(1500)
            return list(posts.values())
        finally:
            page.remove_listener("response", on_response)

    return list_posts
