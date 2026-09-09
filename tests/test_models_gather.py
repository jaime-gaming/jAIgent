"""Live model gathering, driven with a fake HTTP client."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from jaigent import models


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("JAIGENT_HOME", str(tmp_path))
    return tmp_path


def test_openai_shape_is_parsed(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def get(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        return httpx.Response(
            200,
            json={"data": [{"id": "gpt-test"}, {"id": "gpt-other"}]},
            request=httpx.Request("GET", "https://api.openai.com/v1/models"),
        )

    monkeypatch.setattr(httpx, "get", get)
    found = models.gather_provider("openai", api_key="sk-x", base_url="https://api.openai.com/v1")
    assert {m.id for m in found} == {"gpt-test", "gpt-other"}
    assert all(m.provider == "openai" for m in found)


def test_gemini_strips_models_prefix(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def get(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        return httpx.Response(
            200,
            json={"models": [{"name": "models/gemini-2.5-flash"}]},
            request=httpx.Request("GET", "https://example/models"),
        )

    monkeypatch.setattr(httpx, "get", get)
    found = models.gather_provider("gemini", api_key="k", base_url="https://example")
    assert found[0].id == "gemini-2.5-flash"


def test_a_network_error_returns_empty(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "get", boom)
    assert models.gather_provider("openai", api_key="k", base_url="https://api.openai.com/v1") == []


def test_cache_round_trip(home: Path) -> None:
    models.save_cache(
        [models.ModelInfo(id="live-1", provider="groq", label="live-1", note="gathered")]
    )
    cached = models.load_cache()
    assert cached[0].id == "live-1"
    combined = models.combined()
    assert any(m.id == "live-1" for m in combined)
    assert any(m.id == "gpt-4o-mini" for m in combined)


def test_catalogue_wins_on_id_collision(home: Path) -> None:
    live = [models.ModelInfo(id="gpt-4o-mini", provider="openai", label="dup", note="gathered")]
    combined = models.combined(live=live, include_cache=False)
    found = next(m for m in combined if m.id == "gpt-4o-mini")
    assert found.note != "gathered"


def test_gather_available_queries_providers_in_parallel(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sequential gathering waited out every provider's timeout in turn."""
    import threading
    import time

    from jaigent import config as config_mod

    monkeypatch.setattr(config_mod, "KNOWN_PROVIDERS", ("a", "b", "c", "d"))
    monkeypatch.setattr(config_mod, "key_for_provider", lambda name: "k")
    active = 0
    peak = 0
    lock = threading.Lock()

    def slow(provider: str, **kwargs):  # noqa: ANN002, ANN003, ANN202
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.2)
        finally:
            with lock:
                active -= 1
        return [models.ModelInfo(id=f"{provider}-1", provider=provider, label="x")]

    monkeypatch.setattr(models, "gather_provider", slow)

    started = time.monotonic()
    found = models.gather_available(timeout=5.0)
    elapsed = time.monotonic() - started

    assert {m.id for m in found} == {"a-1", "b-1", "c-1", "d-1"}
    assert peak > 1, "providers were gathered one after another"
    assert elapsed < 0.8, f"took {elapsed:.2f}s for 4 x 0.2s gathers"


def test_gather_available_survives_a_broken_provider(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jaigent import config as config_mod

    monkeypatch.setattr(config_mod, "KNOWN_PROVIDERS", ("bad", "good"))
    monkeypatch.setattr(config_mod, "key_for_provider", lambda name: "k")

    def flaky(provider: str, **kwargs):  # noqa: ANN002, ANN003, ANN202
        if provider == "bad":
            raise RuntimeError("boom")
        return [models.ModelInfo(id="good-1", provider="good", label="x")]

    monkeypatch.setattr(models, "gather_provider", flaky)

    assert [m.id for m in models.gather_available()] == ["good-1"]
