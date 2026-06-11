import base64
import gzip
import json
from datetime import datetime, timedelta, timezone

import jwt


ACTIVE_USER = {
    "id": 9,
    "country_code": "86",
    "phone_number": "13800138000",
    "status": "active",
}
ROLE = {
    "id": 1,
    "role_key": "naitang",
    "nickname": "奶糖",
    "description": "猫咪陪伴角色",
    "prompt": "你是奶糖。",
    "avatar_url": "https://example.com/avatar.jpg",
    "background_url": "https://example.com/background.jpg",
}


class FakeResponse:
    def __init__(self, body=b"", headers=None, lines=None):
        self.body = body
        self.headers = headers or {}
        self.lines = lines or []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def __iter__(self):
        return iter(self.lines)

    def read(self):
        return self.body

    def close(self):
        self.closed = True


class FakeCursor:
    def __init__(self, fetchone_values=None, fetchall_values=None, rowcount=0):
        self.fetchone_values = list(fetchone_values or [])
        self.fetchall_values = list(fetchall_values or [])
        self.rowcount = rowcount
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, *args):
        self.executed.append(args)
        return None

    def fetchone(self):
        return self.fetchone_values.pop(0) if self.fetchone_values else None

    def fetchall(self):
        return self.fetchall_values.pop(0) if self.fetchall_values else []


class FakeConnection:
    def __init__(self, cursor):
        self.fake_cursor = cursor
        self.committed = False
        self.closed = False

    def cursor(self):
        return self.fake_cursor

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


def authenticate(monkeypatch, chat_app):
    monkeypatch.setattr(chat_app, "authenticated_user", lambda: (ACTIVE_USER, None))


def test_protected_endpoint_requires_token(chat_app):
    response = chat_app.app.test_client().get("/history?roleId=1")
    assert response.status_code == 401
    assert response.get_json()["code"] == "AUTH_REQUIRED"


def test_expired_token_has_stable_error(chat_app, jwt_environment):
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "sub": "9",
            "iss": "aichat-login",
            "aud": "aichat-chat",
            "iat": now - timedelta(hours=2),
            "exp": now - timedelta(hours=1),
        },
        "test-secret-that-is-at-least-32-characters-long",
        algorithm="HS256",
    )
    response = chat_app.app.test_client().get(
        "/history?roleId=1", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 401
    assert response.get_json()["code"] == "TOKEN_EXPIRED"


def test_valid_token_loads_active_user(chat_app, jwt_environment, monkeypatch):
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "sub": "9",
            "iss": "aichat-login",
            "aud": "aichat-chat",
            "iat": now,
            "exp": now + timedelta(hours=1),
        },
        "test-secret-that-is-at-least-32-characters-long",
        algorithm="HS256",
    )
    cursor = FakeCursor(fetchone_values=[ACTIVE_USER])
    monkeypatch.setattr(chat_app, "open_database", lambda: FakeConnection(cursor))
    with chat_app.app.test_request_context(
        "/history", headers={"Authorization": f"Bearer {token}"}
    ):
        user, error = chat_app.authenticated_user()
    assert error is None
    assert user["id"] == 9


def test_valid_token_rejects_disabled_user(chat_app, jwt_environment, monkeypatch):
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "sub": "9",
            "iss": "aichat-login",
            "aud": "aichat-chat",
            "iat": now,
            "exp": now + timedelta(hours=1),
        },
        "test-secret-that-is-at-least-32-characters-long",
        algorithm="HS256",
    )
    disabled = dict(ACTIVE_USER, status="disabled")
    cursor = FakeCursor(fetchone_values=[disabled])
    monkeypatch.setattr(chat_app, "open_database", lambda: FakeConnection(cursor))
    with chat_app.app.test_request_context(
        "/history", headers={"Authorization": f"Bearer {token}"}
    ):
        user, error = chat_app.authenticated_user()
        response, status = error
        assert user is None
        assert status == 403
        assert response.get_json()["code"] == "USER_DISABLED"


