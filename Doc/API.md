# AIChat API 前端接入文档

> 更新时间：2026-06-11  
> 面向：Web、iOS、Android 前端开发  
> 数据格式：除 SSE 外，所有请求与响应均为 JSON，字符编码为 UTF-8

## 1. 服务地址与版本

| 服务 | Base URL | 当前线上版本 |
| --- | --- | --- |
| LoginService | `https://aichat-login-kemznyglgb.cn-hangzhou.fcapp.run` | `2026.06.10-jwt.1` |
| ChatService | `https://aichat-chat-nitnspniec.cn-hangzhou.fcapp.run` | `2026.06.10-auth-chat.1` |

建议前端分别配置两个 Base URL，不要将它们拼接为同一个服务。

```ts
export const LOGIN_API_BASE =
  "https://aichat-login-kemznyglgb.cn-hangzhou.fcapp.run";

export const CHAT_API_BASE =
  "https://aichat-chat-nitnspniec.cn-hangzhou.fcapp.run";
```

## 2. 接入流程

1. 调用 LoginService `POST /send-code` 发送短信验证码。
2. 调用 LoginService `POST /login` 校验验证码并获取 JWT。
3. 将 JWT 安全保存在客户端。
4. 调用 ChatService 受保护接口时添加：

```http
Authorization: Bearer <accessToken>
```

5. 先调用 `GET /roles` 获取角色，再使用角色 ID 发起聊天、获取历史或清空聊天。

## 3. 通用约定

### 3.1 请求头

JSON 请求：

```http
Content-Type: application/json
```

需要登录的 ChatService 接口：

```http
Authorization: Bearer <accessToken>
```

### 3.2 通用成功响应

```json
{
  "success": true,
  "data": {}
}
```

部分简单接口没有 `data`，例如健康检查。

### 3.3 通用错误响应

```json
{
  "success": false,
  "code": "INVALID_INPUT",
  "message": "面向用户的中文错误说明",
  "details": {}
}
```

- 前端业务判断应使用稳定的 `code`。
- `message` 可直接用于用户提示，但不要依赖其文本进行逻辑判断。
- `details` 为可选字段。

### 3.4 HTTP 状态码

| 状态码 | 含义 |
| --- | --- |
| `200` | 请求成功 |
| `204` | CORS 预检成功 |
| `400` | 请求参数错误 |
| `401` | 未登录、Token 无效或已过期 |
| `403` | 用户被禁用 |
| `404` | 接口或聊天角色不存在 |
| `415` | 请求体不是 JSON |
| `429` | 短信发送频率受限 |
| `500` | 服务配置错误 |
| `502` | 短信、Qwen、天气等上游服务错误 |
| `503` | 数据库或聊天服务暂时不可用 |

### 3.5 认证错误处理

| `code` | 前端处理建议 |
| --- | --- |
| `AUTH_REQUIRED` | 清除本地登录状态并跳转登录页 |
| `INVALID_TOKEN` | 清除本地 Token 并重新登录 |
| `TOKEN_EXPIRED` | 提示登录过期，清除 Token 并重新登录 |
| `USER_DISABLED` | 提示账号不可用，禁止继续请求 |

当前没有刷新 Token 接口。Token 过期后需要重新进行短信登录。

---

## 4. LoginService

Base URL：

```text
https://aichat-login-kemznyglgb.cn-hangzhou.fcapp.run
```

### 4.1 获取短信验证码

```http
POST /send-code
Content-Type: application/json
```

无需 JWT。

#### 请求参数

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `countryCode` | `string` | 否 | 默认 `"86"`，当前仅支持中国大陆 |
| `phoneNumber` | `string` | 是 | 中国大陆手机号，可接受空格或短横线 |

#### 请求示例

```json
{
  "countryCode": "86",
  "phoneNumber": "13800138000"
}
```

#### 成功响应

```json
{
  "success": true,
  "message": "验证码已发送",
  "data": {
    "bizId": "provider-biz-id",
    "expiresIn": 300,
    "retryAfter": 60
  }
}
```

| 字段 | 说明 |
| --- | --- |
| `expiresIn` | 验证码有效秒数 |
| `retryAfter` | 建议再次发送前等待秒数 |

#### 常见错误

| `code` | 说明 |
| --- | --- |
| `INVALID_INPUT` | 手机号、国家代码等参数错误 |
| `BUSINESS_LIMIT_CONTROL` / `FREQUENCY_FAIL` | 发送过于频繁 |
| `SMS_PROVIDER_ERROR` | 阿里云短信服务调用失败 |
| `SERVER_CONFIG_ERROR` | 服务端短信配置错误 |

### 4.2 手机号验证码登录

```http
POST /login
Content-Type: application/json
```

无需 JWT。

#### 请求参数

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `countryCode` | `string` | 否 | 默认 `"86"` |
| `phoneNumber` | `string` | 是 | 中国大陆手机号 |
| `verifyCode` | `string` | 是 | 4 至 8 位数字或字母验证码 |

#### 请求示例

