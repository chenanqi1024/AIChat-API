import base64
import binascii
import gzip
import json
import logging
import os
import re
from datetime import datetime
from functools import wraps
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import jwt
import pymysql
from flask import Flask, Response, g, jsonify, request, stream_with_context
from pymysql.cursors import DictCursor


app = Flask(__name__)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

QWEN_BASE_URL = os.getenv(
    "QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
).rstrip("/")
QWEN_CHAT_URL = f"{QWEN_BASE_URL}/chat/completions"
SERVICE_VERSION = "2026.06.10-auth-chat.1"
IMAGE_DATA_URL_PATTERN = re.compile(
    r"^data:(image/(?:jpeg|png|webp));base64,([A-Za-z0-9+/=\r\n]+)$"
)
WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_current_weather",
        "description": "查询指定城市或经纬度的实时天气。只有用户询问实时天气时才调用。",
        "parameters": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "城市名称、地区名称或经纬度，例如杭州、London、120.15,30.28",
                },
                "adm": {
                    "type": "string",
                    "description": "可选的上级行政区，用于区分同名城市",
                },
            },
            "required": ["location"],
        },
    },
}


class UpstreamError(Exception):
    def __init__(self, message, code="UPSTREAM_ERROR", details=None):
        super().__init__(message)
        self.code = code
        self.details = details


def env_int(name, default, minimum=None, maximum=None):
    raw_value = os.getenv(name)
    try:
        value = default if raw_value is None else int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} 必须是整数") from exc
    if minimum is not None and value < minimum:
        raise RuntimeError(f"{name} 必须大于等于 {minimum}")
    if maximum is not None and value > maximum:
        raise RuntimeError(f"{name} 必须小于等于 {maximum}")
    return value


def optional_env(name):
    value = os.getenv(name)
    return value.strip() if value and value.strip() else None


def required_env(name):
    value = optional_env(name)
    if not value:
        raise RuntimeError(f"缺少 {name}")
    return value


def json_error(message, status_code, code="BAD_REQUEST", details=None):
    body = {"success": False, "code": code, "message": message}
    if details is not None:
        body["details"] = details
    return jsonify(body), status_code


def require_json(handler):
    @wraps(handler)
    def wrapped(*args, **kwargs):
        if request.method != "OPTIONS" and not request.is_json:
            return json_error("请求体必须是 JSON", 415, "UNSUPPORTED_MEDIA_TYPE")
        return handler(*args, **kwargs)

    return wrapped


def open_database():
    return pymysql.connect(
        host=required_env("DB_HOST"),
        port=env_int("DB_PORT", 3306, minimum=1, maximum=65535),
        user=required_env("DB_USER"),
        password=required_env("DB_PASSWORD"),
        database=required_env("DB_NAME"),
        charset="utf8mb4",
        cursorclass=DictCursor,
        autocommit=False,
        connect_timeout=env_int("DB_CONNECT_TIMEOUT", 5, minimum=1, maximum=30),
        read_timeout=env_int("DB_READ_TIMEOUT", 10, minimum=1, maximum=60),
        write_timeout=env_int("DB_WRITE_TIMEOUT", 10, minimum=1, maximum=60),
    )


def jwt_settings():
    secret = required_env("JWT_SECRET")
    if len(secret) < 32:
        raise RuntimeError("JWT_SECRET 长度不能少于 32 个字符")
    return {
        "secret": secret,
        "issuer": os.getenv("JWT_ISSUER", "aichat-login"),
        "audience": os.getenv("JWT_AUDIENCE", "aichat-chat"),
    }


