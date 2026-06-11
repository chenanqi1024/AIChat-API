# AIChat API

AIChat App 后端由两个可独立部署的 Flask 云函数组成：

- `LoginService`：发送短信验证码、校验验证码、创建用户并签发 JWT。
- `ChatService`：提供预设角色、JWT 鉴权聊天、聊天历史、图片理解和实时天气查询。

每个服务目录包含该云函数部署所需的全部代码，默认入口是 `app.py`，默认监听端口为 `9000`。

## 公网地址

- LoginService：`https://aichat-login-kemznyglgb.cn-hangzhou.fcapp.run`
- ChatService：`https://aichat-chat-nitnspniec.cn-hangzhou.fcapp.run`

## 项目结构

```text
.
├── ChatService/
│   ├── app.py
│   └── requirements.txt
├── LoginService/
│   ├── app.py
│   ├── requirements.txt
│   └── test.html
├── database/
│   └── schema.sql
├── tests/
├── AGENTS.md
├── README.md
└── requirements-dev.txt
```

## 初始化数据库

项目使用 MySQL 8.0、InnoDB 和 `utf8mb4`。在已创建的数据库中执行：

```bash
mysql -h <DB_HOST> -u <DB_USER> -p <DB_NAME> < database/schema.sql
```

`database/schema.sql` 会创建：

- `users`：App 用户。
- `chat_roles`：四个预设角色及 Prompt。
- `conversations`：每位用户、每个角色唯一的一份聊天。
- `chat_messages`：用户与助手的纯文本聊天历史。

脚本会幂等初始化奶糖、晚晴、曜川、小芙四个角色。重复执行会更新角色资料，不会重复插入。

建议为两个云函数使用不同的数据库账号：

- LoginService：允许读取、插入和更新 `users`。
- ChatService：允许读取 `users`、读取 `chat_roles`、读写 `conversations` 和 `chat_messages`。

## 环境变量

### 两个服务共用

| 变量 | 必需 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `DB_HOST` | 是 | - | MySQL 地址 |
| `DB_PORT` | 否 | `3306` | MySQL 端口 |
| `DB_USER` | 是 | - | MySQL 用户 |
| `DB_PASSWORD` | 是 | - | MySQL 密码 |
| `DB_NAME` | 是 | - | MySQL 数据库名 |
| `JWT_SECRET` | 是 | - | HS256 密钥，至少 32 个字符；两个服务必须一致 |
| `JWT_ISSUER` | 否 | `aichat-login` | JWT 签发方；两个服务必须一致 |
| `JWT_AUDIENCE` | 否 | `aichat-chat` | JWT 受众；两个服务必须一致 |
| `CORS_ALLOW_ORIGIN` | 否 | `*` | 允许的跨域来源，生产环境应明确配置 |
| `LOG_LEVEL` | 否 | `INFO` | 日志级别 |

数据库超时可通过 `DB_CONNECT_TIMEOUT`、`DB_READ_TIMEOUT`、`DB_WRITE_TIMEOUT` 配置。
如果 RDS 强制使用 SSL，可配置 `DB_SSL_ENABLED=true`；如需指定 CA 文件，再配置云函数内可访问的 `DB_SSL_CA` 路径。

### LoginService

| 变量 | 必需 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `ALIBABA_CLOUD_ACCESS_KEY_ID` | 是 | - | 阿里云 AccessKey ID |
| `ALIBABA_CLOUD_ACCESS_KEY_SECRET` | 是 | - | 阿里云 AccessKey Secret |
| `JWT_EXPIRES_SECONDS` | 否 | `2592000` | JWT 有效期，默认 30 天 |

短信相关可选配置包括 `SMS_SIGN_NAME`、`SMS_TEMPLATE_CODE`、`SMS_TEMPLATE_PARAM`、`SMS_VALID_TIME`、`SMS_INTERVAL`、`SMS_SOURCE_IP`、`SMS_FORWARD_SOURCE_IP` 和 `SMS_SCHEME_NAME`。

`TRUST_PROXY_HEADERS` 默认为关闭。仅当云函数前方代理可信时开启。

### ChatService

| 变量 | 必需 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `DASHSCOPE_API_KEY` | 是 | - | DashScope API Key |
| `QWEATHER_API_HOST` | 是 | - | 和风天气 API Host |
| `QWEATHER_API_KEY` | 是 | - | 和风天气 API KEY |
| `QWEN_MODEL` | 否 | `qwen3.7-plus` | 支持图片和 Function Calling 的模型 |
| `CHAT_CONTEXT_MESSAGES` | 否 | `30` | 每轮发送给模型的最近历史条数 |
| `CHAT_MAX_MESSAGE_CHARS` | 否 | `10000` | 单条用户文本最大字符数 |
| `CHAT_MAX_IMAGE_BYTES` | 否 | `6291456` | 图片解码后最大字节数 |
| `CHAT_TOOL_MAX_ROUNDS` | 否 | `2` | 单轮最大天气工具调用轮数 |

