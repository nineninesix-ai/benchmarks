# Copyright 2026 The Coval Benchmarks Authors
# SPDX-License-Identifier: Apache-2.0

"""Nineninesix TTS provider — Cartesia-compatible API, official cartesia SDK.

Nineninesix serves the Cartesia wire protocol at its own host, so this provider
is the Cartesia one pointed at ``https://api.nineninesix.ai`` via the SDK's
``base_url`` (the SDK derives the ``wss://`` websocket URL from it). Differences
from Cartesia that the shared protocol does not cover:

* the native sample rate is 22050 Hz (8000/16000 are resampled), not 24000;
* streaming is ``raw``-only, as on Cartesia;
* there is no ``language`` parameter. ``gepard-1.0`` is multilingual, but the
  language is a property of the voice (en-US, en-GB, es-MX, pt-BR, nl-NL), not
  of the request, so the auto-detect hazard that forces ``language='en'`` on
  Cartesia sends cannot arise: a benchmark voice fixes the language with it.
"""

from __future__ import annotations

import time
from typing import Any

import structlog
from cartesia import AsyncCartesia
from cartesia.types import VoiceSpecifierParam

from coval_bench.config import Settings
from coval_bench.providers.base import TTSProvider, TTSResult
from coval_bench.providers.tts._common import finalize_tts_result

logger: structlog.BoundLogger = structlog.get_logger(__name__)

BASE_URL = "https://api.nineninesix.ai"
# Override to benchmark a candidate deployment (a GPU pod, a staging host) with
# the production pipeline. The SDK derives its websocket URL by forcing `wss` on
# the base URL, which is wrong for a plain-HTTP pod, so the ws scheme is paired
# to the http one here rather than left to that default.
_WS_SCHEME = {"https": "wss", "http": "ws"}
# Native rate of gepard-1.0. Requesting 8000/16000 would make the provider
# resample, so the benchmark asks for the rate the model actually generates.
SAMPLE_RATE = 22050
OUTPUT_FORMAT: dict[str, Any] = {
    "sample_rate": SAMPLE_RATE,
    "container": "raw",
    "encoding": "pcm_s16le",
}


class NineninesixTTSProvider(TTSProvider):
    """Nineninesix TTS provider using WebSocket streaming (cartesia SDK v3)."""

    _VALID_MODELS = frozenset({"gepard-1.0"})

    def __init__(self, settings: Settings, model: str, voice: str) -> None:
        self._model = model
        self._voice = voice

        api_key_secret = settings.nineninesix_api_key
        if api_key_secret is None:
            raise ValueError("nineninesix_api_key is required in Settings")
        self._api_key = api_key_secret.get_secret_value()

        self._base_url = settings.nineninesix_base_url or BASE_URL
        scheme, _, rest = self._base_url.partition("://")
        if scheme not in _WS_SCHEME:
            raise ValueError(
                f"nineninesix_base_url must be http:// or https://, got {self._base_url!r}"
            )
        self._ws_base_url = f"{_WS_SCHEME[scheme]}://{rest}"

    @property
    def name(self) -> str:
        return f"nineninesix-{self._model}"

    @property
    def model(self) -> str:
        return self._model

    async def synthesize(self, text: str) -> TTSResult:
        """Synthesize speech via the Nineninesix WebSocket and return a TTSResult."""
        if not self._model_supported(self._model):
            return TTSResult(
                provider="nineninesix",
                model=self._model,
                voice=self._voice,
                ttfa_ms=None,
                audio_path=None,
                error=(
                    f"Unsupported Nineninesix model: {self._model}. "
                    f"Valid models: {sorted(self._VALID_MODELS)}"
                ),
            )
        audio_chunks: list[bytes] = []
        start: float | None = None
        first_chunk_at: float | None = None
        voice_spec: VoiceSpecifierParam = {"id": self._voice, "mode": "id"}

        try:
            client = AsyncCartesia(
                api_key=self._api_key,
                base_url=self._base_url,
                websocket_base_url=self._ws_base_url,
            )

            async with client.tts.websocket_connect() as conn:
                ctx = conn.context(
                    model_id=self._model,
                    voice=voice_spec,
                    output_format=OUTPUT_FORMAT,
                )

                start = time.monotonic()
                # Both `output_format` and `model_id` MUST be passed on every
                # send() — ctx.send() does NOT inherit them from conn.context().
                # Omitting output_format causes the SDK to substitute its own
                # default (pcm_f32le / 44100 Hz), which mismatches the WAV header
                # written at finalize (pcm_s16le / 22050 Hz) and produces corrupt
                # audio → Whisper hallucination → WER > 100%.
                await ctx.send(
                    model_id=self._model,
                    transcript=text,
                    voice=voice_spec,
                    output_format=OUTPUT_FORMAT,
                    continue_=False,
                )

                async for event in ctx.receive():
                    event_type: str = getattr(event, "type", "")
                    if event_type == "chunk":
                        audio: bytes | None = getattr(event, "audio", None)
                        if audio and len(audio) > 0:
                            if first_chunk_at is None:
                                first_chunk_at = time.monotonic()
                            audio_chunks.append(audio)
                    elif event_type == "done":
                        break

        except Exception as exc:
            logger.warning(
                "nineninesix_error", provider="nineninesix", model=self._model, exc_info=exc
            )
            return finalize_tts_result(
                provider="nineninesix",
                model=self._model,
                voice=self._voice,
                pcm=b"",
                sample_rate=SAMPLE_RATE,
                audio_synthesis_start=start,
                first_audio_chunk_at=first_chunk_at,
                error=str(exc),
            )

        return finalize_tts_result(
            provider="nineninesix",
            model=self._model,
            voice=self._voice,
            pcm=b"".join(audio_chunks),
            sample_rate=SAMPLE_RATE,
            audio_synthesis_start=start,
            first_audio_chunk_at=first_chunk_at,
        )