def authenticated_user():
    authorization = request.headers.get("Authorization", "")
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token.strip():
        return None, json_error("请先登录", 401, "AUTH_REQUIRED")

    try:
        settings = jwt_settings()
        claims = jwt.decode(
            token.strip(),
            settings["secret"],
            algorithms=["HS256"],
            audience=settings["audience"],
            issuer=settings["issuer"],
            options={"require": ["sub", "iss", "aud", "iat", "exp"]},
        )
        user_id = int(claims["sub"])
        if user_id <= 0:
            raise ValueError
    except jwt.ExpiredSignatureError:
        return None, json_error("登录已过期，请重新登录", 401, "TOKEN_EXPIRED")
    except (jwt.PyJWTError, ValueError, TypeError):
        return None, json_error("登录凭证无效", 401, "INVALID_TOKEN")
    except RuntimeError as exc:
        logger.exception("Invalid JWT configuration")
        return None, json_error(str(exc), 500, "SERVER_CONFIG_ERROR")

    try:
        connection = open_database()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, country_code, phone_number, status
                    FROM users
                    WHERE id = %s
                    """,
                    (user_id,),
                )
                user = cursor.fetchone()
        finally:
            connection.close()
    except RuntimeError as exc:
        logger.exception("Invalid database configuration")
        return None, json_error(str(exc), 500, "SERVER_CONFIG_ERROR")
    except pymysql.MySQLError:
        logger.exception("Failed to load authenticated user")
        return None, json_error("数据库暂时不可用", 503, "DATABASE_ERROR")

    if not user:
        return None, json_error("登录凭证无效", 401, "INVALID_TOKEN")
    if user["status"] != "active":
        return None, json_error("用户已被禁用", 403, "USER_DISABLED")
    return user, None


def require_auth(handler):
    @wraps(handler)
    def wrapped(*args, **kwargs):
        if request.method == "OPTIONS":
            return handler(*args, **kwargs)
        user, error = authenticated_user()
        if error is not None:
            return error
        g.current_user = user
        return handler(*args, **kwargs)

    return wrapped


def parse_positive_int(value, name, required=True, maximum=None):
    if value is None and not required:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} 必须是正整数")
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{name} 必须是正整数")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是正整数") from exc
    if parsed <= 0 or (maximum is not None and parsed > maximum):
        raise ValueError(f"{name} 必须是正整数")
    return parsed


def serialize_datetime(value):
    if isinstance(value, datetime):
        return value.isoformat(timespec="milliseconds") + "Z"
    return value


def role_public_data(role):
    return {
        "id": role["id"],
        "key": role["role_key"],
        "nickname": role["nickname"],
        "description": role["description"],
        "avatarUrl": role["avatar_url"],
        "backgroundUrl": role["background_url"],
    }


def get_role(role_id):
    connection = open_database()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, role_key, nickname, description, prompt,
                       avatar_url, background_url
                FROM chat_roles
                WHERE id = %s AND is_active = 1
                """,
                (role_id,),
            )
            return cursor.fetchone()
    finally:
        connection.close()


