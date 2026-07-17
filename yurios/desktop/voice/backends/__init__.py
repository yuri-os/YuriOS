"""Concrete implementations of the voice seams (SPEC §3).

`fakes` is always importable (no model deps) and is what the test suite runs
against. The real adapters (`stt_whisper`, `tts_kokoro`, `tts_sovits`,
`vad_silero`) import heavy optional deps lazily, so importing this package never
pulls torch."""
