"""Facebook scraper: drives a logged-in page in a real browser and reads comments out of
the GraphQL responses the page receives while it expands them.

Facebook's GraphQL query ids rotate constantly, so nothing here calls an endpoint
directly: we open a post, switch the comment filter to "All comments", click the
"View more comments" / "View N replies" buttons, and parse every Comment node that shows
up in the traffic and in the page's embedded JSON. The button labels are English, so the
scraping account's Facebook language must be English.

Targets: a page / profile / group URL (its recent posts) or a post, video or reel URL.
"""

import base64
import json
import logging
import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

from sqlalchemy.orm import Session

from collector import net
from collector.models import Media
from collector.scrape.common import (
    BlockedError,
    Pacer,
    ScrapeError,
    ScrapeStats,
    Store,
    ensure_account,
    ts_iso,
)

if TYPE_CHECKING:
    from playwright.sync_api import Page, Response

log = logging.getLogger(__name__)

BASE = "https://www.facebook.com"
HOST = net.host_of(BASE)
POST_URL = re.compile(
    r"https://(?:www|web|m)\.facebook\.com/(?:"
    r"[\w.\-]+/(?:posts|videos)/[\w.\-]+"
    r"|reel/\d+"
    r"|permalink\.php\?story_fbid=\w+&(?:amp;)?id=\d+"
    r"|photo/?\?fbid=\d+"
    r"|watch/?\?v=\d+"
    r"|groups/[\w.\-]+/(?:posts|permalink)/\d+)"
)
EXPAND = re.compile(
    r"^(view (more|previous) comments|view all \d+ repl|view \d+ (more )?repl|view 1 reply"
    r"|\d+ (more )?repl(y|ies)|see more comments)",
    re.I,
)
SORT_BUTTON = re.compile(r"^(most relevant|top comments|newest)", re.I)
ALL_COMMENTS = re.compile(r"^all comments", re.I)
PARENT_KEYS = ("comment_direct_parent", "comment_parent", "parent_comment")
_NOT_PAGES = {"permalink.php", "story.php", "photo", "photo.php", "watch", "reel", "share"}


@dataclass(frozen=True)
class Target:
    kind: str  # page | post
    handle: str  # account name the comments are filed under
    url: str


def parse_target(raw: str) -> Target:
    raw = raw.strip()
    if "facebook.com" not in raw:
        if re.fullmatch(r"[\w.\-]+", raw):
            return Target("page", raw.lower(), f"{BASE}/{raw}")
        raise ScrapeError(f"not a Facebook page name or URL: {raw!r}")
    url = raw if "//" in raw else "https://" + raw
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    if not parts:
        raise ScrapeError(f"unrecognized Facebook URL: {raw!r}")
    if parts[0] == "groups" and len(parts) >= 2:
        handle = f"group:{parts[1]}"
        is_post = len(parts) >= 4
    elif parts[0] == "profile.php":
        handle = "id:" + (parse_qs(parsed.query).get("id") or ["?"])[0]
        is_post = False
    else:
        handle = parts[0].lower() if parts[0] not in _NOT_PAGES else "posts"
        is_post = parts[0] in _NOT_PAGES or len(parts) >= 3
    return Target("post" if is_post else "page", handle, url)


def post_key(url: str) -> str:
    """Stable id for a post URL: the numeric / pfbid id when there is one."""
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    for param in ("story_fbid", "fbid", "v"):
        if query.get(param):
            return query[param][0]
    parts = [p for p in parsed.path.split("/") if p]
    return parts[-1] if parts else url


def find_post_urls(text: str) -> list[str]:
    text = text.replace("\\/", "/").replace("&amp;", "&")
    urls = (re.sub(r"//(web|m)\.", "//www.", u) for u in POST_URL.findall(text))
    return list(dict.fromkeys(urls))


def iter_json_docs(text: str) -> Iterator[Any]:
    """GraphQL responses are one JSON document, or several separated by newlines."""
    text = text.removeprefix("for (;;);").strip()
    try:
        yield json.loads(text)
        return
    except ValueError:
        pass
    for line in text.splitlines():
        try:
            yield json.loads(line)
        except ValueError:
            continue


