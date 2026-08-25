# Benchmarking Nineninesix TTS

How to run the TTS benchmark against `nineninesix/gepard-1.0` on a fresh
machine, using the Docker stack.

The provider lives in
[`runner/src/coval_bench/providers/tts/nineninesix.py`](../runner/src/coval_bench/providers/tts/nineninesix.py).
It is the Cartesia provider pointed at `https://api.nineninesix.ai` — the API
speaks the Cartesia wire protocol, so the official `cartesia` SDK drives it with
only a `base_url` override. Two differences from Cartesia are baked in: audio is
requested at gepard's native **22050 Hz** (asking for 8000 or 16000 makes the
server resample), and no `language` field is sent — gepard is multilingual, but
the language is a property of the voice, not of the request.

Everything below assumes you have the repo and are at its root:

```bash
git clone <this-repo> benchmarks && cd benchmarks
```

You need Docker and an API key. Nothing else — no Python, no `uv`, no Postgres
install (those are only for running the test suite, see the end).

## 1. Install Docker

On Ubuntu:

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-v2 docker-buildx
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"    # then log out and back in
```

Until you re-login, the group change isn't active — prefix commands with `sudo`
or run `newgrp docker`. Verify with `docker compose version`.

## 2. Configure keys

Compose reads the **repo-root** `.env` (not `runner/.env`, which is only for
running on the host without Docker):

```bash
cp .env.example .env
```

Fill in:

```bash
NINENINESIX_API_KEY=sk_996_...
OPENAI_API_KEY=sk-...
```

`OPENAI_API_KEY` is only needed for the WER metric: the benchmark transcribes
the synthesized audio with `whisper-1` and scores it against the source text.
Leave it blank and every latency metric still works — the WER rows are skipped
with a logged warning. `.env` is gitignored; keep it that way.

Add other providers' keys here to benchmark them side by side
(`CARTESIA_API_KEY`, `ELEVENLABS_API_KEY`, …). The full provider →
environment-variable map is in
[`registries/provider_keys.py`](../runner/src/coval_bench/registries/provider_keys.py).

## 3. Build

```bash
docker compose build runner
```

Takes a couple of minutes cold. The image pins dependencies from `uv.lock` and
bundles the dataset, so nothing else is fetched at run time.

## 4. Smoke-test one call

Confirm the key and the wire protocol before spending a full run. This needs no
database:

```bash
docker compose run --rm runner coval-bench tts-smoke \
  --provider nineninesix \
  --model gepard-1.0 \
  --voice 9b2cd515-a3cf-4a83-a104-89da1ab1ba05 \
  --text "Your order shipped this morning and should arrive by Thursday."
```

One line of JSON comes back; exit code 0 means success:

```json
{"event": "tts_smoke", "provider": "nineninesix", "model": "gepard-1.0",
 "ttfa_ms": 73.0, "audio_path": "/tmp/tmp9v802w1l.wav", "audio_bytes": 159334,
 "error": null, "ok": true}
```

`audio_path` is inside the container and disappears with `--rm`. To keep the WAV
and listen to it, mount a directory the container's non-root uid can write to
and point `TMPDIR` at it:

```bash
mkdir -p out && chmod 777 out
docker compose run --rm -e TMPDIR=/out -v "$PWD/out:/out" runner \
  coval-bench tts-smoke --provider nineninesix --model gepard-1.0 \
  --voice 9b2cd515-a3cf-4a83-a104-89da1ab1ba05 --text "Hello there."
