"""Library paths: traversal, symlinks, hidden names, file types, no overwrite, browsing, search."""

import os

import pytest

from adm import library
from adm.library import LibraryError


@pytest.mark.parametrize("bad", ["", "/etc/passwd", "../x.mkv", "a/../../x.mkv", ".hidden/x.mkv", "a/.x.mkv",
                                 "a/b\x01.mkv", "a/b:c.mkv", "a /x.mkv", "a./x.mkv", "x" * 300 + ".mkv"])
def test_clean_rel_refuses(bad):
    with pytest.raises(LibraryError):
        library.clean_rel(bad)


def test_clean_rel_accepts_normal_names():
    assert str(library.clean_rel("Üç Anadolu Efsanesi (1996)/Üç Anadolu Efsanesi (1996).pdf")).endswith(".pdf")
    assert str(library.clean_rel("./Dune (2021)//Dune (2021).mkv")) == "Dune (2021)/Dune (2021).mkv"


def test_file_types_per_kind():
    library.check_ext("movies", library.clean_rel("a/b.mkv"))
    library.check_ext("books", library.clean_rel("a/b.epub"))
    for kind, name in (("movies", "a/b.exe"), ("movies", "a/b.sh"), ("books", "a/b.mkv"), ("shows", "a/noext")):
        with pytest.raises(LibraryError):
            library.check_ext(kind, library.clean_rel(name))


def _src(tmp_path, data=b"video"):
    p = tmp_path / "staging.part"
    p.write_bytes(data)
    return p


def test_place_creates_folders_and_never_overwrites(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    rel, size = library.place(_src(tmp_path), root, "movies", "Dune (2021)/Dune (2021).mkv")
    assert rel == "Dune (2021)/Dune (2021).mkv" and size == 5
    assert (root / rel).read_bytes() == b"video" and not (tmp_path / "staging.part").exists()
    with pytest.raises(LibraryError, match="already exists"):
        library.place(_src(tmp_path, b"other"), root, "movies", rel)
    assert (root / rel).read_bytes() == b"video"
    assert not [p for p in (root / "Dune (2021)").iterdir() if p.name.startswith(".")]


def test_place_refuses_symlinks_out_of_the_library(tmp_path):
    root = tmp_path / "lib"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    os.symlink(outside, root / "link")
    with pytest.raises(LibraryError, match="symbolic link"):
        library.place(_src(tmp_path), root, "movies", "link/x.mkv")
    assert list(outside.iterdir()) == []
    (root / "file").write_text("x")
    with pytest.raises(LibraryError, match="not a folder"):
        library.place(_src(tmp_path), root, "movies", "file/x.mkv")


def test_root_must_be_inside_the_allowed_roots(tmp_path):
    (tmp_path / "libraries" / "m").mkdir(parents=True)
    assert library.root_of(str(tmp_path / "libraries" / "m"), [str(tmp_path / "libraries")])
    with pytest.raises(LibraryError, match="outside"):
        library.root_of(str(tmp_path), [str(tmp_path / "libraries")])
    with pytest.raises(LibraryError, match="outside"):
        library.root_of(str(tmp_path / "librariesX"), [str(tmp_path / "libraries")])


def test_tree_and_find(tmp_path):
    root = tmp_path / "lib"
    (root / "Breaking Bad" / "Season 1").mkdir(parents=True)
    (root / "Breaking Bad" / "Season 1" / "Breaking Bad - S01E01.mkv").write_bytes(b"1234")
    (root / ".secret").mkdir()
    t = library.tree(root, "", depth=3)
    names = [e["name"] for e in t["entries"]]
    assert names == ["Breaking Bad/"] and t["entries"][0]["entries"][0]["entries"][0]["size"] == 4
    assert library.find(root, "breaking s01e01") == ["Breaking Bad/Season 1/Breaking Bad - S01E01.mkv"]
    with pytest.raises(LibraryError):
        library.tree(root, "../")
    big = library.tree(root, "Breaking Bad", depth=1, limit=0)
    assert big["truncated"] is True


def test_folder_suggestions_stay_inside_the_roots(tmp_path):
    lib = tmp_path / "libraries"
    for d in ("media/movies", "media/tv", "Music", ".hidden", "books/library"):
        (lib / d).mkdir(parents=True)
    (lib / "media" / "notes.txt").write_text("x")
    (tmp_path / "secret").mkdir()
    os.symlink(tmp_path / "secret", lib / "sneaky")
    roots = [str(lib)]
    r = str(lib)
    assert library.suggest_folders("", roots) == roots
    assert library.suggest_folders("/etc", roots) == roots                  # outside: offer the roots
    assert library.suggest_folders(f"{r}/m", roots) == [f"{r}/media", f"{r}/Music"]   # case-insensitive
    assert library.suggest_folders(f"{r}/media/", roots) == [f"{r}/media/movies", f"{r}/media/tv"]
    assert library.suggest_folders(f"{r}/", roots) == [f"{r}/books", f"{r}/media", f"{r}/Music"]   # no hidden, no symlink
    assert library.suggest_folders(f"{r}/sneaky/", roots) == roots         # a symlink leading out lists nothing of it
    assert library.suggest_folders(f"{r}/media/../../", roots) == roots
    assert library.suggest_folders(str(tmp_path) + "/", roots) == roots      # the parent of the roots stays closed
