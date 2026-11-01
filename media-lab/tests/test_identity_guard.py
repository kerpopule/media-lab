"""Tests for tools/identity_guard.py.

The guard runs on `git ls-files`, so the file-level cases build a throwaway
repository in `tmp_path` and invoke the guard there as a subprocess
(`repo_root()` resolves from cwd).

This file never names a real private word, not even assembled from pieces: the
hashed-word cases load the guard in-process and swap its tables for made-up
words, so the mechanism is tested without publishing what it keeps out. Values
that look private to the guard's patterns (a MagicDNS name, an e-mail address)
are made up too, and assembled at runtime because the guard scans this file.
"""
from __future__ import annotations

import importlib.util
import pathlib
import re
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
GUARD = REPO_ROOT / "media-lab" / "tools" / "identity_guard.py"

# Made up, and built by concatenation: the guard scans this file too.
PRIVATE_ABS_PATH = b"/Users/" + b"someone/proj/mod.py"
PRIVATE_HOSTNAME = "box-1a2b.tail" + "0c1d2e.ts.net"


def _git(repo: pathlib.Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )


def _init_repo(repo: pathlib.Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _commit(repo, "init")


def _commit(repo: pathlib.Path, message: str) -> None:
    _git(
        repo,
        "-c",
        "user.email=guard-test@example.com",
        "-c",
        "user.name=Guard Test",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        message,
    )


def _run_guard(repo: pathlib.Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GUARD)], cwd=repo, capture_output=True, text=True
    )


def test_force_added_pyc_in_pycache_is_rejected(tmp_path: pathlib.Path) -> None:
    _init_repo(tmp_path)
    cache = tmp_path / "pkg" / "__pycache__"
    cache.mkdir(parents=True)
    # A CPython header embeds the absolute source path it was compiled from.
    (cache / "mod.cpython-312.pyc").write_bytes(b"\x55\x0d\x0d\x0a\x00\x00\x00\x00" + PRIVATE_ABS_PATH)
    _git(tmp_path, "add", "-f", "pkg/__pycache__/mod.cpython-312.pyc")

    result = _run_guard(tmp_path)

    assert result.returncode == 1
    assert "pkg/__pycache__/mod.cpython-312.pyc" in result.stderr


def test_force_added_bytecode_without_private_string_is_rejected(tmp_path: pathlib.Path) -> None:
    _init_repo(tmp_path)
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "mod.pyo").write_bytes(b"no private data at all\n")
    _git(tmp_path, "add", "-f", "pkg/mod.pyo")

    result = _run_guard(tmp_path)

    assert result.returncode == 1
    assert "pkg/mod.pyo" in result.stderr
    assert "bytecode" in result.stderr


def test_clean_tree_passes(tmp_path: pathlib.Path) -> None:
    _init_repo(tmp_path)
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "mod.py").write_text("print('hello')\n")
    _git(tmp_path, "add", "pkg/mod.py")
    _commit(tmp_path, "add module")

    result = _run_guard(tmp_path)

    assert result.returncode == 0
    assert "identity_guard: 1 files clean" in result.stdout


def test_private_pattern_still_reported(tmp_path: pathlib.Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "notes.md").write_text(f"host: {PRIVATE_HOSTNAME}\n")
    _git(tmp_path, "add", "notes.md")

    result = _run_guard(tmp_path)

    assert result.returncode == 1
    assert "notes.md:1:" in result.stderr
    assert PRIVATE_HOSTNAME in result.stderr


def test_other_binary_extensions_still_skipped(tmp_path: pathlib.Path) -> None:
    _init_repo(tmp_path)
    # Text content with no NUL byte, so only BINARY_EXT can skip it.
    (tmp_path / "logo.png").write_text(f"host: {PRIVATE_HOSTNAME}\n")
    _git(tmp_path, "add", "logo.png")

    result = _run_guard(tmp_path)

    assert result.returncode == 0
    assert "identity_guard: 1 files clean" in result.stdout


def test_untracked_bytecode_is_rejected(tmp_path: pathlib.Path) -> None:
    _init_repo(tmp_path)
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "mod.pyc").write_bytes(b"\x55\x0d\x0d\x0a\x00\x00\x00\x00")

    result = _run_guard(tmp_path)

    assert result.returncode == 1
    assert "pkg/mod.pyc" in result.stderr


