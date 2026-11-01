#!/usr/bin/env python3
"""Repo guard: nothing private may be committed to this public repository.

Media Lab used to be published through a rewrite step (a sanitizer that swapped
one team's usernames, tailnet addresses and hostnames for placeholders on the
way out). That step is gone: everything per-host now comes from
config/local.env (see config/local.env.example and media_lab_core/local_config.py),
and everything personal to one studio (extra themes, templates, a character
catalog, private assistant notes) lives in the gitignored overlay folder
config/local/ (docs/LOCAL-OVERLAY.md). The tracked tree must simply never
contain such material, and this script is the CI gate that keeps it that way.

What it refuses, on every scanned line:

* private identifiers -- the maintainers' own names, their businesses' and
  clients' brand names, their machines' names, tailnet addresses and home
  directories.
  These are stored below only as salted SHA-256 prefixes (PRIVATE_HASHES), so
  the guard itself never publishes the words it exists to keep out. Matching is
  by word: a line is split into runs of letters/digits joined by whitespace or
  . _ / @ - and every run of up to MAX_WORDS words is hashed and looked up.
* any real MagicDNS name (*.tail<hex>.ts.net). Test fixtures use documentation
  addresses in 100.64.0.0/16 and *.example.ts.net names instead;
* any e-mail address outside the reserved example/test domains (a real contact
  belongs in config/local.env, e.g. MEDIA_LAB_VAPID_SUBJECT);
* a client design system's copied CSS (CLIENT_CSS_SIGNATURES);
* Python bytecode, outright: a CPython .pyc header embeds the absolute source
  path, so a committed .pyc leaks the builder's machine identity without ever
  being read.

Usage:
    python3 media-lab/tools/identity_guard.py            # scan tracked files
    python3 media-lab/tools/identity_guard.py PATH...     # scan given paths
    python3 media-lab/tools/identity_guard.py --hash 'some phrase'
                                                          # print the entry to add

Scope: tracked files plus untracked files git would not ignore
(`git ls-files --cached --others --exclude-standard`). Scanning untracked
files is deliberate pre-add safety -- a brand-new file is checked before it is
ever staged -- so do not drop `--others` to make local runs green. Local run
scratch (`.artifacts/`, `.worktrees/`) and the per-studio overlay
(`media-lab/config/local/`) are instead excluded by `.gitignore`; anything that
must not be committed gets an ignore rule there, never an exemption in this
script.

Exit 0 when clean, 1 with a listing of file:line hits otherwise. To add a new
private word or phrase, run `--hash 'the phrase'` and paste the printed line
into PRIVATE_HASHES (the phrase itself never goes in the tree). A false
positive is a one-line fix: an ALLOW_NEXT_WORD entry or an ALLOW_FILES path.
"""
from __future__ import annotations

import hashlib
import pathlib
import re
import subprocess
import sys

# Generic, non-identifying regexes (case-insensitive). They describe a class of
# private value, never one specific machine, so they can stay in plain text.
PRIVATE_PATTERNS = [
    # MagicDNS names
    r"[a-z0-9-]+\.tail[0-9a-f]{5,6}\.ts\.net",
    # a service account's home directory / ssh target
    r"/home/medialab\b",
    r"\bmedialab@",
    # leftovers of the retired rewrite step
    r"YOUR_TAILNET_IP",
    r"your-tailnet\.ts\.net",
    r"your-server\.your-tailnet",
    r"your-machine\.your-tailnet",
]

# Salted SHA-256 prefixes of private words and phrases, lower-cased, words
# joined by one space ("some phrase", "100 64 1 2"). Value = what kind of thing it
# is, which is all a hit reports besides the matched text.
HASH_SALT = b"vibexstudio-identity-guard:v1:"
PRIVATE_HASHES = {
    # the maintainers' and their family's names
    "90479a4ef6b93fd3": "a maintainer's personal name",
    "df08ded573e1be10": "a maintainer's personal name",
    "534d34cb2d2da671": "a maintainer's personal name",
    "142f0940082743cf": "a maintainer's personal name",
    # the maintainers' own businesses, clients and private productions
    "0849d265ccd99b23": "a private brand",
    "a44fe8245fc6b72a": "a private brand",
    "588f66c2302a49e4": "a private brand",
    "ba8b405ece793a50": "a private brand",
    "f06c35768e3d2e7e": "a client brand",
    "c391695dcf147c15": "a client brand",
    "4cbf5b43526b493e": "a client brand",
    "3ee6285fcc8c7fc5": "a client brand",
    "cbc024a01633b041": "a client brand",
    "3677efa247af3f7a": "a client brand",
    "edfd72b390b17944": "a client brand",
    "72a30af41470e4f8": "a private production",
    "5dd10371586b9aa0": "a private production",
    "949466e202be5410": "a private brand",
    # machines, accounts, homes and hosts
    "90142fe31f4c8dcd": "a private machine identity",
    "ff2acbac2fa0e9c4": "a private machine identity",
    "9ae0eaa216fc93a1": "a private machine identity",
    "f3f7ee7e469b5789": "a private machine identity",
    "214f6ab80babf872": "a private machine identity",
    "24da29c21c8a8f60": "a private machine identity",
    "59f50992a7137a86": "a private machine identity",
    "6dca8b16082cf625": "a private machine identity",
    "7d7e0f70fe017ee1": "a private machine identity",
    "52409fd1206bec4b": "a private machine identity",
}
# First words of the multi-word entries above (same hashing), so only runs
# that start with one of them are expanded into phrases.
PHRASE_FIRST_WORDS = {
    "0849d265ccd99b23", "0e61c6f843847963", "19cc0600a0f5237e", "37320c769d9b86a7",
    "5e04374612e34465", "704acbd956828f1e", "ada4bef93423a92d", "afbbdb984f8dd683",
    "b2ffae80490e9904", "db645ed2d4b03ddc", "e8d871956c5e38a6",
}
MAX_WORDS = 4
# A hashed word written in lower case and followed by one of these words is an
# ordinary English phrase, not the private identifier (keyed by the word's
# hash). "Capitalised Name grey-..." is still a hit.
ALLOW_NEXT_WORD = {
    "df08ded573e1be10": ("gray", "grey"),   # the knit colour
}

