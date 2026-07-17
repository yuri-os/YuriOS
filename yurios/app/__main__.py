"""Run the sanctuary: `python -m app` (SPEC §14).

Reads HOST/PORT from the environment / .env (via Config, §11) so you never
have to pass uvicorn flags. `uvicorn app.main:app --factory` still works and
its own --host/--port flags override these when you want them.
"""
from __future__ import annotations

import uvicorn

from yurios.app.config import Config


def main() -> None:
    cfg = Config()
    # import the factory by string so uvicorn owns the app lifecycle (and the
    # Vault/providers boot inside the server process, not at import — §14)
    uvicorn.run("yurios.app.main:app", factory=True, host=cfg.host, port=cfg.port)


if __name__ == "__main__":
    main()
