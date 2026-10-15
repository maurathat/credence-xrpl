"""Billing arithmetic: integer ceiling division per token class, then a sum."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from credence.core import billing
from credence.core.objects import Quote
from credence.core.primitives import MAX_SAFE_INTEGER, DomainError, MicroTrlusd

VECTOR = Path(__file__).resolve().parents[2] / "vectors" / "c1" / "quote-basic.json"
QUOTE = Quote.from_canonical(json.loads(VECTOR.read_text())["input"])  # 150000 in, 600000 out


def priced(input_price: int, output_price: int) -> Quote:
    return replace(
        QUOTE,
        input_price_per_million_micro_trlusd=MicroTrlusd(input_price),
        output_price_per_million_micro_trlusd=MicroTrlusd(output_price),
    )


@pytest.mark.parametrize(
    ("input_tokens", "output_tokens", "input_cost", "output_cost"),
    [
        (0, 0, 0, 0),
        (1, 1, 1, 1),  # 0.15 -> 1 and 0.6 -> 1
        (1234, 1, 186, 1),  # 185.1 -> 186
        (1_000_000, 1_000_000, 150_000, 600_000),  # exact, no rounding
        (20, 5, 3, 3),  # 3.0 exactly and 3.0 exactly
        (21, 6, 4, 4),  # 3.15 -> 4 and 3.6 -> 4
    ],
)
def test_examples(input_tokens: int, output_tokens: int, input_cost: int, output_cost: int) -> None:
    assert billing.input_cost(QUOTE, input_tokens) == MicroTrlusd(input_cost)
    assert billing.output_cost(QUOTE, output_tokens) == MicroTrlusd(output_cost)
    assert billing.total_cost(QUOTE, input_tokens, output_tokens) == MicroTrlusd(
        input_cost + output_cost
    )


def test_rounds_each_class_before_summing() -> None:
    # 0.5 + 0.5 would be 1 if summed first; ceiling per class gives 2.
    quote = priced(500_000, 500_000)
    assert billing.total_cost(quote, 1, 1) == MicroTrlusd(2)


def test_zero_price_is_free() -> None:
    assert billing.total_cost(priced(0, 0), 10**9, 10**9) == MicroTrlusd(0)


@pytest.mark.parametrize(
    ("tokens", "reason"),
    [(1.0, "FLOAT_NOT_INTEGER"), (True, "BOOL_NOT_INTEGER"), (-1, "NEGATIVE_VALUE")],
)
def test_token_counts_are_strict(tokens: object, reason: str) -> None:
    with pytest.raises(DomainError) as exc:
        billing.input_cost(QUOTE, tokens)  # type: ignore[arg-type]
    assert (exc.value.reason, exc.value.field) == (reason, "input_tokens")


def test_cost_past_the_admitted_range_fails_closed() -> None:
    quote = priced(MAX_SAFE_INTEGER, 0)
    with pytest.raises(DomainError) as exc:
        billing.input_cost(quote, 2_000_000)
    assert exc.value.reason == "OUT_OF_RANGE"


@given(
    st.integers(min_value=0, max_value=10**9),
    st.integers(min_value=0, max_value=10**9),
)
def test_property_is_the_least_sufficient_integer(tokens: int, price: int) -> None:
    cost = billing.input_cost(priced(price, 0), tokens).value
    exact = tokens * price
    assert cost * 1_000_000 >= exact
    assert (cost - 1) * 1_000_000 < exact or cost == 0
