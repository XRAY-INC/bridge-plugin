"""Pure-stdlib HTTP client for Bridge with offline JSONL spooling (.bridge/spool.jsonl) and idempotent flush."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from . import __version__
from .config import BridgeConfig
from .runtime import (
    EXIT_AUTH_MISSING,
    EXIT_CONCURRENCY_BLOCK,
    EXIT_ERROR,
    EXIT_OFFLINE_SPOOLED,
    EXIT_OK,
    emit_stderr,
)


@dataclass
class BridgeResponse:
    """Structured result from a Bridge API call or offline spool fallback."""

    ok: bool
    status_code: int
    exit_code: int
    data: dict[str, Any]
    spooled: bool = False
    error: str | None = None


class BridgeClient:
    """HTTP client for Bridge (/api/v1/*) with automatic header resolution and offline spool."""

    def __init__(
        self,
        config: BridgeConfig,
        *,
        agent_id: str = "bridge-cli",
        timeout: float = 10.0,
        transport: Any = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.config = config
        self.agent_id = agent_id
        self.timeout = timeout
        # Optional callable(req: urllib.request.Request, timeout: float) -> (status, body_bytes) for testing
        self._transport = transport
        self._env = env if env is not None else os.environ

    def build_headers(
        self,
        *,
        idempotency_key: str | None = None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": f"bridge-plugin/{__version__} ({self.agent_id})",
        }
        if self.config.api_key:
            key = self.config.api_key.strip()
            headers["Authorization"] = f"Bearer {key}"
            headers["x-api-key"] = key
        if self.config.organization_id:
            headers["x-organization-id"] = self.config.organization_id.strip()
        if idempotency_key:
            headers["x-idempotency-key"] = idempotency_key
        if extra_headers:
            headers.update(extra_headers)
        return headers

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        spool_on_offline: bool = False,
        require_auth: bool = True,
        allow_unbound: bool = False,
    ) -> BridgeResponse:
        """Perform an HTTP request to Bridge or spool mutations when offline.

        Hard Privacy Firewall: Whenever `config.is_work_context` is False (`LOCAL` mode) and
        `allow_unbound` is False, refuse ALL network requests and offline spooling so unbound
        personal/local directories can NEVER leak session telemetry to a Bridge server.
        """
        if not self.config.is_work_context and not allow_unbound:
            repo_label = self.config.repository or self.config.workspace_root.name
            return BridgeResponse(
                ok=False,
                status_code=0,
                exit_code=EXIT_AUTH_MISSING,
                data={
                    "ok": False,
                    "localOnly": True,
                    "mode": "LOCAL",
                    "repository": repo_label,
                    "error": "LOCAL_WORKSPACE_UNBOUND",
                    "message": (
                        f"Current directory '{repo_label}' is LOCAL (not bound to Bridge via .bridge/bridge.json). "
                        "Refusing to transmit local session data to Bridge. "
                        "Run 'brg init' only if this repository belongs to a Bridge workspace."
                    ),
                },
                error="LOCAL_WORKSPACE_UNBOUND",
            )

        if require_auth and not self.config.is_authenticated and not self.config.is_bound:
            return BridgeResponse(
                ok=False,
                status_code=401,
                exit_code=EXIT_AUTH_MISSING,
                data={
                    "ok": False,
                    "error": "AUTH_OR_CONFIG_MISSING",
                    "message": "Missing BRIDGE_API_KEY and .bridge/bridge.json. Run 'bridge auth login' or 'bridge init'.",
                },
                error="AUTH_OR_CONFIG_MISSING",
            )

        url = f"{self.config.api_url.rstrip('/')}/{path.lstrip('/')}"
        if params:
            clean_params = {
                k: str(v)
                for k, v in params.items()
                if v is not None and str(v) != ""
            }
            if clean_params:
                sep = "&" if "?" in url else "?"
                url = f"{url}{sep}{urllib.parse.urlencode(clean_params)}"

        body_bytes: bytes | None = None
        if json_body is not None:
            body_bytes = json.dumps(json_body).encode("utf-8")

        headers = self.build_headers(idempotency_key=idempotency_key)

        # Simulate offline mode when BRIDGE_OFFLINE=1 is set
        offline_val = (
            self._env.get("BRIDGE_OFFLINE", "") or os.environ.get("BRIDGE_OFFLINE", "")
        ).strip().lower()
        if offline_val in ("1", "true", "yes"):
            if spool_on_offline and json_body is not None:
                return self._spool_mutation(
                    method=method,
                    path=path,
                    payload=dict(json_body),
                    idempotency_key=idempotency_key,
                    reason="BRIDGE_OFFLINE=1",
                )
            return BridgeResponse(
                ok=False,
                status_code=503,
                exit_code=EXIT_ERROR,
                data={"ok": False, "error": "OFFLINE", "message": "Bridge client is in offline mode."},
                error="OFFLINE",
            )

        req = urllib.request.Request(
            url=url,
            data=body_bytes,
            headers=headers,
            method=method.upper(),
        )

        try:
            if self._transport is not None:
                status_code, raw_bytes = self._transport(req, self.timeout)
            else:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    status_code = resp.getcode() or 200
                    raw_bytes = resp.read()
            parsed = self._parse_json_bytes(raw_bytes)
            exit_code = self._map_status_to_exit_code(status_code, parsed)
            ok = 200 <= status_code < 300 and bool(parsed.get("ok", True))
            if not ok and exit_code == EXIT_OK:
                exit_code = EXIT_ERROR
            return BridgeResponse(
                ok=ok,
                status_code=status_code,
                exit_code=exit_code,
                data=parsed,
                error=parsed.get("error") if not ok else None,
            )
        except urllib.error.HTTPError as http_err:
            status_code = http_err.code
            try:
                raw_bytes = http_err.read()
            except Exception:
                raw_bytes = b""
            parsed = self._parse_json_bytes(raw_bytes)
            if "ok" not in parsed:
                parsed["ok"] = False
            if "error" not in parsed:
                parsed["error"] = f"HTTP_{status_code}"
            exit_code = self._map_status_to_exit_code(status_code, parsed)
            return BridgeResponse(
                ok=False,
                status_code=status_code,
                exit_code=exit_code,
                data=parsed,
                error=str(parsed.get("error") or f"HTTP_{status_code}"),
            )
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as net_err:
            if spool_on_offline and json_body is not None:
                return self._spool_mutation(
                    method=method,
                    path=path,
                    payload=dict(json_body),
                    idempotency_key=idempotency_key,
                    reason=str(net_err),
                )
            return BridgeResponse(
                ok=False,
                status_code=0,
                exit_code=EXIT_ERROR,
                data={
                    "ok": False,
                    "error": "NETWORK_ERROR",
                    "message": str(net_err),
                },
                error="NETWORK_ERROR",
            )

    @staticmethod
    def _parse_json_bytes(raw_bytes: bytes) -> dict[str, Any]:
        if not raw_bytes:
            return {"ok": True}
        text = raw_bytes.decode("utf-8", errors="replace").strip()
        if not text:
            return {"ok": True}
        try:
            loaded = json.loads(text)
            if isinstance(loaded, dict):
                return loaded
            return {"ok": True, "result": loaded}
        except Exception:
            return {"ok": False, "raw": text}

    @staticmethod
    def _map_status_to_exit_code(status_code: int, parsed: Mapping[str, Any]) -> int:
        if 200 <= status_code < 300:
            if parsed.get("blocked") is True or parsed.get("status") == "BLOCKED_ON_DECISION":
                return EXIT_CONCURRENCY_BLOCK
            return EXIT_OK
        if status_code in (401, 403):
            return EXIT_AUTH_MISSING
        if status_code == 409:
            return EXIT_CONCURRENCY_BLOCK
        err_code = str(parsed.get("error") or "").upper()
        if "CONFLICT" in err_code or "BLOCKED" in err_code:
            return EXIT_CONCURRENCY_BLOCK
        return EXIT_ERROR

    def _spool_mutation(
        self,
        *,
        method: str,
        path: str,
        payload: dict[str, Any],
        idempotency_key: str | None,
        reason: str,
    ) -> BridgeResponse:
        """Append a failed mutation to .bridge/spool.jsonl with deduplication by idempotencyKey."""
        spool_file = self.config.spool_path
        spool_file.parent.mkdir(parents=True, exist_ok=True)

        existing_entries = self.read_spool()
        if idempotency_key:
            for item in existing_entries:
                if item.get("idempotencyKey") == idempotency_key:
                    emit_stderr(
                        f"Offline ({reason}); mutation already queued in {spool_file} (idempotencyKey={idempotency_key[:12]}...)."
                    )
                    return BridgeResponse(
                        ok=True,
                        status_code=202,
                        exit_code=EXIT_OFFLINE_SPOOLED,
                        spooled=True,
                        data={
                            "ok": True,
                            "spooled": True,
                            "deduplicated": True,
                            "idempotencyKey": idempotency_key,
                            "spoolPath": str(spool_file),
                            "method": method.upper(),
                            "path": path,
                        },
                    )

        record = {
            "idempotencyKey": idempotency_key,
            "method": method.upper(),
            "path": path,
            "payload": payload,
            "queuedAt": datetime.now(timezone.utc).isoformat(),
        }
        with spool_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

        emit_stderr(
            f"Offline ({reason}); spooled {method.upper()} {path} to {spool_file}."
        )
        return BridgeResponse(
            ok=True,
            status_code=202,
            exit_code=EXIT_OFFLINE_SPOOLED,
            spooled=True,
            data={
                "ok": True,
                "spooled": True,
                "deduplicated": False,
                "idempotencyKey": idempotency_key,
                "spoolPath": str(spool_file),
                "method": method.upper(),
                "path": path,
            },
        )

    def read_spool(self) -> list[dict[str, Any]]:
        """Read all queued mutations from .bridge/spool.jsonl."""
        spool_file = self.config.spool_path
        if not spool_file.is_file():
            return []
        entries: list[dict[str, Any]] = []
        for line in spool_file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
                if isinstance(obj, dict):
                    entries.append(obj)
            except Exception:
                continue
        return entries

    def flush_spool(self) -> BridgeResponse:
        """Idempotently replay queued mutations from .bridge/spool.jsonl."""
        entries = self.read_spool()
        spool_file = self.config.spool_path
        if not self.config.is_work_context:
            return BridgeResponse(
                ok=False,
                status_code=0,
                exit_code=EXIT_AUTH_MISSING,
                data={
                    "ok": False,
                    "localOnly": True,
                    "mode": "LOCAL",
                    "error": "LOCAL_WORKSPACE_UNBOUND",
                    "flushedCount": 0,
                    "remainingCount": len(entries),
                    "message": "Refusing to flush spool in a LOCAL (unbound) directory.",
                },
                error="LOCAL_WORKSPACE_UNBOUND",
            )
        if not entries:
            return BridgeResponse(
                ok=True,
                status_code=200,
                exit_code=EXIT_OK,
                data={
                    "ok": True,
                    "flushedCount": 0,
                    "remainingCount": 0,
                    "results": [],
                },
            )

        # Deduplicate entries by idempotencyKey before replaying
        seen_keys: set[str] = set()
        unique_entries: list[dict[str, Any]] = []
        for entry in entries:
            ikey = entry.get("idempotencyKey")
            if isinstance(ikey, str) and ikey:
                if ikey in seen_keys:
                    continue
                seen_keys.add(ikey)
            unique_entries.append(entry)

        flushed: list[dict[str, Any]] = []
        remaining: list[dict[str, Any]] = []

        for entry in unique_entries:
            method = str(entry.get("method") or "POST")
            path = str(entry.get("path") or "/api/v1/fleet/seam")
            payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
            ikey = entry.get("idempotencyKey")

            res = self.request(
                method,
                path,
                json_body=payload,
                idempotency_key=ikey,
                spool_on_offline=False,
            )
            if res.ok:
                flushed.append(
                    {
                        "idempotencyKey": ikey,
                        "path": path,
                        "status": res.status_code,
                        "deduplicated": bool(res.data.get("deduplicated", False)),
                    }
                )
            else:
                remaining.append(entry)

        if remaining:
            spool_file.parent.mkdir(parents=True, exist_ok=True)
            spool_file.write_text(
                "\n".join(json.dumps(item) for item in remaining) + "\n",
                encoding="utf-8",
            )
        elif spool_file.is_file():
            spool_file.unlink()

        ok = len(remaining) == 0
        return BridgeResponse(
            ok=ok,
            status_code=200 if ok else 207,
            exit_code=EXIT_OK if ok else EXIT_ERROR,
            data={
                "ok": ok,
                "flushedCount": len(flushed),
                "remainingCount": len(remaining),
                "results": flushed,
            },
            error=None if ok else "PARTIAL_SPOOL_FLUSH_FAILURE",
        )
