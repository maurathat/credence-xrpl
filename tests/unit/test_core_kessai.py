"""The Kessai serializer binding: pinned provenance, fail-closed admission, stable canonical bytes."""

import hashlib
import json
from collections.abc import Iterator
from enum import IntEnum
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from credence.core import kessai

CUSTODY = Path(__file__).resolve().parents[2] / "third_party" / "custody"


@pytest.fixture
def fresh_canon() -> Iterator[None]:
    kessai._canon.cache_clear()
    yield
    kessai._canon.cache_clear()


def test_pinned_hash_matches_custody_provenance_and_file() -> None:
    provenance = (CUSTODY / "impl" / "CANON_PROVENANCE").read_text(encoding="utf-8")
    pinned = next(line.split()[1] for line in provenance.splitlines() if line.startswith("sha256"))
    assert kessai.CANON_SHA256 == pinned
    assert hashlib.sha256(kessai.CANON_PATH.read_bytes()).hexdigest() == pinned


def test_custody_commit_matches_submodule_head() -> None:
    head = (CUSTODY / ".git").read_text().strip() if (CUSTODY / ".git").is_file() else ""
    if not head.startswith("gitdir:"):
        pytest.skip("custody is not a submodule checkout; cannot read its HEAD")
    gitdir = (CUSTODY / head.removeprefix("gitdir:").strip()).resolve()
    assert (gitdir / "HEAD").read_text().strip() == kessai.CUSTODY_COMMIT


def test_refuses_to_load_altered_canon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fresh_canon: None
) -> None:
    altered = tmp_path / "canon.py"
    altered.write_bytes(kessai.CANON_PATH.read_bytes() + b"\n# altered\n")
    monkeypatch.setattr(kessai, "CANON_PATH", altered)
    with pytest.raises(kessai.SerializerUnavailable, match="sha256"):
        kessai.canonicalize({"a": 1})


def test_refuses_to_load_missing_canon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fresh_canon: None
) -> None:
    monkeypatch.setattr(kessai, "CANON_PATH", tmp_path / "absent.py")
    with pytest.raises(kessai.SerializerUnavailable, match="submodule"):
        kessai.canonicalize({"a": 1})


def test_refuses_other_rfc8785_version(monkeypatch: pytest.MonkeyPatch, fresh_canon: None) -> None:
    monkeypatch.setattr(kessai, "RFC8785_VERSION", "0.0.0")
    with pytest.raises(kessai.SerializerUnavailable, match="rfc8785"):
        kessai.canonicalize({"a": 1})


def test_agrees_with_canon_public_dataset_id() -> None:
    """For a dataset.v1 payload already in normal form, the adapter digest is canon.dataset_id."""
    payload = {"content": {"rows": [1, 2]}, "owner": "o", "schema_version": "1", "parents": []}
    raw = json.dumps(payload).encode()
    assert kessai.canonicalize_json_bytes(raw).sha256_hex == kessai._canon().dataset_id(raw)


def test_digest_is_sha256_of_canonical_bytes() -> None:
    form = kessai.canonicalize({"b": [1, "x"], "a": None, "c": True})
    assert form.data == b'{"a":null,"b":[1,"x"],"c":true}'
    assert form.sha256_hex == hashlib.sha256(form.data).hexdigest()


