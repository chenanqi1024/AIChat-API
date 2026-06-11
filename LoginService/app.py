import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import jwt
import pymysql
from flask import Flask, jsonify, request
from pymysql.cursors import DictCursor


app = Flask(__name__)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

PHONE_PATTERN = re.compile(r"^1[3-9]\d{9}$")
CODE_PATTERN = re.compile(r"^[0-9A-Za-z]{4,8}$")
ALIYUN_API_VERSION = "2017-05-25"
SERVICE_VERSION = "2026.06.11-dbdiag.1"


class ProviderError(Exception):
    def __init__(self, message, code=None, request_id=None, status_code=None):
        super().__init__(message)
        self.code = code
        self.request_id = request_id
        self.status_code = status_code


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


def env_bool(name, default):
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


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
    if details:
        body["details"] = details
    return jsonify(body), status_code


def require_json(handler):
    @wraps(handler)
    def wrapped(*args, **kwargs):
        if request.method != "OPTIONS" and not request.is_json:
            return json_error("请求体必须是 JSON", 415, "UNSUPPORTED_MEDIA_TYPE")
        return handler(*args, **kwargs)

    return wrapped


def get_client_ip():
    candidates = []
    if env_bool("TRUST_PROXY_HEADERS", False):
        candidates.extend(request.headers.get("X-Forwarded-For", "").split(","))
        candidates.append(request.headers.get("X-Real-IP", ""))
    candidates.append(request.remote_addr or "")

    for candidate in candidates:
        value = candidate.strip()
        try:
            return str(ipaddress.ip_address(value))
        except ValueError:
            continue
    return None


def get_request_value(data, *names, default=None):
    for name in names:
        if name in data:
            return data[name]
    return default


def get_source_ip_parameter():
    configured_ip = optional_env("SMS_SOURCE_IP")
    if configured_ip:
        try:
            return str(ipaddress.ip_address(configured_ip))
        except ValueError as exc:
            raise RuntimeError("SMS_SOURCE_IP 必须是有效的 IPv4 或 IPv6 地址") from exc
    if env_bool("SMS_FORWARD_SOURCE_IP", True):
        client_ip = get_client_ip()
        if client_ip and ipaddress.ip_address(client_ip).is_global:
            return client_ip
    return None


def get_sms_scheme_name():
    return optional_env("SMS_SCHEME_NAME")


def build_sms_context_parameters(country_code, phone_number):
    parameters = {
        "CountryCode": country_code,
        "PhoneNumber": phone_number,
    }
    scheme_name = get_sms_scheme_name()
    if scheme_name:
        parameters["SchemeName"] = scheme_name
    source_ip = get_source_ip_parameter()
    if source_ip:
        parameters["SourceIp"] = source_ip
    return parameters


def normalize_phone(value, country_code):
    if not isinstance(value, str):
        raise ValueError("phoneNumber 必须是字符串")

    phone_number = re.sub(r"[\s-]", "", value)
    if country_code == "86" and phone_number.startswith("+86"):
        phone_number = phone_number[3:]

    if country_code != "86":
        raise ValueError("当前服务仅支持 countryCode=86")
    if not PHONE_PATTERN.fullmatch(phone_number):
        raise ValueError("手机号格式不正确")
    return phone_number


def mask_phone(value):
    if isinstance(value, str) and len(value) >= 7:
        return f"{value[:3]}****{value[-4:]}"
    return "<invalid>"


def percent_encode(value):
    return quote(str(value), safe="~")


def sign_aliyun_rpc_parameters(parameters, access_key_secret):
    canonicalized_query = "&".join(
        f"{percent_encode(key)}={percent_encode(parameters[key])}"
        for key in sorted(parameters)
    )
    string_to_sign = f"POST&%2F&{percent_encode(canonicalized_query)}"
    signature = hmac.new(
        f"{access_key_secret}&".encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha1,
    ).digest()
    return base64.b64encode(signature).decode("ascii")


