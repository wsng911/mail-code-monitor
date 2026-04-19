# Mail-Monitor

监控 Gmail / QQ邮箱 / Outlook 收件箱，收到验证码自动推送 Telegram，支持转发完整邮件（HTML附件）。

全部账号使用 **IMAP IDLE** 长连接，新邮件实时推送，无需轮询。

Docker Hub: [wsng911/mail-monitor](https://hub.docker.com/r/wsng911/mail-monitor)

---

## 快速部署

```bash
mkdir -p /home/mail-monitor/config && cd /home/mail-monitor
nano config/config.yaml
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
    volumes:
      - ./config:/config
```

---

## config.yaml 示例

```yaml
telegram:
  bot_token: "your_bot_token"
  chat_id: "your_chat_id"

forward_all: false   # true = 转发所有邮件+HTML附件；false = 只推验证码

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
        client_id: ""
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

## Gmail 配置

> 需要开启两步验证才能使用应用专用密码

**第一步：开启 IMAP**
1. 打开 Gmail → 右上角齿轮 → 查看所有设置
2. 「转发和 POP/IMAP」→ 启用 IMAP → 保存

**第二步：生成应用专用密码**
1. 打开 [应用专用密码页面](https://myaccount.google.com/apppasswords)
2. 确认已开启两步验证
3. 选择「邮件」→ 生成，复制 16 位密码

```yaml
- type: gmail
  mailboxes:
    - label: 我的Gmail
      email: you@gmail.com
      app_pass: "xxxx xxxx xxxx xxxx"
```

---

## QQ邮箱 配置

**第一步：开启 IMAP 服务**
1. 登录 [QQ邮箱](https://mail.qq.com) → 设置 → 账户
2. 找到「IMAP/SMTP服务」→ 开启 → 手机短信验证

**第二步：获取授权码**
1. 开启服务后弹出授权码（16位字母）
2. 如需重新获取：账户页面 → 生成授权码

```yaml
- type: qq
  mailboxes:
    - label: 我的QQ邮箱
      email: 123456@qq.com
      app_pass: "xxxxxxxxxxxxxxxx"   # 授权码，非QQ密码
```

---

## Outlook / Hotmail 配置

微软已关闭基本认证，必须使用 OAuth2 `refresh_token` 连接 IMAP。

### 获取 refresh_token（一次性操作）

**第一步：浏览器打开授权链接**

```
https://login.microsoftonline.com/common/oauth2/v2.0/authorize?client_id=7feada80-d946-4d06-b134-73afa3524fb7&response_type=code&redirect_uri=http://localhost&scope=https://outlook.office.com/IMAP.AccessAsUser.All%20offline_access&prompt=consent
```

**第二步：获取 code**

授权后浏览器跳转到（页面无法打开是正常的）：
```
http://localhost/?code=M.C507_BAY...&session_state=xxx
```
复制 `code=` 后面的值（到 `&session_state` 为止）。

**第三步：换取 refresh_token**

```bash
curl -X POST https://login.microsoftonline.com/common/oauth2/v2.0/token \
  -d "client_id=7feada80-d946-4d06-b134-73afa3524fb7" \
  -d "grant_type=authorization_code" \
  -d "code=你的code" \
  -d "redirect_uri=http://localhost" \
  -d "scope=https://outlook.office.com/IMAP.AccessAsUser.All offline_access"
```

复制响应中的 `refresh_token` 值。

**填入配置：**

```yaml
- type: outlook
  mailboxes:
    - label: 我的Outlook
      email: you@hotmail.com
      refresh_token: "0.AXXX..."
      client_id: ""       # 留空使用内置默认值
      client_secret: ""   # 留空
```

> `refresh_token` 有效期约 90 天，程序运行期间自动续期。到期后重新执行上述步骤获取新的。

### 使用自建 Azure 应用（可选）

如需使用自己的 Azure 应用：

1. 打开 [Azure 门户](https://portal.azure.com) → 应用注册 → 新注册
2. 重定向 URI 类型选「移动和桌面应用程序」，填 `http://localhost`
3. 左侧「API 权限」→ 添加 `IMAP.AccessAsUser.All`、`offline_access`
4. 左侧「身份验证」→ 允许公共客户端流 → 开启
5. 将「应用程序(客户端) ID」填入 `client_id`

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

**`forward_all: true` 时额外发送 HTML 附件**，附件顶部包含发件人、收件人、时间，点开查看完整邮件。

---

## 常见问题

**Q: Gmail 登录失败**
- 使用应用专用密码，不是 Gmail 登录密码
- 确认 IMAP 已开启，两步验证已启用

**Q: QQ邮箱登录失败**
- `app_pass` 是授权码，不是 QQ 密码
- 授权码只显示一次，忘记需重新生成

**Q: Outlook token 刷新失败**
- `refresh_token` 已过期，重新执行授权步骤获取新的
- 确认 `scope` 包含 `IMAP.AccessAsUser.All`（不是 `Mail.Read`）

**Q: 收不到推送**
- 查看容器日志：`docker compose logs -f`
- 确认 `bot_token` 和 `chat_id` 正确
- 确认已给 bot 发过消息（bot 需要先被用户主动联系才能发消息）
