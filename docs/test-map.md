# Test map

Which test files actually exercise each module — **generated**, not written:
`python scripts/test_map.py`. Do not hand-edit.

Measured with `coverage`'s per-test contexts, so this is what *ran*, not what
the file names suggest. A test file appears here if it covered at least
10% as many lines of the module as its top coverer did;
at most 6 are listed, best first. **Bold** marks the one the naming
convention would have guessed — where it is absent, the convention is lying,
and that is the case this file exists for.

| Module | Run these | Files touching it |
|---|---|---|
| `yurios/app/conversation.py` | **`tests/test_conversation.py`**<br>`tests/test_mind_hands.py`<br>`tests/test_mind_goals.py`<br>`tests/test_channels.py`<br>`tests/test_mind_loop.py`<br>`tests/test_inbox.py` | 44 |
| `yurios/app/core/assemble.py` | `tests/test_mind_hands.py`<br>`tests/test_mind_goals.py`<br>`tests/test_mind_scenarios.py`<br>`tests/test_mind_loop.py`<br>`tests/test_mind_soul.py`<br>`tests/test_muse.py` | 26 |
| `yurios/app/core/soul.py` | `tests/test_mind_hands.py`<br>`tests/test_mind_goals.py`<br>`tests/test_mind_loop.py`<br>`tests/test_muse.py`<br>`tests/test_tell_them.py`<br>`tests/test_handed.py` | 29 |
| `yurios/app/corpus.py` | `tests/test_mind_hands.py`<br>`tests/test_ratings.py`<br>`tests/test_mind_goals.py`<br>`tests/test_mind_loop.py`<br>`tests/test_integration.py`<br>`tests/test_muse.py` | 40 |
| `yurios/app/main.py` | `tests/test_mind_hands.py`<br>`tests/test_mind_goals.py`<br>`tests/test_mind_loop.py`<br>`tests/test_muse.py`<br>`tests/test_tell_them.py`<br>`tests/test_handed.py` | 34 |
| `yurios/app/memory/index.py` | `tests/test_mind_hands.py`<br>`tests/test_mind_goals.py`<br>`tests/test_mind_loop.py`<br>`tests/test_dreamjobs.py`<br>`tests/test_muse.py`<br>`tests/test_tell_them.py` | 33 |
| `yurios/app/memory/partner.py` | **`tests/test_partner.py`**<br>`tests/test_mind_hands.py`<br>`tests/test_mind_goals.py`<br>`tests/test_mind_loop.py`<br>`tests/test_mind_scenarios.py` | 33 |
| `yurios/app/memory/reindex.py` | `tests/test_mind_hands.py`<br>`tests/test_mind_goals.py`<br>`tests/test_mind_loop.py`<br>`tests/test_muse.py`<br>`tests/test_tell_them.py`<br>`tests/test_handed.py` | 24 |
| `yurios/app/memory/store.py` | `tests/test_mind_hands.py`<br>`tests/test_mind_goals.py`<br>`tests/test_dreamjobs.py`<br>`tests/test_mind_loop.py`<br>`tests/test_mind_scenarios.py`<br>`tests/test_memory_recall.py` | 33 |
| `yurios/app/providers/admission.py` | `tests/test_mind_hands.py`<br>`tests/test_route_privacy.py`<br>`tests/test_mind_goals.py`<br>`tests/test_utility_schema_fallback.py`<br>`tests/test_mind_loop.py`<br>`tests/test_channels.py` | 44 |
| `yurios/app/providers/catalog.py` | `tests/test_window.py`<br>`tests/test_card_optimize.py` | 2 |
| `yurios/app/providers/gguf.py` | `tests/test_gguf_fallback.py`<br>`tests/test_gguf_park.py`<br>`tests/test_vram.py` | 5 |
| `yurios/app/providers/http.py` | `tests/test_embed_off_loop.py`<br>`tests/test_lmstudio_auth.py` | 2 |
| `yurios/app/providers/lmstudio.py` | `tests/test_lmstudio_preload.py`<br>`tests/test_lmstudio_evict.py`<br>`tests/test_lmstudio_auth.py` | 6 |
| `yurios/app/providers/ollama.py` | `tests/test_embed_off_loop.py` | 1 |
| `yurios/app/providers/openrouter.py` | `tests/test_utility_schema_fallback.py`<br>`tests/test_route_privacy.py`<br>`tests/test_host.py`<br>`tests/test_empty_reply.py`<br>`tests/test_card_optimize.py`<br>`tests/test_inference_admission.py` | 10 |
| `yurios/app/providers/sentence_tf.py` | `tests/test_embedder_fallback.py` | 1 |
| `yurios/app/providers/usage.py` | `tests/test_empty_reply.py`<br>`tests/test_gguf_fallback.py`<br>`tests/test_context_meter.py`<br>`tests/test_route_privacy.py` | 6 |
| `yurios/app/providers/vision.py` | `tests/test_pictures.py`<br>`tests/test_channels.py`<br>`tests/test_studio_routes.py`<br>`tests/test_voice_stack.py` | 17 |
| `yurios/app/routes/chat.py` | `tests/test_integration.py`<br>`tests/test_text_style.py`<br>`tests/test_bootstrap_greeting.py`<br>`tests/test_embed_off_loop.py`<br>`tests/test_promptlog.py` | 5 |
| `yurios/app/sessions.py` | `tests/test_mind_hands.py`<br>`tests/test_mind_goals.py`<br>`tests/test_mind_loop.py`<br>`tests/test_conversation.py`<br>`tests/test_integration.py`<br>`tests/test_text_style.py` | 26 |
| `yurios/app/vaultgit.py` | `tests/test_mind_hands.py`<br>`tests/test_vault_commit_noise.py`<br>`tests/test_dreamjobs.py`<br>`tests/test_mind_routes.py`<br>`tests/test_mind_goals.py`<br>`tests/test_host_debug.py` | 57 |
| `yurios/characters/appearance.py` | `tests/test_host.py`<br>`tests/test_characters_exporter.py`<br>**`tests/test_appearance.py`**<br>`tests/test_export_privacy.py`<br>`tests/test_card_roundtrip.py`<br>`tests/test_characters_importer.py` | 14 |
| `yurios/characters/archive.py` | `tests/test_host.py`<br>`tests/test_characters_clone.py` | 2 |
| `yurios/characters/card.py` | `tests/test_characters_exporter.py`<br>`tests/test_export_privacy.py`<br>`tests/test_card_roundtrip.py`<br>`tests/test_characters_importer.py`<br>`tests/test_characters_card.py`<br>`tests/test_studio_routes.py` | 10 |
| `yurios/characters/cardsplit.py` | `tests/test_characters_exporter.py`<br>`tests/test_export_privacy.py`<br>**`tests/test_cardsplit.py`**<br>`tests/test_characters_importer.py` | 9 |
| `yurios/characters/clone.py` | `tests/test_characters_clone.py`<br>`tests/test_host.py` | 2 |
| `yurios/characters/connections.py` | `tests/test_host.py`<br>`tests/test_character_overrides.py` | 10 |
| `yurios/characters/creator.py` | `tests/test_studio_routes.py`<br>`tests/test_characters_creator.py` | 2 |
| `yurios/characters/defaults.py` | `tests/test_migration.py` | 3 |
| `yurios/characters/exporter.py` | `tests/test_characters_exporter.py`<br>`tests/test_export_privacy.py`<br>`tests/test_card_roundtrip.py`<br>`tests/test_studio_routes.py` | 6 |
| `yurios/characters/importer.py` | `tests/test_characters_exporter.py`<br>`tests/test_export_privacy.py`<br>`tests/test_characters_importer.py`<br>`tests/test_card_roundtrip.py`<br>`tests/test_setting.py` | 10 |
| `yurios/characters/models.py` | `tests/test_host.py`<br>`tests/test_characters_exporter.py`<br>`tests/test_migration.py`<br>`tests/test_export_privacy.py`<br>`tests/test_characters_registry.py`<br>`tests/test_character_overrides.py` | 19 |
| `yurios/characters/optimize.py` | `tests/test_card_optimize.py` | 1 |
| `yurios/characters/overrides.py` | `tests/test_character_overrides.py`<br>`tests/test_host.py` | 10 |
| `yurios/characters/privacy.py` | `tests/test_characters_exporter.py`<br>`tests/test_export_privacy.py`<br>`tests/test_card_roundtrip.py`<br>`tests/test_studio_routes.py` | 5 |
| `yurios/characters/registry.py` | `tests/test_host.py`<br>`tests/test_characters_exporter.py`<br>`tests/test_export_privacy.py`<br>`tests/test_migration.py`<br>`tests/test_character_overrides.py`<br>`tests/test_characters_registry.py` | 22 |
| `yurios/characters/selfiebook.py` | `tests/test_selfie_book_per_character.py`<br>`tests/test_studio_routes.py`<br>`tests/test_house_settings.py` | 3 |
| `yurios/characters/setting.py` | `tests/test_host.py`<br>`tests/test_characters_exporter.py`<br>**`tests/test_setting.py`**<br>`tests/test_export_privacy.py`<br>`tests/test_mind_hands.py`<br>`tests/test_card_roundtrip.py` | 35 |
| `yurios/characters/soulfiles.py` | `tests/test_mind_hands.py`<br>`tests/test_mind_goals.py`<br>`tests/test_mind_loop.py`<br>`tests/test_characters_exporter.py`<br>`tests/test_studio_routes.py`<br>`tests/test_muse.py` | 38 |
| `yurios/characters/studio.py` | `tests/test_studio_routes.py`<br>`tests/test_card_optimize.py`<br>`tests/test_studio_writer.py` | 7 |
| `yurios/characters/vcs.py` | `tests/test_characters_exporter.py`<br>`tests/test_studio_routes.py`<br>`tests/test_card_roundtrip.py`<br>`tests/test_export_privacy.py` | 6 |
| `yurios/cli.py` | `tests/test_model_setup.py`<br>`tests/test_envfile.py`<br>`tests/test_cli_characters.py`<br>`tests/test_cli_documents.py`<br>`tests/test_character_overrides.py`<br>`tests/test_cli_camera.py` | 10 |
| `yurios/ctl/camera.py` | `tests/test_cli_characters.py`<br>`tests/test_cli_documents.py`<br>`tests/test_envfile.py`<br>`tests/test_cli_camera.py`<br>`tests/test_model_setup.py`<br>`tests/test_cli_dreams.py` | 8 |
| `yurios/ctl/characters.py` | `tests/test_cli_characters.py`<br>`tests/test_cli_documents.py`<br>`tests/test_envfile.py`<br>`tests/test_cli_camera.py`<br>`tests/test_model_setup.py`<br>`tests/test_cli_dreams.py` | 8 |
| `yurios/ctl/chat.py` | `tests/test_cli_characters.py`<br>`tests/test_cli_documents.py`<br>`tests/test_envfile.py`<br>`tests/test_cli_camera.py`<br>`tests/test_model_setup.py`<br>`tests/test_cli_dreams.py` | 8 |
| `yurios/ctl/client.py` | `tests/test_cli_characters.py`<br>`tests/test_cli_documents.py`<br>`tests/test_cli_camera.py`<br>`tests/test_cli_dreams.py` | 4 |
| `yurios/ctl/documents.py` | `tests/test_cli_documents.py`<br>`tests/test_cli_characters.py`<br>`tests/test_envfile.py`<br>`tests/test_cli_camera.py`<br>`tests/test_model_setup.py`<br>`tests/test_cli_dreams.py` | 8 |
| `yurios/ctl/dreams.py` | `tests/test_cli_characters.py`<br>`tests/test_cli_documents.py`<br>`tests/test_envfile.py`<br>`tests/test_cli_dreams.py`<br>`tests/test_cli_camera.py`<br>`tests/test_model_setup.py` | 8 |
| `yurios/ctl/studio.py` | `tests/test_cli_characters.py`<br>`tests/test_cli_documents.py`<br>`tests/test_envfile.py`<br>`tests/test_cli_camera.py`<br>`tests/test_model_setup.py`<br>`tests/test_cli_dreams.py` | 8 |
| `yurios/ctl/util.py` | `tests/test_cli_characters.py`<br>`tests/test_cli_documents.py`<br>`tests/test_envfile.py`<br>`tests/test_cli_dreams.py`<br>`tests/test_cli_camera.py`<br>`tests/test_model_setup.py` | 8 |
| `yurios/daemon.py` | **`tests/test_daemon.py`**<br>`tests/test_model_setup.py` | 6 |
| `yurios/desktop/brain.py` | `tests/test_mind_hands.py`<br>`tests/test_mind_goals.py`<br>`tests/test_mind_loop.py`<br>`tests/test_mind_scenarios.py`<br>`tests/test_promptlog.py`<br>`tests/test_text_style.py` | 29 |
| `yurios/desktop/main.py` | `tests/test_voice_stack.py`<br>`tests/test_voice_ws_fork.py`<br>`tests/test_voice_replay.py`<br>`tests/test_voice_handshake.py`<br>`tests/test_text_style.py`<br>`tests/test_pictures.py` | 8 |
| `yurios/desktop/routes/avatar.py` | `tests/test_text_style.py` | 1 |
| `yurios/desktop/routes/health.py` | `tests/test_text_style.py` | 1 |
| `yurios/desktop/routes/settings.py` | `tests/test_pairing.py`<br>`tests/test_host.py`<br>`tests/test_window.py`<br>`tests/test_security.py` | 5 |
| `yurios/desktop/routes/voice_ws.py` | `tests/test_voice_handshake.py`<br>`tests/test_text_style.py` | 2 |
| `yurios/desktop/voice/backends/fakes.py` | `tests/test_channels.py`<br>`tests/test_voice_ws_fork.py`<br>`tests/test_voice_replay.py`<br>`tests/test_voice_stack.py`<br>`tests/test_pictures.py`<br>`tests/test_voice_handshake.py` | 26 |
| `yurios/desktop/voice/backends/stt_whisper.py` | `tests/test_voice_limits.py`<br>**`tests/test_stt_whisper.py`** | 2 |
| `yurios/desktop/voice/backends/tts_kokoro.py` | `tests/test_tts_espeak.py`<br>`tests/test_tts_kokoro_device.py` | 2 |
| `yurios/desktop/voice/emotion.py` | `tests/test_channels.py`<br>`tests/test_voice_replay.py`<br>`tests/test_voice_ws_fork.py`<br>`tests/test_mind_hands.py`<br>`tests/test_voice_stack.py`<br>`tests/test_mind_scenarios.py` | 21 |
| `yurios/desktop/voice/fillers.py` | `tests/test_voice_limits.py`<br>`tests/test_voice_stack.py` | 2 |
| `yurios/desktop/voice/latency.py` | `tests/test_voice_ws_fork.py`<br>`tests/test_voice_replay.py`<br>`tests/test_voice_handshake.py`<br>`tests/test_voice_stack.py`<br>`tests/test_integration.py`<br>`tests/test_pictures.py` | 10 |
| `yurios/desktop/voice/sentences.py` | `tests/test_voice_ws_fork.py`<br>`tests/test_voice_replay.py`<br>`tests/test_voice_handshake.py`<br>`tests/test_voice_stack.py`<br>`tests/test_integration.py`<br>`tests/test_pictures.py` | 9 |
| `yurios/desktop/voice/speech_gate.py` | `tests/test_voice_ws_fork.py`<br>`tests/test_voice_replay.py`<br>`tests/test_voice_handshake.py`<br>`tests/test_voice_stack.py`<br>`tests/test_boot.py`<br>`tests/test_pictures.py` | 8 |
| `yurios/desktop/voice/transcript.py` | `tests/test_channels.py`<br>`tests/test_voice_ws_fork.py`<br>`tests/test_empty_reply.py`<br>`tests/test_text_style.py`<br>`tests/test_boot.py`<br>`tests/test_ratings.py` | 9 |
| `yurios/desktop/voice/turn.py` | `tests/test_voice_replay.py`<br>`tests/test_voice_ws_fork.py`<br>`tests/test_voice_stack.py`<br>`tests/test_voice_handshake.py`<br>`tests/test_integration.py`<br>`tests/test_empty_reply.py` | 10 |
| `yurios/desktop/voice/ws_limits.py` | `tests/test_voice_ws_fork.py`<br>`tests/test_voice_replay.py`<br>`tests/test_voice_handshake.py`<br>`tests/test_voice_stack.py`<br>`tests/test_host.py`<br>`tests/test_voice_limits.py` | 24 |
| `yurios/desktop/voice/ws_session.py` | `tests/test_voice_ws_fork.py`<br>`tests/test_voice_replay.py`<br>`tests/test_voice_handshake.py`<br>`tests/test_voice_stack.py`<br>`tests/test_voice_limits.py`<br>`tests/test_pictures.py` | 8 |
| `yurios/desktop/window.py` | **`tests/test_window.py`** | 1 |
| `yurios/doctor.py` | **`tests/test_doctor.py`** | 1 |
| `yurios/envfile.py` | **`tests/test_envfile.py`**<br>`tests/test_checkpoints.py`<br>`tests/test_pairing.py`<br>`tests/test_host.py`<br>`tests/test_house_settings.py` | 7 |
| `yurios/forge/backends/anima.py` | `tests/test_forge_anima.py` | 2 |
| `yurios/forge/backends/base.py` | `tests/test_gallery.py` | 1 |
| `yurios/forge/backends/diffusers.py` | `tests/test_forge_diffusers.py`<br>`tests/test_selfie.py` | 4 |
| `yurios/forge/backends/krea2.py` | `tests/test_forge_krea2.py` | 2 |
| `yurios/forge/backends/mock.py` | `tests/test_forge_prompts.py`<br>`tests/test_gallery.py`<br>`tests/test_boot.py` | 3 |
| `yurios/forge/backends/openrouter.py` | `tests/test_route_privacy.py`<br>`tests/test_selfie.py`<br>`tests/test_forge_prompts.py` | 3 |
| `yurios/forge/backends/sniff.py` | `tests/test_checkpoints.py`<br>`tests/test_forge_krea2.py`<br>`tests/test_forge_anima.py`<br>`tests/test_forge_diffusers.py` | 5 |
| `yurios/forge/character.py` | `tests/test_channels.py`<br>`tests/test_inbox.py`<br>`tests/test_selfie.py`<br>`tests/test_pictures.py`<br>`tests/test_forge_prompts.py`<br>`tests/test_voice_stack.py` | 27 |
| `yurios/forge/civitai.py` | `tests/test_checkpoints.py` | 6 |
| `yurios/forge/prompts.py` | `tests/test_forge_prompts.py` | 2 |
| `yurios/forge/provenance.py` | `tests/test_forge_prompts.py`<br>`tests/test_gallery.py` | 2 |
| `yurios/forge/service.py` | `tests/test_channels.py`<br>`tests/test_forge_prompts.py`<br>`tests/test_inbox.py`<br>`tests/test_selfie.py`<br>`tests/test_pictures.py`<br>`tests/test_gallery.py` | 26 |
| `yurios/forge/templates.py` | `tests/test_mcp_contract.py`<br>`tests/test_selfie_templates.py`<br>`tests/test_channels.py`<br>`tests/test_inbox.py`<br>`tests/test_selfie.py`<br>`tests/test_pictures.py` | 29 |
| `yurios/forge/types.py` | `tests/test_forge_anima.py`<br>`tests/test_forge_krea2.py`<br>`tests/test_forge_diffusers.py`<br>`tests/test_forge_prompts.py`<br>`tests/test_gallery.py`<br>`tests/test_route_privacy.py` | 6 |
| `yurios/kernel/clock.py` | `tests/test_mind_hands.py`<br>`tests/test_dreamjobs.py`<br>`tests/test_channels.py`<br>`tests/test_mind_goals.py`<br>`tests/test_mind_loop.py`<br>`tests/test_tell_them.py` | 60 |
| `yurios/kernel/correlate.py` | `tests/test_mind_hands.py`<br>`tests/test_mind_goals.py`<br>`tests/test_mind_loop.py`<br>`tests/test_muse.py`<br>`tests/test_handed.py`<br>`tests/test_mind_scenarios.py` | 28 |
| `yurios/kernel/hub.py` | `tests/test_mind_hands.py`<br>`tests/test_mind_goals.py`<br>`tests/test_channels.py`<br>`tests/test_mind_loop.py`<br>`tests/test_events.py`<br>`tests/test_inbox.py` | 47 |
| `yurios/migrate.py` | `tests/test_migration.py` | 1 |
| `yurios/mind/acts.py` | `tests/test_tell_them.py`<br>`tests/test_mind_hands.py`<br>`tests/test_mind_scenarios.py`<br>`tests/test_mind_goals.py`<br>`tests/test_mind_loop.py`<br>`tests/test_muse.py` | 17 |
| `yurios/mind/budget.py` | `tests/test_mind_hands.py`<br>`tests/test_mind_goals.py`<br>`tests/test_mind_loop.py`<br>`tests/test_muse.py`<br>`tests/test_handed.py`<br>`tests/test_mind_scenarios.py` | 20 |
| `yurios/mind/documents.py` | `tests/test_mind_routes.py`<br>`tests/test_pdftext.py`<br>`tests/test_knowledge.py` | 3 |
| `yurios/mind/dream.py` | `tests/test_dreamjobs.py`<br>`tests/test_mind_hands.py`<br>`tests/test_mind_goals.py`<br>`tests/test_mind_loop.py`<br>`tests/test_mind_scenarios.py`<br>**`tests/test_dream.py`** | 22 |
| `yurios/mind/dreamjobs/builtins.py` | `tests/test_dreamjobs.py`<br>`tests/test_self_goals.py`<br>`tests/test_mind_hands.py`<br>`tests/test_mind_goals.py`<br>`tests/test_mind_scenarios.py`<br>`tests/test_mind_routes.py` | 21 |
| `yurios/mind/dreamjobs/context.py` | `tests/test_dreamjobs.py`<br>`tests/test_self_goals.py`<br>`tests/test_mind_hands.py` | 21 |
| `yurios/mind/dreamjobs/filedsl.py` | `tests/test_dreamjobs.py`<br>`tests/test_mind_hands.py`<br>`tests/test_mind_goals.py`<br>`tests/test_mind_loop.py`<br>`tests/test_self_goals.py`<br>`tests/test_muse.py` | 30 |
| `yurios/mind/dreamjobs/research.py` | `tests/test_dreamjobs.py` | 2 |
| `yurios/mind/dreamjobs/runner.py` | `tests/test_dreamjobs.py`<br>`tests/test_mind_hands.py`<br>`tests/test_mind_goals.py`<br>`tests/test_mind_loop.py`<br>`tests/test_self_goals.py`<br>`tests/test_mind_routes.py` | 22 |
| `yurios/mind/goals.py` | `tests/test_mind_hands.py`<br>`tests/test_mind_goals.py`<br>`tests/test_mind_loop.py`<br>`tests/test_tell_them.py`<br>`tests/test_mind_scenarios.py`<br>`tests/test_muse.py` | 22 |
| `yurios/mind/goalwork.py` | `tests/test_mind_hands.py`<br>`tests/test_mind_goals.py`<br>`tests/test_tell_them.py`<br>`tests/test_step_files_goal.py` | 17 |
| `yurios/mind/handed.py` | **`tests/test_handed.py`**<br>`tests/test_mind_hands.py` | 16 |
| `yurios/mind/hands.py` | `tests/test_mind_hands.py`<br>`tests/test_mind_goals.py`<br>`tests/test_mind_loop.py`<br>`tests/test_dreamjobs.py`<br>`tests/test_muse.py`<br>`tests/test_step_files_goal.py` | 29 |
| `yurios/mind/handwork.py` | `tests/test_mind_hands.py`<br>`tests/test_compact_followups.py`<br>`tests/test_muse.py`<br>`tests/test_step_files_goal.py`<br>`tests/test_handed.py`<br>`tests/test_mind_goals.py` | 19 |
| `yurios/mind/housekeeping.py` | `tests/test_mind_hands.py`<br>`tests/test_mind_goals.py`<br>`tests/test_mind_loop.py`<br>`tests/test_mind_scenarios.py`<br>`tests/test_muse.py`<br>`tests/test_handed.py` | 17 |
| `yurios/mind/journal.py` | `tests/test_mind_hands.py`<br>`tests/test_mind_goals.py`<br>`tests/test_mind_loop.py`<br>`tests/test_handed.py`<br>`tests/test_muse.py`<br>`tests/test_mind_scenarios.py` | 25 |
| `yurios/mind/knowledge.py` | **`tests/test_knowledge.py`**<br>`tests/test_mind_hands.py`<br>`tests/test_mind_goals.py`<br>`tests/test_mind_loop.py`<br>`tests/test_muse.py`<br>`tests/test_tell_them.py` | 22 |
| `yurios/mind/loop.py` | `tests/test_mind_hands.py`<br>`tests/test_mind_goals.py`<br>`tests/test_mind_loop.py`<br>`tests/test_muse.py`<br>`tests/test_tell_them.py`<br>`tests/test_handed.py` | 19 |
| `yurios/mind/muse.py` | **`tests/test_muse.py`**<br>`tests/test_mind_hands.py`<br>`tests/test_mind_goals.py`<br>`tests/test_handed.py`<br>`tests/test_mind_scenarios.py`<br>`tests/test_mind_loop.py` | 16 |
| `yurios/mind/pdftext.py` | **`tests/test_pdftext.py`**<br>`tests/test_mind_routes.py` | 2 |
| `yurios/mind/policy.py` | `tests/test_mind_hands.py`<br>`tests/test_mind_goals.py`<br>`tests/test_mind_loop.py`<br>`tests/test_tell_them.py`<br>`tests/test_muse.py`<br>`tests/test_mind_scenarios.py` | 21 |
| `yurios/mind/promptlog.py` | `tests/test_mind_hands.py`<br>`tests/test_mind_goals.py`<br>`tests/test_mind_loop.py`<br>`tests/test_muse.py`<br>`tests/test_handed.py`<br>`tests/test_tell_them.py` | 21 |
| `yurios/mind/prompts.py` | `tests/test_mind_hands.py`<br>`tests/test_mind_goals.py`<br>`tests/test_muse.py`<br>`tests/test_handed.py`<br>`tests/test_tell_them.py`<br>`tests/test_step_files_goal.py` | 18 |
| `yurios/mind/selfedit.py` | `tests/test_mind_hands.py`<br>`tests/test_mind_goals.py`<br>`tests/test_mind_routes.py`<br>**`tests/test_selfedit.py`**<br>`tests/test_mind_loop.py`<br>`tests/test_partner.py` | 21 |
| `yurios/mind/signals.py` | `tests/test_mind_hands.py`<br>`tests/test_mind_loop.py`<br>`tests/test_mind_goals.py`<br>`tests/test_handed.py`<br>`tests/test_mind_scenarios.py`<br>`tests/test_tell_them.py` | 42 |
| `yurios/mind/trace.py` | `tests/test_mind_hands.py`<br>`tests/test_mind_goals.py`<br>`tests/test_mind_loop.py`<br>`tests/test_muse.py`<br>`tests/test_handed.py`<br>`tests/test_tell_them.py` | 19 |
| `yurios/mind/util.py` | `tests/test_mind_hands.py`<br>`tests/test_host_debug.py`<br>`tests/test_mind_goals.py`<br>`tests/test_mind_loop.py`<br>`tests/test_mind_util.py`<br>`tests/test_knowledge.py` | 59 |
| `yurios/mind/vaultio.py` | `tests/test_mind_hands.py`<br>`tests/test_mind_goals.py`<br>`tests/test_mind_loop.py`<br>`tests/test_tell_them.py`<br>`tests/test_mind_scenarios.py`<br>`tests/test_handed.py` | 31 |
| `yurios/mind/workspace.py` | `tests/test_mind_hands.py`<br>`tests/test_mind_goals.py`<br>**`tests/test_workspace.py`**<br>`tests/test_dreamjobs.py`<br>`tests/test_muse.py`<br>`tests/test_mind_loop.py` | 24 |
| `yurios/mind/world.py` | `tests/test_mind_hands.py`<br>`tests/test_mind_goals.py`<br>`tests/test_mind_loop.py`<br>`tests/test_mind_scenarios.py`<br>`tests/test_handed.py`<br>`tests/test_tell_them.py` | 21 |
| `yurios/models.py` | `tests/test_model_setup.py`<br>`tests/test_character_overrides.py` | 8 |
| `yurios/pairing.py` | **`tests/test_pairing.py`** | 1 |
| `yurios/qr.py` | `tests/test_pairing.py`<br>**`tests/test_qr.py`** | 2 |
| `yurios/searxng.py` | **`tests/test_searxng.py`**<br>`tests/test_doctor.py` | 2 |
| `yurios/security.py` | `tests/test_host.py`<br>`tests/test_mind_routes.py`<br>`tests/test_host_debug.py`<br>`tests/test_mind_hands.py`<br>`tests/test_channels.py`<br>`tests/test_studio_routes.py` | 54 |
| `yurios/world/avatar/controller.py` | `tests/test_mind_hands.py`<br>`tests/test_mind_goals.py`<br>`tests/test_channels.py`<br>`tests/test_mind_scenarios.py`<br>`tests/test_avatar_controller.py`<br>`tests/test_mind_loop.py` | 46 |
| `yurios/world/boot.py` | `tests/test_voice_stack.py`<br>**`tests/test_boot.py`**<br>`tests/test_studio_routes.py`<br>`tests/test_channels.py`<br>`tests/test_voice_ws_fork.py`<br>`tests/test_voice_replay.py` | 24 |
| `yurios/world/brain.py` | `tests/test_tool_loop.py`<br>`tests/test_mind_hands.py`<br>`tests/test_mind_goals.py`<br>`tests/test_mind_loop.py`<br>`tests/test_mind_scenarios.py`<br>`tests/test_show_held_picture.py` | 29 |
| `yurios/world/channels/manager.py` | `tests/test_channels.py`<br>`tests/test_inbox.py`<br>`tests/test_pictures.py`<br>`tests/test_voice_stack.py`<br>`tests/test_studio_routes.py`<br>`tests/test_daemon.py` | 21 |
| `yurios/world/channels/notify.py` | `tests/test_inbox.py` | 1 |
| `yurios/world/channels/telegram.py` | `tests/test_channels.py` | 3 |
| `yurios/world/context.py` | `tests/test_context_meter.py`<br>`tests/test_channels.py`<br>`tests/test_studio_routes.py`<br>`tests/test_inbox.py`<br>`tests/test_pictures.py`<br>`tests/test_daemon.py` | 21 |
| `yurios/world/debug.py` | `tests/test_host_debug.py`<br>`tests/test_debug_graph.py` | 2 |
| `yurios/world/debug_graph.py` | **`tests/test_debug_graph.py`**<br>`tests/test_host_debug.py` | 2 |
| `yurios/world/gallery.py` | **`tests/test_gallery.py`**<br>`tests/test_show_held_picture.py` | 15 |
| `yurios/world/host/app.py` | `tests/test_host.py` | 12 |
| `yurios/world/host/brains.py` | `tests/test_host.py` | 6 |
| `yurios/world/host/checkpoints.py` | `tests/test_host.py`<br>**`tests/test_checkpoints.py`** | 6 |
| `yurios/world/host/debug.py` | `tests/test_host.py` | 7 |
| `yurios/world/host/hosting.py` | `tests/test_host.py`<br>`tests/test_studio_routes.py` | 12 |
| `yurios/world/host/house.py` | `tests/test_host.py`<br>`tests/test_house_settings.py` | 7 |
| `yurios/world/host/modules.py` | `tests/test_host.py`<br>`tests/test_house_settings.py` | 6 |
| `yurios/world/host/pages.py` | `tests/test_host.py` | 5 |
| `yurios/world/host/studio.py` | `tests/test_host.py`<br>`tests/test_studio_routes.py` | 9 |
| `yurios/world/host/switchboard.py` | `tests/test_host.py`<br>`tests/test_studio_routes.py` | 6 |
| `yurios/world/inbox.py` | **`tests/test_inbox.py`**<br>`tests/test_host.py`<br>`tests/test_studio_routes.py` | 23 |
| `yurios/world/main.py` | `tests/test_channels.py`<br>`tests/test_inbox.py`<br>`tests/test_pictures.py`<br>`tests/test_studio_routes.py`<br>`tests/test_voice_stack.py`<br>`tests/test_daemon.py` | 25 |
| `yurios/world/research.py` | **`tests/test_research.py`** | 4 |
| `yurios/world/rewire.py` | **`tests/test_rewire.py`**<br>`tests/test_host.py`<br>`tests/test_studio_routes.py` | 3 |
| `yurios/world/routes/camera.py` | `tests/test_gallery.py`<br>`tests/test_boot.py` | 2 |
| `yurios/world/routes/channels.py` | **`tests/test_channels.py`**<br>`tests/test_boot.py` | 2 |
| `yurios/world/routes/chat.py` | `tests/test_channels.py`<br>`tests/test_empty_reply.py`<br>`tests/test_pictures.py`<br>`tests/test_boot.py` | 7 |
| `yurios/world/routes/events.py` | **`tests/test_events.py`**<br>`tests/test_pictures.py`<br>`tests/test_boot.py` | 7 |
| `yurios/world/routes/gallery.py` | **`tests/test_gallery.py`**<br>`tests/test_boot.py` | 2 |
| `yurios/world/routes/health.py` | `tests/test_daemon.py`<br>`tests/test_mind_routes.py`<br>`tests/test_voice_stack.py`<br>`tests/test_context_meter.py`<br>`tests/test_channels.py` | 6 |
| `yurios/world/routes/inbox.py` | `tests/test_boot.py`<br>**`tests/test_inbox.py`** | 2 |
| `yurios/world/routes/live2d.py` | `tests/test_window.py` | 1 |
| `yurios/world/routes/mind.py` | `tests/test_mind_routes.py`<br>`tests/test_boot.py` | 3 |
| `yurios/world/routes/onboarding.py` | `tests/test_model_setup.py`<br>`tests/test_boot.py` | 2 |
| `yurios/world/routes/rate.py` | `tests/test_ratings.py`<br>`tests/test_boot.py` | 2 |
| `yurios/world/routes/uploads.py` | `tests/test_pictures.py`<br>`tests/test_boot.py` | 2 |
| `yurios/world/routes/voice_ws.py` | `tests/test_voice_ws_fork.py`<br>`tests/test_voice_replay.py`<br>`tests/test_voice_handshake.py`<br>`tests/test_voice_stack.py`<br>`tests/test_pictures.py`<br>`tests/test_voice_limits.py` | 7 |
| `yurios/world/runtime.py` | `tests/test_channels.py`<br>`tests/test_inbox.py`<br>`tests/test_studio_routes.py`<br>`tests/test_pictures.py`<br>`tests/test_voice_stack.py`<br>`tests/test_context_meter.py` | 22 |
| `yurios/world/selfies.py` | `tests/test_selfie.py`<br>`tests/test_channels.py`<br>`tests/test_inbox.py`<br>`tests/test_pictures.py`<br>`tests/test_voice_stack.py`<br>`tests/test_daemon.py` | 30 |
| `yurios/world/situation.py` | `tests/test_mind_hands.py`<br>`tests/test_mind_goals.py`<br>`tests/test_muse.py`<br>`tests/test_handed.py`<br>`tests/test_mind_scenarios.py`<br>`tests/test_mind_loop.py` | 26 |
| `yurios/world/tools/client.py` | `tests/test_mcp_contract.py`<br>`tests/test_multi_server.py`<br>`tests/test_mind_hands.py`<br>`tests/test_tool_loop.py`<br>`tests/test_studio_routes.py`<br>`tests/test_house_settings.py` | 15 |
| `yurios/world/tools/fakes.py` | `tests/test_mind_hands.py`<br>`tests/test_tool_loop.py`<br>`tests/test_compact_followups.py`<br>`tests/test_one_rule_hands.py`<br>`tests/test_selfie.py`<br>`tests/test_show_held_picture.py` | 17 |
| `yurios/world/tools/fetch.py` | **`tests/test_fetch.py`**<br>`tests/test_dreamjobs.py` | 6 |
| `yurios/world/tools/guard.py` | `tests/test_mind_hands.py`<br>`tests/test_tool_loop.py`<br>`tests/test_mind_goals.py`<br>`tests/test_mind_loop.py`<br>**`tests/test_guard.py`**<br>`tests/test_show_held_picture.py` | 43 |
| `yurios/world/tools/search.py` | **`tests/test_search.py`**<br>`tests/test_dreamjobs.py`<br>`tests/test_research.py`<br>`tests/test_mcp_contract.py` | 5 |
| `yurios/world/tools/server.py` | `tests/test_mcp_contract.py` | 1 |
| `yurios/world/tools/spawn_env.py` | `tests/test_mcp_contract.py`<br>`tests/test_studio_routes.py`<br>**`tests/test_spawn_env.py`** | 3 |
| `yurios/world/tools/timers.py` | **`tests/test_timers.py`**<br>`tests/test_mind_hands.py`<br>`tests/test_channels.py`<br>`tests/test_mind_goals.py`<br>`tests/test_mind_scenarios.py`<br>`tests/test_mind_loop.py` | 44 |
| `yurios/world/tooltags.py` | `tests/test_tool_tags.py`<br>`tests/test_tool_loop.py`<br>`tests/test_mind_hands.py`<br>`tests/test_selfie.py`<br>`tests/test_dreamjobs.py` | 24 |
| `yurios/world/tray.py` | `tests/test_host.py` | 13 |
| `yurios/world/turns.py` | `tests/test_channels.py`<br>`tests/test_empty_reply.py`<br>`tests/test_pictures.py`<br>`tests/test_draft_cadence.py`<br>`tests/test_text_style.py`<br>`tests/test_ratings.py` | 22 |
| `yurios/world/uploads.py` | `tests/test_pictures.py`<br>`tests/test_channels.py` | 21 |
| `yurios/world/voicestack.py` | `tests/test_voice_stack.py`<br>`tests/test_channels.py`<br>`tests/test_voice_ws_fork.py`<br>`tests/test_voice_replay.py`<br>`tests/test_pictures.py`<br>`tests/test_voice_handshake.py` | 23 |
| `yurios/world/vram.py` | **`tests/test_vram.py`**<br>`tests/test_mind_hands.py`<br>`tests/test_mind_goals.py`<br>`tests/test_channels.py`<br>`tests/test_mind_loop.py`<br>`tests/test_inbox.py` | 43 |
| `yurios/world/window.py` | **`tests/test_window.py`** | 1 |

## Declarations only (12)

Every line ran at import, none inside a test — Protocols, pydantic Config
schemas, ABCs. These are exercised by much of the suite; there is simply no
*statement* of theirs for a test to be executing when it happens.

- `yurios/app/config.py`
- `yurios/app/memory/summarise.py`
- `yurios/app/routes/greeting.py`
- `yurios/app/routes/health.py`
- `yurios/app/routes/rate.py`
- `yurios/app/routes/session.py`
- `yurios/desktop/avatar_models.py`
- `yurios/desktop/config.py`
- `yurios/desktop/voice/protocols.py`
- `yurios/world/brain_protocol.py`
- `yurios/world/channels/base.py`
- `yurios/world/config.py`

## Never executed (8)

Not one line, not even at import. Some are legitimately unreachable offline
(a GPU backend, a native window, a `__main__`); the rest are a list worth
shortening.

- `yurios/app/__main__.py`
- `yurios/app/providers/base.py`
- `yurios/chat/__main__.py`
- `yurios/desktop/__main__.py`
- `yurios/desktop/voice/backends/tts_qwen.py`
- `yurios/desktop/voice/backends/tts_sovits.py`
- `yurios/desktop/voice/backends/vad_silero.py`
- `yurios/world/__main__.py`
