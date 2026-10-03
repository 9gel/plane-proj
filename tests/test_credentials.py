"""The credential wrapper, watched redacting.

**A guard here is not a guard until it has been watched failing.** Asserting
only that the key is absent from a string passes just as well when the string
is empty, or when the object never rendered at all. Each test therefore asserts
the redaction marker is *present* first, and only then that the value is gone.
"""

from __future__ import annotations

import pytest

from plane_proj.credentials import REDACTED, Secret, load_credentials, load_env_file
from plane_proj.guards import ConfigError

KEY = "plane_api_0123456789abcdef"


@pytest.fixture(autouse=True)
def isolated_credentials_directory(tmp_path, monkeypatch):
    """Local credential files must not affect missing-credential tests."""
    monkeypatch.chdir(tmp_path)


def test_repr_redacts():
    rendered = repr(Secret(KEY))

    assert REDACTED in rendered
    assert KEY not in rendered


def test_str_redacts():
    rendered = str(Secret(KEY))

    assert REDACTED in rendered
    assert KEY not in rendered


def test_an_f_string_redacts():
    """The path that actually leaks: a log line interpolating the object."""
    rendered = f"connecting with {Secret(KEY)}"

    assert REDACTED in rendered
    assert KEY not in rendered


def test_a_traceback_rendering_locals_does_not_show_the_value():
    secret = Secret(KEY)
    rendered = repr(locals())

    assert REDACTED in rendered
    assert KEY not in rendered
    assert secret.reveal() == KEY, "the value must still be reachable deliberately"


def test_the_value_is_reachable_only_through_reveal():
    assert Secret(KEY).reveal() == KEY


def test_missing_credentials_name_what_is_missing(monkeypatch):
    monkeypatch.delenv("PLANE_API_HOST_URL", raising=False)
    monkeypatch.delenv("PLANE_API_KEY", raising=False)

    with pytest.raises(ConfigError, match="PLANE_API_HOST_URL and PLANE_API_KEY"):
        load_credentials("example-workspace")


def test_the_config_file_supplies_the_workspace_when_the_environment_does_not(monkeypatch):
    monkeypatch.setenv("PLANE_API_HOST_URL", "https://plane.example.org/")
    monkeypatch.setenv("PLANE_API_KEY", KEY)
    monkeypatch.delenv("PLANE_WORKSPACE_SLUG", raising=False)

    assert load_credentials("from-config").workspace_slug == "from-config"


def test_a_trailing_slash_on_the_host_is_dropped(monkeypatch):
    monkeypatch.setenv("PLANE_API_HOST_URL", "https://plane.example.org/")
    monkeypatch.setenv("PLANE_API_KEY", KEY)

    assert load_credentials("w").host == "https://plane.example.org"


@pytest.mark.parametrize("host", ["https://app.plane.so/", "https://plane.so"])
def test_plane_cloud_web_hosts_are_refused_before_a_request(monkeypatch, host):
    monkeypatch.setenv("PLANE_API_HOST_URL", host)
    monkeypatch.setenv("PLANE_API_KEY", KEY)

    with pytest.raises(ConfigError, match="https://api.plane.so"):
        load_credentials("workspace")


def test_an_env_file_yields_only_the_plane_variables(tmp_path):
    path = tmp_path / "env"
    path.write_text(
        "export PLANE_API_HOST_URL=https://plane.example.org\n"
        f"PLANE_API_KEY='{KEY}'\n"
        "# a comment\n"
        "UNRELATED_SECRET=do-not-import\n",
        encoding="utf-8",
    )

    found = load_env_file(path)

    assert found["PLANE_API_KEY"] == KEY
    assert "UNRELATED_SECRET" not in found


def test_the_environment_supplies_a_slug_the_config_file_omits(monkeypatch):
    """BUG.md §1: PLANE_WORKSPACE_SLUG was demanded and then ignored."""
    monkeypatch.setenv("PLANE_API_HOST_URL", "https://plane.example.org")
    monkeypatch.setenv("PLANE_API_KEY", KEY)
    monkeypatch.setenv("PLANE_WORKSPACE_SLUG", "from-env")

    assert load_credentials(None).workspace_slug == "from-env"


def test_the_config_file_wins_over_the_environment(monkeypatch):
    """Its UUIDs belong to one workspace; the environment must not redirect them."""
    monkeypatch.setenv("PLANE_API_HOST_URL", "https://plane.example.org")
    monkeypatch.setenv("PLANE_API_KEY", KEY)
    monkeypatch.setenv("PLANE_WORKSPACE_SLUG", "from-env")

    assert load_credentials("from-config").workspace_slug == "from-config"


def test_no_slug_anywhere_says_it_cannot_be_captured(monkeypatch):
    """BUG.md §2 and §3: the message must not send the reader to `project capture`."""
    monkeypatch.setenv("PLANE_API_HOST_URL", "https://plane.example.org")
    monkeypatch.setenv("PLANE_API_KEY", KEY)
    monkeypatch.delenv("PLANE_WORKSPACE_SLUG", raising=False)

    with pytest.raises(ConfigError, match="never recover"):
        load_credentials(None, config_path="estimates.json")
