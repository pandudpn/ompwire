

def test_prefs_are_per_key_and_survive(tmp_path):
    from ompwire.store import SessionStore

    store = SessionStore(str(tmp_path / "s.db"))
    store.set_pref("tg:1", "model", "zai/glm-5.1")
    store.set_pref("tg:1", "yolo", "on")
    store.set_pref("tg:2", "model", "omniroute/chat")

    assert store.prefs("tg:1") == {"model": "zai/glm-5.1", "yolo": "on"}
    assert store.prefs("tg:2") == {"model": "omniroute/chat"}

    store.set_pref("tg:1", "model", "zai/glm-5.3")   # upsert, not duplicate
    assert store.prefs("tg:1")["model"] == "zai/glm-5.3"


def test_interrupted_returns_only_its_own_transport(tmp_path):
    from ompwire.store import SessionStore

    store = SessionStore(str(tmp_path / "s.db"))
    store.begin_turn("tg:7", "deploy the thing")
    store.begin_turn("cli:/tmp/x", "fix the test")
    store.begin_turn("tg:8", "and this")

    assert store.interrupted("tg:") == [("tg:7", "deploy the thing", 0),
                                        ("tg:8", "and this", 0)]
    assert store.interrupted("cli:") == [("cli:/tmp/x", "fix the test", 0)]
    assert store.interrupted("web:") == []


def test_a_prefix_with_like_wildcards_is_matched_literally(tmp_path):
    from ompwire.store import SessionStore

    store = SessionStore(str(tmp_path / "s.db"))
    store.begin_turn("a_b:1", "underscore")
    store.begin_turn("axb:1", "other")

    assert store.interrupted("a_b:") == [("a_b:1", "underscore", 0)]

