"""The Live2D models Build #2 can wear (SPEC §6, §8.2).

One registry, two consumers:
  - scripts/fetch_avatar.py copies each model's runtime into
    web/vendor/<key>/runtime/ from a local Cubism SDK / AIRI checkout, and
  - routes/avatar resolves the AVATAR_MODEL key (.env, → desktop/config.py) to
    the model3.json URL the browser mounts.

Keys are lowercase; the default is Hiyori (the model Build #2 ships against, and
whose rig the §6 expression presets in web/avatar.js were tuned on). The other
rigs use the same standard Cubism parameter names where they can, and the ones
that can't (Mark/Rice/Wanko are minimal rigs) just lipsync — setParam() in
avatar.js swallows a missing parameter, so a sparse rig degrades, it doesn't
break. All are Live2D sample / free material, gitignored like the rest of
web/vendor/ — see fetch_avatar.py for the per-model license note.
"""
from __future__ import annotations

# key → model3.json path, relative to web/ (served at / by the StaticFiles mount)
MODELS: dict[str, str] = {
    "hiyori": "vendor/hiyori/runtime/hiyori_free_t08.model3.json",  # Hiyori Free (default)
    # The prettier, modern female rigs — fetched from Live2D's Sample Data
    # collection (scripts/fetch_avatar.py --samples-cdn). moc3 versions noted:
    "miara":  "vendor/miara/runtime/miara_pro_t03.model3.json",     # ♀ full-body, moc3 v3 (Cubism 4) — safest
    "kei":    "vendor/kei/runtime/kei_basic_free.model3.json",      # ♀ moc3 v5 (Cubism 5) — needs current Core
    "ren":    "vendor/ren/runtime/ren.model3.json",                 # ♀ Ren Foster, moc3 v6 (Cubism 5.3) — newest
    "haru":   "vendor/haru/runtime/Haru.model3.json",              # Cubism SDK sample
    "mao":    "vendor/mao/runtime/Mao.model3.json",                # Cubism SDK sample
    "mark":   "vendor/mark/runtime/Mark.model3.json",              # minimal rig (lipsync only)
    "natori": "vendor/natori/runtime/Natori.model3.json",          # Cubism SDK sample
    "rice":   "vendor/rice/runtime/Rice.model3.json",              # minimal rig
    "wanko":  "vendor/wanko/runtime/Wanko.model3.json",            # minimal rig (a dog)
}

DEFAULT = "hiyori"