def parse_provider_response(raw_body, status_code):
    try:
        result = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderError("阿里云返回了无效响应", status_code=status_code) from exc

    if status_code >= 400:
        raise ProviderError(
            result.get("Message") or result.get("message") or "阿里云请求失败",
            code=result.get("Code") or result.get("code"),
            request_id=result.get("RequestId") or result.get("requestId"),
            status_code=status_code,
        )
    return result


def call_aliyun_dypns(action, action_parameters):
    access_key_id = os.getenv("ALIBABA_CLOUD_ACCESS_KEY_ID")
    access_key_secret = os.getenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET")
    if not access_key_id or not access_key_secret:
        raise RuntimeError(
            "缺少 ALIBABA_CLOUD_ACCESS_KEY_ID 或 ALIBABA_CLOUD_ACCESS_KEY_SECRET"
        )

    parameters = {
        "AccessKeyId": access_key_id,
        "Action": action,
        "Format": "JSON",
        "SignatureMethod": "HMAC-SHA1",
        "SignatureNonce": uuid.uuid4().hex,
        "SignatureVersion": "1.0",
        "Timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "Version": ALIYUN_API_VERSION,
    }
    security_token = os.getenv("ALIBABA_CLOUD_SECURITY_TOKEN")
    if security_token:
        parameters["SecurityToken"] = security_token

    parameters.update(
        {key: value for key, value in action_parameters.items() if value is not None}
    )
    parameters["Signature"] = sign_aliyun_rpc_parameters(
        parameters, access_key_secret
    )

    endpoint = os.getenv("DYPNS_ENDPOINT", "dypnsapi.aliyuncs.com")
    api_url = endpoint if endpoint.startswith("http") else f"https://{endpoint}/"
    provider_request = Request(
        api_url,
        data=urlencode(parameters).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            "User-Agent": "AIChat-LoginService/1.0",
        },
        method="POST",
    )

    timeout = env_int("ALIYUN_REQUEST_TIMEOUT", 10, minimum=1, maximum=60)
    try:
        with urlopen(provider_request, timeout=timeout) as response:
            return parse_provider_response(response.read(), response.status)
    except HTTPError as exc:
        return parse_provider_response(exc.read(), exc.code)
    except URLError as exc:
        raise ProviderError(f"无法连接阿里云号码认证服务：{exc.reason}") from exc


def provider_error_details(exc):
    details = {}
    if getattr(exc, "code", None):
        details["providerCode"] = exc.code
    if getattr(exc, "request_id", None):
        details["requestId"] = exc.request_id
    if getattr(exc, "status_code", None):
        details["providerStatusCode"] = exc.status_code
    return details


def database_error_details(exc):
    mysql_error_code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
    mappings = {
        1044: ("DB_AUTH_FAILED", "数据库账号无权访问指定数据库"),
        1045: ("DB_AUTH_FAILED", "数据库账号或密码错误，或账号来源未获授权"),
        1049: ("DB_NOT_FOUND", "指定数据库不存在，请检查 DB_NAME"),
        1054: ("DB_SCHEMA_MISMATCH", "users 表结构与当前代码不一致"),
        1142: ("DB_PERMISSION_DENIED", "数据库账号缺少 users 表读写权限"),
        1146: ("DB_TABLE_MISSING", "指定数据库中不存在 users 表"),
        1290: ("DB_READ_ONLY", "当前数据库连接地址不允许写入"),
        1836: ("DB_READ_ONLY", "当前数据库连接处于只读状态"),
        2002: ("DB_CONNECTION_FAILED", "无法连接数据库，请检查地址、端口和网络"),
        2003: ("DB_CONNECTION_FAILED", "无法连接数据库，请检查地址、端口、白名单或 VPC"),
        2005: ("DB_HOST_NOT_FOUND", "无法解析 DB_HOST，请检查数据库地址"),
        3159: ("DB_SSL_REQUIRED", "数据库要求 SSL 连接，请配置数据库 SSL"),
    }
    code, message = mappings.get(
        mysql_error_code, ("DB_QUERY_FAILED", "数据库连接或查询检查失败")
    )
    return {"code": code, "message": message, "mysqlErrorCode": mysql_error_code}