def test_bytecode_hit_stderr_omits_local_env_remedy(tmp_path: pathlib.Path) -> None:
    """A committed .pyc is a file-type violation: the local.env remedy is wrong.

    "Move the value into config/local.env" is the fix for a leaked secret, not
    for a committed artifact, so a developer following it would mishandle the
    leak. The bytecode branch must print its own removal remedy instead.
    """
    _init_repo(tmp_path)
    cache = tmp_path / "pkg" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "mod.cpython-312.pyc").write_bytes(b"\x55\x0d\x0d\x0a\x00\x00\x00\x00")
    _git(tmp_path, "add", "-f", "pkg/__pycache__/mod.cpython-312.pyc")

    result = _run_guard(tmp_path)

    assert result.returncode == 1
    assert "pkg/__pycache__/mod.cpython-312.pyc" in result.stderr
    assert "config/local.env" not in result.stderr
    assert "Remove the bytecode artifact from the repository" in result.stderr


def test_private_pattern_hit_stderr_keeps_local_env_remedy(tmp_path: pathlib.Path) -> None:
    """The real secret remedy survives the split epilogue."""
    _init_repo(tmp_path)
    (tmp_path / "notes.md").write_text(f"host: {PRIVATE_HOSTNAME}\n")
    _git(tmp_path, "add", "notes.md")

    result = _run_guard(tmp_path)

    assert result.returncode == 1
    assert "config/local.env" in result.stderr
    assert "Remove the bytecode artifact from the repository" not in result.stderr


def test_clean_tree_reports_clean_and_no_remedy(tmp_path: pathlib.Path) -> None:
    """Negative control: a clean tree stays exit 0 with no remedy text at all."""
    _init_repo(tmp_path)
    (tmp_path / "mod.py").write_text("print('hello')\n")
    _git(tmp_path, "add", "mod.py")
    _commit(tmp_path, "add module")

    result = _run_guard(tmp_path)

    assert result.returncode == 0
    assert "identity_guard: 1 files clean" in result.stdout
    assert "config/local.env" not in result.stderr
    assert "Remove the bytecode artifact from the repository" not in result.stderr

# ------------------------------------------------ names, brands, e-mail, CSS
# Made-up words stand in for the real ones: the real list is stored only as
# salted hashes, and this file must not spell it out either.
FAKE_PERSON = "Zorbeth"
FAKE_BRAND = "quuxcorp"
FAKE_CLIENT = "zq widgets"          # a two-word phrase
MAIL_DOMAIN = "mail" + "provider.com"


