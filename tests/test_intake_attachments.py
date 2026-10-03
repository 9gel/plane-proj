"""Intake show supplies attachment IDs without a separate listing command."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from plane_proj.cli import Context, cli
from plane_proj.guards import ConfigError
from tests.conftest import Card, Recorder


@pytest.mark.parametrize("group", ["intake", "card"])
def test_show_lists_compact_attachment_metadata(board, client, monkeypatch, group):
    client.cards = [Card(id="work-id", sequence_id=20)]
    client.work_items.relations = Recorder(client.calls, "relations", result={})
    client.intake_records = [SimpleNamespace(
        id="intake-id", issue="work-id", status=-2,
        issue_detail=Card(id="work-id", sequence_id=20),
    )]
    calls = []
    def attachments(slug, project_id, issue_id):
        calls.append((slug, project_id, issue_id))
        return [SimpleNamespace(
            id="asset-id", attributes={"name": "screen shot.png", "type": "image/png"},
            size=123, asset="private/storage/key",
        )]
    client.work_items.attachments = SimpleNamespace(list=attachments)
    monkeypatch.setattr(Context, "board", property(lambda self: board))
    result = CliRunner().invoke(cli, ["--json", group, "show", "DEMO-20"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "attachment_count" not in payload
    assert len(payload["attachments"]) == 1
    attachment = payload["attachments"][0]
    assert attachment["attachment_id"] == "asset-id"
    assert attachment["name"] == "screen shot.png"
    assert attachment["content_type"] == "image/png"
    assert attachment["size"] == 123
    assert set(attachment) == {"attachment_id", "name", "content_type", "size"}
    assert "private/storage/key" not in result.output
    assert calls == [("example-workspace", "project-uuid", "work-id")]
    if group == "intake":
        assert not client.named("work_items.retrieve")
    else:
        assert not client.named("intake.list")
    human = CliRunner().invoke(cli, [group, "show", "DEMO-20"])
    assert human.exit_code == 0, human.output
    assert "description_html:\n<p>body</p>" in human.output
    assert "attachments:\n  - attachment_id: asset-id\n" in human.output
    assert "    name: screen shot.png\n" in human.output
    assert "    content_type: image/png\n    size: 123 bytes" in human.output
    assert "attachment_count" not in human.output
    assert "intake download" not in human.output


@pytest.mark.parametrize("group", ["intake", "card"])
def test_attachment_command_downloads_by_reference(
    board, client, storage, tmp_path, monkeypatch, group,
):
    url, _, body = storage
    calls = configure_download(client, url + "/image")
    client.cards = [Card(id="work-id", sequence_id=20)]
    client.intake_records = [SimpleNamespace(
        id="intake-id", issue="work-id", issue_detail=client.cards[0],
    )]
    monkeypatch.setattr(Context, "board", property(lambda self: board))
    destination = tmp_path / "image.png"
    result = CliRunner().invoke(cli, [
        "--json", group, "attachment", "DEMO-20", "asset-id", "--out", str(destination),
    ])
    assert result.exit_code == 0, result.output
    assert destination.read_bytes() == body
    assert json.loads(result.output)["attachment_id"] == "asset-id"
    assert calls == [("example-workspace", "project-uuid", "work-id", "asset-id")]
    if group == "card":
        assert not client.named("intake.list")

    client.calls.clear()
    repeated = CliRunner().invoke(cli, [
        group, "attachment", "DEMO-20", "asset-id", "--out", str(destination),
    ])
    assert repeated.exit_code != 0
    assert "already exists" in repeated.output
    assert destination.read_bytes() == body
    assert client.calls == []


@pytest.fixture
def storage():
    received = []
    body = b"image-content-from-storage"
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(dict(self.headers))
            if self.path == "/error":
                self.send_error(403)
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header(
                "Content-Length", str(len(body) + (10 if self.path == "/short" else 0)),
            )
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    assert server.server_port > 10000
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", received, body
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def configure_download(client, url):
    client.intake_records = [SimpleNamespace(id="intake-id", issue="work-id")]
    client.config = SimpleNamespace(base_path="http://plane.invalid/api/v1")
    calls = []
    def signed_url(*args):
        calls.append(args)
        return url
    client.work_items.attachments = SimpleNamespace(get_download_url=signed_url)
    return calls


def test_download_streams_without_plane_credentials(board, client, storage, tmp_path):
    url, received, body = storage
    calls = configure_download(client, url + "/image")
    before = set(tmp_path.iterdir())
    destination = tmp_path / "saved.png"
    result = board.download_attachment("work-id", "asset-id", destination)
    assert destination.read_bytes() == body
    assert result["size"] == len(body)
    assert result["content_type"] == "image/png"
    assert calls == [("example-workspace", "project-uuid", "work-id", "asset-id")]
    assert "X-Api-Key" not in received[0]
    assert "Authorization" not in received[0]
    assert set(tmp_path.iterdir()) == before | {destination}


@pytest.mark.parametrize("endpoint", ["error", "short"])
def test_failed_download_leaves_no_output_or_signed_url_in_error(
    board, client, storage, tmp_path, endpoint,
):
    url, _, _ = storage
    configure_download(client, url + "/" + endpoint)
    before = set(tmp_path.iterdir())
    with pytest.raises(ConfigError, match="no output file") as failure:
        board.download_attachment("work-id", "asset-id", tmp_path / "saved.png")
    assert url not in str(failure.value)
    assert set(tmp_path.iterdir()) == before


def test_download_refuses_existing_file_before_requests(board, client, tmp_path):
    destination = tmp_path / "keep.png"
    destination.write_bytes(b"existing")
    with pytest.raises(ConfigError, match="already exists"):
        board.download_attachment("work-id", "asset-id", destination)
    assert destination.read_bytes() == b"existing"
    assert client.calls == []


def test_no_attachments_is_explicit(board, client, monkeypatch):
    client.intake_records = [SimpleNamespace(
        id="intake-id", issue="work-id", status=-2, issue_detail=Card(id="work-id"),
    )]
    monkeypatch.setattr(Context, "board", property(lambda self: board))
    result = CliRunner().invoke(cli, ["--json", "intake", "show", "work-id"])
    assert result.exit_code == 0, result.output
    assert "attachment_count" not in json.loads(result.output)
    assert json.loads(result.output)["attachments"] == []


@pytest.mark.parametrize("url", [None, "file:///etc/passwd"])
def test_invalid_download_url_creates_no_file(board, client, tmp_path, url):
    configure_download(client, url)
    before = set(tmp_path.iterdir())
    with pytest.raises(ConfigError, match="download URL"):
        board.download_attachment("work-id", "asset-id", tmp_path / "saved.png")
    assert set(tmp_path.iterdir()) == before


def test_missing_output_directory_is_refused_before_requests(board, client, tmp_path):
    with pytest.raises(ConfigError, match="directory"):
        board.download_attachment("work-id", "asset-id", tmp_path / "absent" / "image")
    assert client.calls == []