def diagnose_database():
    result = {
        "databaseReachable": False,
        "usersTableReady": False,
        "usersWriteReady": False,
    }
    try:
        connection = open_database()
        try:
            result["databaseReachable"] = True
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, country_code, phone_number, status
                    FROM users
                    LIMIT 0
                    """
                )
                result["usersTableReady"] = True
                cursor.execute(
                    """
                    EXPLAIN INSERT INTO users
                        (country_code, phone_number, last_login_at)
                    VALUES ('86', '13000000000', UTC_TIMESTAMP(3))
                    """
                )
                result["usersWriteReady"] = True
        finally:
            connection.rollback()
            connection.close()
    except RuntimeError as exc:
        result["error"] = {"code": "DB_CONFIG_INVALID", "message": str(exc)}
    except pymysql.MySQLError as exc:
        result["error"] = database_error_details(exc)
    return result


def get_configuration_status():
    access_key_id = os.getenv("ALIBABA_CLOUD_ACCESS_KEY_ID")
    access_key_secret = os.getenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET")
    database_variables = ("DB_HOST", "DB_USER", "DB_PASSWORD", "DB_NAME")
    database_configured = all(optional_env(name) for name in database_variables)
    jwt_secret = optional_env("JWT_SECRET")
    jwt_configured = bool(jwt_secret and len(jwt_secret) >= 32)
    checks = {
        "aliyunCredentialsConfigured": bool(access_key_id and access_key_secret),
        "smsSignConfigured": bool(os.getenv("SMS_SIGN_NAME", "速通互联验证码")),
        "smsTemplateConfigured": bool(os.getenv("SMS_TEMPLATE_CODE", "100001")),
        "sourceIpForwardingEnabled": env_bool("SMS_FORWARD_SOURCE_IP", True),
        "databaseConfigured": database_configured,
        "jwtConfigured": jwt_configured,
    }
    issues = []

    if not checks["aliyunCredentialsConfigured"]:
        issues.append(
            "请配置 ALIBABA_CLOUD_ACCESS_KEY_ID 和 ALIBABA_CLOUD_ACCESS_KEY_SECRET"
        )
    if not database_configured:
        issues.append("请配置 DB_HOST、DB_USER、DB_PASSWORD 和 DB_NAME")
        database_status = {
            "databaseReachable": False,
            "usersTableReady": False,
            "usersWriteReady": False,
            "error": {
                "code": "DB_CONFIG_MISSING",
                "message": "数据库环境变量未完整配置",
            },
        }
    else:
        database_status = diagnose_database()
        if database_status.get("error"):
            issues.append(database_status["error"]["message"])
    if not jwt_configured:
        issues.append("请配置长度不少于 32 个字符的 JWT_SECRET")
    checks.update(
        {
            "databaseReachable": database_status["databaseReachable"],
            "usersTableReady": database_status["usersTableReady"],
            "usersWriteReady": database_status["usersWriteReady"],
        }
    )
    checks["loginReady"] = all(
        (
            checks["aliyunCredentialsConfigured"],
            checks["smsSignConfigured"],
            checks["smsTemplateConfigured"],
            checks["databaseReachable"],
            checks["usersTableReady"],
            checks["usersWriteReady"],
            checks["jwtConfigured"],
        )
    )

    return checks, issues, database_status


def service_info():
    return {
        "success": True,
        "service": "LoginService",
        "version": SERVICE_VERSION,
        "message": "LoginService 已启动",
        "request": {"method": request.method, "path": request.path},
        "endpoints": {
            "health": "GET /health",
            "config": "GET /health/config",
            "sendCode": "POST /send-code",
            "login": "POST /login",
        },
    }


def aliyun_failure_status(code):
    if code in {"MOBILE_NUMBER_ILLEGAL", "INVALID_PARAMETERS"}:
        return 400
    if code in {"BUSINESS_LIMIT_CONTROL", "FREQUENCY_FAIL"}:
        return 429
    return 502


def provider_request_id(result):
    return result.get("RequestId") or (result.get("Model") or {}).get("RequestId")


def provider_error_response(exc, fallback_message):
    return json_error(
        str(exc) or fallback_message,
        aliyun_failure_status(getattr(exc, "code", None)),
        "SMS_PROVIDER_ERROR",
        provider_error_details(exc),
    )


def open_database():
    ssl_options = None
    if env_bool("DB_SSL_ENABLED", False):
        ssl_options = {}
        ssl_ca = optional_env("DB_SSL_CA")
        if ssl_ca:
            ssl_options["ca"] = ssl_ca
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
        ssl=ssl_options,
    )


def upsert_user(country_code, phone_number):
    connection = open_database()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO users (country_code, phone_number, last_login_at)
                VALUES (%s, %s, UTC_TIMESTAMP(3))
                ON DUPLICATE KEY UPDATE
                    id = LAST_INSERT_ID(id),
                    last_login_at = VALUES(last_login_at),
                    updated_at = UTC_TIMESTAMP(3)
                """,
                (country_code, phone_number),
            )
            user_id = cursor.lastrowid
            cursor.execute(
                """
                SELECT id, country_code, phone_number, status
                FROM users
                WHERE id = %s
                """,
                (user_id,),
            )
            user = cursor.fetchone()
        if not user:
            raise RuntimeError("用户创建失败")
        if user["status"] != "active":
            connection.rollback()
            raise PermissionError("用户已被禁用")
        connection.commit()
        return user
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def jwt_settings():
    secret = required_env("JWT_SECRET")
    if len(secret) < 32:
        raise RuntimeError("JWT_SECRET 长度不能少于 32 个字符")
    return {
        "secret": secret,
        "issuer": os.getenv("JWT_ISSUER", "aichat-login"),
        "audience": os.getenv("JWT_AUDIENCE", "aichat-chat"),
        "expires_in": env_int(
            "JWT_EXPIRES_SECONDS", 2592000, minimum=300, maximum=31536000
        ),
    }


