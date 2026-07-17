"""The voice layer — the only new work in Build #2 (SPEC §2).

`protocols` defines the three vendor-facing seams (STT/TTS/VAD); `turn` is the
real-time loop that streams one into the next and treats barge-in as a cancel;
`emotion`, `latency`, and `fillers` are the small pieces the loop leans on.
"""