def create_user_message(user_id, role_id, content):
    connection = open_database()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO conversations (user_id, role_id)
                VALUES (%s, %s)
                ON DUPLICATE KEY UPDATE
                    id = LAST_INSERT_ID(id),
                    updated_at = UTC_TIMESTAMP(3)
                """,
                (user_id, role_id),
            )
            conversation_id = cursor.lastrowid
            cursor.execute(
                """
                INSERT INTO chat_messages (conversation_id, sender, content)
                VALUES (%s, 'user', %s)
                """,
                (conversation_id, content),
            )
            message_id = cursor.lastrowid
        connection.commit()
        return conversation_id, message_id
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def save_assistant_message(conversation_id, content):
    connection = open_database()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO chat_messages (conversation_id, sender, content)
                VALUES (%s, 'assistant', %s)
                """,
                (conversation_id, content),
            )
            message_id = cursor.lastrowid
            cursor.execute(
                """
                UPDATE conversations
                SET updated_at = UTC_TIMESTAMP(3)
                WHERE id = %s
                """,
                (conversation_id,),
            )
        connection.commit()
        return message_id
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def load_context(conversation_id, current_message_id, image):
    limit = env_int("CHAT_CONTEXT_MESSAGES", 30, minimum=1, maximum=100)
    connection = open_database()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, sender, content
                FROM chat_messages
                WHERE conversation_id = %s
                ORDER BY id DESC
                LIMIT %s
                """,
                (conversation_id, limit),
            )
            rows = list(reversed(cursor.fetchall()))
    finally:
        connection.close()

    messages = []
    for row in rows:
        content = row["content"]
        if row["id"] == current_message_id and image:
            text = content if content != "[图片]" else "请看看这张图片。"
            content = [
                {"type": "text", "text": text},
                {"type": "image_url", "image_url": {"url": image}},
            ]
        messages.append({"role": row["sender"], "content": content})
    return messages


def validate_image(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("image 必须是 Base64 Data URL 字符串")
    match = IMAGE_DATA_URL_PATTERN.fullmatch(value.strip())
    if not match:
        raise ValueError("image 仅支持 JPEG、PNG 或 WebP Base64 Data URL")
    encoded = match.group(2)
    max_bytes = env_int(
        "CHAT_MAX_IMAGE_BYTES", 6291456, minimum=1024, maximum=10485760
    )
    if len(encoded) > ((max_bytes + 2) // 3) * 4 + 8:
        raise ValueError(f"图片不能超过 {max_bytes} 字节")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("image Base64 数据无效") from exc
    if not decoded or len(decoded) > max_bytes:
        raise ValueError(f"图片不能超过 {max_bytes} 字节")
    mime_type = match.group(1)
    signatures_match = {
        "image/jpeg": decoded.startswith(b"\xff\xd8\xff"),
        "image/png": decoded.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/webp": decoded.startswith(b"RIFF")
        and len(decoded) >= 12
        and decoded[8:12] == b"WEBP",
    }
    if not signatures_match[mime_type]:
        raise ValueError("image 内容与声明的图片格式不一致")
    return value.strip()


def validate_chat_request(data):
    role_id = parse_positive_int(data.get("roleId"), "roleId")
    message = data.get("message")
    if message is not None and not isinstance(message, str):
        raise ValueError("message 必须是字符串")
    message = message.strip() if isinstance(message, str) else ""
    max_chars = env_int("CHAT_MAX_MESSAGE_CHARS", 10000, minimum=1, maximum=50000)
    if len(message) > max_chars:
        raise ValueError(f"message 不能超过 {max_chars} 个字符")
    image = validate_image(data.get("image"))
    if not message and not image:
        raise ValueError("message 和 image 至少提供一个")
    stream = data.get("stream", True)
    if not isinstance(stream, bool):
        raise ValueError("stream 必须是布尔值")
    return role_id, message, image, stream


def qwen_payload(messages, stream):
    model = os.getenv("QWEN_MODEL", "qwen3.7-plus")
    if not model.strip():
        raise RuntimeError("QWEN_MODEL 不能为空")
    allowed_models = [
        item.strip()
        for item in os.getenv("QWEN_ALLOWED_MODELS", "").split(",")
        if item.strip()
    ]
    if allowed_models and model not in allowed_models:
        raise RuntimeError("QWEN_MODEL 不在 QWEN_ALLOWED_MODELS 中")
    payload = {
        "model": model,
        "messages": messages,
        "tools": [WEATHER_TOOL],
        "tool_choice": "auto",
        "stream": stream,
    }
    if stream:
        payload["stream_options"] = {"include_usage": True}
    return payload


def open_qwen_response(messages, stream):
    api_key = required_env("DASHSCOPE_API_KEY")
    payload = qwen_payload(messages, stream)
    upstream_request = Request(
        QWEN_CHAT_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
            "User-Agent": "AIChat-ChatService/2.0",
        },
        method="POST",
    )
    timeout = env_int("QWEN_READ_TIMEOUT", 300, minimum=1, maximum=600)
    try:
        return urlopen(upstream_request, timeout=timeout)
    except HTTPError as exc:
        try:
            details = json.loads(exc.read().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            details = {"message": str(exc)}
        raise UpstreamError("Qwen 请求失败", "QWEN_API_ERROR", details) from exc
    except URLError as exc:
        raise UpstreamError(
            f"无法连接 Qwen 服务：{exc.reason}", "QWEN_CONNECTION_ERROR"
        ) from exc


def qweather_request(path, parameters):
    host = required_env("QWEATHER_API_HOST").rstrip("/")
    if not host.startswith(("http://", "https://")):
        host = f"https://{host}"
    url = f"{host}{path}?{urlencode(parameters)}"
    upstream_request = Request(
        url,
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-QW-Api-Key": required_env("QWEATHER_API_KEY"),
            "User-Agent": "AIChat-ChatService/2.0",
        },
    )
    timeout = env_int("QWEATHER_TIMEOUT", 10, minimum=1, maximum=60)
    try:
        with urlopen(upstream_request, timeout=timeout) as response:
            raw_body = response.read()
            content_encoding = response.headers.get("Content-Encoding", "")
    except HTTPError as exc:
        raw_body = exc.read()
        content_encoding = exc.headers.get("Content-Encoding", "")
    except URLError as exc:
        raise UpstreamError(
            f"无法连接和风天气服务：{exc.reason}", "QWEATHER_CONNECTION_ERROR"
        ) from exc

    try:
        if "gzip" in content_encoding.lower():
            raw_body = gzip.decompress(raw_body)
        result = json.loads(raw_body.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpstreamError("和风天气返回了无效响应", "INVALID_QWEATHER_RESPONSE") from exc
    if result.get("code") != "200":
        raise UpstreamError("和风天气请求失败", "QWEATHER_API_ERROR", result)
    return result


def get_current_weather(location, adm=None):
    if not isinstance(location, str) or not location.strip() or len(location) > 100:
        raise ValueError("location 必须是长度不超过 100 的非空字符串")
    parameters = {"location": location.strip(), "number": 1, "lang": "zh"}
    if isinstance(adm, str) and adm.strip():
        parameters["adm"] = adm.strip()[:100]
    lookup = qweather_request("/geo/v2/city/lookup", parameters)
    locations = lookup.get("location") or []
    if not locations:
        return {"success": False, "message": "未找到指定城市"}
    city = locations[0]
    weather = qweather_request(
        "/v7/weather/now", {"location": city["id"], "lang": "zh", "unit": "m"}
    )
    now = weather.get("now") or {}
    return {
        "success": True,
        "location": {
            "id": city.get("id"),
            "name": city.get("name"),
            "adm1": city.get("adm1"),
            "adm2": city.get("adm2"),
            "country": city.get("country"),
        },
        "observationTime": now.get("obsTime"),
        "weather": {
            "text": now.get("text"),
            "temperatureCelsius": now.get("temp"),
            "feelsLikeCelsius": now.get("feelsLike"),
            "humidityPercent": now.get("humidity"),
            "windDirection": now.get("windDir"),
            "windScale": now.get("windScale"),
            "precipitationMm": now.get("precip"),
            "visibilityKm": now.get("vis"),
        },
    }


def execute_tool_call(tool_call):
    function = tool_call.get("function") or {}
    if function.get("name") != "get_current_weather":
        return {"success": False, "message": "不支持该工具"}
    try:
        arguments = json.loads(function.get("arguments") or "{}")
        if not isinstance(arguments, dict):
            raise ValueError
    except (json.JSONDecodeError, ValueError):
        return {"success": False, "message": "天气工具参数无效"}
    try:
        return get_current_weather(arguments.get("location"), arguments.get("adm"))
    except (ValueError, RuntimeError, UpstreamError) as exc:
        logger.warning("Weather tool failed: %s", exc)
        return {"success": False, "message": str(exc)}


def normalize_tool_calls(tool_calls):
    normalized = []
    for index, tool_call in enumerate(tool_calls or []):
        normalized.append(
            {
                "id": tool_call.get("id") or f"weather_call_{index}",
                "type": "function",
                "function": {
                    "name": (tool_call.get("function") or {}).get("name", ""),
                    "arguments": (tool_call.get("function") or {}).get(
                        "arguments", "{}"
                    ),
                },
            }
        )
    return normalized


def append_tool_results(messages, assistant_message, tool_calls):
    messages.append(assistant_message)
    for tool_call in tool_calls:
        result = execute_tool_call(tool_call)
        messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call["id"],
                "content": json.dumps(result, ensure_ascii=False),
            }
        )


def run_non_stream_chat(messages):
    max_rounds = env_int("CHAT_TOOL_MAX_ROUNDS", 2, minimum=0, maximum=5)
    rounds = 0
    while True:
        upstream = open_qwen_response(messages, False)
        try:
            result = json.loads(upstream.read().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UpstreamError("Qwen 返回了无效响应", "INVALID_QWEN_RESPONSE") from exc
        finally:
            upstream.close()
        choices = result.get("choices") or []
        if not choices:
            raise UpstreamError("Qwen 未返回回复", "INVALID_QWEN_RESPONSE")
        message = choices[0].get("message") or {}
        tool_calls = normalize_tool_calls(message.get("tool_calls"))
        if tool_calls:
            if rounds >= max_rounds:
                raise UpstreamError("天气工具调用次数过多", "TOOL_ROUND_LIMIT")
            append_tool_results(
                messages,
                {
                    "role": "assistant",
                    "content": message.get("content"),
                    "tool_calls": tool_calls,
                },
                tool_calls,
            )
            rounds += 1
            continue
        content = message.get("content")
        if not isinstance(content, str) or not content:
            raise UpstreamError("Qwen 未返回有效文本", "INVALID_QWEN_RESPONSE")
        return content, result.get("usage")


def iter_qwen_chunks(upstream):
    for raw_line in upstream:
        try:
            line = raw_line.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise UpstreamError("Qwen 返回了无效流", "INVALID_QWEN_RESPONSE") from exc
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            yield json.loads(data)
        except json.JSONDecodeError as exc:
            raise UpstreamError("Qwen 返回了无效流", "INVALID_QWEN_RESPONSE") from exc


def merge_stream_tool_calls(accumulator, delta_calls):
    for delta_call in delta_calls or []:
        index = delta_call.get("index", 0)
        current = accumulator.setdefault(
            index,
            {
                "id": "",
                "type": "function",
                "function": {"name": "", "arguments": ""},
            },
        )
        if delta_call.get("id"):
            current["id"] = delta_call["id"]
        function = delta_call.get("function") or {}
        current["function"]["name"] += function.get("name") or ""
        current["function"]["arguments"] += function.get("arguments") or ""


def sse_event(event, data):
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n"


def base_messages(role, conversation_id, user_message_id, image):
    return [{"role": "system", "content": role["prompt"]}] + load_context(
        conversation_id, user_message_id, image
    )


def service_info():
    return {
        "success": True,
        "service": "ChatService",
        "version": SERVICE_VERSION,
        "message": "ChatService 已启动",
        "request": {"method": request.method, "path": request.path},
        "endpoints": {
            "health": "GET /health",
            "config": "GET /health/config",
            "roles": "GET /roles",
            "chat": "POST /chat",
            "history": "GET|DELETE /history",
        },
    }


@app.after_request
def add_response_headers(response):
    response.headers["Access-Control-Allow-Origin"] = os.getenv(
        "CORS_ALLOW_ORIGIN", "*"
    )
    response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    response.headers["X-Accel-Buffering"] = "no"
    response.headers["X-ChatService-Version"] = SERVICE_VERSION
    if response.mimetype == "text/event-stream":
        response.headers["Cache-Control"] = "no-cache, no-transform"
    else:
        response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/", methods=["GET", "POST", "OPTIONS"])
@app.route("/test", methods=["GET", "POST", "OPTIONS"])
def service_discovery():
    if request.method == "OPTIONS":
        return "", 204
    return jsonify(service_info())


@app.route("/health", methods=["GET"])
def health():
    return jsonify(
        {"success": True, "service": "ChatService", "version": SERVICE_VERSION}
    )


@app.route("/health/config", methods=["GET"])
def health_config():
    checks = {
        "dashscopeApiKeyConfigured": bool(optional_env("DASHSCOPE_API_KEY")),
        "databaseConfigured": all(
            optional_env(name)
            for name in ("DB_HOST", "DB_USER", "DB_PASSWORD", "DB_NAME")
        ),
        "jwtConfigured": bool(
            optional_env("JWT_SECRET") and len(optional_env("JWT_SECRET")) >= 32
        ),
        "qweatherConfigured": bool(
            optional_env("QWEATHER_API_HOST") and optional_env("QWEATHER_API_KEY")
        ),
        "authenticationRequired": True,
    }
    ready = all(
        checks[name]
        for name in (
            "dashscopeApiKeyConfigured",
            "databaseConfigured",
            "jwtConfigured",
            "qweatherConfigured",
        )
    )
    return (
        jsonify(
            {
                "success": ready,
                "service": "ChatService",
                "version": SERVICE_VERSION,
                "checks": checks,
                "qwenBaseUrl": QWEN_BASE_URL,
            }
        ),
        200 if ready else 503,
    )


@app.route("/roles", methods=["GET", "OPTIONS"])
def roles():
    if request.method == "OPTIONS":
        return "", 204
    try:
        connection = open_database()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, role_key, nickname, description,
                           avatar_url, background_url
                    FROM chat_roles
                    WHERE is_active = 1
                    ORDER BY sort_order, id
                    """
                )
                rows = cursor.fetchall()
        finally:
            connection.close()
        return jsonify({"success": True, "data": {"roles": [role_public_data(r) for r in rows]}})
    except RuntimeError as exc:
        return json_error(str(exc), 500, "SERVER_CONFIG_ERROR")
    except pymysql.MySQLError:
        logger.exception("Failed to load roles")
        return json_error("数据库暂时不可用", 503, "DATABASE_ERROR")


