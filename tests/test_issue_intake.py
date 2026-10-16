"""The Issue intake contract: what `issue-parse` writes must satisfy what the workflow checks.

This is the project's headline feature -- submit a site through the Issue form, get a recipe, get
it harvested, get a comment back. It had never been exercised, and it was broken in two ways that
produced no error at all:

1. **`ok=True` where the workflow compares to `'true'`.** Python renders booleans capitalised,
   GitHub expressions use lowercase. `'True' != 'true'`, so every `if: steps.parse.outputs.ok ==
   'true'` was false. The intake step posted "已受理，站点标识 X，采集任务已排队。" and then skipped
   both the recipe commit and the harvest job. The user was told the job was queued; nothing was.

2. **A multi-line `message=value` line keeps only its first line.** GitHub's file-command parser
   takes the text up to the first newline, and lines without `=` are dropped. The rejection
   message for a bad URL was two paragraphs -- the first said the URL was invalid, the second said
   what to do about it. Users only ever saw the first.

Both are invisible to ordinary unit tests of the parsing logic; they live in the *transport*
between Python and GitHub Actions. So these tests read the output file back with a parser that
implements GitHub's own rules, and then evaluate the workflow's conditions against it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from harvest.cli import app
from harvest.config import get_settings

RUNNER = CliRunner()

VALID_BODY = """\
### 站点地址

https://www.example.com/

### 站点名称

示例影视

### 适配器类型

自动探测

### 关注分类

_No response_

### 备注

_No response_

### 合规确认

- [x] 我确认该站点可公开访问，提交仅为生成地址索引供个人学习与技术研究，不用于分发受版权保护的内容；我理解最终使用责任由我自己承担。
"""

BAD_URL_BODY = VALID_BODY.replace("https://www.example.com/", "不是网址")


def parse_github_output(path: Path) -> dict[str, str]:
    """Parse `$GITHUB_OUTPUT` the way GitHub does.

    `key=value` takes the remainder of that line; `key<<DELIM` reads until a line equal to
    DELIM; anything else on its own line is ignored.
    """
    outputs: dict[str, str] = {}
    lines = path.read_text(encoding="utf-8").split("\n")
    index = 0
    while index < len(lines):
        line = lines[index]
        heredoc = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*)<<(\S+)$", line)
        if heredoc:
            key, delimiter = heredoc.group(1), heredoc.group(2)
            index += 1
            collected: list[str] = []
            while index < len(lines) and lines[index] != delimiter:
                collected.append(lines[index])
                index += 1
            outputs[key] = "\n".join(collected)
            index += 1  # skip the closing delimiter
            continue
        if "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            if re.match(r"^[A-Za-z_][A-Za-z0-9_-]*$", key):
                outputs[key] = value
        index += 1
    return outputs


# -- the transport conventions -----------------------------------------------------------------


def _run_issue_parse(tmp_path: Path, body: str, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    out = tmp_path / "github_output"
    out.write_text("", encoding="utf-8")
    monkeypatch.setenv("HARVEST_ROOT", str(tmp_path))
    monkeypatch.setenv("ISSUE_BODY", body)
    monkeypatch.setenv("ISSUE_NUMBER", "1234")
    get_settings.cache_clear()

    result = RUNNER.invoke(app, ["issue-parse", "--github-output", str(out)])
    assert result.exit_code == 0, result.output
    get_settings.cache_clear()
    return parse_github_output(out)


def test_booleans_are_written_lowercase(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`ok=True` reads as false to a GitHub expression, silently skipping every downstream step."""
    outputs = _run_issue_parse(tmp_path, VALID_BODY, monkeypatch)

    assert outputs["ok"] == "true", f"expected lowercase 'true', got {outputs['ok']!r}"