```

The file lands in `out/` owned by uid 65532; `sudo chown "$USER" out/*.wav` to
play it. Without `TMPDIR`, Python falls through to a container-local `/var/tmp`
and the WAV is lost with the container.

## 5. Benchmark (no database)

```bash
docker compose run --rm runner coval-bench probe \
  --only nineninesix/gepard-1.0 --samples 30 --concurrency 1
```

This runs the 30 customer-service transcripts of the `tts-v1` dataset through
the same per-item pipeline as production, aggregates the metrics and prints one
JSON line. Nothing is persisted. Roughly three minutes.

**Keep `--concurrency 1`.** It stays inside the tier-1 limit of 5 concurrent
WebSocket streams, and more importantly a second in-flight stream contends with
the first and inflates TTFA — the number stops meaning what it claims to. Thirty
items at concurrency 1 issues about 20–25 requests per minute, under the 60 rpm
cap.

Add models to the same run by repeating `--only`:

```bash
docker compose run --rm runner coval-bench probe \
  --only nineninesix/gepard-1.0 \
  --only cartesia/sonic-3.5 \
  --samples 30 --concurrency 1
```

A selector matching no registry entry exits 2 and lists the known ones.

## 6. Characterise the distribution (the run that produces publishable numbers)

`probe` prints one aggregate per invocation over 30 prompts. That is enough for a
median and nothing more: a 30-sample run cannot resolve a tail, and averaging
several passes' summaries corrupts the pooled median and p90. Use
[`scripts/tts_repeat_bench.py`](../runner/scripts/tts_repeat_bench.py), which
repeats the dataset and computes percentiles over every raw measurement at once.

The image ships `src/` but not `scripts/`, so mount it:

```bash
docker compose run --rm -v "$PWD/runner/scripts:/app/scripts:ro" runner \
  python scripts/tts_repeat_bench.py \
  --provider nineninesix --model gepard-1.0 --passes 6
```

Add `--out /out/prod180.jsonl` with the writable mount from step 4 to keep the
raw per-request rows.

Six passes is 180 samples and takes about 12 minutes. That size is not arbitrary:
resolving a ~14% tail event down to ~5% at 80% power needs roughly 172 samples.
Thirty samples only detects gross changes — during this work a 30-sample run
twice suggested a fix that 180 samples then disproved.

It prints pooled percentiles plus the TTFA component split and the slow-mode
rate, and writes one JSON row per synthesis to `--out` for your own analysis.
A single pass against production from us-west-1 looks like this — six passes
prints the same shape over 180 rows:

```
n=30  errors=0
  TTFA (perceived)       median=   74.9ms mean=   77.2 p90=   91.6 p95=  100.0 max=  107.2
    roundtrip            median=   54.9ms mean=   56.7 p90=   67.7 p95=   76.0 max=   86.2
    leading silence      median=   21.0ms mean=   20.5 p90=   24.9 p95=   24.9 max=   24.9
  WER                    median=    0.0% mean=    5.0 p90=   16.7 p95=   21.4 max=   30.0
                         perfect=19/30 (63.3%)
  slow mode (>150 ms): 0/30 = 0.0%
```

`TTFA = roundtrip + leading silence`. The split is what tells you whether a
latency change is in the network path or inside the audio — a build that streams
bytes promptly but pads them with silence looks fine on `roundtrip` and terrible
on `TTFA`.

Useful flags:

- `--pause-s 5` idles before each request, to test whether a latency effect is
  load-dependent. If the numbers don't move, it isn't queueing.
- `--rtt-ms 2.8` subtracts a known network round trip — see below.

### Where you run it changes every number

TTFA is measured from an already-open socket, so it contains exactly one network
round trip to `api.nineninesix.ai`. **Measure yours before comparing against any
number in this repo:**

```bash
for i in 1 2 3; do
  curl -s -o /dev/null -w "%{time_connect}\n" https://api.nineninesix.ai/health
done
```

The numbers here were produced from a benchmark host in **AWS us-west-1**, which
is 2.8 ms from the endpoint — the API is served from California, so this is a
same-region measurement. From Frankfurt or Singapore the same service will
measure 150 ms slower without anything having changed about it. Pass `--rtt-ms`
with your measured round trip to compare server behaviour rather than geography:

```bash
docker compose run --rm -v "$PWD/runner/scripts:/app/scripts:ro" runner \
  python scripts/tts_repeat_bench.py \
  --provider nineninesix --model gepard-1.0 --passes 6 --rtt-ms 2.8
```

Absolute latency from your machine is a sound baseline for tracking drift over
time. It is not directly rankable against [benchmarks.coval.ai](https://benchmarks.coval.ai),
whose runner measures from its own region.

### Pointing at another endpoint

`NINENINESIX_BASE_URL` swaps the endpoint while keeping everything else — dataset,
metrics, voice pool — identical, so the numbers stay comparable. Both `http://`
and `https://` work; the websocket scheme is paired to the http one automatically,
which a plain-HTTP host needs (the Cartesia SDK otherwise forces `wss://`). Use a
placeholder key rather than a real one over cleartext:

```bash
docker compose run --rm \
  -e NINENINESIX_BASE_URL=http://203.0.113.10:8000 \
  -e NINENINESIX_API_KEY=placeholder \
  -v "$PWD/runner/scripts:/app/scripts:ro" runner \
  python scripts/tts_repeat_bench.py \
  --provider nineninesix --model gepard-1.0 --passes 6
```

Remember to re-measure RTT against that host — a machine in another region will
shift every number.

## 7. Full run with the database and API

The persisted path additionally records the TTFA component split
(`TTFARoundtrip` + `TTFALeadingSilence`), which `probe` does not emit.

```bash
docker compose up -d db                 # Postgres on :5432
docker compose run --rm migrate         # alembic upgrade head — once
docker compose run --rm runner coval-bench run --kind tts --smoke
```

`--smoke` runs a single dataset item; drop it for the full 30. Note that `run`
executes **every** ACTIVE model in the registry, not a selection — providers
whose key is missing fail fast with `ValueError: <provider>_api_key is required`
and the run ends `partial`. That is expected when you only hold your own key;
your rows still land. Use `probe --only` when you want just one model.

Read the results back with SQL:

```bash
docker compose exec db psql -U postgres -d benchmarks -c \
  "select provider, model, metric_type, round(metric_value::numeric,1) v, status
     from benchmarks_v2.results where provider='nineninesix' order by metric_type;"
```

```
  provider   |   model    |    metric_type     |  v   | status
-------------+------------+--------------------+------+---------
 nineninesix | gepard-1.0 | TTFA               | 78.2 | success
 nineninesix | gepard-1.0 | TTFALeadingSilence | 19.0 | success
 nineninesix | gepard-1.0 | TTFARoundtrip      | 59.3 | success
 nineninesix | gepard-1.0 | WER                | 20.0 | success
```

Or over HTTP:

```bash
docker compose up -d api                # FastAPI on http://localhost:8000
curl -s localhost:8000/healthz
curl -s "localhost:8000/v1/results?benchmark=TTS&provider=nineninesix&limit=5"
curl -s "localhost:8000/v1/leaderboard?metric=TTFA&benchmark=TTS&window=24h"
```

Tear down with `docker compose down` (add `-v` to drop the Postgres volume and
start from an empty database).

## Sample counts, and why `--samples 471` does nothing

The sample count shown per model on [benchmarks.coval.ai](https://benchmarks.coval.ai)
— 471 at the time of writing — is **not** a dataset size. It is
`sample_count` from the windowed materialized view: the number of metric *rows*
pooled over the leaderboard's window. `tts-v1` holds exactly **30 unique
prompts**, and the site reaches several hundred by re-running those 30 every 30
minutes.

`--samples` above 30 is silently a no-op:
[`_sample_items`](../runner/src/coval_bench/datasets/loader.py#L185) returns the
list unchanged when the requested size is larger. To match the site's
statistical weight, repeat the run instead — sixteen passes over 30 prompts is
480 measurements. Pool the raw per-item values rather than averaging each pass's
summary, or the median and p90 come out wrong.

Repetition is also the only way to see the latency *distribution*. Thirty
samples give a median; several hundred reveal whether the tail is a smooth
slope or a discrete second mode.

## Reading the metrics

| Metric | What it measures |
|---|---|
| `TTFA` | Perceived time-to-first-audio: network roundtrip **plus** the leading silence at the head of the audio. The headline latency number. |
| `TTFARoundtrip` | The network/inference half of TTFA. |
| `TTFALeadingSilence` | The dead air the model emits before speech. It counts against you — a listener waits through it. |
| `WER` | Word error rate of `whisper-1` transcribing the synthesized audio, scored against the source transcript. |

The two components always sum back to `TTFA`.

Expect a nonzero WER floor that is not your model's fault. Text is normalized
with Whisper's `EnglishTextNormalizer`, which does not reconcile `11:30 PM`
against Whisper's `11.30 p.m.`, or `250 MB` against `250 megabytes`. Every
provider on the leaderboard pays the same tax, so the metric is fair for
comparison and misleading as an absolute. When a WER row looks bad, read the
transcript pair before believing it.

Latency measured this way includes the network path from wherever you ran it.
It is a self-consistent baseline for tracking your own drift, not a number
rankable directly against [benchmarks.coval.ai](https://benchmarks.coval.ai),
whose runner measures in-region.

## Tests

The test suite runs on the host, not in the image — the image installs
`--no-dev`, so it has no pytest. It needs [uv](https://docs.astral.sh/uv/):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env
cd runner && uv sync
uv run pytest -q tests/providers tests/runner    # offline, no keys, no network
```

The full suite (`uv run pytest -q`) also covers DB-backed tests that spawn their
own throwaway Postgres via `pytest-postgresql`, which needs real `postgresql`
binaries on the host — the compose `db` container does not satisfy it. Without
them, everything in `tests/api/` plus `test_db_writer`,
`test_normalized_db_writer`, `test_registry_store`, `test_arena_store`,
`test_arena_snapshot` and `test_arena_provider_recovery` errors at *setup*,
which is an environment gap rather than a regression. Fix with
`sudo apt-get install -y postgresql` (no service needs to run).

## Voices

The registry entry pins **Cara** (`9b2cd515-…`, feminine, en-US) as the voice
`probe` measures, and carries **Jason** (`993ba5f6-…`, masculine, en-US) as a
second pool voice so the model can field both sides of an arena battle. A full
`run` splits its items evenly across the pool; `probe` always uses the pinned
scalar voice.

List what your account has:

```bash
curl -s -H "Authorization: Bearer $NINENINESIX_API_KEY" \
  "https://api.nineninesix.ai/voices?limit=100" \
  | jq '.data[] | {id, name, gender, language}'
```

To change which voices are benchmarked, edit the `nineninesix` entry in
[`registries/models.py`](../runner/src/coval_bench/registries/models.py) and
rebuild the image. Pool voices need a `gender`, and a test enforces that the
pool covers both.
