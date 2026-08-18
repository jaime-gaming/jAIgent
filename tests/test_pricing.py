"""Token accounting and cost estimation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jaigent.pricing import (
    DEFAULT_PRICES,
    Cost,
    estimate,
    load_price_overrides,
    price_for,
)


class TestPriceLookup:
    def test_exact_match(self) -> None:
        assert price_for("gpt-4o") == DEFAULT_PRICES["gpt-4o"]

    def test_longest_prefix_wins(self) -> None:
        # "gpt-4o-mini" must not resolve to the pricier "gpt-4o".
        assert price_for("gpt-4o-mini") == DEFAULT_PRICES["gpt-4o-mini"]
        assert price_for("gpt-4o-mini-2024-07-18") == DEFAULT_PRICES["gpt-4o-mini"]

    def test_dated_snapshot(self) -> None:
        assert price_for("claude-3-5-sonnet-20241022") == DEFAULT_PRICES["claude-3-5-sonnet"]

    def test_gateway_prefix_is_stripped(self) -> None:
        assert price_for("anthropic/claude-3-opus") == DEFAULT_PRICES["claude-3-opus"]

    def test_case_insensitive(self) -> None:
        assert price_for("GPT-4O-MINI") == DEFAULT_PRICES["gpt-4o-mini"]

    def test_unknown_model(self) -> None:
        assert price_for("some-local-llama") is None

    def test_empty_model(self) -> None:
        assert price_for("") is None

    def test_overrides_take_precedence(self) -> None:
        assert price_for("gpt-4o", overrides={"gpt-4o": (1.0, 2.0)}) == (1.0, 2.0)


class TestEstimate:
    def test_openai_vocabulary(self) -> None:
        cost = estimate("gpt-4o-mini", {"prompt_tokens": 1000, "completion_tokens": 500})
        assert cost.input_tokens == 1000
        assert cost.output_tokens == 500
        assert cost.total_tokens == 1500

    def test_anthropic_vocabulary(self) -> None:
        cost = estimate("claude-3-5-sonnet", {"input_tokens": 200, "output_tokens": 100})
        assert cost.input_tokens == 200
        assert cost.output_tokens == 100

    def test_arithmetic(self) -> None:
        # gpt-4o-mini: $0.15/Mtok in, $0.60/Mtok out.
        cost = estimate("gpt-4o-mini", {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000})
        assert cost.usd == pytest.approx(0.75)

    def test_total_only_is_attributed_to_input(self) -> None:
        cost = estimate("gpt-4o-mini", {"total_tokens": 900})
        assert cost.total_tokens == 900

    def test_unknown_model_has_no_price(self) -> None:
        cost = estimate("mystery-model", {"prompt_tokens": 100})
        assert cost.usd is None
        assert cost.total_tokens == 100

    def test_empty_usage(self) -> None:
        cost = estimate("gpt-4o", {})
        assert cost.total_tokens == 0


class TestFormatting:
    def test_token_breakdown(self) -> None:
        assert Cost(1000, 240).format_tokens() == "1,240 tokens (1,000 in / 240 out)"

    def test_no_usage(self) -> None:
        assert Cost().format_tokens() == "no token usage reported"

    def test_small_amounts_get_four_decimals(self) -> None:
        assert Cost(1, 1, usd=0.00042).format_usd() == "$0.0004"

    def test_larger_amounts_get_two(self) -> None:
        assert Cost(1, 1, usd=1.239).format_usd() == "$1.24"

    def test_zero(self) -> None:
        assert Cost(1, 1, usd=0.0).format_usd() == "$0.00"

    def test_unknown_price_renders_empty(self) -> None:
        assert Cost(10, 10, usd=None).format_usd() == ""

    def test_summary_combines_both(self) -> None:
        summary = Cost(1000, 240, usd=0.0043).summary()
        assert "1,240 tokens" in summary
        assert "~$0.0043" in summary

    def test_summary_without_price(self) -> None:
        assert "~$" not in Cost(100, 20, usd=None).summary()


class TestOverrideFile:
    def test_loads_valid_file(self, tmp_path: Path) -> None:
        file = tmp_path / "prices.json"
        file.write_text(json.dumps({"my-model": {"input": 1.5, "output": 3.0}}), encoding="utf-8")
        assert load_price_overrides(file) == {"my-model": (1.5, 3.0)}

    def test_missing_file_is_empty(self, tmp_path: Path) -> None:
        assert load_price_overrides(tmp_path / "nope.json") == {}

    def test_malformed_json_is_ignored(self, tmp_path: Path) -> None:
        file = tmp_path / "bad.json"
        file.write_text("{not json", encoding="utf-8")
        assert load_price_overrides(file) == {}

    def test_entries_missing_keys_are_skipped(self, tmp_path: Path) -> None:
        file = tmp_path / "partial.json"
        file.write_text(
            json.dumps({"good": {"input": 1, "output": 2}, "bad": {"input": 1}}), encoding="utf-8"
        )
        assert load_price_overrides(file) == {"good": (1.0, 2.0)}

    def test_env_var_is_used(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        file = tmp_path / "p.json"
        file.write_text(json.dumps({"m": {"input": 9, "output": 9}}), encoding="utf-8")
        monkeypatch.setenv("JAIGENT_PRICES", str(file))
        assert load_price_overrides() == {"m": (9.0, 9.0)}

    def test_no_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("JAIGENT_PRICES", raising=False)
        assert load_price_overrides() == {}
