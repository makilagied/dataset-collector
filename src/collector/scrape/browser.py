"""Persistent Chromium profile for the scrapers.

You log in by hand once (`collector scrape login <platform>`); the cookies stay in
the profile directory and later runs reuse them. Never commit or share that directory: it
holds live sessions for your accounts.
"""

import json
import logging
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from collector.scrape import instagram, tiktok
from collector.scrape.common import BlockedError, Pacer, ScrapeError

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Page, Response

log = logging.getLogger(__name__)

LOGIN_URLS = {
    "instagram": "https://www.instagram.com/accounts/login/",
    "facebook": "https://www.facebook.com/login/",
    "tiktok": "https://www.tiktok.com/login",
}
# (cookie name, site domain): Instagram and TikTok both call theirs "sessionid".
SESSION_COOKIES = {
    "instagram": ("sessionid", "instagram.com"),
    "facebook": ("c_user", "facebook.com"),
    "tiktok": ("sessionid", "tiktok.com"),
}

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
        options: dict[str, Any] = {
            "headless": headless,
            "locale": "en-US",
            "viewport": {"width": 1280, "height": 900},
            # Don't announce automation (navigator.webdriver, the "controlled by automated
            # software" bar): TikTok answers its own API calls with empty bodies otherwise.
            "args": ["--disable-blink-features=AutomationControlled"],
            "ignore_default_args": ["--enable-automation"],
        }
        try:  # the installed Google Chrome looks like a normal visitor's browser
            context = pw.chromium.launch_persistent_context(
                str(profile_dir), channel="chrome", **options
            )
        except Exception as e:
            log.info("Google Chrome not available (%s); using Playwright's Chromium", e)
            context = pw.chromium.launch_persistent_context(str(profile_dir), **options)
        try:
            yield context
        finally:
            context.close()


def first_page(context: "BrowserContext") -> "Page":
    return context.pages[0] if context.pages else context.new_page()


def logged_in(context: "BrowserContext", platform: str) -> bool:
    name, domain = SESSION_COOKIES[platform]
    return any(
        c.get("name") == name and str(c.get("domain", "")).lstrip(".").endswith(domain)
        for c in context.cookies()
    )


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


# --- tiktok -------------------------------------------------------------------------

TIKTOK_DATA_SCRIPTS = ("__UNIVERSAL_DATA_FOR_REHYDRATION__", "SIGI_STATE", "__NEXT_DATA__")
TIKTOK_COMMENT_ITEM = (
    '[data-e2e^="comment-level"], [class*="CommentObjectWrapper"], [class*="CommentItemContainer"]'
)
TIKTOK_OPEN_COMMENTS = ('[data-e2e="comment-icon"]', '[data-e2e="browse-comment-icon"]')
TIKTOK_CLOSE_VIDEO = '[data-e2e="browse-close"]'  # the X of a video opened from a grid
TIKTOK_VIDEO_LISTS = ("/api/post/item_list", "/api/challenge/item_list")
# Close buttons of the "Log in to TikTok" pop-up shown to logged-out visitors.
# Not a generic dialog close: an opened video is a dialog too, and closing it ends the scrape.
TIKTOK_MODAL_CLOSE = ('[data-e2e="modal-close-inner-button"]', '[data-e2e="modal-close-button"]')
TIKTOK_REPLIES = re.compile(r"^\s*view (\d+ )?(more )?repl(y|ies)|^\s*view more\s*$", re.I)
_CAPTCHA_JS = """
() => [...document.querySelectorAll(
  '[id*="captcha" i], [class*="captcha" i], iframe[src*="captcha"]'
)].some(e => { const r = e.getBoundingClientRect(); return r.width > 100 && r.height > 100; })
"""
_VIDEO_ID = re.compile(r"/(?:video|photo)/(\d+)")
# Finds TikTok's login pop-up by its content (the login options) and clicks its close
# button. Returns "closed", "open" (no button found) or "none".
_CLOSE_LOGIN_JS = """
() => {
  const options = document.querySelector(
    '[class*="LoginOptionContainer"], [data-e2e="login-modal"]'
  );
  if (!options) return "none";
  const modal = options.closest('[class*="ModalContainer"]') || options.parentElement;
  const close = modal && modal.querySelector(
    '[data-e2e*="close"], [aria-label*="lose"], [class*="CloseWrapper"], [class*="CloseButton"]'
  );
  if (!close) return "open";
  close.click();
  return "closed";
}
"""


