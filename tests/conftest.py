"""Every test reads bronze from its own temporary tree, never the user's data.

Komachi is the only answer to where the data is: it reads `ROOT_PATH` from its
settings file. Pointing that file at a temporary one is how a test is given a
root, the same way a user is. Autouse, so a test that forgets cannot silently
replay whatever happens to be in the real data lake.
"""

import pytest
from komachi import settings


@pytest.fixture(autouse=True)
def data_root(tmp_path, monkeypatch):
    root = tmp_path / "data"
    env_file = tmp_path / "kamakuraquantlab.env"
    env_file.write_text(f"ROOT_PATH={root}\n")
    monkeypatch.setattr(settings, "ENV_FILE", env_file)
    return root
