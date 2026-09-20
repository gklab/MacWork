"""Web research loop offline: scripted decider, fake pages, fake search."""

from macwork.config import Config
from macwork.privacy import Audit, Gate, Redactor
from macwork.web import page_options, parse_key, passages, pick_passages, research, result_options

RESULTS = [{"title": "埃菲尔铁塔 - 百科", "url": "https://baike.example/eiffel", "snippet": "埃菲尔铁塔现高330米。"},
           {"title": "旅游攻略", "url": "https://trip.example/eiffel", "snippet": "门票和开放时间"}]


def cfg(tmp_path):
    return Config.load(user_dir=tmp_path / "none", overrides={"config": {"audit": {"path": str(tmp_path / "a.jsonl")}},
                                                              "privacy": {"redact": {"enabled": False}}})


class Script:
    def __init__(self, steps):
        self.steps, self.asked, self.calls, self.cost_usd, self.last_ms = list(steps), [], 0, 0.0, 1.0

    def decide(self, state, questions):
        self.asked.append(questions)
        self.calls += 1
        s = self.steps.pop(0)
        out = {"action": {"type": "choice", "choice": s["action"]}}
        for k in ("useful", "enough", "stuck"):
            if k in questions:
                out[k] = {"type": "noul", "noul": s.get(k, 0.0)}
        if "passage" in questions:
            out["passage"] = {"type": "choice", "probabilities": s.get("passage", {})}
        return out


class Page:
    def __init__(self, sites):
        self.sites, self.url = sites, ""
        self.context = type("C", (), {"pages": [self]})()

    def goto(self, url, **_):
        if self.sites.get(url) is None:
            raise RuntimeError("net::ERR_CONNECTION_CLOSED")
        self.url = url

    def wait_for_timeout(self, ms):
        pass

    def evaluate(self, js):
        return {"url": self.url, "title": self.url, "elements": [], "text": self.sites[self.url], "more_below": False}


def run(tmp_path, steps, sites=None, goal="埃菲尔铁塔有多高"):
    c = cfg(tmp_path)
    d = Script(steps)
    page = Page(sites or {})
    res = research(c, Gate(d, Audit(c)), Redactor(c), goal=goal, search_fn=lambda q: RESULTS, page_factory=lambda: page)
    return res, d


def test_answers_from_snippets_without_opening_a_page(tmp_path):
    res, d = run(tmp_path, [{"action": "done", "useful": 0.95, "enough": 0.9}])
    assert res["status"] == "found" and d.calls == 1 and "330米" in res["found"][0]["text"]
    assert set(d.asked[0]["action"]["criteria"]) == {"done", "r0", "r1"}


def test_skips_dead_result_and_keeps_relevant_passage(tmp_path):
    body = "导航\n首页\n" + "无关内容。" * 80 + "\n埃菲尔铁塔高330米，建于1889年。"
    res, d = run(tmp_path, [{"action": "r0"}, {"action": "r1"}, {"action": "done", "useful": 0.9, "enough": 0.9, "passage": {"p2": 0.8}}],
                 {"https://trip.example/eiffel": body})
    assert res["status"] == "found" and "unreachable" in res["steps"][1]
    assert "330米" in res["found"][0]["text"] and "导航" not in res["found"][0]["text"]


def test_stuck_site_goes_back_to_results(tmp_path):
    res, _ = run(tmp_path, [{"action": "r0"}, {"action": "c1", "stuck": 0.95}, {"action": "r1"}, {"action": "done", "useful": 0.9, "enough": 0.9}],
                 {"https://baike.example/eiffel": "请完成验证" * 10, "https://trip.example/eiffel": "塔高330米" * 10})
    assert res["status"] == "found" and any("back to results" in s for s in res["steps"])
    assert res["found"][0]["url"] == "https://trip.example/eiffel"


def test_helpers():
    assert parse_key("c12") == ("click", "12", 0) and parse_key("t3.1") == ("type", "3", 1) and parse_key("r2") == ("result", "2", 0)
    chunks = passages("首页\n新闻\n" + "长" * 800 + "\n结尾", size=360)
    assert chunks[0] == "首页\n新闻" and all(len(c) <= 360 for c in chunks)
    assert pick_passages(["a", "b", "c", "d"], {"p3": 0.6, "p1": 0.3, "p0": 0.05}) == "b\n…\nd"
    assert set(result_options(RESULTS, {"https://baike.example/eiffel"})) == {"done", "r1"}
    els = [{"id": str(i), "field": False, "label": f"l{i}", "href": f"https://x.com/{i}", "inview": i > 250} for i in range(1, 300)]
    opts = page_options({"elements": els, "more_below": True}, ["q"], can_leave=True)
    assert len(opts) <= 255 and list(opts)[:3] == ["done", "scroll", "back"] and "c251" in opts
