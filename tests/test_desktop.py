import json
import sys
import types
import urllib.request

from paramfabric import desktop


def _isolate(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for name in ("PARAMFABRIC_DATABASE_URL", "PARAMFABRIC_AUDIT_PATH", "PARAMFABRIC_AUTH_REQUIRED", "PARAMFABRIC_CONFIG_PATH"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "params.yaml").write_text("parameters:\n  - name: chord\n    value: 2\n    unit: m\n", encoding="utf-8")


def test_local_server_boots_with_injected_admin_session_and_no_prompt(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    local = desktop.start_local_server(tmp_path / "params.yaml")
    try:
        token = local.app.state.local_session_token
        assert local.studio_url == f"{local.base_url}/studio#token={token}"
        # No Authorization header: local mode must not prompt for credentials
        with urllib.request.urlopen(f"{local.base_url}/api/local/session") as response:
            session = json.load(response)
        assert session["role"] == "Local Admin" and session["token"] == token
        with urllib.request.urlopen(f"{local.base_url}/api/workbench") as response:
            workbench = json.load(response)
        assert workbench["diagnostics"]["ok"] is True
        assert workbench["engines"]["builtin"]["available"] is True
        assert workbench["mode"] == "local"
        with urllib.request.urlopen(f"{local.base_url}/studio") as response:
            html = response.read().decode()
        assert "Run Simulation" in html and "Import CAD" in html
        assert (tmp_path / ".paramfabric" / "studio.db").is_file()
    finally:
        local.stop()
    assert not local.thread.is_alive()


def test_launch_opens_native_window_with_token_then_shuts_down(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    opened = {}

    def create_window(title, url, **options):
        opened.update(title=title, url=url, **options)
        with urllib.request.urlopen(url.split("#")[0].replace("/studio", "/health")) as response:
            opened["health"] = response.status

    fake = types.SimpleNamespace(create_window=create_window, start=lambda: None)
    monkeypatch.setitem(sys.modules, "webview", fake)
    desktop.launch(tmp_path / "params.yaml")
    assert opened["title"] == "Paramfabric Studio"
    assert "/studio#token=" in opened["url"]
    assert opened["health"] == 200


def test_frozen_executable_uses_its_own_folder_as_workspace(monkeypatch, tmp_path):
    executable = tmp_path / "ParamfabricStudio.exe"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    assert desktop.workspace_root() == tmp_path
