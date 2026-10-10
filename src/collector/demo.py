"""Synthetic demo account for trying the pipeline without a Meta app.

`load_demo` runs the real backfill code against an in-process fake of graph.instagram.com
(httpx.MockTransport): paged media, paged comments, inline replies, usernames in payloads.
Everything is synthetic and tied to one demo account that `remove_demo` deletes.
"""

from dataclasses import dataclass
from itertools import count
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from collector.ig.backfill import BackfillStats, backfill_account
from collector.ig.client import GraphClient
from collector.models import Account, Comment, CommentFeatures, Media, Sample, SyncState
from collector.security import encrypt_token

DEMO_IG_USER_ID = "demo-0000000000"
DEMO_TOKEN = "demo-token"  # never sent anywhere: requests are answered in-process
VERSION = "v25.0"
PAGE_SIZE = 6

_POSTS: dict[str, tuple[str, list[str]]] = {
    "demo-M1": (
        "Mechi ya leo ⚽",
        [
            "Goli safi sana! 🔥",
            "@demo_fan_2 umeona hii?",
            "wewe ni mjinga kabisa",
            "M.J.1.N.G.@ hujui mpira",
            "hongera kwa ushindi",
            "refa ni mpumbavu",
            "mpumb@vu huyu refa",
            "😂😂😂",
            "simba nguvu moja",
            "timu yetu imeshinda",
            "mbwa wewe",
            "nimeipenda hii",
            "https://example.com/tiketi",
            "wapumbavu wote hawa",
            "safi sana kaka",
        ],
    ),
    "demo-M2": (
        "Habari za mjini 💅",
        [
            "malaya tu huyu",
            "m@l@y@ wewe",
            "habari za asubuhi dada",
            "mrembo sana",
            "ww ni fala",
            "pole sana dada",
            "cjui kwanini watu ni washenzi",
            "@demo_fan_3 @demo_fan_4",
            "mbwa wangu anapenda video hii",
            "wewe ni mjinga kabisa",
            "asante kwa habari",
            "nice video bro",
            "Mungu akubariki",
        ],
    ),
    "demo-M3": (
        "Chakula cha jioni 🍖",
        [
            "mjnga wewe",
            "kahaba",
            "habari",
            "karibu tena",
            "unakula kama nguruwe",
            "nyama choma tamu sana",
            "poa sana",
            "mwaka 2026 utakuwa mzuri",
        ],
    ),
}
# Replies to the first comment of each post.
_REPLIES = ["acha matusi wewe", "kweli kabisa", "nyamaza mpumbavu"]


@dataclass
class _Data:
    media: list[dict[str, Any]]
    comments: dict[str, list[dict[str, Any]]]
    replies: dict[str, list[dict[str, Any]]]


def _build() -> _Data:
    ids = count(1)

    def comment(text: str, **extra: Any) -> dict[str, Any]:
        n = next(ids)
        return {
            "id": f"demo-c{n}",
            "text": text,
            "timestamp": f"2026-09-{n % 28 + 1:02d}T12:00:00+0000",
            "from": {"id": f"demo-igsid-{n % 9}", "username": f"demo_user_{n % 9}"},
            "username": f"demo_user_{n % 9}",
            "like_count": n % 5,
            "hidden": False,
            **extra,
        }

    data = _Data(media=[], comments={}, replies={})
    for day, (mid, (caption, texts)) in enumerate(_POSTS.items(), start=1):
        data.media.append(
            {
                "id": mid,
                "caption": caption,
                "media_type": "IMAGE",
                "permalink": f"https://www.instagram.com/p/{mid}/",
                "timestamp": f"2026-09-{day:02d}T08:00:00+0000",
                "comments_count": len(texts),
            }
        )
        items = [comment(t) for t in texts]
        first = items[0]
        replies = [comment(t, parent_id=first["id"]) for t in _REPLIES]
        first["replies"] = {"data": replies}
        data.comments[mid] = items
        data.replies[first["id"]] = replies
    return data


def _transport(data: _Data) -> httpx.MockTransport:
    base = f"https://graph.instagram.com/{VERSION}"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix(f"/{VERSION}/")
        if path == "me/media":
            return httpx.Response(200, json={"data": data.media})
        if path.endswith("/comments"):
            items = data.comments.get(path.removesuffix("/comments"), [])
            start = int(request.url.params.get("after", 0))
            body: dict[str, Any] = {"data": items[start : start + PAGE_SIZE]}
            if start + PAGE_SIZE < len(items):
                query = urlencode({"after": start + PAGE_SIZE, "access_token": DEMO_TOKEN})
                body["paging"] = {"next": f"{base}/{path}?{query}"}
            return httpx.Response(200, json=body)
        if path.endswith("/replies"):
            return httpx.Response(200, json={"data": data.replies.get(path[:-8], [])})
        return httpx.Response(404, json={"error": {"message": "not in demo", "code": 100}})

    return httpx.MockTransport(handler)


def load_demo(session: Session) -> tuple[Account, BackfillStats]:
    account = session.scalars(select(Account).where(Account.ig_user_id == DEMO_IG_USER_ID)).first()
    if account is None:
        account = Account(
            ig_user_id=DEMO_IG_USER_ID,
            username="demo_account",
            category="demo",
            consent_ref="DEMO - synthetic data",
            access_token_enc=encrypt_token(DEMO_TOKEN),
            active=True,
        )
        session.add(account)
        session.commit()
    with httpx.Client(transport=_transport(_build())) as http:
        client = GraphClient(DEMO_TOKEN, http=http, version=VERSION)
        stats = backfill_account(session, client, account)
    return account, stats


def remove_demo(session: Session) -> int:
    """Delete the demo account and everything collected for it. Returns comments removed."""
    account = session.scalars(select(Account).where(Account.ig_user_id == DEMO_IG_USER_ID)).first()
    if account is None:
        return 0
    media_ids = select(Media.id).where(Media.account_id == account.id)
    comment_ids = select(Comment.id).where(Comment.media_id.in_(media_ids))
    removed = len(session.scalars(comment_ids).all())
    session.execute(delete(Sample).where(Sample.comment_id.in_(comment_ids)))
    session.execute(delete(CommentFeatures).where(CommentFeatures.comment_id.in_(comment_ids)))
    session.execute(delete(Comment).where(Comment.media_id.in_(media_ids)))
    session.execute(delete(Media).where(Media.account_id == account.id))
    session.execute(delete(SyncState).where(SyncState.key.startswith(f"backfill:{account.id}:")))
    session.delete(account)
    session.commit()
    return removed
