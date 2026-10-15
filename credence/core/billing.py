"""Billing arithmetic: integer-only, frozen in C1 for the later policy engine and verifier.

    input_cost  = ceil(input_tokens  * input_price_per_million_micro_trlusd  / 1_000_000)
    output_cost = ceil(output_tokens * output_price_per_million_micro_trlusd / 1_000_000)
    total_cost  = input_cost + output_cost

Integer ceiling division only; no floats or Decimals. Whether token counts are within the quote's
caps is a policy and verifier question (C2), not checked here.

Owner: Credence Core.
"""

from credence.core.objects import Quote
from credence.core.primitives import MicroTrlusd, strict_int

TOKENS_PER_PRICE_UNIT = 1_000_000


def _ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)


def _cost(tokens: int, price_per_million: MicroTrlusd, field: str) -> MicroTrlusd:
    count = strict_int(tokens, field)
    return MicroTrlusd(_ceil_div(count * price_per_million.value, TOKENS_PER_PRICE_UNIT))


def input_cost(quote: Quote, input_tokens: int) -> MicroTrlusd:
    return _cost(input_tokens, quote.input_price_per_million_micro_trlusd, "input_tokens")


def output_cost(quote: Quote, output_tokens: int) -> MicroTrlusd:
    return _cost(output_tokens, quote.output_price_per_million_micro_trlusd, "output_tokens")


def total_cost(quote: Quote, input_tokens: int, output_tokens: int) -> MicroTrlusd:
    return input_cost(quote, input_tokens) + output_cost(quote, output_tokens)
