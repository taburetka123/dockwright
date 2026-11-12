import hashlib
import os
import stat
import time
import uuid
from pathlib import Path

from . import paths

STORE_FILE_MAX = 1024 * 1024
EXPORT_MAX_AGE_SEC = 24 * 3600


def read_content_file(content_path: str) -> str:
    if not os.path.isabs(content_path):
        raise ValueError(f"content_path must be an absolute path, got {content_path!r}")
    real = os.path.realpath(content_path)
    store = os.path.realpath(paths.ARTIFACTS)
    if real == store or real.startswith(store + os.sep):
        raise ValueError(f"content_path {content_path} is inside the store: pass a file outside {paths.ARTIFACTS}")
    path = Path(real)
    if not path.exists():
        raise ValueError(f"content_path {content_path} does not exist")
    if not path.is_file():
        raise ValueError(f"content_path {content_path} is not a regular file")
    size = path.stat().st_size
    if size > STORE_FILE_MAX:
        raise ValueError(f"content_path {content_path} is {size} bytes, over the {STORE_FILE_MAX}-byte limit")
    try:
        return path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"content_path {content_path} is not UTF-8 text") from None


def prune(now: float | None = None) -> None:
    if not paths.EXPORTS.is_dir():
        return
    cutoff = (time.time() if now is None else now) - EXPORT_MAX_AGE_SEC
    for path in paths.EXPORTS.rglob("*"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
        except FileNotFoundError:
            continue


def make_dir(path: Path) -> Path:
    paths.EXPORTS.mkdir(mode=0o700, parents=True, exist_ok=True)
    root = os.lstat(paths.EXPORTS)
    if stat.S_ISLNK(root.st_mode) or root.st_uid != os.getuid():
        raise ValueError(f"{paths.EXPORTS} is not a folder this user owns: remove it, or set TMPDIR to a folder "
                         "only this user can write")
    if root.st_mode & 0o077:
        os.chmod(paths.EXPORTS, 0o700)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _reuse(target: Path, data: bytes) -> bool:
    try:
        if target.read_bytes() != data:
            return False
        os.utime(target)
        return True
    except FileNotFoundError:
        return False


def _export(target: Path, data: bytes, prune_after: bool = True) -> Path:
    if not _reuse(target, data):
        make_dir(target.parent)
        tmp = target.parent / f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        tmp.write_bytes(data)
        os.replace(tmp, target)
    if prune_after:
        prune()
    return target


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def export_body(task_key: str, phase: str, name: str, body: str, prune_after: bool = True) -> Path:
    data = body.encode("utf-8")
    return _export(paths.artifact_export_path(task_key, phase, name, _digest(data)), data, prune_after)


def export_view(task_key: str, text: str) -> Path:
    data = text.encode("utf-8")
    return _export(paths.view_export_path(task_key, _digest(data)), data)
