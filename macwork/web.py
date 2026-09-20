"""Web research: search over plain HTTP (engines block headless browsers), then let the decider drive a
headless Chromium through results and pages. Each step is one request with parallel questions — next action,
is this page useful, is it enough, are we stuck, which passage matters — so notes keep only relevant text.

Search engines are a registry (``SEARCH``); ``web.search`` in the config picks and orders them. Playwright
objects are thread-bound, so the browser lives on one worker thread and stays warm between tasks.
"""

from __future__ import annotations

import html as _html
import logging
import re
import subprocess
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Any, Callable

from .config import Config
from .decider import MAX_OPTIONS, DeciderError, choice, noul

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"


def _strip(html: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"(?s)<[^>]+>", " ", html))).strip()


def _squash(text: str) -> str:
    return re.sub(r"[ \t\r\f\v]+", " ", re.sub(r"\n\s*\n+", "\n", text or "")).strip()


def _duckduckgo(q: str, n: int) -> list[dict[str, str]]:
    import requests

    r = requests.get("https://html.duckduckgo.com/html/", params={"q": q}, timeout=8, headers={"User-Agent": UA})
    out = []
    for m in re.finditer(r'<a rel="nofollow" class="result__a" href="([^"]+)"[^>]*>(.*?)</a>.*?<a class="result__snippet"[^>]*>(.*?)</a>', r.text if r.ok else "", re.S):
        href = m.group(1)
        mm = re.search(r"uddg=([^&]+)", href)
        out.append({"title": _strip(m.group(2))[:80], "url": urllib.parse.unquote(mm.group(1)) if mm else href, "snippet": _strip(m.group(3))[:200]})
        if len(out) >= n:
            break
    return out


