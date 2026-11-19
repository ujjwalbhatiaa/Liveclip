import re, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import liveclipper as L, thumbmaker as T


def test_headline_never_invented():
    assert T.headline_from("MY HOOK", "x", [], "c") == ("MY HOOK", "hook")
    assert T.headline_from(None, "", [], "c") == ("LIVE CLIP", "default")
    t, src = T.headline_from(None, "So that happened. Wait, did you see that? Yes.", [], "c")
    assert src == "transcript" and t.startswith("Wait")


def test_ass_pop_and_hype(tmp_path):
    words = [(0.0, 0.3, "Wait,"), (0.3, 0.6, "this"), (0.6, 1.0, "insane!")]
    p = tmp_path / "c.ass"
    L.build_ass(words, 1080, 1920, p, "Arial", None, None, re.compile(L.HYPE % "", re.I))
    s = p.read_text()
    assert "\\t(0,110" in s and "&H3C3CFF&" in s and "INSANE" in s


def test_layout_graph():
    assert "[pre]" in L.layout_graph("blur", 10) and "[pre]" in L.layout_graph("none", 10)


def test_direct_targets_and_audio_spike():
    import types
    assert L.parse_target("https://example.com/live/index.m3u8")[0] == "direct"
    assert L.parse_target("srt://host:9000")[0] == "direct"
    assert L.parse_target("https://youtube.com/watch?v=abcdefghijk")[0] == "youtube"
    cfg = types.SimpleNamespace(warmup=3, audio_db=8.0)
    assert not L.audio_spike([-50, -50], -30, cfg)          # still warming up
    assert not L.audio_spike([-50, -50, -50], -45, cfg)     # not loud enough
    assert not L.audio_spike([-60, -60, -60], -45, cfg)     # near-silent floor
    assert L.audio_spike([-50, -50, -50], -35, cfg)


def test_combined_trigger_semantics():
    import collections, types
    cfg = types.SimpleNamespace(warmup=3, audio_db=8.0)
    h = collections.deque([-50, -50, -50], maxlen=30)
    assert L.audio_decision(False, None, h, cfg) is False          # no audio track: never blocks, never triggers
    assert L.audio_decision(True, -30, h, cfg) is False            # chat already fired: no second trigger
    assert len(h) == 3                                             # ...and the loud window is not learned as baseline
    assert L.audio_decision(False, -60, h, cfg) is False          # near silence
    assert L.audio_decision(False, -30, h, cfg) is True            # quiet chat but loud audio still clips
    h2 = collections.deque([], maxlen=30)
    assert L.audio_decision(False, -20, h2, cfg) is False          # warm-up: no trigger, baseline learned
    assert list(h2) == [-20]


def test_js_runtime_args_shape():
    args, warn = L.js_runtime_args()
    assert isinstance(args, list) and (warn is None or isinstance(warn, str))