def issue_access_token(user_id):
    settings = jwt_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "iss": settings["issuer"],
        "aud": settings["audience"],
        "iat": now,
        "exp": now + timedelta(seconds=settings["expires_in"]),
    }
    token = jwt.encode(payload, settings["secret"], algorithm="HS256")
    return token, settings["expires_in"]


def dispatch_json_request():
    if not request.is_json:
        return None

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return None

    action = get_request_value(data, "action", "Action")
    normalized_action = str(action).replace("_", "-").lower() if action else ""
    if normalized_action in {
        "send-code",
        "sendcode",
        "sendsmsverifycode",
    }:
        return send_code()
    if normalized_action in {
        "login",
        "verify-code",
        "verifycode",
        "checksmsverifycode",
    }:
        return login()

    phone_number = get_request_value(data, "phoneNumber", "PhoneNumber", "phone_number")
    verify_code = get_request_value(data, "verifyCode", "VerifyCode", "verify_code")
    if phone_number is not None and verify_code is not None:
        return login()
    if phone_number is not None:
        return send_code()
    return None


@app.after_request
def add_response_headers(response):
    response.headers["Access-Control-Allow-Origin"] = os.getenv(
        "CORS_ALLOW_ORIGIN", "*"
    )
    response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-LoginService-Version"] = SERVICE_VERSION
    return response


@app.route("/", methods=["GET", "POST", "OPTIONS"])
@app.route("/test", methods=["GET", "POST", "OPTIONS"])
def service_discovery():
    if request.method == "OPTIONS":
        return "", 204
    if request.method == "POST":
        dispatched = dispatch_json_request()
        if dispatched is not None:
            return dispatched
    return jsonify(service_info())


