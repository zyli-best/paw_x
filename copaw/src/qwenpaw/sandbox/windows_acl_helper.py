# -*- coding: utf-8 -*-
"""Elevated helper for granting AppContainer ACLs to MSVC runtime roots."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


_SID_RE = re.compile(r"^S-1-15-2(?:-\d+)+$")
_ALLOWED_PERMISSIONS = {"R", "RX"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    args = parser.parse_args(argv)

    manifest_path = Path(args.manifest).expanduser().resolve()
    try:
        manifest = _load_manifest(manifest_path)
        result_path = Path(manifest["result_path"]).expanduser().resolve()
        sid = str(manifest["sid"])
        permission = str(manifest.get("permission") or "RX").upper()
        paths = [Path(os.fspath(p)).expanduser().resolve() for p in manifest["paths"]]
        _validate_manifest(sid, permission, paths)

        results = []
        ok_all = True
        for path in paths:
            ok, output = _grant_acl(str(path), sid, permission)
            ok_all = ok_all and ok
            results.append({"path": str(path), "ok": ok, "output": output})

        _write_result(
            result_path,
            {
                "ok": ok_all,
                "results": results,
            },
        )
        if ok_all:
            _clear_pending_acl_metadata(
                str(manifest.get("container_name") or ""),
                [str(path) for path in paths],
            )
        return 0 if ok_all else 1
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        result_path = _safe_result_path_from_manifest(manifest_path)
        if result_path is not None:
            _write_result(result_path, {"ok": False, "error": str(exc)})
        print(str(exc), file=sys.stderr)
        return 1


def _load_manifest(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("Manifest must be a JSON object.")
    return data


def _validate_manifest(
    sid: str,
    permission: str,
    paths: list[Path],
) -> None:
    if not _SID_RE.match(sid):
        raise ValueError("Invalid AppContainer SID.")
    if permission not in _ALLOWED_PERMISSIONS:
        raise ValueError("Permission must be R or RX.")
    if not paths:
        raise ValueError("No paths were provided.")
    for path in paths:
        if not path.is_dir():
            raise ValueError(f"ACL target is not a directory: {path}")
        if not _is_allowed_admin_acl_path(path):
            raise ValueError(f"ACL target is not an allowed MSVC/SDK path: {path}")


def _is_allowed_admin_acl_path(path: Path) -> bool:
    normalized = str(path).lower()
    return (
        "\\microsoft visual studio\\" in normalized
        or "\\windows kits\\" in normalized
        or "\\vc\\tools\\msvc\\" in normalized
        or "\\vc\\auxiliary\\build" in normalized
        or "\\msbuild\\current\\bin" in normalized
    )


def _grant_acl(path: str, sid: str, permission: str) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["icacls", path, "/grant", f"*{sid}:(OI)(CI)({permission})"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    return result.returncode == 0, output.strip()


def _safe_result_path_from_manifest(manifest_path: Path) -> Path | None:
    try:
        data = _load_manifest(manifest_path)
        raw = data.get("result_path")
        if raw:
            return Path(os.fspath(raw)).expanduser().resolve()
    except Exception:
        return None
    return None


def _write_result(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def _clear_pending_acl_metadata(
    container_name: str,
    granted_paths: list[str],
) -> None:
    if not container_name:
        return
    state_dir = Path(os.environ.get("USERPROFILE", os.path.expanduser("~"))) / ".qwenpaw"
    meta_file = state_dir / "containers" / f"{container_name}.json"
    if not meta_file.is_file():
        return
    try:
        data = json.loads(meta_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    manifest = data.get("acl_manifest")
    if isinstance(manifest, dict):
        manifest.pop("failed_admin_acl_roots", None)
        grant_paths = manifest.setdefault("grant_paths", [])
        if isinstance(grant_paths, list):
            for path in granted_paths:
                if path not in grant_paths:
                    grant_paths.append(path)
    data.pop("acl_elevation_pending", None)
    tmp = meta_file.with_suffix(meta_file.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(meta_file)


if __name__ == "__main__":
    raise SystemExit(main())