def find_comments(doc: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("__typename") == "Comment" and "body" in node:
                found.append(node)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(doc)
    return found


def _fbid(node: Any) -> str | None:
    """legacy_fbid, else the comment part of a base64 "comment:<post>_<comment>" id."""
    if not isinstance(node, dict):
        return None
    if node.get("legacy_fbid"):
        return str(node["legacy_fbid"])
    gid = node.get("id")
    if not isinstance(gid, str):
        return None
    try:
        decoded = base64.b64decode(gid + "=" * (-len(gid) % 4)).decode()
    except ValueError:
        return gid
    if decoded.startswith("comment:"):
        return decoded.split(":", 1)[1].rsplit("_", 1)[-1]
    return gid


def _count(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def comment_item(node: dict[str, Any]) -> dict[str, Any] | None:
    cid = _fbid(node)
    if not cid:
        return None
    body = node.get("body")
    parent = next((_fbid(node[k]) for k in PARENT_KEYS if node.get(k)), None)
    reactors = ((node.get("feedback") or {}).get("reactors")) or {}
    author = (node.get("author") or {}).get("id")
    return {
        "id": f"fb:{cid}",
        "text": (body or {}).get("text") or "",
        "timestamp": ts_iso(node.get("created_time")),
        "like_count": _count(reactors.get("count")),
        "parent_id": f"fb:{parent}" if parent and parent != cid else None,
        # Only used to compute author_hash; sanitize() drops it before storage.
        "from": {"id": f"fb:{author}"} if author else {},
    }


def store_docs(store: Store, media: Media, texts: Iterable[str], seen: set[str]) -> None:
    """Store every new Comment node found in raw response / script texts."""
    for text in texts:
        for doc in iter_json_docs(text):
            for node in find_comments(doc):
                item = comment_item(node)
                if item and item["id"] not in seen:
                    seen.add(item["id"])
                    store.comment(media, item)
    store.session.commit()


class FacebookScraper:
    def __init__(
        self,
        session: Session,
        page: "Page",
        pacer: Pacer,
        *,
        max_posts: int = 20,
        max_comments: int = 5000,
        category: str | None = None,
        refresh: bool = False,
        progress: Callable[[str], None] | None = None,
        only_hits: bool | None = None,
        max_rounds: int = 80,
    ):
        self.session = session
        self.page = page
        self.pacer = pacer
        self.max_posts = max_posts
        self.max_comments = max_comments
        self.category = category
        self.refresh = refresh
        self.only_hits = only_hits
        self.progress = progress
        self.max_rounds = max_rounds
        self.captured: list[str] = []
        page.on("response", self._on_response)

    def _on_response(self, response: "Response") -> None:
        if "/api/graphql" not in response.url:
            return
        try:
            self.captured.append(response.text())
        except Exception:  # redirects and aborted requests have no readable body
            pass

    def _drain(self) -> list[str]:
        texts, self.captured = self.captured, []
        return texts

    def scrape(self, raw_target: str) -> ScrapeStats:
        target = parse_target(raw_target)
        stats = ScrapeStats()
        urls = [target.url] if target.kind == "post" else self._page_posts(target.url)
        account = ensure_account(self.session, "facebook", target.handle, self.category)
        store = Store(self.session, account, stats, self.refresh, self.progress, self.only_hits)
        for url in urls[: self.max_posts]:
            media_id = f"fb:{post_key(url)}"
            if store.unchanged(media_id, None):
                stats.posts_skipped += 1
                continue
            try:
                self._post(store, url, media_id)
            except BlockedError:
                raise
            except ScrapeError as e:
                log.warning("%s: %s", url, e)
            self.pacer.wait()
        return stats

    def _goto(self, url: str) -> None:
        net.call(lambda: self.page.goto(url, wait_until="domcontentloaded"), HOST)
        self.page.wait_for_timeout(3000)
        if any(s in self.page.url for s in ("/login", "/checkpoint")):
            raise BlockedError(f"Facebook sent us to {self.page.url}; log in again")

    def _page_posts(self, url: str) -> list[str]:
        self._drain()
        self._goto(url)
        found: dict[str, None] = {}
        idle = 0
        for _ in range(self.max_rounds):
            before = len(found)
            hrefs: list[str] = self.page.eval_on_selector_all(
                "a[href]", "els => els.map(e => e.href)"
            )
            for u in find_post_urls("\n".join(hrefs + self._drain())):
                found.setdefault(u)
            if len(found) >= self.max_posts:
                break
            idle = idle + 1 if len(found) == before else 0
            if idle >= 4:
                if not net.wait_for_network(HOST):
                    break
                idle = 0  # the connection had dropped; keep scrolling now it's back
            self.page.mouse.wheel(0, 2500)
            self.pacer.wait(0.5)
        if not found:
            raise ScrapeError(f"no posts found on {url}")
        log.info("%s: found %d posts", url, len(found))
        return list(found)

    def _post(self, store: Store, url: str, media_id: str) -> None:
        self._drain()
        self._goto(url)
        caption = None
        meta = self.page.locator('meta[property="og:description"]')
        if meta.count():
            caption = meta.first.get_attribute("content")
        media = store.post(
            {"id": media_id, "caption": caption, "permalink": url, "comments_count": None}
        )
        seen: set[str] = set()
        blobs = self.page.locator('script[type="application/json"]').all_text_contents()
        store_docs(store, media, blobs + self._drain(), seen)
        self._all_comments()

        limit = store.stats.comments_seen + self.max_comments
        idle = 0
        for _ in range(self.max_rounds):
            clicked = self._expand()
            self.page.mouse.wheel(0, 1500)
            self.pacer.wait(0.5)
            store_docs(store, media, self._drain(), seen)
            if store.stats.comments_seen >= limit:
                break
            idle = 0 if clicked else idle + 1
            if idle >= 3:
                if not net.wait_for_network(HOST):
                    break
                idle = 0  # the connection had dropped; keep reading now it's back
        store.finish(media)
        log.info("%s: %d comments", url, len(seen))

    def _all_comments(self) -> None:
        """Switch the comment filter from "Most relevant" (a subset) to "All comments"."""
        try:
            self.page.get_by_role("button", name=SORT_BUTTON).first.click(timeout=3000)
            self.page.get_by_role("menuitem", name=ALL_COMMENTS).first.click(timeout=3000)
            self.pacer.wait(0.5)
        except Exception:
            log.debug("comment filter menu not found; keeping the default order")

    def _expand(self) -> int:
        buttons = self.page.get_by_role("button", name=EXPAND)
        clicked = 0
        for i in range(min(buttons.count(), 8)):
            try:
                buttons.nth(i).click(timeout=2000)
                clicked += 1
                self.pacer.wait(0.3)
            except Exception:  # detached or covered by another element; next round
                continue
        return clicked
