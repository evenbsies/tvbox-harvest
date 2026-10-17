"""Parse routes: selecting a player by its name.

These are the third kind of "line" and the one most easily confused with the other two. A
collection line belongs to a site; a parse route belongs to a player. Publishing one means play
requests go through that third party, so the selection has to be explicit -- which is what these
tests are about.
"""

from __future__ import annotations

from harvest.generate.parses import ParseSource, load_parse_sources, select_routes

#: Shape copied from 洛雪TV's `official_routes` (2026-10-17), trimmed to what matters.
ROUTES = [
    {"id": "official_default", "name": "默认解析", "type": "iframe", "api": "https://p.example/?url="},
    {
        "id": "official_musbpyqz",
        "name": "洛雪4K",
        "type": "json",
        "api": "/json-player.php?route=official_musbpyqz&url=",
        "official_only": True,
        "player_codes": ["qq", "bilibili"],
    },
    {"id": "official_mugf3fgl", "name": "闪电", "type": "iframe", "api": "https://bfq.example/?url="},
]


def _source(**kwargs) -> ParseSource:
    params = {
        "name": "洛雪TV",
        "config": "https://tv.lxyy.club/api/proxy.php?action=config",
        "keep": ["4K"],
    }
    params.update(kwargs)
    return ParseSource(**params)


def test_only_the_route_whose_name_matches_is_kept() -> None:
    entries, seen = select_routes(ROUTES, _source(), base_url="https://tv.lxyy.club")

    assert [e["name"] for e in entries] == ["洛雪4K"]
    assert seen == ["默认解析", "洛雪4K", "闪电"]


def test_a_json_route_becomes_type_1_and_a_relative_api_is_absolutised() -> None:
    """TVBox resolves a relative parse url against the client's own host -- always make it absolute."""
    entries, _ = select_routes(ROUTES, _source(), base_url="https://tv.lxyy.club")
    entry = entries[0]

    assert entry["type"] == 1  # json
    assert entry["url"] == "https://tv.lxyy.club/json-player.php?route=official_musbpyqz&url="
    assert entry["ext"]["player_codes"] == ["qq", "bilibili"]
    assert entry["ext"]["official_only"] is True


def test_an_iframe_route_becomes_type_0() -> None:
    entries, _ = select_routes(ROUTES, _source(keep=["闪电"]), base_url="https://tv.lxyy.club")
    assert entries[0]["type"] == 0


def test_without_keep_every_route_is_published() -> None:
    """The default is "none", not "all": publishing parsers is an explicit choice per source."""
    entries, _ = select_routes(ROUTES, _source(keep=[]), base_url="https://tv.lxyy.club")
    assert len(entries) == 3


def test_drop_wins_over_keep() -> None:
    entries, _ = select_routes(
        ROUTES, _source(keep=[], drop=["闪电", "默认"]), base_url="https://tv.lxyy.club"
    )
    assert [e["name"] for e in entries] == ["洛雪4K"]


def test_a_route_with_no_address_is_skipped() -> None:
    routes = [{"name": "洛雪4K", "type": "json", "api": ""}]
    entries, _ = select_routes(routes, _source(), base_url="https://tv.lxyy.club")
    assert entries == []


def test_the_config_file_is_read_with_its_documented_defaults(tmp_path, monkeypatch) -> None:
    from harvest.config import Settings

    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "parses.yaml").write_text(
        "parses:\n  - name: 洛雪TV\n    config: https://tv.lxyy.club/api/proxy.php?action=config\n"
        '    keep: ["4K"]\n',
        encoding="utf-8",
    )
    settings = Settings(root=tmp_path)

    sources = load_parse_sources(settings)

    assert len(sources) == 1
    assert sources[0].routes_key == "official_routes"
    assert sources[0].url_field == "api"
    assert sources[0].keep == ["4K"]
    assert sources[0].type_map["json"] == 1


def test_build_config_keeps_the_parses_it_was_given() -> None:
    """Regression: `config.update(top)` put the defaults' empty list back over the real one.

    The run logged "保留 1 条 (洛雪4K)" and the published file had `parses: []`. A silent
    overwrite is worse than an error, because the note said it worked.
    """
    from harvest.generate.tvbox import build_config

    entries = [{"name": "洛雪4K", "type": 1, "url": "https://tv.lxyy.club/json-player.php?x=1&url="}]
    config = build_config(
        entries=[],
        defaults={"top_level": {"spider": "", "parses": []}},
        parses=entries,
    )
    assert config["parses"] == entries


def test_build_config_falls_back_to_the_defaults_when_nothing_is_passed() -> None:
    from harvest.generate.tvbox import build_config

    declared = [{"name": "手写解析", "type": 0, "url": "https://p.example/?url="}]
    config = build_config(entries=[], defaults={"top_level": {"parses": declared}})
    assert config["parses"] == declared