```json
{
  "countryCode": "86",
  "phoneNumber": "13800138000",
  "verifyCode": "1234"
}
```

#### 成功响应

```json
{
  "success": true,
  "message": "登录成功",
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

前端应保存：

- `accessToken`：调用 ChatService 时使用。
- `expiresIn`：单位为秒，默认 30 天。
- `user`：当前登录用户基础信息。

#### 常见错误

| `code` | 说明 |
| --- | --- |
| `INVALID_INPUT` | 手机号或验证码格式错误 |
| `INVALID_VERIFY_CODE` | 验证码错误或已过期 |
| `SMS_PROVIDER_ERROR` | 验证码服务调用失败 |
| `USER_DISABLED` | 用户已被禁用 |
| `DATABASE_ERROR` | 用户写入数据库失败 |
| `SERVER_CONFIG_ERROR` | JWT 或数据库配置错误 |

### 4.3 LoginService 健康检查

```http
GET /health
GET /health/config
GET /version
```

这些接口仅用于开发和运维检查，不应作为 App 核心业务流程。

---

## 5. ChatService

Base URL：

```text
https://aichat-chat-nitnspniec.cn-hangzhou.fcapp.run
```

### 5.1 获取聊天角色

```http
GET /roles
```

无需 JWT。

#### 成功响应

```json
{
  "success": true,
  "data": {
    "roles": [
      {
        "id": 1,
        "key": "naitang",
        "nickname": "奶糖",
        "description": "一只会撒娇、会贴贴、会蹭蹭人的猫咪系陪伴角色。",
        "avatarUrl": "https://example.com/avatar.jpg",
        "backgroundUrl": "https://example.com/background.jpg"
      }
    ]
  }
}
```

角色 Prompt 不会返回给前端。

### 5.2 LLM 聊天

```http
POST /chat
Authorization: Bearer <accessToken>
Content-Type: application/json
```

`POST /api/chat` 是相同功能的兼容路径。新前端统一使用 `/chat`。

#### 请求参数

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `roleId` | `number` | 是 | `GET /roles` 返回的正整数角色 ID |
| `message` | `string` | 条件必填 | 文本消息，默认最多 10000 字符 |
| `image` | `string` | 条件必填 | Base64 Data URL；与 `message` 至少提供一个 |
| `stream` | `boolean` | 否 | 默认 `true` |

客户端不能提交 `messages`、系统 Prompt 或模型名。

#### 图片约束

- 仅支持 JPEG、PNG、WebP。
- 必须使用 Base64 Data URL：

```text
data:image/jpeg;base64,/9j/4AAQ...
```

- 默认解码后最大 6MB。
- 图片不会持久化，也不会进入后续聊天上下文。
- 纯图片消息允许发送，历史记录中保存为 `[图片]`。

#### 请求示例

```json
{
  "roleId": 1,
  "message": "杭州今天天气怎么样？",
  "stream": true
}
```

图片请求：

```json
{
  "roleId": 2,
  "message": "帮我看看这张图片",
  "image": "data:image/jpeg;base64,...",
  "stream": true
}
```

#### 非流式成功响应

当 `stream=false`：

```json
{
  "success": true,
  "data": {
    "conversationId": 10,
    "roleId": 1,
    "userMessage": {
      "id": 20,
      "sender": "user",
      "content": "你好"
    },
    "assistantMessage": {
      "id": 21,
      "sender": "assistant",
      "content": "你好呀，喵。"
    },
    "usage": {
      "prompt_tokens": 100,
      "completion_tokens": 20,
      "total_tokens": 120
    }
  }
}
```

#### 流式 SSE 响应

当 `stream=true`，响应类型为：

```http
Content-Type: text/event-stream
```

事件顺序：

```text
event: start
data: {"conversationId":10,"roleId":1,"userMessage":{"id":20,"sender":"user","content":"你好"}}

event: delta
data: {"content":"你好"}

event: delta
data: {"content":"呀，喵。"}

