"""Text that is not in the Latin script — where the script itself is what is being tested.

The rest of the suite is written in English (see test_english_first.py). These cannot be: a name is tagged
through a carrier sentence in *its own* script, and a selection inside a document is counted in UTF-16 units,
which only shows when the text holds characters that are not one unit each.
"""

from macwork.model import Observation
from macwork.observe import Ctx
from macwork.privacy import Redactor
from tests.test_engine import FakeHelper, cfg


def test_names_in_mixed_text_are_found_through_their_own_script(tmp_path):
    def tagger(texts):   # like the real one: reads a clause in one language, needs context to see a bare name
        return [[{"type": "PERSON", "text": "王芳"}] if t == "我和王芳开会。" else
                [{"type": "PERSON", "text": "Grace Lee"}] if t == "I met Grace Lee yesterday." else [] for t in texts]
    r = Redactor(cfg(tmp_path), entities=tagger)
    assert r.text("Meeting with 王芳 at 3pm") == "Meeting with ⟦PERSON_1⟧ at 3pm"
    assert r.text("发送给 Grace Lee") == "发送给 ⟦PERSON_2⟧"
    assert r.text("我和开会") == "我和开会"          # the carrier's own words are never taken for names


def test_a_part_of_a_documents_text_is_selected_by_utf16_offsets(tmp_path):
    from macwork.act import window_channel
    from macwork.observe import element_affordances
    nodes = [{"ref": "d", "role": "AXTextArea", "value": "你好😀hello world", "editable": True, "frame": [0, 0, 400, 300]}]
    obs = Observation(app=None, window=None, affordances=[])
    h = FakeHelper()
    h.values["d"] = "你好😀hello world"
    ctx = Ctx(cfg(tmp_path), h, app={"pid": 42})
    element_affordances(ctx, obs, nodes, "w")
    sel = next(a for a in obs.affordances if a.verb == "select_text")
    end = next(a for a in obs.affordances if a.verb == "cursor_end")
    h.calls.clear()
    assert window_channel(ctx, sel, {"selection": "hello"}).ok
    assert [p for m, p in h.calls if m == "ax.set_range"] == [{"ref": "d", "location": 4, "length": 5}]   # 你 好 😀(2) = 4
    window_channel(ctx, end, {})
    assert [p for m, p in h.calls if m == "ax.set_range"][-1] == {"ref": "d", "location": 15, "length": 0}
    assert not window_channel(ctx, sel, {"selection": "absent"}).ok
