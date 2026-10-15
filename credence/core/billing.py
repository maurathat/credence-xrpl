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


def _cost_value(tokens: int, price_per_million: MicroTrlusd, field: str) -> int:
    count = strict_int(tokens, field)
    return _ceil_div(count * price_per_million.value, TOKENS_PER_PRICE_UNIT)


def _cost(tokens: int, price_per_million: MicroTrlusd, field: str) -> MicroTrlusd:
    return MicroTrlusd(_cost_value(tokens, price_per_million, field))


def total_cost_value(quote: Quote, input_tokens: int, output_tokens: int) -> int:
    """total_cost as a plain integer, unbounded, for comparisons that must not fail on range."""
    return _cost_value(
        input_tokens, quote.input_price_per_million_micro_trlusd, "input_tokens"
    ) + _cost_value(output_tokens, quote.output_price_per_million_micro_trlusd, "output_tokens")


def max_total_cost_value(quote: Quote) -> int:
    """The deterministic pre-purchase maximum: total_cost at the quote's own token caps."""
    return total_cost_value(quote, quote.max_input_tokens, quote.max_output_tokens)


def input_cost(quote: Quote, input_tokens: int) -> MicroTrlusd:
    return _cost(input_tokens, quote.input_price_per_million_micro_trlusd, "input_tokens")


def output_cost(quote: Quote, output_tokens: int) -> MicroTrlusd:
    return _cost(output_tokens, quote.output_price_per_million_micro_trlusd, "output_tokens")


def total_cost(quote: Quote, input_tokens: int, output_tokens: int) -> MicroTrlusd:
    return input_cost(quote, input_tokens) + output_cost(quote, output_tokens)
