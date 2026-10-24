

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