class TikTokSite:
    """tiktok.Site on a logged-in browser page: collects what the page itself loads."""

    def __init__(
        self,
        page: "Page",
        pacer: Pacer,
        *,
        max_scrolls: int = 300,
        max_reply_clicks: int = 300,
        captcha_wait: float = 180.0,
    ):
        self.page = page
        self.pacer = pacer
        self.max_scrolls = max_scrolls
        self.max_reply_clicks = max_reply_clicks
        self.captcha_wait = captcha_wait
        self.video_bodies: list[Any] = []
        self.comment_bodies: list[tuple[str, Any]] = []
        self.known: dict[str, dict[str, Any]] = {}  # videos seen in grids, by id
        self.grid_url: str | None = None  # the profile / tag page the videos are listed on
        page.on("response", self._on_response)

    def _on_response(self, response: "Response") -> None:
        url = response.url
        if "tiktok.com/api/" not in url:
            return
        is_comments = "/api/comment/list" in url
        # A profile's and a tag's own lists; not /api/recommend/item_list (For You videos).
        if not is_comments and not any(k in url for k in TIKTOK_VIDEO_LISTS):
            return
        try:
            data = response.json()
        except Exception as e:  # empty, redirected or aborted responses
            log.info("unreadable TikTok response %s: %s", url.split("?")[0], e)
            return
        if is_comments:
            n = len(data.get("comments") or []) if isinstance(data, dict) else 0
            log.info("TikTok %s: %d comments", url.split("?")[0].split("/api/")[1], n)
        if is_comments:
            self.comment_bodies.append((url, data))
        else:
            self.video_bodies.append(data)

    # --- pages ------------------------------------------------------------------

    def _goto(self, url: str) -> None:
        try:
            self.page.goto(url, wait_until="domcontentloaded")
        except Exception as e:  # TikTok answers bot-looking page loads with HTTP errors
            raise ScrapeError(f"TikTok refused to open {url} ({str(e).splitlines()[0]})") from e
        self.page.wait_for_timeout(3000)
        self._check_blocked()

    def _close_video(self) -> None:
        """Close a video opened from a grid, back to the grid."""
        try:
            button = self.page.locator(TIKTOK_CLOSE_VIDEO).first
            if button.count() and button.is_visible():
                button.click(timeout=3000)
                self.page.wait_for_timeout(1000)
        except Exception:  # already closed
            pass

    def _click_open(self, video_id: str) -> bool:
        """Open a video by clicking it in the grid on screen, as a visitor does. TikTok
        refuses many direct page loads of video URLs but not this. Logged out, after a few
        videos TikTok keeps putting its login pop-up back over the grid; a fresh load of
        the grid page gets past it."""
        if self._click_once(video_id):
            return True
        if not self.grid_url:
            return False
        log.info("reloading %s to get past TikTok's login pop-up", self.grid_url)
        self._goto(self.grid_url)
        self.comment_bodies.clear()
        return self._click_once(video_id)

    def _click_once(self, video_id: str) -> bool:
        self._close_video()
        self._close_login_popup()
        link = self.page.locator(f'a[href*="/video/{video_id}"], a[href*="/photo/{video_id}"]')
        try:
            if not link.count():
                return False
            link.first.scroll_into_view_if_needed(timeout=3000)
            try:
                link.first.click(timeout=5000)
            except Exception:  # a pop-up came up over the grid: close it, try once more
                self._close_login_popup()
                link.first.click(timeout=5000)
            # The address changes once the video has opened, which takes a few seconds.
            self.page.wait_for_url(re.compile(re.escape(video_id)), timeout=12000)
            self.page.wait_for_timeout(2000)
        except Exception as e:
            log.info("could not open video %s from the grid: %s", video_id, e)
            return False
        self._check_blocked()
        return True

    def _check_blocked(self) -> None:
        if "/login" in self.page.url:
            raise BlockedError(
                "TikTok wants a login to go on: run `collector scrape login tiktok` "
                "(or Log in to TikTok on the Scrape page) and scrape again"
            )
        self._close_login_popup()
        if not self._captcha():
            return
        log.warning("TikTok shows a captcha: solve it in the browser window")
        deadline = time.monotonic() + self.captcha_wait
        while self._captcha():
            if time.monotonic() > deadline:
                raise BlockedError("TikTok shows a captcha; solve it in the browser window")
            self.page.wait_for_timeout(2000)
        self.page.wait_for_timeout(2000)

    def _close_login_popup(self) -> None:
        """Logged out, TikTok covers pages with a login pop-up; closing it is enough."""
        try:
            state = self.page.evaluate(_CLOSE_LOGIN_JS)
            if state == "none":
                for selector in TIKTOK_MODAL_CLOSE:
                    button = self.page.locator(selector).first
                    if button.count() and button.is_visible():
                        button.click(timeout=2000)
                        state = "closed"
                        break
            if state == "open":
                self.page.keyboard.press("Escape")
            if state != "none":
                log.debug("closed TikTok's login pop-up")
                self.page.wait_for_timeout(800)
        except Exception:  # gone before we clicked, or navigating
            pass

    def _captcha(self) -> bool:
        try:
            return bool(self.page.evaluate(_CAPTCHA_JS))
        except Exception:  # navigating
            return False

    def _page_json(self) -> list[Any]:
        docs = []
        for script_id in TIKTOK_DATA_SCRIPTS:
            loc = self.page.locator(f"script#{script_id}")
            if not loc.count():
                continue
            try:
                docs.append(json.loads(loc.first.text_content() or ""))
            except ValueError:
                continue
        return docs

    # --- tiktok.Site --------------------------------------------------------------

    def list_videos(self, url: str, enough: Callable[[tiktok.Videos], bool]) -> tiktok.Videos:
        self.video_bodies.clear()
        self._goto(url)
        self.grid_url = url
        videos: dict[str, dict[str, Any]] = {}

        def absorb(payload: Any) -> None:
            for v in tiktok.find_videos(payload):
                videos.setdefault(str(v["id"]), v)

        for doc in self._page_json():
            absorb(doc)
        try:
            return self._scroll_grid(videos, absorb, enough)
        finally:
            self.known.update(videos)

    def _scroll_grid(
        self,
        videos: dict[str, dict[str, Any]],
        absorb: Callable[[Any], None],
        enough: Callable[[tiktok.Videos], bool],
    ) -> tiktok.Videos:
        idle = 0
        for _ in range(self.max_scrolls):
            before = len(videos)
            while self.video_bodies:
                absorb(self.video_bodies.pop(0))
            if enough(list(videos.values())):
                break
            idle = idle + 1 if len(videos) == before else 0
            if idle >= 4:  # end of the grid
                break
            self.page.mouse.wheel(0, 3000)
            self.pacer.wait(0.6)
            self.page.wait_for_timeout(1500)
            self._check_blocked()
        return list(videos.values())

    def open_video(self, url: str) -> dict[str, Any] | None:
        """Open a video the way a visitor does, by clicking it in a grid. Loading a video
        URL directly is a last resort: TikTok often refuses it, and after a refusal it
        refuses the next pages too."""
        self.comment_bodies.clear()
        wanted = _VIDEO_ID.search(url)
        video_id = wanted.group(1) if wanted else ""
        if video_id and self._click_open(video_id):
            return self.known.get(video_id) or self._video_on_page(video_id)
        if video_id in self.known:  # listed from this grid, yet it would not open
            raise ScrapeError(f"could not open {url} from the grid")
        author = re.match(r"https://www\.tiktok\.com/(@[^/]+)/", url)
        if video_id and author:  # a single video: find it on its author's profile
            self._goto(f"{tiktok.BASE}/{author.group(1)}")
            self.comment_bodies.clear()
            if self._click_open(video_id):
                return self._video_on_page(video_id) or self.known.get(video_id)
        self._goto(url)
        found = _VIDEO_ID.search(self.page.url)
        return self._video_on_page(found.group(1) if found else video_id)

    def _video_on_page(self, video_id: str) -> dict[str, Any] | None:
        videos = [v for doc in self._page_json() for v in tiktok.find_videos(doc)]
        if video_id:
            videos = [v for v in videos if str(v["id"]) == video_id]
        return videos[0] if videos else None

    def read_comments(self, on_comments: tiktok.OnComments) -> None:
        self._open_comment_panel()
        clicks = idle = 0
        more = True  # the top-level list says it has more
        for _ in range(self.max_scrolls):
            got = bool(self.comment_bodies)
            while self.comment_bodies:
                url, data = self.comment_bodies.pop(0)
                if not on_comments(tiktok.find_comments(data)):
                    return
                if "/reply" not in url and isinstance(data, dict):
                    more = bool(data.get("has_more"))
            clicked = 0
            if clicks < self.max_reply_clicks:
                clicked = self._expand_replies(self.max_reply_clicks - clicks)
                clicks += clicked
            idle = 0 if got or clicked else idle + 1
            if idle >= (4 if more else 2):
                return
            self._scroll_comments()
            self.pacer.wait(0.5)
            self.page.wait_for_timeout(1200)
            self._check_blocked()

    # --- comment panel ------------------------------------------------------------

    def _open_comment_panel(self) -> None:
        if self.page.locator(TIKTOK_COMMENT_ITEM).count():
            return
        try:
            tab = self.page.get_by_role("tab", name=re.compile(r"^comments", re.I))
            if tab.count() and tab.first.is_visible():
                tab.first.click(timeout=3000)
                self.page.wait_for_timeout(2000)
                return
            for selector in TIKTOK_OPEN_COMMENTS:
                button = self.page.locator(selector).first
                if button.count() and button.is_visible():
                    button.click(timeout=3000)
                    self.page.wait_for_timeout(2000)
                    return
        except Exception as e:  # layout changed; scrolling may still load comments
            log.info("could not open the comment panel: %s", e)

    def _scroll_comments(self) -> None:
        items = self.page.locator(TIKTOK_COMMENT_ITEM)
        try:
            n = items.count()
            if n:
                last = items.nth(n - 1)
                last.scroll_into_view_if_needed(timeout=3000)
                box = last.bounding_box()
                if box:
                    self.page.mouse.move(box["x"] + box["width"] / 2, box["y"] + 5)
        except Exception:  # the list re-rendered under us; the wheel below still scrolls
            pass
        self.page.mouse.wheel(0, 2500)

    def _expand_replies(self, budget: int) -> int:
        clicked = 0
        buttons = self.page.get_by_text(TIKTOK_REPLIES)
        try:
            for i in range(min(buttons.count(), 5, budget)):
                button = buttons.nth(i)
                if not button.is_visible():
                    continue
                button.click(timeout=3000)
                clicked += 1
                self.pacer.wait(0.4)
                self.page.wait_for_timeout(800)
        except Exception as e:  # a button vanished as the list changed
            log.debug("reply button: %s", e)
        return clicked
