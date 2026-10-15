"""Which comments get stored. With STORE_ONLY_HITS (the default) only lexicon hits are:
comments with a Swahili or English abusive word scoring >= HIT_THRESHOLD, aimed at
someone when the word is only abusive in context. Applied by every way comments come in
(API backfill, webhooks, scraping), before anything is written.
"""

from collector.config import get_settings
from collector.nlp.matcher import match
from collector.nlp.normalize import normalize, normalize_plain
from collector.pipeline.sample import HIT_THRESHOLD


def is_hit(text: str) -> bool:
    result = match(normalize(text), text_plain=normalize_plain(text))
    return any(m.score >= HIT_THRESHOLD for m in result.hits)


def should_store(text: str) -> bool:
    return not get_settings().store_only_hits or is_hit(text)