def test_a_rejection_message_survives_intact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both paragraphs must reach the Issue, not just the complaint."""
    outputs = _run_issue_parse(tmp_path, BAD_URL_BODY, monkeypatch)

    assert outputs["ok"] == "false", f"expected lowercase 'false', got {outputs['ok']!r}"
    message = outputs["message"]
    assert "表单填写不完整或地址无效" in message
    # This is the half that told the user what to do, and it used to be dropped.
    assert "请在「站点地址」里填写完整的公网 URL" in message, (
        f"the actionable second paragraph was lost: {message!r}"
    )


def test_the_parsed_output_matches_what_the_workflow_tests_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The actual contract, asserted as the workflow expresses it.

    These are the literals from .github/workflows/issue.yml; if someone changes one side without
    the other, this fails rather than the workflow going quiet.
    """
    accepted = _run_issue_parse(tmp_path, VALID_BODY, monkeypatch)
    rejected = _run_issue_parse(tmp_path / "rejected", BAD_URL_BODY, monkeypatch)

    # `if: steps.parse.outputs.ok == 'true' && steps.parse.outputs.duplicate != 'true'`
    assert (accepted["ok"] == "true") is True
    assert (accepted.get("duplicate", "") != "true") is True
    assert (rejected["ok"] == "true") is False


def test_accepted_submission_persists_a_recipe_and_a_registry_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ok == 'true'` is only useful if the work behind it actually happened."""
    outputs = _run_issue_parse(tmp_path, VALID_BODY, monkeypatch)

    key = outputs["key"]
    assert key, "no site key emitted"
    recipe = tmp_path / "sites" / f"{key}.yaml"
    assert recipe.is_file(), f"recipe not written: {recipe}"
    assert "site-request" not in recipe.read_text(encoding="utf-8")  # sanity: it is a recipe

    registry = (tmp_path / "sites" / "registry.yaml").read_text(encoding="utf-8")
    assert key in registry
    # The origin must record that this came from an Issue, so it can be traced back.
    assert "issue: 1234" in registry


def test_duplicate_submission_is_reported_as_lowercase_true(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`duplicate != 'true'` also relies on the lowercase convention."""
    _run_issue_parse(tmp_path, VALID_BODY, monkeypatch)
    again = _run_issue_parse(tmp_path, VALID_BODY, monkeypatch)

    assert again["ok"] == "true"
    assert again["duplicate"] == "true"
    assert "已收录" in again["message"]


def test_the_emitter_is_the_only_writer(tmp_path: Path) -> None:
    """`_emit` is the single place that knows the GITHUB_OUTPUT format.

    Two writers drifting apart is exactly how one output ends up capitalised and the other not.
    """
    source = (Path(__file__).resolve().parents[1] / "src" / "harvest" / "cli.py").read_text(
        encoding="utf-8"
    )
    writers = re.findall(r'open\("a"|github_output\.open\("a"', source)

    assert len(writers) <= 2, (
        f"{len(writers)} places append to a github_output file; route them through `_emit`"
    )


def test_the_rejection_message_is_actually_multi_line() -> None:
    """Guard the premise: if the message became single-line, the heredoc test proves nothing."""
    source = (Path(__file__).resolve().parents[1] / "src" / "harvest" / "cli.py").read_text(
        encoding="utf-8"
    )
    match = re.search(r"表单填写不完整或地址无效：\{url_or_error\}\\n\\n", source)

    assert match, "the rejection message no longer spans lines; re-check the heredoc handling"


def test_empty_url_is_rejected_with_a_usable_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outputs = _run_issue_parse(tmp_path, "### 站点地址\n\n_No response_\n", monkeypatch)

    assert outputs["ok"] == "false"
    assert "请在「站点地址」里填写" in outputs["message"]


@pytest.mark.parametrize(
    "host",
    ["http://127.0.0.1/x", "http://localhost/x", "http://10.0.0.5/x", "http://192.168.1.1/x"],
)
def test_private_hosts_are_refused(
    host: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SSRF hardening: the Issue body is untrusted and must never aim a fetch inward."""
    outputs = _run_issue_parse(tmp_path, VALID_BODY.replace("https://www.example.com/", host), monkeypatch)

    assert outputs["ok"] == "false", f"{host} was accepted"


def test_emitted_json_on_stdout_is_machine_readable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_emit` also prints the record as JSON; the workflow logs rely on it being parseable."""
    monkeypatch.setenv("HARVEST_ROOT", str(tmp_path))
    monkeypatch.setenv("ISSUE_BODY", VALID_BODY)
    monkeypatch.setenv("ISSUE_NUMBER", "9")
    get_settings.cache_clear()

    result = RUNNER.invoke(app, ["issue-parse"])
    get_settings.cache_clear()

    payload = json.loads(result.output.strip().splitlines()[-1])
    assert payload["ok"] is True
    assert payload["key"]


# -- recipe keys -------------------------------------------------------------------------------
#
# Found by submitting a real issue: the form asks for a site name in the operator's language, so
# the name is normally Chinese, which slugifies to nothing. The code then fell back to the
# constant "site" -- and a submission of cj.lziapi.com named "采集接口验证站" was registered under
# the key `site`. Every such submission would have received that same key, so each would have
# overwritten the previous one, showing up only as a site mysteriously disappearing.


def _body(url: str, name: str) -> str:
    return f"### 站点地址\n\n{url}\n\n### 站点名称\n\n{name}\n\n### 适配器类型\n\n自动探测\n"


def test_a_chinese_name_does_not_become_the_constant_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outputs = _run_issue_parse(tmp_path, _body("https://cj.example.com/", "采集接口验证站"), monkeypatch)

    assert outputs["ok"] == "true"
    assert outputs["key"] != "site", "the key fell back to a constant that every site would share"
    # The host is the only ASCII material available, so that is what the key should come from.
    assert "example" in outputs["key"], outputs["key"]


def test_two_differently_named_chinese_sites_get_different_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _run_issue_parse(tmp_path / "a", _body("https://aaa.example.com/", "影视一"), monkeypatch)
    second = _run_issue_parse(tmp_path / "b", _body("https://bbb.example.com/", "影视二"), monkeypatch)

    assert first["key"] != second["key"], (
        f"both submissions got the key {first['key']!r}; the second would overwrite the first"
    )
    assert (tmp_path / "a" / "sites" / f"{first['key']}.yaml").is_file()
    assert (tmp_path / "b" / "sites" / f"{second['key']}.yaml").is_file()


def test_an_ascii_name_is_preferred_over_the_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operators recognise their own names, so use them when they are usable."""
    outputs = _run_issue_parse(
        tmp_path, _body("https://cdn.example.com/", "My Cinema"), monkeypatch
    )

    assert outputs["key"] == "my-cinema", outputs["key"]