event: done
data: {"assistantMessage":{"id":21,"sender":"assistant","content":"你好呀，喵。"},"usage":{"total_tokens":120}}
```

流开始后发生错误：

```text
event: error
data: {"code":"CHAT_SERVICE_ERROR","message":"聊天服务暂时不可用"}
```

注意：

- 天气工具调用由服务端处理，前端不会收到内部工具消息。
- `done` 事件表示完整助手回复已写入数据库。
- 收到 `error` 或网络中断时，不应将未完成文本当成持久化历史。

#### 浏览器 SSE 解析示例

`EventSource` 只支持 GET，不能用于当前 POST `/chat`。应使用 `fetch` 读取响应流：

```ts
async function streamChat(
  token: string,
  payload: {
    roleId: number;
    message?: string;
    image?: string;
    stream: true;
  },
  onDelta: (text: string) => void
) {
  const response = await fetch(`${CHAT_API_BASE}/chat`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify(payload),
  });

  if (!response.ok || !response.body) {
    throw new Error(`HTTP ${response.status}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let eventName = "";

  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    const blocks = buffer.split("\n\n");
    buffer = blocks.pop() || "";

    for (const block of blocks) {
      for (const line of block.split("\n")) {
        if (line.startsWith("event:")) {
          eventName = line.slice(6).trim();
        }
        if (!line.startsWith("data:")) continue;

        const data = JSON.parse(line.slice(5).trim());
        if (eventName === "delta") onDelta(data.content || "");
        if (eventName === "error") throw new Error(data.message || data.code);
      }
    }

    if (done) break;
  }
}
```

#### 常见错误

| `code` | 说明 |
| --- | --- |
| `AUTH_REQUIRED` | 缺少 JWT |
| `INVALID_TOKEN` | JWT 无效 |
| `TOKEN_EXPIRED` | JWT 已过期 |
| `USER_DISABLED` | 用户已禁用 |
| `INVALID_INPUT` | `roleId`、消息或图片不合法 |
| `ROLE_NOT_FOUND` | 角色不存在或已停用 |
| `DATABASE_ERROR` | 用户消息写入或历史加载失败 |
| `QWEN_API_ERROR` | Qwen 返回错误 |
| `QWEN_CONNECTION_ERROR` | 无法连接 Qwen |
| `INVALID_QWEN_RESPONSE` | Qwen 返回格式异常 |
| `TOOL_ROUND_LIMIT` | 天气工具调用次数超过限制 |
| `CHAT_SERVICE_ERROR` | 聊天服务异常 |

### 5.3 获取聊天历史

```http
GET /history?roleId=1&beforeId=<messageId>&limit=50
Authorization: Bearer <accessToken>
```

#### Query 参数

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `roleId` | 正整数 | 是 | 当前角色 ID |
| `beforeId` | 正整数 | 否 | 获取此消息 ID 之前的更早消息 |
| `limit` | 正整数 | 否 | 默认 `50`，最大 `100` |

#### 成功响应

```json
{
  "success": true,
  "data": {
    "conversationId": 10,
    "roleId": 1,
    "messages": [
      {
        "id": 20,
        "sender": "user",
        "content": "你好",
        "createdAt": "2026-06-11T02:00:00.000Z"
      },
      {
        "id": 21,
        "sender": "assistant",
        "content": "你好呀，喵。",
        "createdAt": "2026-06-11T02:00:01.000Z"
      }
    ],
    "hasMore": true,
    "nextBeforeId": 20
  }
}
```

- `messages` 按时间从旧到新排列。
- 首次请求不要传 `beforeId`。
- `hasMore=true` 时，将 `nextBeforeId` 作为下一次请求的 `beforeId`。
- 没有会话时，`conversationId` 和 `nextBeforeId` 为 `null`，`messages` 为空数组。

### 5.4 清空当前角色聊天

```http
DELETE /history?roleId=1
Authorization: Bearer <accessToken>
```

#### 成功响应

```json
{
  "success": true,
  "data": {
    "conversationId": 10,
    "roleId": 1,
    "deletedCount": 12
  }
}
```

只会清空当前 JWT 用户与指定角色的消息，不影响其他用户或角色。

### 5.5 ChatService 健康检查

```http
GET /health
GET /health/config
```

仅用于开发和运维检查。

---

## 6. TypeScript 类型参考

```ts
export interface ApiError {
  success: false;
  code: string;
  message: string;
  details?: unknown;
}

export interface User {
  id: number;
  countryCode: string;
  phoneNumber: string;
}

export interface LoginData {
  accessToken: string;
  tokenType: "Bearer";
  expiresIn: number;
  user: User;
}

export interface ChatRole {
  id: number;
  key: string;
  nickname: string;
  description: string;
  avatarUrl: string;
  backgroundUrl: string;
}

export type MessageSender = "user" | "assistant";

export interface ChatMessage {
  id: number;
  sender: MessageSender;
  content: string;
  createdAt?: string;
}

export interface HistoryData {
  conversationId: number | null;
  roleId: number;
  messages: ChatMessage[];
  hasMore: boolean;
  nextBeforeId: number | null;
}
```

## 7. 前端实现建议

- App 启动时可公开请求 `/roles`，无需等待登录。
- 收到 `401` 且错误码为认证错误时，统一清除 Token 并跳转登录。
- 登录成功后不要记录或打印完整 JWT、手机号和验证码。
- 图片转换 Base64 前先在客户端检查类型和大小，减少无效上传流量。
- 流式聊天过程中立即展示 `delta`；收到 `done` 后再用正式消息 ID 更新本地记录。
- 切换角色时使用该角色独立的历史列表和分页游标。
- 清空聊天属于破坏性操作，前端必须二次确认。
- 网络重试不要自动重复发送短信、聊天或清空历史，避免重复副作用。

## 8. 当前能力边界

- 当前仅支持中国大陆手机号登录。
- 当前没有刷新 Token、退出登录、注销账号接口。
- 当前每位用户对每个角色只有一份持续聊天。
- 当前不支持自定义角色和多会话列表。
- 图片不持久化，下一轮无法继续识别上一轮图片。
- 天气查询由聊天模型自动判断并调用，仅支持指定城市实时天气。

