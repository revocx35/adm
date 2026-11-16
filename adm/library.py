"""Media libraries on disk: browse their structure (for the agent) and place finished downloads into them.

Every path the agent names is relative to the library root and checked here: no absolute paths, no `..`, no
hidden or control-character components, no symlinks on the way, and the final file must not exist yet. Only
media file types of the library's kind are accepted (an agent can't drop a script or an .exe into a library
other software reads).
"""

from __future__ import annotations

import os
import shutil
import stat
import unicodedata
from pathlib import Path, PurePosixPath

VIDEO = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".webm", ".ts", ".m2ts", ".wmv", ".mpg", ".mpeg", ".ogv"}
SUBS = {".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt", ".sup"}
ART = {".jpg", ".jpeg", ".png", ".webp", ".nfo"}
BOOKS = {".epub", ".pdf", ".mobi", ".azw", ".azw3", ".kfx", ".fb2", ".djvu", ".cbz", ".cbr", ".cb7", ".m4b",
         ".mp3", ".m4a", ".opus", ".txt", ".rtf", ".docx", ".jpg", ".jpeg", ".png", ".opf"}
ALLOWED = {"movies": VIDEO | SUBS | ART, "shows": VIDEO | SUBS | ART, "books": BOOKS}
MAX_COMPONENT = 200
MAX_PATH = 900


class LibraryError(Exception):
    pass


def clean_rel(rel: str) -> PurePosixPath:
    """A safe relative path inside a library, or LibraryError."""
    rel = unicodedata.normalize("NFC", (rel or "").strip()).replace("\\", "/")
    if not rel or rel.startswith("/") or len(rel.encode()) > MAX_PATH:
        raise LibraryError("give a path relative to the library root, e.g. 'Movie Title (2024)/Movie Title (2024).mkv'")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts:
        raise LibraryError("empty path")
    for p in parts:
        if p == ".." or p.startswith(".") or any(ord(ch) < 32 or ch in '\x7f<>:"|?*' for ch in p):
            raise LibraryError(f"invalid path component {p!r} (no '..', hidden names or < > : \" | ? *)")
        if len(p.encode()) > MAX_COMPONENT or p != p.strip() or p.endswith("."):
            raise LibraryError(f"invalid path component {p!r} (too long, or spaces/dot at the end)")
    return PurePosixPath(*parts)


def check_ext(kind: str, rel: PurePosixPath) -> None:
    ext = rel.suffix.lower()
    if ext not in ALLOWED[kind]:
        raise LibraryError(f"'{ext or rel.name}' files can't go into a {kind} library "
                           f"(allowed: {', '.join(sorted(ALLOWED[kind]))})")


def _inside(root: Path, rel: PurePosixPath, create: bool = False) -> Path:
    """root/rel with every existing component checked: directories only, never a symlink."""
    cur = root
    for i, part in enumerate(rel.parts):
        cur = cur / part
        last = i == len(rel.parts) - 1
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            if create and not last:
                os.mkdir(cur, 0o775)
                continue
            if last:
                return cur
            raise LibraryError(f"{'/'.join(rel.parts[:i + 1])} does not exist") from None
        if stat.S_ISLNK(st.st_mode):
            raise LibraryError(f"{'/'.join(rel.parts[:i + 1])} is a symbolic link")
        if not last and not stat.S_ISDIR(st.st_mode):
            raise LibraryError(f"{'/'.join(rel.parts[:i + 1])} is a file, not a folder")
    return cur


def root_of(path: str, allowed_roots: list[str]) -> Path:
    p = Path(path)
    real = os.path.realpath(p)
    if not any(real == os.path.realpath(r) or real.startswith(os.path.realpath(r).rstrip("/") + "/")
               for r in allowed_roots):
        raise LibraryError(f"{path} is outside the allowed library roots ({', '.join(allowed_roots)})")
    if not p.is_dir():
        raise LibraryError(f"{path} is not a folder (is the share mounted?)")
    return p


def tree(root: Path, rel: str = "", depth: int = 1, limit: int = 400) -> dict:
    """The folder structure below root/rel: folders (with their entries down to `depth`) and files with sizes."""
    base = _inside(root, clean_rel(rel)) if rel.strip(" /") else root
    if not base.is_dir():
        raise LibraryError(f"{rel} is not a folder")
    count = 0
    truncated = False

    def walk(d: Path, level: int) -> list[dict]:
        nonlocal count, truncated
        out: list[dict] = []
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name.lower())
        except OSError as e:
            return [{"error": e.__class__.__name__}]
        for e in entries:
            if e.name.startswith("."):
                continue
            if count >= limit:
                truncated = True
                break
            count += 1
            if e.is_symlink():
                continue
            if e.is_dir():
                item: dict = {"name": e.name + "/"}
                if level < depth:
                    item["entries"] = walk(Path(e.path), level + 1)
                out.append(item)
            elif e.is_file():
                out.append({"name": e.name, "size": e.stat().st_size})
        return out

    entries = walk(base, 1)
    return {"path": rel.strip("/") or ".", "entries": entries, "truncated": truncated,
            **({"note": f"only the first {limit} entries; narrow it down with a path"} if truncated else {})}


def find(root: Path, query: str, limit: int = 50, max_depth: int = 3) -> list[str]:
    """Folders and files whose name contains every word of `query` (case-insensitive), up to 3 levels deep."""
    words = [w for w in unicodedata.normalize("NFC", query).lower().split() if w]
    if not words:
        return []
    hits: list[str] = []
    seen = 0
    stack = [(root, 0)]
    while stack and len(hits) < limit and seen < 50_000:
        d, level = stack.pop()
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            seen += 1
            if e.name.startswith(".") or e.is_symlink():
                continue
            name = e.name.lower()
            rel = os.path.relpath(e.path, root)
            if all(w in name for w in words):
                hits.append(rel + ("/" if e.is_dir() else ""))
            if e.is_dir() and level + 1 < max_depth:
                stack.append((Path(e.path), level + 1))
    return sorted(hits)[:limit]


def place(src: Path, root: Path, kind: str, rel: str) -> tuple[str, int]:
    """Move the finished upload `src` to root/rel (folders created). Never overwrites. Returns (rel path, size)."""
    r = clean_rel(rel)
    check_ext(kind, r)
    dest = _inside(root, r, create=True)
    if os.path.lexists(dest):
        raise LibraryError(f"{r} already exists in the library; pick another name or skip it")
    size = src.stat().st_size
    tmp = dest.with_name(f".adm-{os.getpid()}-{dest.name}.part")
    try:
        try:
            os.link(src, tmp)                     # same file system: no copy
        except OSError:
            shutil.copyfile(src, tmp)              # e.g. staging on local disk, library on NFS
        os.chmod(tmp, 0o664)
        # link() refuses an existing target (no overwrite race); fall back to rename on file systems without links
        try:
            os.link(tmp, dest)
            os.unlink(tmp)
        except FileExistsError:
            raise LibraryError(f"{r} already exists in the library") from None
        except OSError:
            if os.path.lexists(dest):
                raise LibraryError(f"{r} already exists in the library") from None
            os.replace(tmp, dest)
    finally:
        if os.path.lexists(tmp):
            os.unlink(tmp)
    src.unlink(missing_ok=True)
    return str(r), size