class _Small(IntEnum):
    ONE = 1


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ({"m": 1.0}, "FLOAT_NOT_ADMITTED"),
        ({"m": [0.5]}, "FLOAT_NOT_ADMITTED"),
        ({"m": float("nan")}, "FLOAT_NOT_ADMITTED"),
        ({"m": (1, 2)}, "UNSUPPORTED_TYPE"),
        ({"m": _Small.ONE}, "UNSUPPORTED_TYPE"),
        ({"m": b"x"}, "UNSUPPORTED_TYPE"),
        ({1: "x"}, "NON_STRING_MEMBER_NAME"),
        ({"address": "x"}, "ADDRESS_MEMBER_RESERVED"),
        ({"m": 2**53}, "NUMBER_OUT_OF_RANGE"),
        ({"m": -(2**53)}, "NUMBER_OUT_OF_RANGE"),
        ({"m": "\ud800"}, "NON_SCALAR_UNICODE"),
        ({"\u00e9": 1, "e\u0301": 2}, "NFC_NAME_COLLISION"),
        ([1], "NOT_AN_OBJECT"),
        ("x", "NOT_AN_OBJECT"),
    ],
)
def test_rejects(value: object, reason: str) -> None:
    with pytest.raises(kessai.SerializationRejected) as exc:
        kessai.canonicalize(value)  # type: ignore[arg-type]
    assert exc.value.reason == reason


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (b"\xef\xbb\xbf{}", "BOM_PRESENT"),
        (b'{"m":"\xff"}', "INVALID_UTF8"),
        (b'{"m":1,"m":1}', "DUPLICATE_MEMBER"),
        (b'{"m":1.5}', "NUMBER_NOT_INTEGRAL"),
        (b'{"m":NaN}', "NON_FINITE"),
        (b'{"m":', "MALFORMED_JSON"),
    ],
)
def test_preserves_canon_rejections(raw: bytes, reason: str) -> None:
    with pytest.raises(kessai.SerializationRejected) as exc:
        kessai.canonicalize_json_bytes(raw)
    assert exc.value.reason == reason


def test_nested_address_member_is_ordinary() -> None:
    assert kessai.canonicalize({"x": {"address": "a"}}).data == b'{"x":{"address":"a"}}'


def test_bool_stays_a_json_boolean() -> None:
    assert kessai.canonicalize({"t": True, "f": False}).data == b'{"f":false,"t":true}'


EQUIVALENT_ENCODINGS = [
    b'{"a":"\xc3\xa9","b":[1,2]}',
    b'{"b":[1,2],"a":"\xc3\xa9"}',
    b'{ "b" : [ 1 , 2 ] ,\n "a" : "\\u00e9" }',
    b'{"a":"e\xcc\x81","b":[1,2]}',
    b'{"a":"e\\u0301","b":[1.0,2e0]}',
]


def test_semantically_identical_encodings_share_bytes_and_digest() -> None:
    forms = {kessai.canonicalize_json_bytes(raw) for raw in EQUIVALENT_ENCODINGS}
    assert len(forms) == 1
    (form,) = forms
    assert form.data == '{"a":"\u00e9","b":[1,2]}'.encode()


def test_array_order_is_identity() -> None:
    assert (
        kessai.canonicalize({"l": [1, 2]}).sha256_hex
        != kessai.canonicalize({"l": [2, 1]}).sha256_hex
    )


_scalars = (
    st.none()
    | st.booleans()
    | st.integers(min_value=-(2**53 - 1), max_value=2**53 - 1)
    | st.text(st.characters(codec="utf-8"), max_size=8)
)
_json = st.recursive(
    _scalars,
    lambda inner: (
        st.lists(inner, max_size=4) | st.dictionaries(st.text(max_size=4), inner, max_size=4)
    ),
    max_leaves=12,
)
_documents = st.dictionaries(
    st.text(st.characters(codec="utf-8"), max_size=4).filter(lambda k: k != "address"),
    _json,
    max_size=5,
)


@given(_documents)
def test_property_value_and_text_paths_agree(document: dict[str, object]) -> None:
    try:
        by_value = kessai.canonicalize(document)  # type: ignore[arg-type]
    except kessai.SerializationRejected as exc:
        assert exc.reason == "NFC_NAME_COLLISION"
        return
    by_text = kessai.canonicalize_json_bytes(json.dumps(document).encode())
    reordered = dict(reversed(list(document.items())))
    assert by_value == by_text == kessai.canonicalize(reordered)  # type: ignore[arg-type]
    assert by_value.sha256_hex == hashlib.sha256(by_value.data).hexdigest()
