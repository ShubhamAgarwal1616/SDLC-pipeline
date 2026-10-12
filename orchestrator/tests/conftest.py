import os

import pytest


@pytest.fixture(autouse=True)
def no_env_models(monkeypatch):
    """Keep config hermetic: the developer's .env model choices never reach unit tests."""
    for name in [n for n in os.environ if n.startswith(("SDLC_MODEL_", "SDLC_ALLOWED_MODELS"))]:
        monkeypatch.delenv(name)
    monkeypatch.setattr("sdlc.secrets._dotenv", lambda: {})
