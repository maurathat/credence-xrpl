"""Decision record.

Tech: Python

Responsibility (Policy engine and verifier > Decision record, build plan):
Every decision, accept or reject, is itself serialized, digested and stored with: inputs' digests, rule that fired, engine version, timestamp, and, for an accepted payment, the digest of the exact unsigned XRPL transaction (destination, amount, fee, sequence, network ID, last ledger sequence) that the policy key is then allowed to sign. The UI shows these records; it never shows a decision that does not have one.

Owner: Credence Core. Checkpoint 0 stub: no product logic.
"""

from typing import NoReturn


def record(*args: object, **kwargs: object) -> NoReturn:
    raise NotImplementedError("Decision record.record: not implemented (Checkpoint 0 stub)")
