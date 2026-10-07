import os

import pytest


@pytest.fixture(autouse=True)
def _isolated_posted_state(monkeypatch, tmp_path):
    """Дедупликация матчей (state.py) не должна читать/писать реальный файл
    проекта во время тестов — иначе прогоны тестов заражают друг друга и
    рабочую директорию."""
    monkeypatch.setenv("POSTED_STATE_FILE", os.path.join(tmp_path, "posted.json"))