# E-mail addresses: only reserved example/test domains and the forges' no-reply
# relays may appear. A real contact goes in config/local.env.
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})\b")
ALLOWED_EMAIL_DOMAINS = (
    "example.com", "example.org", "example.net", "users.noreply.github.com",
    "noreply.github.com", "anthropic.com",
)
ALLOWED_EMAIL_SUFFIXES = (".example", ".invalid", ".test", ".localhost", ".local")
# name@thing.ext that is not an address: retina assets (icon@2x.png) and
# systemd template units (user@1000.service).
NOT_EMAIL_TLDS = {
    "png", "jpg", "jpeg", "gif", "webp", "svg", "ico", "icns", "service", "socket",
    "timer", "target", "mount", "path", "scope", "slice",
}

# A client's design-system CSS that was once copied into the studio page: the
# exact six-stop rim gradient, compared with all whitespace removed.
CLIENT_CSS_SIGNATURES = (
    "rgba(255,255,255,.50)0%,rgba(255,255,255,.16)22%",
)

BINARY_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".mp3", ".mp4", ".wav",
    ".woff", ".woff2", ".ttf", ".otf", ".onnx", ".whl", ".zip", ".gz", ".pdf",
    ".icns", ".pyc", ".p8", ".pem", ".dmg", ".deb", ".glb", ".safetensors",
    ".jsonl", ".bin", ".pt", ".pth",
}

# Python bytecode is never allowed in the tree, whatever it contains: the .pyc
# header carries the absolute source path it was compiled from. Checked before
# the BINARY_EXT skip so it is a violation, not an ignored blob.
BYTECODE_EXT = {".pyc", ".pyo", ".pyd"}

# Shared bytecode-hit text. `scan()` still returns a flat `list[str]`, so the
# epilogue in `main()` tells the two hit classes apart by this marker instead of
# widening the public return type.
BYTECODE_HIT = "Python bytecode must not be committed"

# Files that legitimately mention the patterns: this guard.
ALLOW_FILES = {
    "media-lab/tools/identity_guard.py",
    "tools/identity_guard.py",
}

_RE = re.compile("|".join(f"(?:{p})" for p in PRIVATE_PATTERNS), re.IGNORECASE)
# A run: letters/digits, joined by whitespace or . _ / @ - (never by quotes,
# '+', commas or brackets, so a value assembled at runtime from two string
# literals is not a hit).
_RUN_RE = re.compile(r"[a-z0-9]+(?:[\s._/@-]+[a-z0-9]+)*")
_WORD_SPLIT = re.compile(r"[\s._/@-]+")
_WORD = re.compile(r"[a-z0-9]+")
_WORD_HASH: dict[str, str] = {}


def phrase_hash(phrase: str) -> str:
    """The PRIVATE_HASHES key of a word or phrase (case and separators ignored)."""
    words = [w for w in _WORD_SPLIT.split(phrase.lower()) if w]
    return hashlib.sha256(HASH_SALT + " ".join(words).encode()).hexdigest()[:16]


def _word_hash(word: str) -> str:
    h = _WORD_HASH.get(word)
    if h is None:
        h = hashlib.sha256(HASH_SALT + word.encode()).hexdigest()[:16]
        if len(_WORD_HASH) < 500_000:
            _WORD_HASH[word] = h
    return h


