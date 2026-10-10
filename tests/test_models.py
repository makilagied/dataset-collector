from collector.models import Base


def test_schema_has_all_tables() -> None:
    assert set(Base.metadata.tables) == {
        "accounts",
        "media",
        "comments",
        "comment_features",
        "samples",
        "sync_state",
        "jobs",
    }
