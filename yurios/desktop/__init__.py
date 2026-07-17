"""Build #2 — the Desktop Companion (book ch. 32, SPEC.md).

A voice + Live2D-avatar shell around Build #1's brain. Nothing in Build #1's
`app` package is modified; this package *composes* it (SPEC §2). The only new
work is the real-time loop (`desktop.voice.turn`) and the expression mapping
(`desktop.voice.emotion` + `web/avatar.js`).
"""