def _bing(q: str, n: int) -> list[dict[str, str]]:
    import requests

    r = requests.get("https://www.bing.com/search", params={"q": q}, timeout=8, headers={"User-Agent": UA})
    out = []
    for block in re.findall(r'<li class="b_algo".*?</li>', r.text if r.ok else "", re.S):
        m = re.search(r'<h2[^>]*><a[^>]*href="(https?://[^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if m:
            sn = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
            out.append({"title": _strip(m.group(2))[:80], "url": m.group(1), "snippet": _strip(sn.group(1))[:200] if sn else ""})
        if len(out) >= n:
            break
    return out


SEARCH: dict[str, Callable[[str, int], list[dict[str, str]]]] = {"duckduckgo": _duckduckgo, "bing": _bing}


def search(cfg: Config, q: str) -> list[dict[str, str]]:
    n = int(cfg.get("web.results", 8))
    for name in cfg.get("web.search") or []:
        fn = SEARCH.get(name)
        if not fn:
            continue
        try:
            res = fn(q, n)
        except Exception as exc:  # noqa: BLE001
            log.info("search %s: %s", name, exc)
            continue
        if res:
            return res
    return []


SNAPSHOT_JS = r"""() => {
  document.querySelectorAll('[data-macwork]').forEach(e => e.removeAttribute('data-macwork'));
  const sel = 'a[href], button, input:not([type=hidden]), textarea, [role=button], [role=link], [role=tab], [role=menuitem], [role=searchbox], [contenteditable=true], summary';
  const vh = innerHeight, els = [];
  let n = 0;
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2 || r.bottom < -vh || r.top > 2 * vh) continue;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || el.disabled) continue;
    const tag = el.tagName.toLowerCase();
    const field = tag === 'textarea' || el.isContentEditable || el.getAttribute('role') === 'searchbox' ||
      (tag === 'input' && !['button', 'submit', 'checkbox', 'radio', 'image', 'reset', 'file', 'range', 'color'].includes(el.type));
    let label = field
      ? (el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.getAttribute('title') || el.name || el.type || '')
      : (el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('title') || '');
    if (!label && !field) { const img = el.querySelector('img[alt]'); if (img) label = img.alt; }
    label = String(label).replace(/\s+/g, ' ').trim();
    if (!label && !field) continue;
    const id = String(++n);
    el.setAttribute('data-macwork', id);
    els.push({id, field, label: label.slice(0, 90), href: tag === 'a' ? String(el.href).slice(0, 120) : '', inview: r.top >= 0 && r.top < vh});
  }
  const main = [...document.querySelectorAll('article, #mw-content-text, [role=main], main')].find(m => m.innerText.length > 400);
  const text = ((main || document.body)?.innerText || '').slice(0, 30000);
  return {url: location.href, title: document.title, elements: els, text,
          more_below: document.documentElement.scrollHeight > scrollY + innerHeight + 50};
}"""


def _where(url: str) -> str:
    u = urllib.parse.urlsplit(url or "")
    return f"{u.netloc}{u.path[:60]}" if u.netloc else ""


def passages(text: str, size: int = 360, limit: int = 60) -> list[str]:
    """Page text cut at line breaks into chunks of about ``size`` chars (short nav lines get merged)."""
    out: list[str] = []
    cur = ""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        while len(line) > size:
            if cur:
                out.append(cur)
                cur = ""
            out.append(line[:size])
            line = line[size:]
        if len(cur) + len(line) + 1 > size and cur:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n{line}" if cur else line
        if len(out) >= limit:
            break
    if cur and len(out) < limit:
        out.append(cur)
    return out[:limit]


def pick_passages(chunks: list[str], probs: dict[str, float], top: int = 3, floor: float = 0.1) -> str:
    keys = [k for k, v in sorted(probs.items(), key=lambda kv: -kv[1]) if v >= floor][:top]
    idx = sorted(int(k[1:]) for k in keys if re.fullmatch(r"p\d+", k) and int(k[1:]) < len(chunks))
    return "\n…\n".join(chunks[i] for i in idx)


def result_options(results: list[dict[str, str]], visited: set[str]) -> dict[str, str]:
    opts = {"done": "done: the goal is accomplished or answered, stop browsing"}
    for i, r in enumerate(results):
        if r["url"] not in visited:
            opts[f"r{i}"] = f"open result 「{r['title']}」 ({_where(r['url'])}): {r.get('snippet', '')[:140]}"
    return opts


def page_options(snap: dict[str, Any], texts: list[str], can_leave: bool) -> dict[str, str]:
    opts: dict[str, str] = {"done": "done: the goal is accomplished or answered, stop browsing"}
    if snap.get("more_below"):
        opts["scroll"] = "scroll down to see more of this page"
    opts["back"] = "go back to the search results and try another one" if can_leave else "go back to the previous page"
    for e in [e for e in snap.get("elements", []) if e.get("field")][:5]:
        for k, t in enumerate(texts[:3]):
            opts[f"t{e['id']}.{k}"] = f"type 「{t}」 into the input 「{e['label'] or 'text field'}」 and press Enter"
    for e in sorted((e for e in snap.get("elements", []) if not e.get("field")), key=lambda e: not e.get("inview")):
        if len(opts) >= MAX_OPTIONS - 5:
            break
        where = _where(e.get("href", ""))
        opts[f"c{e['id']}"] = f"click 「{e['label']}」" + (f" (link to {where})" if where else "")
    return opts


def parse_key(key: str) -> tuple[str, str | None, int]:
    for kind, pat in (("click", r"c(\d+)"), ("result", r"r(\d+)")):
        m = re.fullmatch(pat, key)
        if m:
            return kind, m.group(1), 0
    m = re.fullmatch(r"t(\d+)\.(\d+)", key)
    if m:
        return "type", m.group(1), int(m.group(2))
    return key, None, 0


class Browser:
    """One warm headless Chromium on its own thread."""

    def __init__(self, headed: bool) -> None:
        self.headed = headed
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="macwork-web")
        self._pw: Any = None
        self.ctx: Any = None

    def context(self) -> Any:
        if self.ctx is None:
            from playwright.sync_api import sync_playwright

            self._pw = sync_playwright().start()
            browser = self._pw.chromium.launch(headless=not self.headed, args=["--disable-blink-features=AutomationControlled"])
            self.ctx = browser.new_context(locale="zh-CN", viewport={"width": 1280, "height": 900}, user_agent=UA)
            if not self.headed:
                self.ctx.route(re.compile(r"\.(png|jpe?g|gif|webp|svg|woff2?|ttf|mp4|webm|mp3)(\?|$)", re.I), lambda r: r.abort())
        return self.ctx

    def reset(self) -> None:
        try:
            if self.ctx is not None:
                self.ctx.browser.close()
            if self._pw is not None:
                self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        self.ctx = self._pw = None