def _load_guard():
    spec = importlib.util.spec_from_file_location("identity_guard_under_test", GUARD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _guard_with(monkeypatch, words: dict[str, str], allow_next: dict[str, tuple] | None = None):
    """The guard, its hash tables replaced by these made-up words -> kinds."""
    g = _load_guard()
    table = {g.phrase_hash(w): kind for w, kind in words.items()}
    firsts = {g.phrase_hash(w.split()[0]) for w in words if len(w.split()) > 1}
    monkeypatch.setattr(g, "PRIVATE_HASHES", table)
    monkeypatch.setattr(g, "PHRASE_FIRST_WORDS", firsts)
    monkeypatch.setattr(g, "ALLOW_NEXT_WORD",
                        {g.phrase_hash(w): nxt for w, nxt in (allow_next or {}).items()})
    return g


def _scan_text(tmp_path: pathlib.Path, name: str, text: str) -> subprocess.CompletedProcess[str]:
    _init_repo(tmp_path)
    (tmp_path / name).write_text(text)
    _git(tmp_path, "add", name)
    return _run_guard(tmp_path)


def test_personal_name_is_rejected(monkeypatch, tmp_path: pathlib.Path) -> None:
    g = _guard_with(monkeypatch, {FAKE_PERSON.lower(): "a maintainer's personal name"})
    hit = g.line_hit(f"# {FAKE_PERSON}'s rule for the studio")
    assert hit and FAKE_PERSON in hit and "personal name" in hit
    notes = tmp_path / "notes.md"
    notes.write_text(f"ok line\nasked by {FAKE_PERSON.upper()}\n")
    hits = g.scan([notes], tmp_path)
    assert len(hits) == 1 and hits[0].startswith("notes.md:2:")
    assert g.line_hit("an ordinary line about the studio") is None


def test_lower_case_colour_exception_keeps_the_capitalised_name(monkeypatch) -> None:
    g = _guard_with(monkeypatch, {FAKE_PERSON.lower(): "a maintainer's personal name"},
                    allow_next={FAKE_PERSON.lower(): ("gray", "grey")})
    assert g.line_hit(f"a fitted {FAKE_PERSON.lower()}-gray tee") is None
    assert g.line_hit(f"the {FAKE_PERSON} grey-canvas lesson")
    assert g.line_hit(f"{FAKE_PERSON.lower()} said so")


def test_private_brand_in_a_hostname_is_rejected(monkeypatch) -> None:
    g = _guard_with(monkeypatch, {FAKE_BRAND: "a private brand"})
    hit = g.line_hit(f"URL=https://media.{FAKE_BRAND}.ai/x")
    assert hit and "private brand" in hit
    # a word that merely contains it is a different word
    assert g.line_hit(f"URL=https://media.{FAKE_BRAND}ish.ai/x") is None


def test_multi_word_client_phrase_is_rejected(monkeypatch) -> None:
    g = _guard_with(monkeypatch, {FAKE_CLIENT: "a client brand"})
    hit = g.line_hit("/* colours from ZQ  Widgets, verbatim */")
    assert hit and "client brand" in hit
    assert g.line_hit("zq-widgets.css") and g.line_hit("zq_widgets")
    assert g.line_hit("zq alone is fine; widgets too") is None


def test_copied_client_css_is_rejected(tmp_path: pathlib.Path) -> None:
    rim = ("rgba(255,255,255," + ".50) 0%,\n  rgba(255,255,255," + ".16) 22%")
    result = _scan_text(tmp_path, "b.css", f".x{{background:linear-gradient(180deg,\n  {rim})}}\n")
    assert result.returncode == 1 and "copied CSS" in result.stderr


def test_personal_email_is_rejected_and_reserved_domains_pass(tmp_path: pathlib.Path) -> None:
    result = _scan_text(tmp_path, "push.py", f'SUB = "mailto:someone@{MAIL_DOMAIN}"\n')
    assert result.returncode == 1 and "e-mail address" in result.stderr
    ok = ('a = "mailto:ops@example.com"\n'
          'b = "icon@2x.png"\n'
          'c = "user@1000.service"\n'
          'd = "https://token@queue.example.org/job"\n'
          'e = "Co-Authored-By: bot <noreply@anthropic.com>"\n')
    result = _scan_text(tmp_path / "b", "ok.py", ok)
    assert result.returncode == 0, result.stderr


def test_guard_source_never_spells_the_words_it_blocks() -> None:
    """Every hashed word or phrase stays out of the guard's own source."""
    g = _load_guard()
    for n, line in enumerate(GUARD.read_text(encoding="utf-8").splitlines(), 1):
        assert g.private_phrase(line) is None, f"identity_guard.py:{n}"


def test_this_test_file_never_spells_a_blocked_word() -> None:
    """Not even assembled from pieces: join the string literals on each line."""
    g = _load_guard()
    literal = re.compile(r'''b?(["'])((?:\\.|(?!\1).)*?)\1''')
    for n, line in enumerate(pathlib.Path(__file__).read_text(encoding="utf-8").splitlines(), 1):
        joined = "".join(m.group(2) for m in literal.finditer(line))
        assert g.private_phrase(joined) is None, f"line {n}"
        assert g.private_phrase(line) is None, f"line {n}"


def test_hash_table_is_well_formed_and_covers_each_kind() -> None:
    g = _load_guard()
    assert all(re.fullmatch(r"[0-9a-f]{16}", h) for h in g.PRIVATE_HASHES)
    assert all(re.fullmatch(r"[0-9a-f]{16}", h) for h in g.PHRASE_FIRST_WORDS)
    kinds = set(g.PRIVATE_HASHES.values())
    for kind in ("a maintainer's personal name", "a private brand", "a client brand",
                 "a private production", "a private machine identity"):
        assert kind in kinds, kind


def test_hash_helper_prints_an_entry_to_paste() -> None:
    out = subprocess.run([sys.executable, str(GUARD), "--hash", "Some Phrase"],
                         capture_output=True, text=True, check=True).stdout
    assert out.strip().startswith('"') and '": "<what it is>",' in out
