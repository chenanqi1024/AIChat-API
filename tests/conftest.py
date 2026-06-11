import importlib.util
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_app_module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    module.app.config.update(TESTING=True)
    return module


@pytest.fixture(scope="session")
def login_app():
    return load_app_module("aichat_login_app", "LoginService/app.py")


@pytest.fixture(scope="session")
def chat_app():
    return load_app_module("aichat_chat_app", "ChatService/app.py")


@pytest.fixture
def jwt_environment(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret-that-is-at-least-32-characters-long")
    monkeypatch.setenv("JWT_ISSUER", "aichat-login")
    monkeypatch.setenv("JWT_AUDIENCE", "aichat-chat")