def test_the_same_origin_under_a_different_name_is_a_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Duplicate detection has to compare origins.

    Keying it on the derived name meant the same site resubmitted under another display name was
    registered twice, and nothing else compares base URLs -- so both entries would be harvested
    and published.
    """
    _run_issue_parse(tmp_path, _body("https://same.example.com/", "影视一"), monkeypatch)
    again = _run_issue_parse(tmp_path, _body("https://same.example.com/", "完全不同的名字"), monkeypatch)

    assert again["duplicate"] == "true", "the same origin was registered twice"
    assert "已收录" in again["message"]


def test_a_trailing_slash_does_not_defeat_duplicate_detection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run_issue_parse(tmp_path, _body("https://same.example.com/", "影视一"), monkeypatch)
    again = _run_issue_parse(tmp_path, _body("https://same.example.com", "影视一"), monkeypatch)

    assert again["duplicate"] == "true"


def test_a_second_site_on_another_host_is_not_a_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guard the other direction: duplicate detection must not be so eager it blocks new sites."""
    _run_issue_parse(tmp_path, _body("https://one.example.com/", "站一"), monkeypatch)
    second = _run_issue_parse(tmp_path, _body("https://two.example.com/", "站二"), monkeypatch)

    assert second.get("duplicate", "false") != "true"
    assert second["ok"] == "true"


def test_a_key_collision_is_disambiguated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two origins with the same ASCII slug must not share a recipe file."""
    first = _run_issue_parse(tmp_path, _body("https://site.example.com/", "name"), monkeypatch)
    # A different host that slugs to the same thing is impossible here, so force the collision by
    # seeding the registry with the derived key pointing at another origin.
    import yaml as _yaml

    registry_path = tmp_path / "sites" / "registry.yaml"
    doc = _yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    doc["sites"].append(
        {
            "key": f"{first['key']}-x",
            "name": "x",
            "base_url": "https://elsewhere.example.net",
            "adapter": "auto",
            "enabled": True,
            "origin": {"type": "manual"},
            "last_run": {},
        }
    )
    registry_path.write_text(_yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")

    second = _run_issue_parse(tmp_path, _body("https://other.example.com/", "name"), monkeypatch)

    assert second["key"] != first["key"]
    assert (tmp_path / "sites" / f"{second['key']}.yaml").is_file()
