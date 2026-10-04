"""The config file: what it must contain, and what it refuses to load."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from plane_proj.config import load_config
from plane_proj.guards import ConfigError, UnknownEstimateValue, UnknownModule
from tests.conftest import ALICE, AUTOMATION


def write(tmp_path: Path, document: dict) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_a_missing_default_file_is_reported(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PLANE_PROJ_CONFIG", raising=False)

    with pytest.raises(ConfigError, match="No config file"):
        load_config(None)


def test_the_environment_names_it_when_the_flag_does_not(config_path, monkeypatch):
    monkeypatch.setenv("PLANE_PROJ_CONFIG", str(config_path))
    assert load_config(None).workspace_slug == "example-workspace"


def test_workspace_can_come_from_credentials(tmp_path):
    assert load_config(write(tmp_path, {"defaults": {"project": "DEMO"}})).workspace_slug is None


@pytest.mark.parametrize("key", ["workspace", "projects", "states", "cycles", "members"])
def test_server_metadata_is_rejected(tmp_path, key):
    with pytest.raises(ConfigError, match="unsupported config keys"):
        load_config(write(tmp_path, {key: {}}))


@pytest.mark.parametrize("document", [
    {"defaults": []},
    {"defaults": {"project": ""}},
    {"defaults": {"web_url": "plane.example.com"}},
    {"defaults": {"web_url": "ftp://plane.example.com"}},
    {"estimate_points": {"3": "u"}},
    {"defaults": {"project": "DEMO"}, "estimate_points": {"three": "u"}},
    {"defaults": {"project": "DEMO"}, "estimate_points": {"3": "u", "5": "u"}},
])
def test_malformed_config_is_refused(tmp_path, document):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, document))


def test_web_url_defaults_to_plane_cloud_and_is_configurable(tmp_path):
    assert load_config(write(tmp_path, {})).web_url == "https://app.plane.so"
    configured = {"defaults": {"web_url": "https://plane.example.com/"}}
    assert load_config(write(tmp_path, configured)).web_url == (
        "https://plane.example.com"
    )


def test_the_default_project_is_used_when_none_is_given(config):
    assert config.project(None).key == "DEMO"


def test_a_member_resolves_by_name_or_uuid(config):
    assert config.member_id("alice") == ALICE
    assert config.member_id(AUTOMATION) == AUTOMATION


def test_an_unknown_member_names_the_known_ones(config):
    with pytest.raises(ConfigError, match="automation"):
        config.member_id("nobody")


def test_an_unknown_module_says_to_create_it_on_plane(config):
    with pytest.raises(UnknownModule, match="Create the module on Plane"):
        config.project("DEMO").module_id("webapp")


def test_an_off_scale_estimate_is_refused_rather_than_rounded(config):
    with pytest.raises(UnknownEstimateValue):
        config.project("DEMO").estimate_uuid(4)


def test_a_uuid_maps_back_to_its_scale_value(config):
    assert config.project("DEMO").estimate_value("uuid-5") == 5
    assert config.project("DEMO").estimate_value("uuid-unknown") is None


def test_runtime_guards_default_to_strict(config_path):
    rules = load_config(config_path).project("DEMO").rules
    assert rules.require_cycle
    assert rules.require_module
    assert rules.require_estimate
