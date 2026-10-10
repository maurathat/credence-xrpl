# vectors

Owner: Credence Core (Claude Code).

Serializer conformance vectors for each Credence class (execution profile, purchase request, receipt, decision record, credit event): input JSON, expected canonical bytes and expected digest, covering NFC normalization, key ordering with non-ASCII keys, large integers and integer amounts. Empty at Checkpoint 0; blocked on the general serializer package (see `docs/decisions/0001-foundational-architecture.md`).
