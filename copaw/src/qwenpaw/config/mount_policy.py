"""Shared validation for explicit request RX/RW mount relationships."""

import sys
from pathlib import Path


def normalize_rx_mount_overlaps(
    rw_mounts: list[str | Path],
    rx_mounts: list[str | Path],
    *,
    is_coding_mode: bool | None = None,
) -> list[str]:
    """Let explicit RW parents absorb RX children on Windows outside Coding.

    Equal RX roots are dropped in every mode. Windows also permits RW
    children of RX outside Coding. Chat ingress does not yet know the
    effective mode (None), so nested RX roots are retained until the builder
    repeats validation with the actual mode. Coding and other platforms
    continue to reject unequal RX/RW overlaps.
    """
    writable = [Path(p).expanduser().resolve() for p in rw_mounts]
    windows_overlap_allowed = (
        sys.platform == "win32" and is_coding_mode is not True
    )
    readonly: list[Path] = []
    for raw in rx_mounts:
        rx = Path(raw).expanduser().resolve()
        if rx in writable or rx in readonly:
            continue
        if (
            windows_overlap_allowed
            and is_coding_mode is False
            and any(rx.is_relative_to(rw) for rw in writable)
        ):
            continue
        for rw in writable:
            if not windows_overlap_allowed and (
                rx.is_relative_to(rw) or rw.is_relative_to(rx)
            ):
                raise ValueError(
                    "request_context.sandbox_rx_mounts conflicts with "
                    f"a writable mount: {rx} / {rw}"
                )
        readonly.append(rx)
    return [str(p) for p in readonly]
