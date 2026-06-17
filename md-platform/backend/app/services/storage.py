"""Storage path helpers + disk usage + zip streaming (CONTRACT §8).

Layout under STORAGE_ROOT:

    uploads/{upload_id}/{pose_file, chemistry_file?, receptor_file?, meta.json}
    jobs/{job_id}/
      metadata.json
      input/{original/, processed/}
      pose_01/{prep/, md/, analysis/, visualization/, logs/, results.zip}
      ...
      summary/{pose_comparison.csv, summary_report.html, summary_report.pdf, all_results.zip}
    results/
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Iterator

from ..config import get_settings


def storage_root() -> Path:
    return get_settings().storage_root


def uploads_dir() -> Path:
    return storage_root() / "uploads"


def jobs_dir() -> Path:
    return storage_root() / "jobs"


def results_dir() -> Path:
    return storage_root() / "results"


def upload_dir(upload_id: str) -> Path:
    return uploads_dir() / upload_id


def job_dir(job_id: str) -> Path:
    return jobs_dir() / job_id


def pose_dirname(pose_index: int, replica_index: int = 1) -> str:
    """Pose artifact dir name. MD replicas of a pose get a `_rep_RR` suffix; replica 1 keeps the
    plain `pose_NN` name so single-replica jobs are byte-identical to before. MUST stay in sync
    with the worker's JobContext.pose_name."""
    if replica_index and int(replica_index) > 1:
        return f"pose_{pose_index:02d}_rep_{int(replica_index):02d}"
    return f"pose_{pose_index:02d}"


def pose_dir(job_id: str, pose_index: int, replica_index: int = 1) -> Path:
    return job_dir(job_id) / pose_dirname(pose_index, replica_index)


def summary_dir(job_id: str) -> Path:
    return job_dir(job_id) / "summary"


def ensure_dirs(*paths: Path) -> None:
    for p in paths:
        p.mkdir(parents=True, exist_ok=True)


def ensure_base_tree() -> None:
    ensure_dirs(uploads_dir(), jobs_dir(), results_dir())


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def remove_job_storage(job_id: str) -> None:
    """Recursively delete a job's artifact tree. No-op if absent."""
    d = job_dir(job_id)
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)


def remove_upload_storage(upload_id: str) -> None:
    d = upload_dir(upload_id)
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)


def disk_usage_gb() -> tuple[float, float]:
    """Return (used_gb, total_gb) for the filesystem holding STORAGE_ROOT."""
    root = storage_root()
    target = root
    # Walk up to the first existing ancestor so statvfs doesn't fail pre-mkdir.
    while not target.exists() and target != target.parent:
        target = target.parent
    try:
        usage = shutil.disk_usage(str(target))
    except OSError:
        return (0.0, 0.0)
    gb = 1024 ** 3
    return (round(usage.used / gb, 2), round(usage.total / gb, 2))


def _iter_zip_directory(base: Path, arc_prefix: str) -> Iterator[tuple[Path, str]]:
    """Yield (file_path, arcname) for every file under base."""
    for p in sorted(base.rglob("*")):
        if p.is_file():
            rel = p.relative_to(base)
            yield p, f"{arc_prefix}/{rel.as_posix()}"


def stream_zip_of_directory(base: Path, arc_prefix: str, chunk_size: int = 256 * 1024) -> Iterator[bytes]:
    """Stream a valid zip of ``base`` directory's contents as bytes chunks.

    The archive is fully built to a temporary file first (so ZipFile can write a
    correct central directory using stable offsets), then streamed back in chunks
    and deleted. Memory stays bounded to ``chunk_size`` per yield regardless of
    tree size, and the produced zip is never corrupted.
    """
    fd, tmp_name = tempfile.mkstemp(suffix=".zip", prefix="mdresults_")
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        with zipfile.ZipFile(tmp_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            if not base.exists():
                # Emit a tiny note so the download is never an empty/invalid zip.
                zf.writestr(f"{arc_prefix}/EMPTY.txt", "No artifacts are available yet.\n")
            else:
                wrote_any = False
                for file_path, arcname in _iter_zip_directory(base, arc_prefix):
                    zf.write(file_path, arcname)
                    wrote_any = True
                if not wrote_any:
                    zf.writestr(f"{arc_prefix}/EMPTY.txt", "No artifacts are available yet.\n")
        with tmp_path.open("rb") as fh:
            while True:
                data = fh.read(chunk_size)
                if not data:
                    break
                yield data
    finally:
        tmp_path.unlink(missing_ok=True)


def existing_zip_bytes(path: Path) -> bytes | None:
    """Return bytes of a pre-built zip if present, else None."""
    if path.exists() and path.is_file():
        return path.read_bytes()
    return None
