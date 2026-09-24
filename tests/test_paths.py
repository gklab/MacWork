"""Finding a path someone named, in any language.

The path was pulled out of the caller's words with a regex that excluded CJK ideographs and a handful of
Chinese punctuation marks. It was there to stop `打开 ~/Downloads/x.pdf 这个文件` from swallowing the
trailing words — and it broke the same script it was written for: `~/文稿/发票.pdf` truncated at the first
Chinese character, so a Chinese filename could not be found at all. Korean, Thai and Greek got neither the
help nor the harm: nothing stripped their particles either.

Guessing where a path ends is the wrong job. The file system knows.
"""

import pathlib

import pytest

from macwork.config import Config
from macwork.model import Observation
from macwork.observe import Ctx, get_provider


class Helper:
    mode = "fake"

    def call(self, method, timeout=30.0, **p):
        if method in ("apps.installed", "apps.openers", "apps.running"):
            return []
        raise AssertionError(method)


def found(tmp_path, goal: str) -> set[str]:
    ctx = Ctx(cfg=Config.load(), helper=Helper(), app={"pid": 1, "name": "X"}, goal=goal, inputs={},
              task="t", gate=None, cache={})
    obs = Observation(app=ctx.app, window="w", affordances=[])
    get_provider("files")(ctx, obs)
    return {a.target["path"] for a in obs.affordances if a.target.get("path")}


@pytest.fixture
def box(tmp_path):
    for name in ("发票.pdf", "invoice.pdf", "청구서.pdf", "ใบแจ้งหนี้.pdf", "Rechnung Januar.pdf", "счёт.pdf"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    (tmp_path / "文稿").mkdir()
    (tmp_path / "文稿" / "发票.pdf").write_text("x", encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("name", ["发票.pdf", "invoice.pdf", "청구서.pdf", "ใบแจ้งหนี้.pdf", "счёт.pdf"])
def test_a_file_is_found_whatever_its_name_is_written_in(box, name):
    assert str(box / name) in found(box, f"open {box / name}")


def test_a_nested_path_in_a_non_latin_folder(box):
    assert str(box / "文稿" / "发票.pdf") in found(box, f"打开 {box / '文稿' / '发票.pdf'}")


@pytest.mark.parametrize("goal", [
    "打开 {p} 这个文件",          # Chinese words after the path, with a space
    "打开{p}这个文件",            # and with none
    "{p}를 열어줘",               # Korean particle, attached
    "เปิด {p} หน่อย",             # Thai
    "öffne {p} bitte",           # German
    "open {p}.",                  # a sentence's full stop
    "open {p}, then close it",
    "see 「{p}」",                 # quoted
])
def test_the_words_around_a_path_are_not_taken_for_part_of_it(box, goal):
    p = box / "invoice.pdf"
    assert str(p) in found(box, goal.format(p=p)), f"not found in: {goal.format(p=p)}"


def test_a_path_that_does_not_exist_is_not_offered(box):
    assert found(box, f"open {box / 'nothing-here.pdf'}") == set()


def test_nothing_is_invented_from_ordinary_prose(box):
    assert found(box, "tidy up the desktop and/or the downloads folder") == set()


def test_a_folder_is_found_as_well_as_a_file(box):
    assert str(box / "文稿") in found(box, f"整理 {box / '文稿'}")


def test_the_engine_does_not_decide_where_a_path_ends_by_script():
    """The rule that broke Chinese filenames was a character-class guess. Whether a prefix is a path is a
    question for the file system, and asking it is the whole of the fix."""
    import inspect

    from macwork import observe
    src = inspect.getsource(observe.files)
    assert "\\u4e00" not in src and "一" not in src, "the files provider still names one script"


@pytest.mark.parametrize("goal", ["turn the volume down to ~30% in the Sound settings", "send the notes to ~alice"])
def test_a_tilde_before_a_word_that_names_no_user_is_no_path(tmp_path, goal):
    """Expanding 「~30%」 or 「~alice」 asks for the home of a user of that name, and raised when there is none: the
    files provider failed the task with it, and once the texts a task holds were read from its goal on every step,
    so did a task with no files provider at all."""
    from macwork.engine import Engine
    from macwork.observe import named_paths
    from tests.test_engine import FakeHelper, ScriptedDecider, cfg

    assert named_paths(goal) == [] and found(tmp_path, goal) == set()
    eng = Engine(cfg(tmp_path), helper=FakeHelper(), decider=ScriptedDecider([{"pick": "done"}]))
    res = eng.do(goal)
    assert res["status"] == "done", (res["status"], res.get("reason"))
