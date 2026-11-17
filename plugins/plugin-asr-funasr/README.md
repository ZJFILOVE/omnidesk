# plugin-asr-funasr

OmniDesk v2 Protocol (`schema_version = 2`) provider plugin exposing `speech.asr.v1` for local offline Vulkan/CPU speech recognition via `ffprobe`, `ffmpeg`, and `llama-funasr-sensevoice`.

## Capabilities
- **Provides**: `speech.asr.v1` (streaming + unary via `CapabilityStream`)
- **Bus Events**:
  - `asr.job.progress`: `{job_id, media_path, completed_chunks, total_chunks, progress}`
  - `asr.job.completed`: `{job_id, media_path, output_srt_path, cue_count}`

## Supported Actions (`speech.asr.v1`)
- `status`: Checks binary availability (`funasr_bin`, `ffmpeg_bin`, `ffprobe_bin`), `device`, and user-configured `model_dir` without starting any model.
- `probe` / `list_streams`: Inspects media duration and audio/video/subtitle streams via `ffprobe`.
- `transcribe_audio` / `transcribe_media`: Splits media into `chunk_seconds` (default `300`) chunks, computes deterministic `job_id = md5(media_path + mtime + size + chunk_seconds)`, extracts 16 kHz mono PCM WAV via `ffmpeg`, invokes `llama-funasr-sensevoice --srt`, persists `.checkpoint.json` atomically after each chunk, supports resume (`resumed` stream event), and writes merged/sorted `<media>.srt` atomically.
- `cancel` / `list_jobs` / `clear_cache`: Cancels jobs with process-group cleanup, lists job status, and removes plugin checkpoints/cache without touching user media or subtitles.

## Verification
```bash
UV_CACHE_DIR=/tmp/omnidesk-uv-cache uv run pytest -q plugins/plugin-asr-funasr/tests
UV_CACHE_DIR=/tmp/omnidesk-uv-cache uv run omnidesk-pm verify plugin-asr-funasr
```
