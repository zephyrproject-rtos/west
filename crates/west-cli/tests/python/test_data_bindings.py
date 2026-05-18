# Copyright 2026 The Zephyr Project Contributors.
#
# SPDX-License-Identifier: Apache-2.0

'''Pytest covering the structured-data parse/dump bindings.

All three formats (YAML, TOML, JSON) route through a single
`serde_json::Value` intermediate on the rust side. These tests pin
down the python-visible behaviour: type coverage in both directions,
round-trips, and the rough edges (TOML's table-only root, NaN
floats, bool vs int).

Run with:
    PYTHONPATH=src pytest crates/west-cli/tests/python/
'''

import math
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from west._west_native import (  # noqa: E402
    dump_json,
    dump_toml,
    dump_yaml,
    parse_json,
    parse_toml,
    parse_yaml,
)


# ---- Parse: scalar types --------------------------------------------------


def test_parse_yaml_scalars():
    assert parse_yaml("42") == 42
    assert parse_yaml("3.14") == 3.14
    assert parse_yaml("true") is True
    assert parse_yaml("false") is False
    assert parse_yaml("null") is None
    assert parse_yaml('"hello"') == "hello"


def test_parse_json_scalars():
    assert parse_json("42") == 42
    assert parse_json("3.14") == 3.14
    assert parse_json("true") is True
    assert parse_json("null") is None


def test_parse_toml_requires_table_root():
    # TOML has no scalar/array root; bare "42" should error out.
    with pytest.raises(ValueError):
        parse_toml("42")


# ---- Parse: container types ----------------------------------------------


def test_parse_yaml_dict_and_list():
    assert parse_yaml("a: 1\nb: [2, 3]\n") == {"a": 1, "b": [2, 3]}


def test_parse_toml_basic_table():
    assert parse_toml('a = 1\nb = ["x", "y"]\n') == {"a": 1, "b": ["x", "y"]}


def test_parse_toml_nested_table():
    src = """\
[manifest]
path = "zephyr"
file = "west.yml"
"""
    assert parse_toml(src) == {"manifest": {"path": "zephyr", "file": "west.yml"}}


def test_parse_json_nested():
    src = '{"a": [{"b": 1}, {"c": [true, false]}]}'
    assert parse_json(src) == {"a": [{"b": 1}, {"c": [True, False]}]}


# ---- Parse: malformed -----------------------------------------------------


def test_parse_yaml_malformed_raises_value_error():
    with pytest.raises(ValueError):
        parse_yaml("[unclosed: bracket\n")


def test_parse_toml_malformed_raises_value_error():
    with pytest.raises(ValueError):
        parse_toml("[broken\nno = good")


def test_parse_json_malformed_raises_value_error():
    with pytest.raises(ValueError):
        parse_json('{"unterminated":')


# ---- Dump: scalars --------------------------------------------------------


def test_dump_json_scalars():
    assert dump_json(42) == "42"
    assert dump_json(3.14) == "3.14"
    assert dump_json(True) == "true"
    assert dump_json(None) == "null"
    assert dump_json("hi") == '"hi"'


def test_dump_bool_distinct_from_int():
    # `True` is an int subclass in python; the dumper must keep them apart.
    assert dump_json(True) == "true"
    assert dump_json(1) == "1"


def test_dump_nan_inf_raise():
    with pytest.raises(ValueError):
        dump_json(float("nan"))
    with pytest.raises(ValueError):
        dump_yaml(math.inf)


# ---- Dump: containers ----------------------------------------------------


def test_dump_json_roundtrip():
    data = {"a": 1, "b": [True, "x", None], "c": {"nested": 2}}
    assert parse_json(dump_json(data)) == data


def test_dump_yaml_roundtrip():
    data = {"a": 1, "b": [True, "x", None], "c": {"nested": 2}}
    assert parse_yaml(dump_yaml(data)) == data


def test_dump_toml_roundtrip():
    data = {"manifest": {"path": "zephyr", "file": "west.yml"}}
    assert parse_toml(dump_toml(data)) == data


def test_dump_toml_top_level_must_be_dict():
    with pytest.raises(TypeError):
        dump_toml(42)
    with pytest.raises(TypeError):
        dump_toml([1, 2])
    with pytest.raises(TypeError):
        dump_toml("string")


# ---- Dump: non-string dict key rejected ----------------------------------


def test_dump_rejects_non_string_dict_key():
    with pytest.raises(TypeError):
        dump_json({1: "x"})


# ---- Dump: iterables become arrays ---------------------------------------


def test_dump_accepts_tuple_as_array():
    assert dump_json((1, 2, 3)) == "[1,2,3]"


def test_dump_accepts_generator_as_array():
    assert dump_json(i * 2 for i in range(3)) == "[0,2,4]"


# ---- Dump: unsupported types reject --------------------------------------


def test_dump_rejects_unsupported_type():
    class Opaque:
        pass

    with pytest.raises(TypeError):
        dump_json(Opaque())