@app.route("/health", methods=["GET"])
def health():
    return jsonify(
        {"success": True, "service": "LoginService", "version": SERVICE_VERSION}
    )


@app.route("/version", methods=["GET"])
def version():
    return jsonify(
        {"success": True, "service": "LoginService", "version": SERVICE_VERSION}
    )


@app.route("/health/config", methods=["GET"])
def health_config():
    checks, issues, database_status = get_configuration_status()
    return (
        jsonify(
            {
                "success": not issues,
                "service": "LoginService",
                "version": SERVICE_VERSION,
                "checks": checks,
                "issues": issues,
                "databaseStatus": database_status,
                "note": "此接口会真实检查数据库连接、users 表和写权限，但不验证阿里云 AccessKey 权限。",
            }
        ),
        200 if not issues else 503,
    )


@app.route("/send-code", methods=["POST", "OPTIONS"])
@app.route("/api/login/send-code", methods=["POST", "OPTIONS"])
@app.route("/SendSmsVerifyCode", methods=["POST", "OPTIONS"])
@require_json
def send_code():
    if request.method == "OPTIONS":
        return "", 204

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return json_error("请求体必须是 JSON 对象", 400, "INVALID_INPUT")

    country_code = str(
        get_request_value(
            data,
            "countryCode",
            "CountryCode",
            "country_code",
            default=os.getenv("SMS_COUNTRY_CODE", "86"),
        )
    )
    try:
        phone_number = normalize_phone(
            get_request_value(data, "phoneNumber", "PhoneNumber", "phone_number"),
            country_code,
        )
        valid_time = env_int("SMS_VALID_TIME", 300, minimum=60)
        interval = env_int("SMS_INTERVAL", 60, minimum=1)
        parameters = build_sms_context_parameters(country_code, phone_number)
        parameters.update(
            {
                "SignName": os.getenv("SMS_SIGN_NAME", "速通互联验证码"),
                "TemplateCode": os.getenv("SMS_TEMPLATE_CODE", "100001"),
                "TemplateParam": os.getenv(
                    "SMS_TEMPLATE_PARAM", '{"code":"##code##","min":"5"}'
                ),
                "CodeLength": env_int("SMS_CODE_LENGTH", 4, minimum=4, maximum=8),
                "CodeType": env_int("SMS_CODE_TYPE", 1, minimum=1, maximum=7),
                "ValidTime": valid_time,
                "Interval": interval,
                "DuplicatePolicy": env_int(
                    "SMS_DUPLICATE_POLICY", 1, minimum=1, maximum=2
                ),
                "AutoRetry": env_int("SMS_AUTO_RETRY", 1, minimum=0, maximum=1),
                "ReturnVerifyCode": "false",
                "OutId": uuid.uuid4().hex,
            }
        )
        result = call_aliyun_dypns("SendSmsVerifyCode", parameters)
    except ValueError as exc:
        return json_error(str(exc), 400, "INVALID_INPUT")
    except RuntimeError as exc:
        logger.exception("Invalid LoginService configuration")
        return json_error(str(exc), 500, "SERVER_CONFIG_ERROR")
    except ProviderError as exc:
        logger.exception(
            "Failed to send SMS verification code to %s from %s",
            mask_phone(
                get_request_value(data, "phoneNumber", "PhoneNumber", "phone_number")
            ),
            get_client_ip(),
        )
        return provider_error_response(exc, "验证码发送失败")

    if not result.get("Success") or result.get("Code") != "OK":
        code = result.get("Code", "SMS_SEND_FAILED")
        return json_error(
            result.get("Message", "验证码发送失败"),
            aliyun_failure_status(code),
            code,
            {"requestId": provider_request_id(result)},
        )

    model = result.get("Model") or {}
    return jsonify(
        {
            "success": True,
            "message": "验证码已发送",
            "data": {
                "bizId": model.get("BizId"),
                "expiresIn": valid_time,
                "retryAfter": interval,
            },
        }
    )


