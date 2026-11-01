# Supplemental runtime notices

These upstream notices are retained because the corresponding pinned wheel did
not include a discovered notice file. They do not relicense third-party code.
`manifest.json` binds each notice to one package version, immutable source commit,
and SHA-256. `runtime_inventory.py` verifies those bytes before including them in
an installation inventory, with source attribution distinct from wheel files.

Tokenizers 0.22.2: Apache-2.0 LICENSE from upstream commit
[f383101a26663708484cac0727792aad74f78234](https://github.com/huggingface/tokenizers/tree/f383101a26663708484cac0727792aad74f78234),
resolved from its v0.22.2 tag. The original license is copied without edits.

Having a notice for every top-level package is not proof that every bundled
native library, transitive component or distribution obligation has been audited.

ANTLR Python runtime 4.9.3: complete upstream LICENSE.txt from commit
[e4c1a74c66bd5290364ea2b36c97cd724b247357](https://github.com/antlr/antlr4/tree/e4c1a74c66bd5290364ea2b36c97cd724b247357),
resolved from tag 4.9.3. The locally built Python wheel omitted this file. The
source archive SHA-256 is f224469b4168294902bb1efa80a8bf7855f24c99aef99cbefc1bcd3cce77881b.
The complete notice is retained without edits, including the upstream file's
additional JavaScript MIT notices; this does not assert those JavaScript files
are part of the Python wheel.