@app.route("/chat", methods=["POST", "OPTIONS"])
@app.route("/api/chat", methods=["POST", "OPTIONS"])
@require_auth
@require_json
def chat():
    if request.method == "OPTIONS":
        return "", 204
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return json_error("请求体必须是 JSON 对象", 400, "INVALID_INPUT")
    try:
        role_id, message, image, stream = validate_chat_request(data)
        role = get_role(role_id)
        if not role:
            return json_error("聊天角色不存在", 404, "ROLE_NOT_FOUND")
        stored_content = message or "[图片]"
        conversation_id, user_message_id = create_user_message(
            g.current_user["id"], role_id, stored_content
        )
        messages = base_messages(role, conversation_id, user_message_id, image)
    except ValueError as exc:
        return json_error(str(exc), 400, "INVALID_INPUT")
    except RuntimeError as exc:
        logger.exception("Invalid ChatService configuration")
        return json_error(str(exc), 500, "SERVER_CONFIG_ERROR")
    except pymysql.MySQLError:
        logger.exception("Failed to prepare chat")
        return json_error("数据库暂时不可用", 503, "DATABASE_ERROR")

    if not stream:
        try:
            content, usage = run_non_stream_chat(messages)
            assistant_message_id = save_assistant_message(conversation_id, content)
            return jsonify(
                {
                    "success": True,
                    "data": {
                        "conversationId": conversation_id,
                        "roleId": role_id,
                        "userMessage": {
                            "id": user_message_id,
                            "sender": "user",
                            "content": stored_content,
                        },
                        "assistantMessage": {
                            "id": assistant_message_id,
                            "sender": "assistant",
                            "content": content,
                        },
                        "usage": usage,
                    },
                }
            )
        except UpstreamError as exc:
            return json_error(str(exc), 502, exc.code, exc.details)
        except (RuntimeError, pymysql.MySQLError):
            logger.exception("Failed to complete chat")
            return json_error("聊天服务暂时不可用", 503, "CHAT_SERVICE_ERROR")

    @stream_with_context
    def generate():
        yield sse_event(
            "start",
            {
                "conversationId": conversation_id,
                "roleId": role_id,
                "userMessage": {
                    "id": user_message_id,
                    "sender": "user",
                    "content": stored_content,
                },
            },
        )
        try:
            max_rounds = env_int("CHAT_TOOL_MAX_ROUNDS", 2, minimum=0, maximum=5)
            rounds = 0
            while True:
                upstream = open_qwen_response(messages, True)
                content_parts = []
                tool_accumulator = {}
                usage = None
                emitted_content = False
                try:
                    for chunk in iter_qwen_chunks(upstream):
                        if chunk.get("usage") is not None:
                            usage = chunk["usage"]
                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        delta = choices[0].get("delta") or {}
                        if delta.get("tool_calls"):
                            if emitted_content:
                                raise UpstreamError(
                                    "Qwen 返回了无效工具调用流",
                                    "INVALID_QWEN_RESPONSE",
                                )
                            merge_stream_tool_calls(
                                tool_accumulator, delta["tool_calls"]
                            )
                        content = delta.get("content")
                        if isinstance(content, str) and content:
                            if tool_accumulator:
                                content_parts.append(content)
                            else:
                                emitted_content = True
                                content_parts.append(content)
                                yield sse_event("delta", {"content": content})
                finally:
                    upstream.close()

                tool_calls = normalize_tool_calls(
                    [tool_accumulator[index] for index in sorted(tool_accumulator)]
                )
                if tool_calls:
                    if rounds >= max_rounds:
                        raise UpstreamError(
                            "天气工具调用次数过多", "TOOL_ROUND_LIMIT"
                        )
                    append_tool_results(
                        messages,
                        {
                            "role": "assistant",
                            "content": "".join(content_parts) or None,
                            "tool_calls": tool_calls,
                        },
                        tool_calls,
                    )
                    rounds += 1
                    continue

                content = "".join(content_parts)
                if not content:
                    raise UpstreamError(
                        "Qwen 未返回有效文本", "INVALID_QWEN_RESPONSE"
                    )
                assistant_message_id = save_assistant_message(
                    conversation_id, content
                )
                yield sse_event(
                    "done",
                    {
                        "assistantMessage": {
                            "id": assistant_message_id,
                            "sender": "assistant",
                            "content": content,
                        },
                        "usage": usage,
                    },
                )
                return
        except UpstreamError as exc:
            logger.warning("Chat stream failed: %s", exc)
            yield sse_event("error", {"code": exc.code, "message": str(exc)})
        except (RuntimeError, pymysql.MySQLError, OSError):
            logger.exception("Chat stream failed")
            yield sse_event(
                "error",
                {"code": "CHAT_SERVICE_ERROR", "message": "聊天服务暂时不可用"},
            )

    return Response(generate(), status=200, mimetype="text/event-stream")


