# tests/integration

Owner: each workstream for its own area; the `test_i1_*` files and `i1_world.py` belong to Integration (San Antonio).

- `test_i1_*`: offline integration of Inference delivery with Core accounting settlement (`contracts/INTEGRATION-FLOW.md`), including response authorization and real-subprocess crash recovery (`test_i1_subprocess_crash.py`, which runs `i1_child.py` and kills it with SIGKILL). No network, model or XRPL. Required in CI through `make test`.
- `test_local_llama.py`, `test_local_qwen.py`: real local models, opt-in only (`CREDENCE_LOCAL_LLAMA=1`, `CREDENCE_LOCAL_QWEN=1`); skipped otherwise.
- Tests against a local rippled or Speculos (`make ledger-up`) carry the `integration` marker.
