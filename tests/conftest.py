import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sample_data")


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    monkeypatch.setenv("TREASURER_DATA_DIR", str(tmp_path))
    from treasurer import create_app
    return create_app({"TESTING": True, "SECRET_KEY": "test"})


@pytest.fixture
def client(app):
    return app.test_client()


def sample(name):
    with open(os.path.join(SAMPLES, name), "rb") as f:
        return f.read()
