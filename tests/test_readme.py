"""The README has to describe this program, not an earlier one.

It documented `macwork web` and `pip install -e ".[web]"` for weeks after the web subsystem was torn out —
so the very first command a new reader runs did not work. It also advertised "three levels of control"
above a table with two rows, `engine.mode: foreground` for a setting whose values are yield/exclusive/
background, and eval results from an engine that had since been rewritten.

Prose is not checkable. Commands, tools, settings and paths are.
"""

import pathlib
import re

import pytest
import yaml

README = pathlib.Path("README.md").read_text(encoding="utf-8")
CODE = "\n".join(p.read_text(encoding="utf-8") for p in pathlib.Path("macwork").rglob("*.py"))


def cli_commands() -> set[str]:
    return set(re.findall(r'sub\.add_parser\("([a-z-]+)"', pathlib.Path("macwork/cli.py").read_text(encoding="utf-8")))


def extras() -> set[str]:
    body = pathlib.Path("pyproject.toml").read_text(encoding="utf-8")
    block = re.search(r"\[project\.optional-dependencies\]\n(.*?)(\n\[|\Z)", body, re.S)
    return set(re.findall(r"^(\w+)\s*=", block.group(1), re.M)) if block else set()


MENTIONED_CLI = sorted(w for w in set(re.findall(r"\bmacwork ([a-z][a-z-]*)", README)) if not w.startswith("-"))
MENTIONED_TOOLS = sorted(set(re.findall(r"\bmac_[a-z_]+", README)))
MENTIONED_PATHS = sorted({p for p in re.findall(r"\(([a-zA-Z0-9_/.-]+\.(?:md|yaml|toml))\)", README)
                          if not p.startswith("http")})
MENTIONED_EXTRAS = sorted(set(re.findall(r'pip install -e "\.\[([a-z,]+)\]"', README)))


@pytest.mark.parametrize("cmd", MENTIONED_CLI)
def test_every_command_it_shows_exists(cmd):
    assert cmd in cli_commands(), f"README shows `macwork {cmd}` and the CLI has no such subcommand"


@pytest.mark.parametrize("tool", MENTIONED_TOOLS)
def test_every_mcp_tool_it_names_exists(tool):
    assert f"def {tool}(" in pathlib.Path("macwork/server.py").read_text(encoding="utf-8"), \
        f"README names the MCP tool {tool} and the server does not define it"


@pytest.mark.parametrize("path", MENTIONED_PATHS)
def test_every_file_it_links_to_is_there(path):
    assert pathlib.Path(path).exists(), f"README links to {path}, which does not exist"


@pytest.mark.parametrize("spec", MENTIONED_EXTRAS)
def test_the_install_line_asks_for_extras_that_exist(spec):
    """The first command a reader runs. It named `[web]` long after that extra was removed."""
    missing = {e for e in spec.split(",")} - extras()
    assert not missing, f'README says pip install -e ".[{spec}]" and pyproject has no {sorted(missing)}'


def test_settings_it_quotes_are_real():
    """`engine.mode: foreground` was quoted for a setting whose values are yield / exclusive / background.

    The value need not be the default — "set `helper.mode: stdio`" is advice, not a claim about defaults —
    but it has to be one the setting accepts. Where a default carries an inline `# a | b | c`, that is the
    list; otherwise the default itself is all there is to check against.
    """
    defaults, choices = {}, {}
    for f in pathlib.Path("macwork/defaults").glob("*.yaml"):
        defaults[f.stem] = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        stack: list[tuple[int, str]] = []       # `mode` lives under both helper. and engine.: keep the path
        for line in f.read_text(encoding="utf-8").splitlines():
            m = re.match(r"( *)([a-z_]+):(.*)$", line)
            if not m:
                continue
            indent, key, rest = len(m.group(1)), m.group(2), m.group(3)
            while stack and stack[-1][0] >= indent:
                stack.pop()
            path = ".".join([k for _, k in stack] + [key])
            stack.append((indent, key))
            listed = re.match(r" *\S+ *# *([a-z_]+(?: *\| *[a-z_]+)+)", rest)
            if listed:
                choices[path] = {v.strip() for v in listed.group(1).split("|")}

    def get(dotted: str):
        for doc in defaults.values():
            node = doc
            for part in dotted.split("."):
                if not isinstance(node, dict) or part not in node:
                    node = None
                    break
                node = node[part]
            if node is not None:
                return node
        return None

    for setting, value in re.findall(r"`([a-z_]+(?:\.[a-z_]+)+): ([a-z_]+)`", README):
        got = get(setting)
        assert got is not None, f"README quotes `{setting}`, which is in no defaults file"
        allowed = ({"true", "false"} if isinstance(got, bool) else choices.get(setting, {str(got)}))
        assert value in allowed, f"README says `{setting}: {value}`; it takes {sorted(allowed)}"


def test_the_number_it_reports_is_the_one_the_suite_holds():
    """`evals/v2.yaml is 29 tasks` has to stay true as tasks are added."""
    n = len((yaml.safe_load(pathlib.Path("evals/v2.yaml").read_text(encoding="utf-8")) or {}).get("tasks") or [])
    assert f"{n} tasks" in README, f"v2.yaml holds {n} tasks and the README says otherwise"


def test_a_count_in_prose_matches_the_table_under_it():
    """It said "three levels of control" over a table with two rows."""
    words = {"one": 1, "two": 2, "three": 3, "four": 4}
    for word, heading in re.findall(r"## (\w+) ([a-z ]+)\n", README):
        if word.lower() in words:
            section = README.split(f"## {word} {heading}", 1)[1].split("\n## ", 1)[0]
            rows = [l for l in section.splitlines() if l.startswith("| ") and not re.match(r"\|[\s|:-]+\|$", l)]
            assert len(rows) - 1 == words[word.lower()], \
                f'"{word} {heading}" has {len(rows) - 1} rows under it'


def test_it_does_not_still_describe_the_web_subsystem():
    for gone in ("macwork web", '".[web]"', "web_research", "bundled browser and no built-in list"):
        assert gone not in README or gone == "bundled browser and no built-in list", f"README still mentions {gone!r}"


def test_it_is_a_front_door_not_a_changelog():
    """The measurement history moved to docs/benchmarks.md; a README that grows a round per week is one
    nobody reads to the end of."""
    assert len(README.splitlines()) < 300
    assert "docs/benchmarks.md" in README
    assert "round | what changed" not in README