def test_roles_are_public_and_do_not_expose_prompt(chat_app, monkeypatch):
    cursor = FakeCursor(fetchall_values=[[
        {
            key: ROLE[key]
            for key in (
                "id",
                "role_key",
                "nickname",
                "description",
                "avatar_url",
                "background_url",
            )
        }
    ]])
    monkeypatch.setattr(chat_app, "open_database", lambda: FakeConnection(cursor))

    response = chat_app.app.test_client().get("/roles")

    assert response.status_code == 200
    role = response.get_json()["data"]["roles"][0]
    assert role["nickname"] == "奶糖"
    assert "prompt" not in role


def test_non_stream_chat_uses_business_contract(chat_app, monkeypatch):
    authenticate(monkeypatch, chat_app)
    monkeypatch.setattr(chat_app, "get_role", lambda *_args: ROLE)
    monkeypatch.setattr(chat_app, "create_user_message", lambda *_args: (10, 20))
    monkeypatch.setattr(
        chat_app,
        "base_messages",
        lambda *_args: [{"role": "system", "content": "你是奶糖。"}],
    )
    monkeypatch.setattr(chat_app, "run_non_stream_chat", lambda *_args: ("你好呀，喵。", {"total_tokens": 8}))
    monkeypatch.setattr(chat_app, "save_assistant_message", lambda *_args: 30)

    response = chat_app.app.test_client().post(
        "/chat", json={"roleId": 1, "message": "你好", "stream": False}
    )

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["conversationId"] == 10
    assert data["assistantMessage"]["id"] == 30
    assert data["assistantMessage"]["content"] == "你好呀，喵。"


def test_chat_rejects_legacy_messages_contract(chat_app, monkeypatch):
    authenticate(monkeypatch, chat_app)
    response = chat_app.app.test_client().post(
        "/chat", json={"messages": [{"role": "user", "content": "你好"}]}
    )
    assert response.status_code == 400
    assert response.get_json()["code"] == "INVALID_INPUT"


def test_image_validation_accepts_data_url_and_rejects_bad_type(chat_app):
    valid = "data:image/png;base64," + base64.b64encode(
        b"\x89PNG\r\n\x1a\nfake-image-data"
    ).decode()
    assert chat_app.validate_image(valid) == valid

    try:
        chat_app.validate_image("data:image/gif;base64,aGVsbG8=")
    except ValueError as exc:
        assert "JPEG、PNG 或 WebP" in str(exc)
    else:
        raise AssertionError("GIF should be rejected")


