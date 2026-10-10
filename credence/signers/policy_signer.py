"""Credence policy signer.

Tech: Credence policy key

Responsibility (XRPL integration > Accounts and keys; Security and key management > Rules, build plan):
Signs only after the policy engine passes. Second signature on the borrower's spends.

The policy service signs only an XRPL transaction whose full unsigned form (destination, amount, fee, sequence, network ID, last ledger sequence) hashes to the digest in the engine's decision record, checked at signing time.

Owner: Credence Core (kept separate from the engine to isolate keys). Checkpoint 0 stub: no product logic.
"""

from typing import NoReturn


def sign_spend(*args: object, **kwargs: object) -> NoReturn:
    raise NotImplementedError(
        "Credence policy signer.sign_spend: not implemented (Checkpoint 0 stub)"
    )
