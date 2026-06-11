# AGENTS.md

## 项目定位

本项目是 AI 聊天 App 的后端，由两个可独立部署的 Flask 云函数组成：

- `LoginService`：负责中国大陆手机号短信验证码发送与校验、用户创建或更新，以及 JWT 签发。
- `ChatService`：负责 JWT 鉴权、预设角色、Qwen 聊天、图片理解、和风天气工具调用与聊天历史管理。

后续开发应围绕“安全、稳定、可观测、便于客户端接入”演进。除联调页和 API 文档外，不要在本仓库加入正式客户端 UI 或无关基础设施。

## 线上地址与版本事实

当前公网地址：

- 登录服务：`https://aichat-login-kemznyglgb.cn-hangzhou.fcapp.run`
- 聊天服务：`https://aichat-chat-nitnspniec.cn-hangzhou.fcapp.run`

本地源码与线上部署可能暂时不同，修改前必须区分：

- 本地 `LoginService/app.py` 当前版本：`2026.06.11-dbdiag.1`
- 线上 LoginService 当前已知版本：`2026.06.10-jwt.1`
- 本地与线上 ChatService 当前已知版本：`2026.06.10-auth-chat.1`

不要仅根据源码中的版本常量推断线上版本。更新线上联调页、README 或 API 文档中的部署事实前，应实际检查公网 `/health`、`/health/config`，必要时检查 ChatService 的 `/roles`。

## 目录职责

```text
.
├── ChatService/
│   ├── app.py
│   └── requirements.txt
├── LoginService/
│   ├── app.py
│   ├── requirements.txt
│   ├── login-test.html
│   └── test.html
├── Doc/
│   ├── API.md
│   └── API.html
├── database/
│   └── schema.sql
├── tests/
├── full-test.html
├── requirements-dev.txt
├── pytest.ini
├── README.md
└── AGENTS.md
```

- 两个服务均以各自目录中的 `app.py` 为默认入口，必须保持可独立部署。
- 不要让一个服务通过本地 Python import 依赖另一个服务目录。
- `database/schema.sql` 是 MySQL 8.0 表结构及预设角色初始化的事实来源。
- `Doc/API.md` 是供前端使用的详细 API 契约；`Doc/API.html` 是内容等价的可浏览版本。
- `full-test.html` 是线上 LoginService 与 ChatService 的完整链路联调页。
- `LoginService/login-test.html` 用于线上登录服务单独联调。
- `LoginService/test.html` 当前用于本地数据库诊断版 LoginService 联调。

## 开发原则

- 使用 Python 3，并遵循现有 Flask 与 Python 标准库风格。
- 优先做小范围、可验证的修改，不顺手重构无关代码。
- 新增依赖前先确认标准库或现有依赖无法合理完成需求。
- 公共逻辑出现真实重复后再抽取，不要过早增加共享层。
- 用户可见错误信息使用中文；协议字段、变量名、函数名和日志字段保持清晰一致。
- 密钥、AccessKey、JWT、验证码、数据库密码和 API Key 只能来自环境变量或密钥管理服务。
- 不得把真实敏感配置写入源码、测试页、文档、日志或提交记录。

## API 约定

JSON 接口必须验证 `Content-Type`、请求体类型、必填字段、字段类型和合理范围。

成功响应尽量保持：

```json
{
  "success": true,
  "data": {}
}
```

错误响应统一使用：

```json
{
  "success": false,
  "code": "STABLE_ERROR_CODE",
  "message": "面向用户的中文说明",
  "details": {}
}
```

- `code` 必须稳定并适合客户端判断，不要让客户端依赖 `message` 做逻辑分支。
- 输入错误使用 `4xx`，限流使用 `429`，上游或服务异常使用 `5xx`。
- 新增或修改接口时，同步维护服务发现信息、404 提示、README、`Doc/API.md`、`Doc/API.html`、相关联调页和自动化测试。
- 对兼容旧路径或旧字段的行为设置明确边界，避免无限扩展隐式兼容。

## 登录、JWT 与隐私

- LoginService 在验证码校验成功后按手机号创建或更新用户，并签发 JWT。
- ChatService 必须校验 JWT 的 HS256 签名、过期时间、签发方、受众和用户状态。
- 跨服务的 `JWT_SECRET`、`JWT_ISSUER`、`JWT_AUDIENCE` 必须保持一致。
- 日志不得记录完整手机号、验证码、JWT、Authorization 请求头或完整聊天内容。
- 登录与验证码接口必须考虑频率限制、重复发送、暴力尝试、可信代理边界和上游错误映射。
- `TRUST_PROXY_HEADERS` 仅可在请求确实经过受信任代理时开启。
- CORS 在生产环境中应通过环境变量配置明确来源，不能默认无条件信任所有来源。

## 聊天与 SSE