def private_phrase(line: str) -> tuple[str, str] | None:
    """(matched text as written, kind) of the first hashed private word/phrase."""
    lower = line.lower()
    same_length = len(lower) == len(line)   # else report the lower-cased text
    source = line if same_length else lower
    for run in _RUN_RE.finditer(lower):
        spans = [(m.start() + run.start(), m.end() + run.start())
                 for m in _WORD.finditer(run.group(0))]
        words = [lower[a:b] for a, b in spans]
        for i, word in enumerate(words):
            h = _word_hash(word)
            kind = PRIVATE_HASHES.get(h)
            if kind:
                nxt = words[i + 1] if i + 1 < len(words) else ""
                as_written = source[spans[i][0]:spans[i][1]]
                if not (nxt in ALLOW_NEXT_WORD.get(h, ()) and as_written.islower()):
                    return as_written, kind
            if h in PHRASE_FIRST_WORDS:
                for n in range(2, MAX_WORDS + 1):
                    if i + n > len(words):
                        break
                    phrase = " ".join(words[i:i + n])
                    kind = PRIVATE_HASHES.get(
                        hashlib.sha256(HASH_SALT + phrase.encode()).hexdigest()[:16])
                    if kind:
                        return source[spans[i][0]:spans[i + n - 1][1]], kind
    return None


def private_email(line: str) -> str | None:
    for m in EMAIL_RE.finditer(line):
        if line[max(0, m.start() - 3):m.start()] == "://":
            continue  # URL userinfo (https://user@host/...), not an address
        domain = m.group(1).lower()
        if domain.rsplit(".", 1)[-1] in NOT_EMAIL_TLDS:
            continue
        if domain in ALLOWED_EMAIL_DOMAINS or domain.endswith(ALLOWED_EMAIL_SUFFIXES):
            continue
        return m.group(0)
    return None


def client_css(text: str) -> bool:
    squashed = re.sub(r"\s+", "", text)
    return any(sig in squashed for sig in CLIENT_CSS_SIGNATURES)


def line_hit(line: str) -> str | None:
    m = _RE.search(line)
    if m:
        return m.group(0)
    found = private_phrase(line)
    if found:
        return f"{found[0]} ({found[1]})"
    email = private_email(line)
    if email:
        return f"{email} (an e-mail address)"
    return None


def repo_root() -> pathlib.Path:
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                             text=True, check=True).stdout.strip()
        return pathlib.Path(out)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return pathlib.Path.cwd()


def tracked_files(root: pathlib.Path) -> list[pathlib.Path]:
    """Tracked files plus untracked files git would not ignore (a new file is
    checked before it is ever added)."""
    out = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached", "--others",
                          "--exclude-standard"], capture_output=True, check=True).stdout
    files = []
    for raw in out.split(b"\0"):
        if raw:
            p = root / raw.decode()
            if p.is_file():
                files.append(p)
    return files


def iter_paths(args: list[str], root: pathlib.Path) -> list[pathlib.Path]:
    if not args:
        return tracked_files(root)
    files: list[pathlib.Path] = []
    for arg in args:
        p = pathlib.Path(arg)
        if p.is_dir():
            files.extend(q for q in p.rglob("*") if q.is_file() and ".git" not in q.parts)
        elif p.is_file():
            files.append(p)
    return files


def scan(files: list[pathlib.Path], root: pathlib.Path) -> list[str]:
    hits: list[str] = []
    for p in files:
        try:
            rel = str(p.resolve().relative_to(root.resolve()))
        except ValueError:
            rel = str(p)
        if rel in ALLOW_FILES:
            continue
        if p.suffix.lower() in BYTECODE_EXT or "__pycache__" in pathlib.PurePath(rel).parts:
            hits.append(f"{rel}: {BYTECODE_HIT} (it can embed absolute local paths)")
            continue
        if p.suffix.lower() in BINARY_EXT:
            continue
        try:
            data = p.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:8192]:
            continue
        text = data.decode("utf-8", "replace")
        for n, line in enumerate(text.splitlines(), 1):
            hit = line_hit(line)
            if hit:
                hits.append(f"{rel}:{n}: {hit}")
        if p.suffix.lower() in {".css", ".html", ".htm", ".scss", ".tsx", ".jsx", ".ts", ".js"} \
                and client_css(text):
            hits.append(f"{rel}: a client design system's copied CSS (rim gradient)")
    return hits


def main(argv: list[str]) -> int:
    if argv[:1] == ["--hash"]:
        for phrase in argv[1:]:
            print(f'    "{phrase_hash(phrase)}": "<what it is>",')
        return 0
    root = repo_root()
    files = iter_paths(argv, root)
    hits = scan(files, root)
    if hits:
        print("identity_guard: private material found in the tree:", file=sys.stderr)
        for h in hits:
            print(f"  {h}", file=sys.stderr)
        if any(BYTECODE_HIT not in h for h in hits):
            print("\nMove per-host values into config/local.env (see config/local.env.example, "
                  "read through media_lab_core/local_config.py) and per-studio content into the "
                  "gitignored config/local/ overlay (docs/LOCAL-OVERLAY.md).", file=sys.stderr)
        if any(BYTECODE_HIT in h for h in hits):
            print("\nRemove the bytecode artifact from the repository (add it to .gitignore "
                  "instead of committing it).", file=sys.stderr)
        return 1
    print(f"identity_guard: {len(files)} files clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