@app.route("/login", methods=["POST", "OPTIONS"])
@app.route("/verify-code", methods=["POST", "OPTIONS"])
@app.route("/api/login", methods=["POST", "OPTIONS"])
@app.route("/CheckSmsVerifyCode", methods=["POST", "OPTIONS"])
@require_json
def login():
    if request.method == "OPTIONS":
        return "", 204

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return json_error("请求体必须是 JSON 对象", 400, "INVALID_INPUT")

    country_code = str(
        get_request_value(
            data,
            "countryCode",
            "CountryCode",
            "country_code",
            default=os.getenv("SMS_COUNTRY_CODE", "86"),
        )
    )
    verify_code = get_request_value(data, "verifyCode", "VerifyCode", "verify_code")
    try:
        phone_number = normalize_phone(
            get_request_value(data, "phoneNumber", "PhoneNumber", "phone_number"),
            country_code,
        )
        if not isinstance(verify_code, str) or not CODE_PATTERN.fullmatch(verify_code):
            raise ValueError("验证码格式不正确")
    except ValueError as exc:
        return json_error(str(exc), 400, "INVALID_INPUT")

    try:
        parameters = build_sms_context_parameters(country_code, phone_number)
        parameters["VerifyCode"] = verify_code
        result = call_aliyun_dypns("CheckSmsVerifyCode", parameters)
    except RuntimeError as exc:
        logger.exception("Invalid Alibaba Cloud configuration")
        return json_error(str(exc), 500, "SERVER_CONFIG_ERROR")
    except ProviderError as exc:
        logger.exception(
            "Failed to check SMS verification code for %s from %s",
            mask_phone(
                get_request_value(data, "phoneNumber", "PhoneNumber", "phone_number")
            ),
            get_client_ip(),
        )
        return provider_error_response(exc, "验证码校验失败")

    if not result.get("Success") or result.get("Code") != "OK":
        code = result.get("Code", "SMS_CHECK_FAILED")
        return json_error(
            result.get("Message", "验证码校验失败"),
            aliyun_failure_status(code),
            code,
            {"requestId": provider_request_id(result)},
        )

    if (result.get("Model") or {}).get("VerifyResult") != "PASS":
        return json_error("验证码错误或已过期", 401, "INVALID_VERIFY_CODE")

    try:
        user = upsert_user(country_code, phone_number)
        access_token, expires_in = issue_access_token(user["id"])
    except PermissionError:
        return json_error("用户已被禁用", 403, "USER_DISABLED")
    except RuntimeError as exc:
        logger.exception("Invalid database or JWT configuration")
        return json_error(str(exc), 500, "SERVER_CONFIG_ERROR")
    except pymysql.MySQLError as exc:
        logger.exception("Failed to create or update user")
        details = database_error_details(exc)
        return json_error(details["message"], 503, "DATABASE_ERROR", details)

    return jsonify(
        {
            "success": True,
            "message": "登录成功",
            "data": {
                "accessToken": access_token,
                "tokenType": "Bearer",
                "expiresIn": expires_in,
                "user": {
                    "id": user["id"],
                    "countryCode": user["country_code"],
                    "phoneNumber": user["phone_number"],
                },
            },
        }
    )


@app.route("/<path:path>", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])
def compatibility_route(path):
    if request.method == "OPTIONS":
        return "", 204

    if request.method == "POST":
        dispatched = dispatch_json_request()
        if dispatched is not None:
            return dispatched

    info = service_info()
    info["warning"] = "请求路径未匹配具体接口，已返回服务说明"
    info["requestedPath"] = request.path
    info["consoleTestExamples"] = [
        {"method": "GET", "path": "/health/config"},
        {
            "method": "POST",
            "path": "/send-code",
            "body": {"countryCode": "86", "phoneNumber": "<你的手机号>"},
        },
    ]
    return jsonify(info)


@app.errorhandler(405)
def method_not_allowed(_error):
    return json_error(
        "请求方法不支持",
        405,
        "METHOD_NOT_ALLOWED",
        {"method": request.method, "path": request.path},
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9000)
