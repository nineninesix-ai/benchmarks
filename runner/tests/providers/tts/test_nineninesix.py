# Copyright 2026 The Coval Benchmarks Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the Nineninesix TTS provider."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from coval_bench.config import Settings
from coval_bench.providers.tts.nineninesix import (
    BASE_URL,
    OUTPUT_FORMAT,
    SAMPLE_RATE,
    NineninesixTTSProvider,
)

from .conftest import make_fake_cartesia_client, make_pcm_bytes

VOICE = "a0e99841-438c-4a64-b679-ae501e7d6091"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nineninesix_happy_path(fake_settings: Settings) -> None:
    """WS synthesize → ttfa set, valid WAV written."""
    chunks = [make_pcm_bytes(240), make_pcm_bytes(240)]
    provider = NineninesixTTSProvider(fake_settings, model="gepard-1.0", voice=VOICE)

    fake_client = make_fake_cartesia_client(chunks)

    with patch(
        "coval_bench.providers.tts.nineninesix.AsyncCartesia", return_value=fake_client
    ) as ctor:
        result = await provider.synthesize("Hello from Nineninesix")

    assert result.error is None, f"Unexpected error: {result.error}"
    assert result.ttfa_ms is not None
    assert 0 < result.ttfa_ms < 60_000
    assert result.audio_path is not None
    assert result.audio_path.exists()
    assert result.audio_path.read_bytes()[:4] == b"RIFF"
    assert result.provider == "nineninesix"
    assert result.model == "gepard-1.0"
    assert result.voice == VOICE

    # The SDK is the Cartesia one; only base_url makes it talk to Nineninesix.
    assert ctor.call_args.kwargs["base_url"] == BASE_URL

    # Provider must not auto-delete — orchestrator owns lifecycle
    result.audio_path.unlink()


@pytest.mark.asyncio
async def test_nineninesix_wav_uses_native_sample_rate(fake_settings: Settings) -> None:
    """The WAV header rate must match the rate requested on the wire.

    gepard-1.0 is native 22050 Hz; a mismatch between the requested
    ``output_format`` and the header written at finalize yields pitch-shifted
    audio, which Whisper then mis-transcribes into a bogus WER.
    """
    import wave

    provider = NineninesixTTSProvider(fake_settings, model="gepard-1.0", voice=VOICE)
    fake_client = make_fake_cartesia_client([make_pcm_bytes(240)])

    with patch("coval_bench.providers.tts.nineninesix.AsyncCartesia", return_value=fake_client):
        result = await provider.synthesize("Hello world")

    assert result.audio_path is not None
    with wave.open(str(result.audio_path), "rb") as wav_file:
        assert wav_file.getframerate() == SAMPLE_RATE == OUTPUT_FORMAT["sample_rate"]
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2

    result.audio_path.unlink()


@pytest.mark.asyncio
async def test_nineninesix_send_includes_output_format(fake_settings: Settings) -> None:
    """Regression: ctx.send() must carry output_format and model_id.

    ``AsyncWebSocketContext.send()`` does not inherit ``output_format`` from
    ``conn.context()``; when it is omitted the SDK substitutes its own default
    (pcm_f32le / 44100 Hz) and the WAV written at finalize (pcm_s16le / 22050 Hz)
    is corrupt.
    """
    provider = NineninesixTTSProvider(fake_settings, model="gepard-1.0", voice=VOICE)
    fake_client = make_fake_cartesia_client([make_pcm_bytes(240)])

    with patch("coval_bench.providers.tts.nineninesix.AsyncCartesia", return_value=fake_client):
        result = await provider.synthesize("Hello world")

    assert result.error is None
    fake_conn = fake_client._fake_conn
    assert fake_conn.last_context is not None, "conn.context(...) was never called"
    send_kwargs = fake_conn.last_context.send_kwargs
    assert send_kwargs is not None, "ctx.send(...) was never called"
    assert send_kwargs.get("output_format") == OUTPUT_FORMAT, send_kwargs
    assert send_kwargs.get("model_id") == "gepard-1.0", send_kwargs
    assert send_kwargs.get("voice") == {"id": VOICE, "mode": "id"}, send_kwargs
    # Language rides on the voice, not the request; the API has no such field.
    assert "language" not in send_kwargs, send_kwargs

    if result.audio_path is not None:
        result.audio_path.unlink()


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nineninesix_ws_error(fake_settings: Settings) -> None:
    """WebSocket connection error → result.error populated, audio_path None."""
    provider = NineninesixTTSProvider(fake_settings, model="gepard-1.0", voice=VOICE)

    with patch(
        "coval_bench.providers.tts.nineninesix.AsyncCartesia",
        side_effect=RuntimeError("websocket refused"),
    ):
        result = await provider.synthesize("error test")

    assert result.error is not None
    assert "websocket refused" in result.error
    assert result.audio_path is None


@pytest.mark.asyncio
async def test_nineninesix_no_audio_chunks(fake_settings: Settings) -> None:
    """No audio chunks received → audio_path is None, silent-failure error."""
    provider = NineninesixTTSProvider(fake_settings, model="gepard-1.0", voice=VOICE)
    fake_client = make_fake_cartesia_client([])  # empty chunks

    with patch("coval_bench.providers.tts.nineninesix.AsyncCartesia", return_value=fake_client):
        result = await provider.synthesize("silence test")

    assert result.error == "provider closed the stream without sending audio or an error"
    assert result.audio_path is None
    assert result.ttfa_ms is None


@pytest.mark.asyncio
async def test_nineninesix_rejects_unknown_model(fake_settings: Settings) -> None:
    """An unregistered model id fails before any network call."""
    provider = NineninesixTTSProvider(fake_settings, model="sonic-3", voice=VOICE)

    result = await provider.synthesize("wrong model")

    assert result.error is not None
    assert "Unsupported Nineninesix model" in result.error
    assert result.audio_path is None


# ---------------------------------------------------------------------------
# Properties / config
# ---------------------------------------------------------------------------


def test_nineninesix_name_and_model(fake_settings: Settings) -> None:
    p = NineninesixTTSProvider(fake_settings, model="gepard-1.0", voice=VOICE)
    assert p.name == "nineninesix-gepard-1.0"
    assert p.model == "gepard-1.0"
    assert p._model_supported("gepard-1.0")


def test_nineninesix_missing_api_key() -> None:
    settings_no_key = Settings(
        database_url="postgresql://runner:password@localhost:5432/benchmarks",
        dataset_bucket="test-bucket",
        dataset_id="stt-v1",
        nineninesix_api_key=None,
    )
    with pytest.raises(ValueError, match="nineninesix_api_key"):
        NineninesixTTSProvider(settings_no_key, model="gepard-1.0", voice=VOICE)
