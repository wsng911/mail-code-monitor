# Mail-Monitor

监控 Gmail / QQ邮箱 / Outlook 收件箱，收到验证码自动推送 Telegram，支持转发完整邮件（HTML附件）。

- Gmail — 应用密码 IMAP 轮询 + OAuth Push（Pub/Sub 实时）
- QQ邮箱 — IMAP IDLE 长连接（实时）
- Outlook/Hotmail — OAuth2 Graph API + Change Notifications Push（实时）

Docker Hub: [wsng911/mail-monitor](https://hub.docker.com/r/wsng911/mail-monitor)

---

## 快速部署

```bash
mkdir -p /home/mail-monitor/config && cd /home/mail-monitor
nano config/config.yaml   # 填写配置
docker compose up -d
docker compose logs -f
```

`docker-compose.yml`：

```yaml
services:
  mail-monitor:
    image: wsng911/mail-monitor:v1
    container_name: mail-monitor
    restart: unless-stopped
    environment:
      - TZ=Asia/Shanghai
    ports:
      - "8080:8080"     # Outlook OAuth 回调端口
    volumes:
      - ./config:/config
```

---

## config.yaml 完整示例

```yaml
telegram:
  bot_token: "your_bot_token"
  chat_id: "your_chat_id"

poll_interval: 30       # 轮询间隔（秒），Push 账号不受此影响
forward_all: false      # true = 转发所有邮件+HTML附件；false = 只推验证码

# Outlook OAuth 回调服务（用于一键授权 + Change Notifications Push）
oauth:
  enabled: true
  client_id: "your_azure_client_id"
  client_secret: "your_azure_client_secret"   # 公共客户端留空
  redirect_uri: "https://your-domain.com/api/emails/oauth/outlook/callback"
  port: 8080

# Gmail Push 配置（可选，不填则使用 IMAP 轮询）
gmail_push:
  client_id: ""
  client_secret: ""
  pubsub_topic: ""

accounts:
  - type: gmail
    mailboxes:
      - label: 我的Gmail
        email: you@gmail.com
        app_pass: "xxxx xxxx xxxx xxxx"

  - type: qq
    mailboxes:
      - label: 我的QQ邮箱
        email: 123456@qq.com
        app_pass: "xxxxxxxxxxxxxxxx"

  - type: outlook
    mailboxes:
      - label: 我的Outlook
        email: you@hotmail.com
        refresh_token: "0.AXXX..."
        client_id: ""   # 留空使用内置默认值
```

---

## Telegram 配置

**获取 bot_token：**
1. Telegram 搜索 `@BotFather` → `/newbot` → 按提示创建
2. 创建完成后获得 `bot_token`

**获取 chat_id：**
1. 给你的 bot 发任意一条消息
2. 访问以下地址，在返回 JSON 里找 `message.chat.id`：
```
https://api.telegram.org/bot<你的bot_token>/getUpdates
```

---

## Gmail 配置（应用专用密码 + IMAP）

> 需要开启两步验证才能使用应用专用密码

**第一步：开启 Gmail IMAP**
1. 打开 Gmail → 右上角齿轮 → 查看所有设置
2. 选择「转发和 POP/IMAP」标签 → 启用 IMAP → 保存

**第二步：生成应用专用密码**
1. 打开 [Google 账号安全设置](https://myaccount.google.com/apppasswords)
2. 确认已开启「两步验证」
3. 选择应用「邮件」→ 生成，复制 16 位密码

```yaml
- type: gmail
  mailboxes:
    - label: 我的Gmail
      email: you@gmail.com
      app_pass: "xxxx xxxx xxxx xxxx"
```

---

## QQ邮箱 配置（授权码 + IMAP IDLE）

**第一步：开启 IMAP 服务**
1. 登录 [QQ邮箱](https://mail.qq.com) → 设置 → 账户
2. 开启「IMAP/SMTP服务」→ 手机短信验证

**第二步：获取授权码**
1. 开启服务后弹出授权码（16位字母）
2. 如需重新获取：账户页面 → 生成授权码

```yaml
- type: qq
  mailboxes:
    - label: 我的QQ邮箱
      email: 123456@qq.com
      app_pass: "xxxxxxxxxxxxxxxx"
```

> QQ邮箱使用 IMAP IDLE 长连接，新邮件实时推送，无需轮询。

---

## Outlook / Hotmail 配置

### 方案一：内置公共 client_id（简单，无需 Azure）

**第一步：浏览器打开授权链接**

```
https://login.microsoftonline.com/common/oauth2/v2.0/authorize?client_id=7feada80-d946-4d06-b134-73afa3524fb7&response_type=code&redirect_uri=http://localhost&scope=https://graph.microsoft.com/Mail.Read%20offline_access&prompt=consent
```

**第二步：获取 code**

授权后浏览器跳转到：
```
http://localhost/?code=M.C507_BAY...&session_state=xxx
```
复制 `code=` 后面的值（到 `&` 为止）。

**第三步：换取 refresh_token**

```bash
curl -X POST https://login.microsoftonline.com/common/oauth2/v2.0/token \
  -d "client_id=7feada80-d946-4d06-b134-73afa3524fb7" \
  -d "grant_type=authorization_code" \
  -d "code=你的code" \
  -d "redirect_uri=http://localhost" \
  -d "scope=https://graph.microsoft.com/Mail.Read offline_access"
```

复制响应中的 `refresh_token`。

```yaml
- type: outlook
  mailboxes:
    - label: 我的Outlook
      email: you@hotmail.com
      refresh_token: "0.AXXX..."
      client_id: ""
```

---

### 方案二：自建 Azure 应用 + Change Notifications Push（实时推送）

> 支持实时收信，无需轮询，推荐多账号场景使用。

#### 1. 注册 Azure 应用

1. 打开 [Azure 门户](https://portal.azure.com) → 搜索「应用注册」→ 新注册
2. 名称随意，受支持的账户类型选「任何组织目录中的账户和个人 Microsoft 账户」
3. 重定向 URI 类型选「移动和桌面应用程序」，填入：
   ```
   https://your-domain.com/api/emails/oauth/outlook/callback
   ```
4. 注册完成后记录「应用程序(客户端) ID」

#### 2. 配置 API 权限

1. 左侧「API 权限」→ 添加权限 → Microsoft Graph → 委托的权限
2. 添加：`Mail.Read`、`Mail.ReadWrite`、`User.Read`、`offline_access`
3. 点击「代表xxx授予管理员同意」

#### 3. 允许公共客户端流（无 client_secret 方案）

1. 左侧「身份验证」→ 高级设置
2. 「允许公共客户端流」→ 开启

#### 4. 填写配置

```yaml
oauth:
  enabled: true
  client_id: "你的应用ID"
  client_secret: ""   # 公共客户端留空
  redirect_uri: "https://your-domain.com/api/emails/oauth/outlook/callback"
  port: 8080
```

#### 5. 授权账号

启动容器后，浏览器访问：
```
https://your-domain.com/auth/outlook
```
登录 Outlook 账号完成授权，系统自动保存 token 并注册 Change Notifications 订阅。

> Change Notifications 订阅有效期 3 天，程序自动续期，无需手动操作。

---

## Gmail Push 配置（实时推送，可选）

> 使用 Google Cloud Pub/Sub 实现实时推送，替代 IMAP 轮询。

#### 1. 创建 Google Cloud 项目

1. 打开 [Google Cloud Console](https://console.cloud.google.com)
2. 创建新项目，启用 Gmail API 和 Cloud Pub/Sub API

#### 2. 创建 Pub/Sub Topic

1. 搜索「Pub/Sub」→ 主题 → 创建主题，名称如 `gmail-push`
2. 添加发布者权限：
   - 成员：`gmail-api-push@system.gserviceaccount.com`
   - 角色：`Pub/Sub 发布者`

#### 3. 创建 Pub/Sub 订阅

1. 订阅 → 创建订阅
2. 类型选「推送」，端点填：
   ```
   https://your-domain.com/api/gmail/push
   ```

#### 4. 创建 OAuth 客户端

1. 「API 和服务」→「凭据」→ 创建 OAuth 客户端 ID
2. 类型选「Web 应用」，授权重定向 URI 填：
   ```
   https://your-domain.com/api/gmail/oauth/callback
   ```

#### 5. 填写配置

```yaml
gmail_push:
  client_id: "your_google_client_id"
  client_secret: "your_google_client_secret"
  pubsub_topic: "projects/your-project/topics/gmail-push"
```

#### 6. 授权账号

启动容器后，浏览器访问：
```
https://your-domain.com/auth/gmail
```
登录 Gmail 账号完成授权，系统自动注册 Watch 并开始实时推送。

---

## 推送格式

**有验证码：**
```
`821543`

📬 我的Gmail
发件人: noreply@example.com
时间: 2026-04-16 10:08
主题: 验证您的邮箱地址
```

**forward_all: true 时额外发送 HTML 附件**，附件顶部包含发件人、收件人、时间等信息，点开即可查看完整邮件。

---

## 常见问题

**Q: Gmail 提示登录失败**
- 确认已开启两步验证并使用应用专用密码（非 Gmail 登录密码）
- 确认 IMAP 已在 Gmail 设置中开启

**Q: QQ邮箱登录失败**
- `app_pass` 填的是授权码，不是 QQ 密码
- 授权码只显示一次，忘记需重新生成

**Q: Outlook token 刷新失败**
- refresh_token 已过期（约 90 天），重新执行授权流程
- 使用自建 Azure 应用时，访问 `/auth/outlook` 重新授权即可

**Q: Change Notifications 收不到推送**
- 确认 `redirect_uri` 域名可从公网访问
- 确认端口 8080 已在防火墙开放
- 查看容器日志确认订阅是否注册成功