def test_stream_chat_emits_business_sse(chat_app, monkeypatch):
    authenticate(monkeypatch, chat_app)
    monkeypatch.setattr(chat_app, "get_role", lambda *_args: ROLE)
    monkeypatch.setattr(chat_app, "create_user_message", lambda *_args: (10, 20))
    monkeypatch.setattr(chat_app, "base_messages", lambda *_args: [{"role": "system", "content": "prompt"}])
    monkeypatch.setattr(chat_app, "save_assistant_message", lambda *_args: 30)
    chunks = [
        'data: {"choices":[{"delta":{"content":"你"}}]}\n'.encode(),
        'data: {"choices":[{"delta":{"content":"好"}}]}\n'.encode(),
        b'data: {"choices":[],"usage":{"total_tokens":8}}\n',
        b"data: [DONE]\n",
    ]
    monkeypatch.setattr(chat_app, "open_qwen_response", lambda *_args: FakeResponse(lines=chunks))

    response = chat_app.app.test_client().post(
        "/chat", json={"roleId": 1, "message": "你好", "stream": True}
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "event: start" in body
    assert body.count("event: delta") == 2
    assert "event: done" in body
    assert '"content":"你好"' in body


def test_qweather_gzip_city_lookup_and_weather(chat_app, monkeypatch):
    monkeypatch.setenv("QWEATHER_API_HOST", "https://weather.example.com")
    monkeypatch.setenv("QWEATHER_API_KEY", "test-key")
    city = gzip.compress(
        json.dumps(
            {
                "code": "200",
                "location": [
                    {
                        "id": "101210101",
                        "name": "杭州",
                        "adm1": "浙江省",
                        "adm2": "杭州",
                        "country": "中国",
                    }
                ],
            }
        ).encode()
    )
    weather = gzip.compress(
        json.dumps(
            {
                "code": "200",
                "now": {
                    "obsTime": "2026-06-10T12:00+08:00",
                    "text": "晴",
                    "temp": "28",
                    "feelsLike": "30",
                    "humidity": "55",
                    "windDir": "东风",
                    "windScale": "2",
                    "precip": "0.0",
                    "vis": "20",
                },
            }
        ).encode()
    )
    responses = iter(
        [
            FakeResponse(city, {"Content-Encoding": "gzip"}),
            FakeResponse(weather, {"Content-Encoding": "gzip"}),
        ]
    )
    monkeypatch.setattr(chat_app, "urlopen", lambda *_args, **_kwargs: next(responses))

    result = chat_app.get_current_weather("杭州")

    assert result["success"] is True
    assert result["location"]["id"] == "101210101"
    assert result["weather"]["text"] == "晴"


def test_non_stream_weather_tool_call_is_hidden(chat_app, monkeypatch):
    first = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "get_current_weather",
                                "arguments": '{"location":"杭州"}',
                            },
                        }
                    ],
                }
            }
        ]
    }
    second = {
        "choices": [{"message": {"content": "杭州现在晴，28℃。"}}],
        "usage": {"total_tokens": 20},
    }
    responses = iter(
        [
            FakeResponse(json.dumps(first).encode()),
            FakeResponse(json.dumps(second).encode()),
        ]
    )
    monkeypatch.setattr(chat_app, "open_qwen_response", lambda *_args: next(responses))
    monkeypatch.setattr(
        chat_app,
        "execute_tool_call",
        lambda *_args: {"success": True, "weather": {"text": "晴"}},
    )
    messages = [{"role": "system", "content": "prompt"}]

    content, usage = chat_app.run_non_stream_chat(messages)

    assert content == "杭州现在晴，28℃。"
    assert usage["total_tokens"] == 20
    assert messages[-1]["role"] == "tool"


def test_history_pagination_and_clear(chat_app, monkeypatch):
    authenticate(monkeypatch, chat_app)
    monkeypatch.setattr(chat_app, "get_role", lambda *_args: ROLE)
    rows = [
        {"id": 3, "sender": "assistant", "content": "三", "created_at": datetime(2026, 6, 10)},
        {"id": 2, "sender": "user", "content": "二", "created_at": datetime(2026, 6, 10)},
        {"id": 1, "sender": "assistant", "content": "一", "created_at": datetime(2026, 6, 10)},
    ]
    get_cursor = FakeCursor(fetchone_values=[{"id": 10}], fetchall_values=[rows])
    monkeypatch.setattr(chat_app, "open_database", lambda: FakeConnection(get_cursor))

    response = chat_app.app.test_client().get("/history?roleId=1&limit=2")
    data = response.get_json()["data"]
    assert [message["id"] for message in data["messages"]] == [2, 3]
    assert data["hasMore"] is True
    assert data["nextBeforeId"] == 2
    assert get_cursor.executed[0][1] == (9, 1)

    delete_cursor = FakeCursor(fetchone_values=[{"id": 10}], rowcount=3)
    delete_connection = FakeConnection(delete_cursor)
    monkeypatch.setattr(chat_app, "open_database", lambda: delete_connection)
    response = chat_app.app.test_client().delete("/history?roleId=1")
    assert response.get_json()["data"]["deletedCount"] == 3
    assert delete_connection.committed is True
