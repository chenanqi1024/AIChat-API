import jwt
import pymysql


def verified_sms_result():
    return {"Success": True, "Code": "OK", "Model": {"VerifyResult": "PASS"}}


def test_login_returns_jwt(login_app, jwt_environment, monkeypatch):
    monkeypatch.setattr(login_app, "call_aliyun_dypns", lambda *_args: verified_sms_result())
    monkeypatch.setattr(
        login_app,
        "upsert_user",
        lambda *_args: {
            "id": 7,
            "country_code": "86",
            "phone_number": "13800138000",
            "status": "active",
        },
    )

    response = login_app.app.test_client().post(
        "/login",
        json={
            "countryCode": "86",
            "phoneNumber": "13800138000",
            "verifyCode": "1234",
        },
    )

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["expiresIn"] == 2592000
    assert data["user"]["id"] == 7
    claims = jwt.decode(
        data["accessToken"],
        "test-secret-that-is-at-least-32-characters-long",
        algorithms=["HS256"],
        issuer="aichat-login",
        audience="aichat-chat",
    )
    assert claims["sub"] == "7"


def test_login_rejects_invalid_code_without_provider_call(login_app, monkeypatch):
    provider_called = False

    def provider(*_args):
        nonlocal provider_called
        provider_called = True

    monkeypatch.setattr(login_app, "call_aliyun_dypns", provider)
    response = login_app.app.test_client().post(
        "/login",
        json={"countryCode": "86", "phoneNumber": "13800138000", "verifyCode": "1"},
    )

    assert response.status_code == 400
    assert response.get_json()["code"] == "INVALID_INPUT"
    assert provider_called is False


def test_login_rejects_disabled_user(login_app, jwt_environment, monkeypatch):
    monkeypatch.setattr(login_app, "call_aliyun_dypns", lambda *_args: verified_sms_result())

    def disabled_user(*_args):
        raise PermissionError("disabled")

    monkeypatch.setattr(login_app, "upsert_user", disabled_user)
    response = login_app.app.test_client().post(
        "/login",
        json={"countryCode": "86", "phoneNumber": "13800138000", "verifyCode": "1234"},
    )

    assert response.status_code == 403
    assert response.get_json()["code"] == "USER_DISABLED"


def test_health_config_requires_database_and_jwt(login_app, monkeypatch):
    for name in (
        "ALIBABA_CLOUD_ACCESS_KEY_ID",
        "ALIBABA_CLOUD_ACCESS_KEY_SECRET",
        "DB_HOST",
        "DB_USER",
        "DB_PASSWORD",
        "DB_NAME",
        "JWT_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)

    response = login_app.app.test_client().get("/health/config")

    assert response.status_code == 503
    checks = response.get_json()["checks"]
    assert checks["databaseConfigured"] is False
    assert checks["databaseReachable"] is False
    assert checks["jwtConfigured"] is False
    assert checks["loginReady"] is False


def test_database_error_details_are_safe(login_app):
    details = login_app.database_error_details(
        pymysql.err.OperationalError(1045, "sensitive provider message")
    )

    assert details == {
        "code": "DB_AUTH_FAILED",
        "message": "数据库账号或密码错误，或账号来源未获授权",
        "mysqlErrorCode": 1045,
    }
    assert "sensitive" not in str(details)