- `/roles` 是公开接口，但不得返回角色 Prompt。
- `/chat` 与 `/api/chat` 必须要求 JWT；客户端不能指定系统 Prompt、模型、完整历史或上游 URL。
- `/chat` 的流式协议是 SSE，保持 `text/event-stream`，并避免代理缓存或缓冲。
- SSE 应使用 `start`、`delta`、`done`、`error` 业务事件；流开始后不得改为普通 JSON 错误。
- 流式转发必须在完成或异常时关闭上游响应。
- 修改聊天逻辑时，同时验证流式与非流式路径。
- 对消息长度、图片格式与大小、上下文数量、工具调用轮数和输出 token 设置服务端限制。
- 图片仅用于当前请求，不落盘、不入库，也不进入后续聊天上下文。
- 天气只能依据和风天气工具结果回答；内部工具调用不得暴露给客户端。
- 不要向 API 文档或角色接口泄露角色 Prompt、天气工具内部消息或服务端密钥。

## 数据库与健康检查

- 使用 MySQL 8.0、InnoDB 和 `utf8mb4`；结构变化必须同步更新 `database/schema.sql`。
- 数据库查询必须参数化，禁止拼接用户输入。
- 事务失败必须回滚，连接和游标必须可靠关闭。
- LoginService 与 ChatService 应使用符合最小权限原则的独立数据库账号。
- 健康检查可执行安全的连接、读取或写入能力诊断，但不得修改业务数据或泄露数据库地址、账号、密码、原始异常和 SQL。
- 本地数据库诊断版 LoginService 的 `/health/config` 会进行真实数据库检查；较早的线上版本可能只检查环境变量是否存在。
- 健康检查行为变化时，必须同步更新 README、API 文档、联调页提示和测试。

## 文档与联调页

- `Doc/API.md` 是供前端实现使用的详细契约，必须覆盖认证、请求参数、响应、错误码、SSE、分页和调用示例。
- `Doc/API.html` 必须与 `Doc/API.md` 语义一致，不能只更新其中一份。
- README 面向项目开发与部署；API 文档面向前端接入。避免在两者中写互相冲突的事实。
- `full-test.html` 默认针对当前线上两个云函数，用于完整登录、角色、聊天、历史和清空链路。
- `LoginService/login-test.html` 默认针对线上登录服务；`LoginService/test.html` 当前针对本地数据库诊断版本。
- 联调页不得把手机号、验证码、JWT、图片内容或其他敏感数据持久化到浏览器存储。
- 页面加载或自动化验证时，不得自动发送短信、发起真实聊天或清空历史；有费用或破坏性的操作必须由用户明确点击确认。
- 修改 HTML 联调页后，应进行 JavaScript 语法检查，并在浏览器中验证关键交互和响应展示。

## 本地运行

建议创建虚拟环境并分别启动服务。直接执行两个 `app.py` 时都会监听 `9000`；本地同时联调时使用 Flask CLI 分配不同端口。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r LoginService/requirements.txt
flask --app LoginService/app.py run --port 9000
```

```bash
source .venv/bin/activate
pip install -r ChatService/requirements.txt
flask --app ChatService/app.py run --port 9001 --with-threads
```

进行真实短信、模型、天气或数据库请求前，需要配置对应环境变量。不要为了让本地启动成功而在源码中填写真实密钥。

## 测试与验证

自动化测试应使用 Flask 测试客户端，并 mock MySQL、短信、Qwen 和和风天气等所有外部服务。

最低验证范围：

- 两个服务能够导入并启动。
- `/health` 和 `/health/config` 返回符合当前版本的结果。
- JSON 类型错误、缺少字段、非法字段和边界值返回稳定错误码。
- 登录覆盖验证码发送成功与失败、首次创建与重复登录、JWT 声明及配置缺失。
- 鉴权覆盖缺少、篡改、过期 JWT 和禁用用户。
- 角色接口公开可访问、只返回启用角色且不泄露 Prompt。
- 聊天覆盖文本、纯图片、图文、流式、非流式、上游失败与流中断。
- 天气覆盖城市查询、Gzip 解压、实时天气、上游失败和工具调用轮数限制。
- 历史覆盖用户与角色隔离、游标分页、清空当前角色和图片不入库。

任何自动化测试都不得发送真实短信、调用真实 Qwen 或天气 API、删除线上聊天历史，也不得依赖开发者机器上的真实密钥。

## 交付检查清单

1. 行为符合当前需求，没有无关改动。
2. 输入校验、HTTP 状态码和稳定错误码保持一致。
3. 未引入或泄露任何密钥、令牌、验证码和个人信息。
4. 外部请求具有超时、异常处理和资源关闭逻辑。
5. 流式与非流式聊天路径均未被破坏。
6. 数据库结构变化已同步更新 SQL，事务和权限边界正确。
7. 已运行相关自动化测试；仅修改文档时至少完成链接和内容一致性检查。
8. 接口、配置、版本、部署事实或调用流程变化已同步更新 README、API 文档和相关联调页。