def research(cfg: Config, gate: Any, redactor: Any, goal: str, query: str = "", url: str = "",
             cache: dict[str, Any] | None = None, search_fn: Callable[[str], list[dict[str, str]]] | None = None,
             page_factory: Callable[[], Any] | None = None) -> dict[str, Any]:
    """Look something up / get something done on the web. Returns found notes (url, title, relevant text)."""
    cache = cache if cache is not None else {}
    browser = cache.get("web.browser")
    if browser is None and page_factory is None:
        browser = cache["web.browser"] = Browser(bool(cfg.get("web.headed", False)))
    budget = float(cfg.get("web.budget_s", 30))
    if page_factory is not None:  # tests: no browser, no thread
        return _task(cfg, gate, redactor, goal, query, url, search_fn or (lambda q: search(cfg, q)), page_factory)
    fut = browser.pool.submit(_task, cfg, gate, redactor, goal, query, url, search_fn or (lambda q: search(cfg, q)),
                              lambda: browser.context().new_page(), browser)
    try:
        return fut.result(timeout=budget + 25)
    except FutureTimeout:
        return {"goal": goal, "status": "error", "error": "browser did not answer in time"}


def _task(cfg: Config, gate: Any, redactor: Any, goal: str, query: str, url: str,
          search_fn: Callable[[str], list[dict[str, str]]], new_page: Callable[[], Any], browser: Browser | None = None) -> dict[str, Any]:
    w = cfg.section("web")
    th = cfg.section("web.thresholds")
    t0 = time.monotonic()
    texts = [t for t in dict.fromkeys([query, goal]) if t]
    results = [] if url else search_fn(query or goal)
    if not url and not results:
        return {"goal": goal, "status": "error", "error": "no search results (offline?)"}
    page: Any = None
    trail = [f"search 「{query or goal}」" if results else f"open {url}"]
    notes: list[dict[str, str]] = []
    visited: set[str] = set()
    on_results, depth = bool(results), 0
    status, error = "budget", None
    last: tuple[Any, str] | None = None
    snap: dict[str, Any] = {}
    calls = 0
    q = {k: cfg.question(f"web_{k}") for k in ("action", "passage", "useful", "enough", "stuck")}
    try:
        if not on_results:
            page = new_page()
            _goto(page, url if re.match(r"^https?://", url) else "https://" + url)
        for _step in range(int(w.get("max_steps", 12))):
            if time.monotonic() - t0 > float(w.get("budget_s", 30)):
                break
            saved = [{"title": n["title"], "excerpt": n["text"][:300]} for n in notes]
            if on_results:
                options = result_options(results, visited)
                if len(options) < 2:
                    status = "found" if notes else "stuck"
                    break
                listing = "\n".join(f"{r['title']} — {r.get('snippet', '')}" for r in results)
                snap = {"url": "search:" + (query or goal), "title": f"search results: {query or goal}", "text": listing}
                ans = gate.decide(redactor, {"goal": goal, "page": "search results", "results": listing, "history": trail[-6:], "saved_notes": saved},
                                  {"action": choice(q["action"], options), "useful": noul(q["useful"]), "enough": noul(q["enough"])}, task="web")
                chunks = [f"{r['title']} ({_where(r['url'])}): {r.get('snippet', '')}" for r in results]
                probs = {f"p{i}": 1.0 for i in range(len(chunks))}   # snippets are short: keep them all
                stuck = 0.0
            else:
                snap = page.evaluate(SNAPSHOT_JS)
                text = _squash(snap.get("text", ""))
                options = page_options(snap, texts, can_leave=bool(results) and depth == 0)
                chunks = passages(text)
                questions = {"action": choice(q["action"], options), "useful": noul(q["useful"]), "enough": noul(q["enough"]), "stuck": noul(q["stuck"])}
                if len(chunks) >= 2:
                    questions["passage"] = choice(q["passage"], {f"p{i}": c for i, c in enumerate(chunks)})
                ans = gate.decide(redactor, {"goal": goal, "page": {"url": snap.get("url"), "title": snap.get("title")},
                                             "page_text": text[:4000], "history": trail[-6:], "saved_notes": saved}, questions, task="web")
                probs = (ans.get("passage") or {}).get("probabilities") or {}
                stuck = float(ans["stuck"].get("noul", 0.0))
            calls += 1
            useful, enough = float(ans["useful"].get("noul", 0.0)), float(ans["enough"].get("noul", 0.0))
            key = ans["action"].get("choice", "done")
            if useful >= float(th.get("useful", 0.6)) and all(n["url"] != snap.get("url") for n in notes):
                kept = pick_passages(chunks, probs, top=len(chunks) if on_results else 3) or _squash(snap.get("text", ""))
                if len(kept) > 40:
                    notes.append({"title": str(snap.get("title", ""))[:80], "url": str(snap.get("url", "")), "text": kept[: int(w.get("note_chars", 2500))]})
            if notes and (enough >= float(th.get("enough", 0.8)) or len(notes) >= int(w.get("max_notes", 3))):
                status = "found"
                break
            leave = stuck >= float(th.get("stuck", 0.85)) or (key == "back" and depth == 0) or last == (snap.get("url"), key)
            if leave and not on_results:
                if results and len(result_options(results, visited)) >= 2:
                    trail.append(f"leave {_where(snap.get('url', ''))}, back to results")
                    on_results, depth, last = True, 0, None
                    continue
                status = "stuck"
                break
            if key == "done" or key not in options:
                status = "found" if notes else "done"
                break
            last = (snap.get("url"), key)
            kind, ident, k = parse_key(key)
            if kind == "result":
                r = results[int(ident or 0)]
                visited.add(r["url"])
                trail.append(f"open 「{r['title']}」")
                try:
                    page = page or new_page()
                    _goto(page, r["url"])
                    on_results, depth, last = False, 0, None
                except Exception as exc:  # noqa: BLE001
                    trail[-1] += " (unreachable)"
                    log.info("web open %s: %s", r["url"], str(exc)[:120])
                continue
            before = page.url
            page, ok = _act(page, kind, ident, texts[min(k, len(texts) - 1)] if texts else "")
            depth = max(0, depth - 1) if kind == "back" else depth + (1 if ok and page.url != before else 0)
            trail.append(options[key] + ("" if ok else " (failed)"))
    except DeciderError as exc:
        status, error = "error", f"decider: {exc}"
    except Exception as exc:  # noqa: BLE001
        status, error = "error", f"browser: {str(exc)[:200]}"
        log.warning("web research: %s", exc)
        if browser:
            browser.reset()
    res: dict[str, Any] = {"goal": goal, "status": status, "found": notes, "steps": trail, "decisions": calls,
                           "seconds": round(time.monotonic() - t0, 1)}
    if error:
        res["error"] = error
    if not notes and snap.get("text"):
        res["page_text"] = _squash(snap["text"])[:1500]
    if browser and browser.ctx is not None:
        for p in list(browser.ctx.pages):
            try:
                p.close()
            except Exception:  # noqa: BLE001
                pass
    return res


def _goto(page: Any, url: str) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=15000)
    page.wait_for_timeout(300)


def _act(page: Any, kind: str, ident: str | None, text: str) -> tuple[Any, bool]:
    try:
        if kind == "scroll":
            page.evaluate("window.scrollBy(0, innerHeight * 0.85)")
            page.wait_for_timeout(250)
            return page, True
        if kind == "back":
            page.go_back(wait_until="domcontentloaded", timeout=10000)
            return page, True
        loc = page.locator(f'[data-macwork="{ident}"]').first
        n_pages = len(page.context.pages)
        if kind == "type":
            loc.fill(text, timeout=4000)
            loc.press("Enter", timeout=4000)
        else:
            loc.evaluate("e => { e.removeAttribute('target'); e.closest('a')?.removeAttribute('target'); }")
            loc.click(timeout=4000)
        if len(page.context.pages) > n_pages:
            page = page.context.pages[-1]
        try:
            page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(400)
        return page, True
    except Exception as exc:  # noqa: BLE001
        log.info("web act %s%s: %s", kind, ident, str(exc)[:160])
        return page, False


def open_in_browser(url: str) -> None:
    if url.startswith("http"):
        subprocess.run(["open", url], capture_output=True, timeout=10, check=False)
