"""Delete only project-owned document directories, preserving shared uploads."""
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from uuid import UUID

from kb.errors import StoragePathError


@dataclass(frozen=True)
class DeletionPaths:
    batch: Path
    assets: Path
    previews: Path


def _check_entry(path):
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    if (stat.S_ISLNK(metadata.st_mode)
            or getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
        raise StoragePathError("删除路径包含符号链接或重解析目录，已停止操作")
    return metadata


def _safe_chain(root, target):
    if not target.is_relative_to(root) or target == root:
        raise StoragePathError("删除路径不在文档存储范围内")
    chain = list(reversed(target.parents)) + [target]
    for path in chain:
        metadata = _check_entry(path)
        if metadata is None:
            break
        if path != target and not stat.S_ISDIR(metadata.st_mode):
            raise StoragePathError("删除路径的父级不是目录")
    resolved_root, resolved_target = root.resolve(), target.resolve()
    if resolved_target == resolved_root or not resolved_target.is_relative_to(resolved_root):
        raise StoragePathError("删除路径解析后超出文档存储范围")


def _safe_tree(root, target):
    _safe_chain(root, target)
    metadata = _check_entry(target)
    if metadata is None:
        return
    if not stat.S_ISDIR(metadata.st_mode):
        raise StoragePathError("文档清理目标不是目录")
    pending = [target]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                metadata = _check_entry(Path(entry.path))
                if metadata is not None and stat.S_ISDIR(metadata.st_mode):
                    pending.append(Path(entry.path))


def deletion_paths(data_dir, document_id, source_path):
    root = Path(data_dir).absolute()
    identifier = str(UUID(str(document_id)))
    if not isinstance(source_path, str) or not source_path:
        raise StoragePathError("文档上传路径无效")
    normalized = source_path.replace("\\", "/")
    parts = normalized.split("/")
    windows = PureWindowsPath(normalized)
    if (windows.drive or windows.anchor or PurePosixPath(normalized).is_absolute()
            or len(parts) < 3 or parts[0] != "uploads"
            or any(part in {"", ".", ".."} or ":" in part for part in parts)):
        raise StoragePathError("文档上传路径不在受控批次目录内")
    try:
        if str(UUID(parts[1])) != parts[1]:
            raise ValueError()
    except ValueError:
        raise StoragePathError("文档上传批次编号无效") from None
    paths = DeletionPaths(root / "uploads" / parts[1], root / "assets" / identifier,
                          root / "previews" / identifier)
    _safe_chain(root, root.joinpath(*parts))
    _safe_chain(root, paths.batch)
    _safe_tree(root, paths.assets)
    _safe_tree(root, paths.previews)
    return paths


def clean_deletion_files(data_dir, document_id, source_path, shared_uploads):
    root = Path(data_dir).absolute()
    paths = deletion_paths(root, document_id, source_path)
    for directory in (paths.assets, paths.previews):
        _safe_tree(root, directory)
        if directory.exists():
            shutil.rmtree(directory)
    if shared_uploads:
        return True
    # An upload registers its documents one at a time. Never remove its batch
    # while registration may still be in progress; the durable job can retry.
    if (paths.batch / ".uploading").exists():
        return False
    _safe_tree(root, paths.batch)
    if paths.batch.exists():
        shutil.rmtree(paths.batch)
    return True