还可配置 `QWEN_BASE_URL`、`QWEN_ALLOWED_MODELS`、`QWEN_READ_TIMEOUT` 和 `QWEATHER_TIMEOUT`。

## API

面向前端的完整接入文档：

- `Doc/API.md`
- `Doc/API.html`

错误响应统一使用：

```json
{
  "success": false,
  "code": "STABLE_ERROR_CODE",
  "message": "中文错误说明"
}
```

### 获取短信验证码

```http
POST /send-code
Content-Type: application/json
```

```json
{
  "countryCode": "86",
  "phoneNumber": "13800138000"
}
```

### 手机号验证码登录

```http
POST /login
Content-Type: application/json
```

```json
{
  "countryCode": "86",
  "phoneNumber": "13800138000",
  "verifyCode": "1234"
}
```

成功后返回 30 天有效的 `accessToken`：

```json
{
  "success": true,
  "data": {
    "accessToken": "<JWT>",
    "tokenType": "Bearer",
    "expiresIn": 2592000,
    "user": {
      "id": 1,
      "countryCode": "86",
      "phoneNumber": "13800138000"
    }
  }
}
```

### 获取聊天角色

该接口不需要 JWT，也不会返回角色 Prompt。

```http
GET /roles
```

### LLM 聊天

```http
POST /chat
Authorization: Bearer <JWT>
Content-Type: application/json
```

```json
{
  "roleId": 1,
  "message": "杭州今天天气怎么样？",
  "image": "data:image/jpeg;base64,...",
  "stream": true
}
```

- `message` 与 `image` 至少提供一个。
- 图片只支持 JPEG、PNG、WebP Base64 Data URL，且不会持久化。
- 客户端不能提交系统 Prompt、完整历史或模型名。
- `/api/chat` 是采用相同请求格式的兼容路径。

默认返回业务化 SSE：

```text
event: start
data: {"conversationId":1,"roleId":1,"userMessage":{"id":1,...}}

event: delta
data: {"content":"今天"}

event: done
data: {"assistantMessage":{"id":2,...},"usage":{...}}
```

流开始后的异常使用 `event: error` 返回。不完整的助手回复不会写入数据库。

### 获取聊天历史

```http
GET /history?roleId=1&beforeId=<可选消息ID>&limit=50
Authorization: Bearer <JWT>
```

- 默认返回最近 50 条，最大 100 条。
- 使用响应中的 `nextBeforeId` 加载更早消息。

### 清空当前聊天

```http
DELETE /history?roleId=1
Authorization: Bearer <JWT>
```

只会清空当前用户与指定角色的消息。

## 本地运行与测试

安装开发依赖：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

两个服务直接运行时都会监听 `9000`。同时联调可使用不同端口：

```bash
flask --app LoginService/app.py run --port 9000
flask --app ChatService/app.py run --port 9001 --with-threads
```

运行自动化测试：

```bash
pytest -q
```

测试会 mock MySQL、短信、Qwen 与和风天气，不会调用真实外部服务。

浏览器手工联调页面：

- `full-test.html`：完整测试两个线上云函数，包括短信登录、JWT、角色、聊天、图片、天气、历史与清空聊天。
- `LoginService/login-test.html`：专门测试线上 LoginService 的配置检查、短信验证码、登录与 JWT 解码。
- `LoginService/test.html`：在当前浏览器会话中保存登录 JWT，并使用两个公网地址测试完整登录与聊天流程。

LoginService 的 `/health/config` 会真实连接数据库，并检查 `users` 表读取与写入权限。数据库异常只返回安全的错误分类和 MySQL 错误码，不会暴露密码。

## 安全说明

- 密钥、JWT、验证码和数据库密码只能通过环境变量或密钥管理服务配置。
- 聊天服务固定校验 JWT 签名、过期时间、签发方、受众和用户状态。
- 手机号异常日志会脱敏；不要记录 JWT、验证码、完整聊天内容或图片。
- 图片仅在当前模型请求期间使用，不落盘、不入库，也不会进入后续聊天上下文。
- 天气查询仅通过服务端 Function Calling 调用，不向客户端暴露内部工具消息。