@app.route("/history", methods=["GET", "DELETE", "OPTIONS"])
@require_auth
def history():
    if request.method == "OPTIONS":
        return "", 204
    try:
        role_id = parse_positive_int(request.args.get("roleId"), "roleId")
        before_id = parse_positive_int(
            request.args.get("beforeId"), "beforeId", required=False
        )
        limit = parse_positive_int(
            request.args.get("limit", 50), "limit", maximum=100
        )
        role = get_role(role_id)
        if not role:
            return json_error("聊天角色不存在", 404, "ROLE_NOT_FOUND")

        connection = open_database()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id
                    FROM conversations
                    WHERE user_id = %s AND role_id = %s
                    """,
                    (g.current_user["id"], role_id),
                )
                conversation = cursor.fetchone()
                if not conversation:
                    if request.method == "DELETE":
                        return jsonify(
                            {
                                "success": True,
                                "data": {
                                    "conversationId": None,
                                    "roleId": role_id,
                                    "deletedCount": 0,
                                },
                            }
                        )
                    return jsonify(
                        {
                            "success": True,
                            "data": {
                                "conversationId": None,
                                "roleId": role_id,
                                "messages": [],
                                "hasMore": False,
                                "nextBeforeId": None,
                            },
                        }
                    )
                conversation_id = conversation["id"]

                if request.method == "DELETE":
                    cursor.execute(
                        "DELETE FROM chat_messages WHERE conversation_id = %s",
                        (conversation_id,),
                    )
                    deleted_count = cursor.rowcount
                    connection.commit()
                    return jsonify(
                        {
                            "success": True,
                            "data": {
                                "conversationId": conversation_id,
                                "roleId": role_id,
                                "deletedCount": deleted_count,
                            },
                        }
                    )

                parameters = [conversation_id]
                before_clause = ""
                if before_id is not None:
                    before_clause = "AND id < %s"
                    parameters.append(before_id)
                parameters.append(limit + 1)
                cursor.execute(
                    f"""
                    SELECT id, sender, content, created_at
                    FROM chat_messages
                    WHERE conversation_id = %s {before_clause}
                    ORDER BY id DESC
                    LIMIT %s
                    """,
                    tuple(parameters),
                )
                rows = cursor.fetchall()
        finally:
            connection.close()

        has_more = len(rows) > limit
        rows = rows[:limit]
        rows.reverse()
        messages = [
            {
                "id": row["id"],
                "sender": row["sender"],
                "content": row["content"],
                "createdAt": serialize_datetime(row["created_at"]),
            }
            for row in rows
        ]
        return jsonify(
            {
                "success": True,
                "data": {
                    "conversationId": conversation_id,
                    "roleId": role_id,
                    "messages": messages,
                    "hasMore": has_more,
                    "nextBeforeId": messages[0]["id"] if has_more else None,
                },
            }
        )
    except ValueError as exc:
        return json_error(str(exc), 400, "INVALID_INPUT")
    except RuntimeError as exc:
        logger.exception("Invalid ChatService configuration")
        return json_error(str(exc), 500, "SERVER_CONFIG_ERROR")
    except pymysql.MySQLError:
        logger.exception("Failed to access chat history")
        return json_error("数据库暂时不可用", 503, "DATABASE_ERROR")


@app.errorhandler(404)
def not_found(_error):
    return json_error(
        "接口不存在，请检查请求路径",
        404,
        "NOT_FOUND",
        {
            "method": request.method,
            "path": request.path,
            "availableRoutes": [
                "GET|POST /",
                "GET /health",
                "GET /health/config",
                "GET /roles",
                "POST /chat",
                "GET|DELETE /history",
            ],
        },
    )


@app.errorhandler(405)
def method_not_allowed(_error):
    return json_error(
        "请求方法不支持",
        405,
        "METHOD_NOT_ALLOWED",
        {"method": request.method, "path": request.path},
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9000, threaded=True)
