"""Protect message-bearing files without changing the process-wide umask.

Parents of these paths must be trusted against concurrent replacement by other users.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path


def _open(path: Path, flags: int, mode: int = 0o600) -> int:
    if not hasattr(os, 'O_NOFOLLOW'):
        raise OSError('private state requires O_NOFOLLOW')
    return os.open(path, flags | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0), mode)


def file(path: Path, *, create: bool = False) -> int:
    fd = _open(path, os.O_RDWR | (os.O_CREAT if create else 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError(f'unsafe private file: {path}')
        os.fchmod(fd, stat.S_IMODE(info.st_mode) & 0o600)
        return fd
    except BaseException:
        os.close(fd)
        raise


def directory(path: Path, *, create: bool = False) -> None:
    if create:
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
    fd = _open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            raise OSError(f'unsafe private directory: {path}')
        os.fchmod(fd, stat.S_IMODE(info.st_mode) & 0o700)
    finally:
        os.close(fd)


def existing_file(path: Path) -> None:
    try:
        fd = file(path)
    except FileNotFoundError:
        return
    os.close(fd)


def store_files(path: Path, *, create: bool) -> None:
    if create:
        os.close(file(path, create=True))
    else:
        existing_file(path)
    for suffix in ('-wal', '-shm', '-journal'):
        existing_file(Path(str(path) + suffix))
