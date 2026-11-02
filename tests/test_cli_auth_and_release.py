"""Login never saves a rejected key, init needs no flags, and release is scoped and honest."""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path
from typing import Any

from bridge_plugin.cli import run_cli
from bridge_plugin.runtime import EXIT_AUTH_MISSING, EXIT_ERROR, EXIT_OK


class NonTTYBuffer(io.StringIO):
    def isatty(self) -> bool:
        return False


def make_key(org: str) -> str:
    claims = base64.urlsafe_b64encode(json.dumps({"organizationId": org}).encode()).decode().rstrip("=")
    return f"xrkey_v1.{claims}.signature"


def respond(status: int, body: dict[str, Any]) -> Any:
    def transport(_req: Any, _timeout: float) -> tuple[int, bytes]:
        return status, json.dumps(body).encode("utf-8")

    return transport


def login(tmp_path: Path, key: str, transport: Any) -> tuple[int, dict[str, Any], Path]:
    creds = tmp_path / "creds.json"
    out = NonTTYBuffer()
    code = run_cli(
        ["auth", "login", "--api-key", key],
        cwd=tmp_path,
        env={"BRIDGE_CREDENTIALS_PATH": str(creds)},
        stdout_stream=out,
        transport=transport,
    )
    return code, json.loads(out.getvalue()), creds


def test_login_rejects_text_that_is_not_a_bridge_key(tmp_path: Path) -> None:
    calls: list[Any] = []

    def transport(req: Any, _timeout: float) -> tuple[int, bytes]:
        calls.append(req)
        return 200, b"{}"

    code, data, creds = login(tmp_path, "garbage", transport)
    assert code == EXIT_AUTH_MISSING
    assert data["error"] == "INVALID_API_KEY"
    assert not creds.exists()
    assert calls == []


def test_login_does_not_save_a_key_bridge_rejects(tmp_path: Path) -> None:
    code, data, creds = login(tmp_path, make_key("org_a"), respond(401, {"ok": False, "error": "INVALID"}))
    assert code == EXIT_AUTH_MISSING
    assert data["authenticated"] is False
    assert not creds.exists()


def test_login_reads_the_workspace_from_the_key(tmp_path: Path) -> None:
    code, data, creds = login(tmp_path, make_key("org_from_key"), respond(200, {"ok": True, "keys": []}))
    assert code == EXIT_OK
    assert data["organizationId"] == "org_from_key"
    assert data["verified"] is True
    assert creds.exists()


def test_login_saves_when_offline_and_says_it_is_unverified(tmp_path: Path) -> None:
    def offline(_req: Any, _timeout: float) -> tuple[int, bytes]:
        raise ConnectionError("no route")

    code, data, creds = login(tmp_path, make_key("org_a"), offline)
    assert code == EXIT_OK
    assert data["verified"] is False
    assert creds.exists()


def test_whoami_verify_fails_on_a_rejected_key(tmp_path: Path) -> None:
    out = NonTTYBuffer()
    code = run_cli(
        ["auth", "whoami", "--verify"],
        cwd=tmp_path,
        env={"BRIDGE_API_KEY": make_key("org_a"), "BRIDGE_CREDENTIALS_PATH": str(tmp_path / "creds.json")},
        stdout_stream=out,
        transport=respond(401, {"ok": False, "error": "INVALID"}),
    )
    assert code == EXIT_AUTH_MISSING
    data = json.loads(out.getvalue())
    assert data["ok"] is False
    assert data["verified"] is False


def test_init_uses_the_workspace_in_the_key_and_ignores_local_state(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text("node_modules", encoding="utf-8")
    env = {"BRIDGE_API_KEY": make_key("org_from_key"), "BRIDGE_CREDENTIALS_PATH": str(tmp_path / "creds.json")}

    out = NonTTYBuffer()
    assert run_cli(["init", "--repo", "acme/api"], cwd=tmp_path, env=env, stdout_stream=out) == EXIT_OK
    assert json.loads(out.getvalue())["organizationId"] == "org_from_key"
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == "node_modules\n.bridge/\n"

    # Running it again changes nothing.
    out_again = NonTTYBuffer()
    assert run_cli(["init", "--repo", "acme/api"], cwd=tmp_path, env=env, stdout_stream=out_again) == EXIT_OK
    assert json.loads(out_again.getvalue())["gitignoreUpdated"] is False


def bound_project(tmp_path: Path) -> dict[str, str]:
    (tmp_path / ".bridge.json").write_text(
        json.dumps({"plugin": "bridge", "organizationId": "org_a", "apiUrl": "https://bridge.test", "repository": "acme/api"}),
        encoding="utf-8",
    )
    return {"BRIDGE_API_KEY": make_key("org_a"), "BRIDGE_CREDENTIALS_PATH": str(tmp_path / "creds.json")}


def release_body(args: list[str], tmp_path: Path) -> dict[str, Any]:
    bodies: list[dict[str, Any]] = []

    def transport(req: Any, _timeout: float) -> tuple[int, bytes]:
        bodies.append(json.loads(req.data.decode("utf-8")))
        return 200, b'{"ok": true, "releasedCount": 1}'

    out = NonTTYBuffer()
    code = run_cli(["release", *args], cwd=tmp_path, env=bound_project(tmp_path), stdout_stream=out, transport=transport)
    assert code == EXIT_OK
    return bodies[0]


def test_release_all_only_touches_this_agents_claims(tmp_path: Path) -> None:
    body = release_body(["--all", "--agent", "cursor"], tmp_path)
    assert body["agentId"] == "cursor"
    assert body["all"] is True
    assert "leaseId" not in body


def test_release_everyone_is_explicit(tmp_path: Path) -> None:
    body = release_body(["--everyone"], tmp_path)
    assert body["all"] is True
    assert "agentId" not in body


def test_release_reports_a_server_failure(tmp_path: Path) -> None:
    out = NonTTYBuffer()
    code = run_cli(
        ["release", "--all"],
        cwd=tmp_path,
        env=bound_project(tmp_path),
        stdout_stream=out,
        transport=respond(500, {"ok": False, "error": "BOOM"}),
    )
    assert code == EXIT_ERROR
    data = json.loads(out.getvalue())
    assert data["ok"] is False
    assert data["released"] is False
