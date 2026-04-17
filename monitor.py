"""
邮箱验证码监控 - 多账号，支持 Gmail(应用密码/Push) + Outlook(OAuth2)
"""
import os, re, time, imaplib, email as email_lib, logging, httpx, yaml, html, threading, base64, json
from html.parser import HTMLParser
from email.header import decode_header
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

CONFIG_FILE = os.environ.get("CONFIG_FILE", "/config/config.yaml")

def load_config() -> dict:
    with open(CONFIG_FILE) as f:
        return yaml.safe_load(f)

cfg = load_config()
TG_BOT_TOKEN  = cfg["telegram"]["bot_token"]
TG_CHAT_ID    = cfg["telegram"]["chat_id"]
POLL_INTERVAL = cfg.get("poll_interval", 30)
FORWARD_ALL   = cfg.get("forward_all", False)

# OAuth2 回调服务配置
OAUTH_ENABLED     = cfg.get("oauth", {}).get("enabled", False)
OAUTH_CLIENT_ID     = cfg.get("oauth", {}).get("client_id", "7feada80-d946-4d06-b134-73afa3524fb7")
OAUTH_CLIENT_SECRET = cfg.get("oauth", {}).get("client_secret", "")
OAUTH_REDIRECT    = cfg.get("oauth", {}).get("redirect_uri", "https://oa.idays.gq/api/emails/oauth/outlook/callback")
OAUTH_PORT        = cfg.get("oauth", {}).get("port", 8080)

# Gmail Push 配置
GMAIL_CLIENT_ID     = cfg.get("gmail_push", {}).get("client_id", "")
GMAIL_CLIENT_SECRET = cfg.get("gmail_push", {}).get("client_secret", "")
GMAIL_PUBSUB_TOPIC  = cfg.get("gmail_push", {}).get("pubsub_topic", "")
GMAIL_PUSH_ENABLED  = bool(GMAIL_CLIENT_ID and GMAIL_PUBSUB_TOPIC)
GMAIL_TOKEN_URL     = "https://oauth2.googleapis.com/token"
GMAIL_AUTH_URL      = "https://accounts.google.com/o/oauth2/v2/auth"

STARTUP_TIME = datetime.now(timezone.utc)  # 启动时间，用于过滤历史邮件
CODE_RE = re.compile(r'\b\d{6}\b')

def find_code(text: str) -> str | None:
    for m in CODE_RE.finditer(text or ""):
        c = m.group()
        # 排除全同数字
        if len(set(c)) == 1:
            continue
        # 排除常见误判
        if c in ("123456", "654321", "000000", "100000", "200000", "300000",
                 "400000", "500000", "600000", "700000", "800000", "900000"):
            continue
        # 排除末尾4个0（如 120000、250000 等大整数）
        if c.endswith("0000"):
            continue
        return c
    return None

OUTLOOK_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
OUTLOOK_DEFAULT_CLIENT_ID = "7feada80-d946-4d06-b134-73afa3524fb7"

class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts = []
        self._skip = False
    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script"):
            self._skip = True
    def handle_endtag(self, tag):
        if tag in ("style", "script"):
            self._skip = False
        if tag in ("p", "br", "div", "tr", "li"):
            self._parts.append("\n")
    def handle_data(self, data):
        if not self._skip:
            self._parts.append(data)
    def get_text(self):
        return re.sub(r'\n{3,}', '\n\n', "".join(self._parts)).strip()

def html_to_text(raw: str) -> str:
    try:
        p = _TextExtractor()
        p.feed(html.unescape(raw))
        return p.get_text()
    except Exception:
        return re.sub(r'<[^>]+>', '', html.unescape(raw)).strip()

# ── Telegram ──────────────────────────────────────────────────────────────────
def send_tg(text: str):
    try:
        r = httpx.post(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                       json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "Markdown"}, timeout=10)
        if r.status_code != 200:
            log.error(f"TG 推送失败: {r.text}")
    except Exception as e:
        log.error(f"TG 推送异常: {e}")

def send_tg_document(caption: str, filename: str, content: str):
    """发送 HTML 文件附件"""
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendDocument",
            data={"chat_id": TG_CHAT_ID, "caption": caption},
            files={"document": (filename, content.encode("utf-8"), "text/html")},
            timeout=30
        )
        if r.status_code != 200:
            log.error(f"TG 附件推送失败: {r.text}")
    except Exception as e:
        log.error(f"TG 附件推送异常: {e}")

# ── 工具 ──────────────────────────────────────────────────────────────────────
def extract_imap_body(msg) -> str:
    plain = html_body = None
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain" and plain is None:
                plain = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="replace")
            elif ct == "text/html" and html_body is None:
                html_body = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="replace")
        return plain or html_body or ""
    payload = msg.get_payload(decode=True)
    return payload.decode(msg.get_content_charset() or "utf-8", errors="replace") if payload else ""

def decode_subject(msg) -> str:
    raw, enc = decode_header(msg.get("Subject", ""))[0]
    return raw.decode(enc or "utf-8") if isinstance(raw, bytes) else (raw or "")

def decode_from(msg) -> str:
    parts = decode_header(msg.get("From", ""))
    result = []
    for raw, enc in parts:
        if isinstance(raw, bytes):
            result.append(raw.decode(enc or "utf-8", errors="replace"))
        else:
            result.append(raw or "")
    return "".join(result)

def extract_to_email(msg) -> str:
    """提取实际收件地址（支持 +tag 别名）"""
    to = msg.get("Delivered-To") or msg.get("To", "")
    m = re.search(r'[\w.+%-]+@[\w.-]+', to)
    return m.group(0) if m else ""

def parse_date(msg) -> str:
    try:
        dt = parsedate_to_datetime(msg.get("Date", ""))
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""

# ── 通用 IMAP 轮询（Gmail / QQ 等应用密码方案）───────────────────────────────
_imap_pool: dict[str, imaplib.IMAP4_SSL] = {}  # email -> 复用连接
_imap_lock = threading.Lock()

def _get_imap(email: str, app_pass: str, host: str) -> imaplib.IMAP4_SSL:
    """获取复用的 IMAP 连接，断开时自动重连"""
    with _imap_lock:
        conn = _imap_pool.get(email)
        if conn:
            try:
                conn.noop()
                return conn
            except Exception:
                _imap_pool.pop(email, None)
        conn = imaplib.IMAP4_SSL(host, 993)
        conn.login(email, app_pass)
        _imap_pool[email] = conn
        return conn

def _poll_imap(acc: dict, host: str, skip_existing: bool = False) -> list[dict]:
    results = []
    try:
        imap = _get_imap(acc["email"], acc["app_pass"], host)
        imap.select("INBOX")
        _, data = imap.search(None, "UNSEEN")
        for uid in data[0].split():
            _, raw = imap.fetch(uid, "(RFC822)")
            if not raw or not raw[0]:
                continue
            msg = email_lib.message_from_bytes(raw[0][1])
            subject = decode_subject(msg)
            body    = extract_imap_body(msg)
            date    = parse_date(msg)
            code    = find_code(body) or find_code(subject)
            if skip_existing:
                imap.store(uid, "+FLAGS", "\\Seen")
                continue
            to_addr = extract_to_email(msg) or acc.get("label", acc["email"])
            if code or FORWARD_ALL:
                results.append({"label": to_addr, "subject": subject,
                                 "from": decode_from(msg), "code": code, "body": body, "date": date})
            imap.store(uid, "+FLAGS", "\\Seen")
    except Exception as e:
        log.error(f"[IMAP:{acc['email']}] {e}")
        _imap_pool.pop(acc["email"], None)  # 出错时清除连接，下次重连
    return results

# ── Gmail（应用专用密码）─────────────────────────────────────────────────────
def poll_gmail(acc: dict, skip_existing: bool = False) -> list[dict]:
    return _poll_imap(acc, "imap.gmail.com", skip_existing=skip_existing)

# ── QQ 邮箱（授权码）─────────────────────────────────────────────────────────
def poll_qq(acc: dict, skip_existing: bool = False) -> list[dict]:
    return _poll_imap(acc, "imap.qq.com", skip_existing=skip_existing)

# ── Outlook（OAuth2，Graph + IMAP fallback）──────────────────────────────────
_outlook_tokens: dict[str, dict] = {}  # email -> {access_token, expiry, token_type}
_token_fail_alerted: set[str] = set()  # 已推送过失效通知的账号
_processed_msg_ids: set[str] = set()  # 已处理的 Graph message id

def _outlook_refresh(acc: dict) -> dict:
    client_id = acc.get("client_id") or OAUTH_CLIENT_ID or OUTLOOK_DEFAULT_CLIENT_ID
    for scope in [
        "https://graph.microsoft.com/.default offline_access",
        "https://outlook.office.com/IMAP.AccessAsUser.All offline_access",
    ]:
        payload = {
            "client_id": client_id, "grant_type": "refresh_token",
            "refresh_token": acc["refresh_token"], "scope": scope,
        }
        if OAUTH_CLIENT_SECRET:
            payload["client_secret"] = OAUTH_CLIENT_SECRET
        r = httpx.post(OUTLOOK_TOKEN_URL, data=payload, timeout=15)
        d = r.json()
        if r.status_code == 200 and "access_token" in d:
            returned = d.get("scope", "").lower()
            token_type = "imap" if "imap" in returned else "graph"
            # 更新 refresh_token（如果有新的）
            if d.get("refresh_token"):
                acc["refresh_token"] = d["refresh_token"]
            return {"access_token": d["access_token"],
                    "expiry": time.time() + d.get("expires_in", 3600) - 60,
                    "token_type": token_type}
    raise RuntimeError(f"Outlook token 刷新失败: {acc['email']}")

def _outlook_get_token(acc: dict) -> tuple[str, str]:
    email = acc["email"]
    cached = _outlook_tokens.get(email)
    if not cached or time.time() >= cached["expiry"]:
        _outlook_tokens[email] = _outlook_refresh(acc)
    t = _outlook_tokens[email]
    return t["access_token"], t["token_type"]

def poll_outlook(acc: dict, skip_existing: bool = False) -> list[dict]:
    """acc: {email, refresh_token, client_id(可选), label(可选)}"""
    results = []
    email = acc["email"]
    try:
        token, token_type = _outlook_get_token(acc)
        _token_fail_alerted.discard(email)  # 恢复正常时清除记录
        label = acc.get("label", email)
        if token_type == "imap":
            results = _outlook_imap(acc, token, label, skip_existing=skip_existing)
        else:
            results = _outlook_graph(acc, token, label, skip_existing=skip_existing)
    except Exception as e:
        log.error(f"[Outlook:{email}] {e}")
        if email not in _token_fail_alerted:
            _token_fail_alerted.add(email)
            send_tg(f"⚠️ Outlook 账号失效：`{email}`\n请重新授权：https://oa.idays.gq/auth/outlook")
    return results

def _outlook_graph(acc: dict, token: str, label: str, skip_existing: bool = False) -> list[dict]:
    results = []
    headers = {"Authorization": f"Bearer {token}"}
    r = httpx.get("https://graph.microsoft.com/v1.0/me/messages",
                  params={"$filter": "isRead eq false", "$select": "id,subject,from,body,receivedDateTime",
                          "$top": 10, "$orderby": "receivedDateTime desc"},
                  headers=headers, timeout=15)
    if r.status_code != 200:
        log.error(f"[Outlook Graph:{acc['email']}] {r.status_code} {r.text[:200]}")
        return results
    for msg in r.json().get("value", []):
        msg_id = msg.get("id", "")
        if msg_id in _processed_msg_ids:
            continue
        subject = msg.get("subject", "")
        sender  = msg.get("from", {}).get("emailAddress", {}).get("address", "")
        body    = msg.get("body", {}).get("content", "")
        raw_dt  = msg.get("receivedDateTime", "")
        try:
            date = datetime.fromisoformat(raw_dt.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
            received_dt = datetime.fromisoformat(raw_dt.replace("Z", "+00:00"))
        except Exception:
            date = raw_dt[:16]
            received_dt = None
        # 跳过启动前的历史邮件
        if received_dt and received_dt < STARTUP_TIME:
            try:
                httpx.patch(f"https://graph.microsoft.com/v1.0/me/messages/{msg['id']}",
                            json={"isRead": True}, headers=headers, timeout=3)
            except Exception:
                pass
            _processed_msg_ids.add(msg_id)
            continue
        code    = find_code(body) or find_code(subject)
        if not skip_existing and (code or FORWARD_ALL):
            results.append({"label": label, "subject": subject, "from": sender, "code": code, "body": body, "date": date})
        try:
            httpx.patch(f"https://graph.microsoft.com/v1.0/me/messages/{msg['id']}",
                        json={"isRead": True}, headers=headers, timeout=3)
        except Exception:
            pass
        _processed_msg_ids.add(msg_id)
    return results

def _outlook_imap(acc: dict, token: str, label: str, skip_existing: bool = False) -> list[dict]:
    results = []
    auth_str = f"user={acc['email']}\x01auth=Bearer {token}\x01\x01"
    try:
        imap = imaplib.IMAP4_SSL("outlook.office365.com", 993)
        imap.authenticate("XOAUTH2", lambda _: auth_str.encode("ascii"))
        for folder in ["INBOX", "Junk"]:
            if imap.select(folder)[0] != "OK":
                continue
            _, data = imap.search(None, "UNSEEN")
            for uid in data[0].split():
                _, raw = imap.fetch(uid, "(RFC822)")
                if not raw or not raw[0]:
                    continue
                msg = email_lib.message_from_bytes(raw[0][1])
                subject = decode_subject(msg)
                body    = extract_imap_body(msg)
                date    = parse_date(msg)
                code    = find_code(body) or find_code(subject)
                if skip_existing:
                    imap.store(uid, "+FLAGS", "\\Seen")
                    continue
                if code or FORWARD_ALL:
                    results.append({"label": label, "subject": subject,
                                    "from": decode_from(msg), "code": code, "body": body, "date": date})
                imap.store(uid, "+FLAGS", "\\Seen")
        imap.logout()
    except Exception as e:
        log.error(f"[Outlook IMAP:{acc['email']}] {e}")
    return results

# ── Gmail Push OAuth ──────────────────────────────────────────────────────────
_gmail_tokens: dict[str, dict] = {}  # email -> {access_token, refresh_token, expiry}
_gmail_push_queue: list[dict] = []   # 待处理的 push 消息
_gmail_push_lock = threading.Lock()

GMAIL_SCOPES = "https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.modify"

def _gmail_refresh_token(email: str) -> str:
    t = _gmail_tokens.get(email, {})
    if t and time.time() < t.get("expiry", 0):
        return t["access_token"]
    r = httpx.post(GMAIL_TOKEN_URL, data={
        "client_id": GMAIL_CLIENT_ID,
        "client_secret": GMAIL_CLIENT_SECRET,
        "refresh_token": t["refresh_token"],
        "grant_type": "refresh_token",
    }, timeout=10)
    d = r.json()
    if "access_token" not in d:
        raise RuntimeError(f"Gmail token 刷新失败: {email} {d}")
    _gmail_tokens[email]["access_token"] = d["access_token"]
    _gmail_tokens[email]["expiry"] = time.time() + d.get("expires_in", 3600) - 60
    return d["access_token"]

def _gmail_watch(email: str):
    """注册 Gmail Push Watch，有效期 7 天"""
    try:
        token = _gmail_refresh_token(email)
        r = httpx.post(
            f"https://gmail.googleapis.com/gmail/v1/users/me/watch",
            headers={"Authorization": f"Bearer {token}"},
            json={"topicName": GMAIL_PUBSUB_TOPIC, "labelIds": ["INBOX"]},
            timeout=10
        )
        if r.status_code == 200:
            log.info(f"[Gmail Push] {email} watch 注册成功，到期: {r.json().get('expiration')}")
        else:
            log.error(f"[Gmail Push] {email} watch 失败: {r.text[:200]}")
    except Exception as e:
        log.error(f"[Gmail Push] {email} watch 异常: {e}")

def _gmail_fetch_message(email: str, msg_id: str) -> dict | None:
    """获取单封邮件内容"""
    try:
        token = _gmail_refresh_token(email)
        r = httpx.get(
            f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg_id}",
            headers={"Authorization": f"Bearer {token}"},
            params={"format": "full"},
            timeout=10
        )
        if r.status_code != 200:
            return None
        msg = r.json()
        headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
        subject = headers.get("subject", "")
        from_   = headers.get("from", "")
        date    = headers.get("date", "")
        try:
            date = parsedate_to_datetime(date).astimezone().strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass

        # 提取 body
        body = ""
        def extract_parts(parts):
            nonlocal body
            for p in parts:
                if p.get("mimeType") == "text/plain" and not body:
                    data = p.get("body", {}).get("data", "")
                    if data:
                        body = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
                elif p.get("mimeType") == "text/html" and not body:
                    data = p.get("body", {}).get("data", "")
                    if data:
                        body = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
                if p.get("parts"):
                    extract_parts(p["parts"])

        payload = msg.get("payload", {})
        if payload.get("parts"):
            extract_parts(payload["parts"])
        else:
            data = payload.get("body", {}).get("data", "")
            if data:
                body = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")

        # 标为已读
        httpx.post(
            f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg_id}/modify",
            headers={"Authorization": f"Bearer {token}"},
            json={"removeLabelIds": ["UNREAD"]},
            timeout=5
        )
        return {"subject": subject, "from": from_, "date": date, "body": body, "email": email}
    except Exception as e:
        log.error(f"[Gmail Push] 获取邮件失败 {email}/{msg_id}: {e}")
        return None

def _process_gmail_push(data: dict):
    """处理 Pub/Sub 推送的 Gmail 通知"""
    try:
        msg_data = base64.b64decode(data.get("message", {}).get("data", "")).decode()
        notification = json.loads(msg_data)
        email = notification.get("emailAddress", "")
        history_id = notification.get("historyId")
        if not email or not history_id:
            return

        token = _gmail_refresh_token(email)
        # 获取新邮件列表
        r = httpx.get(
            f"https://gmail.googleapis.com/gmail/v1/users/me/history",
            headers={"Authorization": f"Bearer {token}"},
            params={"startHistoryId": history_id, "historyTypes": "messageAdded", "labelId": "INBOX"},
            timeout=10
        )
        if r.status_code != 200:
            return
        for record in r.json().get("history", []):
            for added in record.get("messagesAdded", []):
                msg_id = added["message"]["id"]
                if msg_id in _processed_msg_ids:
                    continue
                _processed_msg_ids.add(msg_id)
                item = _gmail_fetch_message(email, msg_id)
                if not item:
                    continue
                body = item["body"]
                code = find_code(body) or find_code(item["subject"])
                label = _gmail_tokens.get(email, {}).get("label", email)
                if code or FORWARD_ALL:
                    is_html = "<" in body and ">" in body
                    plain = html_to_text(body)
                    if code:
                        text = (f"`{code}`\n\n📬 *{label}*\n"
                                f"发件人: {item['from']}\n时间: {item['date']}\n主题: {item['subject']}")
                        log.info(f"[Gmail Push:{label}] 验证码: {code}")
                        send_tg(text)
                        if FORWARD_ALL and is_html:
                            send_tg_document(f"📎 {item['subject'][:60]}", f"{item['subject'][:40]}.html", body)
                    elif FORWARD_ALL:
                        caption = (f"📩 *{label}*\n发件人: {item['from']}\n"
                                   f"时间: {item['date']}\n主题: {item['subject']}")
                        if plain and len(plain) >= 50:
                            send_tg(caption + f"\n\n{plain[:1500]}")
                        else:
                            send_tg(caption + "\n\n📎 邮件以图片为主，已附原始文件")
                        if is_html:
                            send_tg_document(caption, f"{item['subject'][:40]}.html", body)
    except Exception as e:
        log.error(f"[Gmail Push] 处理通知异常: {e}")

def _renew_gmail_watches():
    """每 6 天自动续期所有 Gmail Watch"""
    while True:
        time.sleep(6 * 24 * 3600)
        for email in list(_gmail_tokens.keys()):
            _gmail_watch(email)

# ── OAuth2 回调服务 ───────────────────────────────────────────────────────────
AUTH_URL = (
    f"https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
    f"?client_id={{client_id}}&response_type=code&redirect_uri={{redirect}}"
    f"&scope=https://graph.microsoft.com/Mail.Read%20https://graph.microsoft.com/Mail.ReadWrite%20https://graph.microsoft.com/User.Read%20offline_access&prompt=select_account"
)

class OAuthHandler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass  # 静默 HTTP 日志

    def do_GET(self):
        parsed = urlparse(self.path)

        # 授权入口：跳转微软登录
        if parsed.path == "/auth/outlook":
            url = AUTH_URL.format(client_id=OAUTH_CLIENT_ID, redirect=OAUTH_REDIRECT)
            self._redirect(url)

        # 微软回调
        elif parsed.path == "/api/emails/oauth/outlook/callback":
            params = parse_qs(parsed.query)
            code = params.get("code", [None])[0]
            if not code:
                self._respond(400, "缺少 code 参数")
                return
            try:
                rt, email = _exchange_code(code)
                _save_outlook_account(rt, email)
                self._respond(200, f"✅ 授权成功！{email} 已添加，监控将在下一轮询周期生效。")
                send_tg(f"✅ Outlook 账号已授权：`{email}`")
                log.info(f"新 Outlook 账号授权成功：{email}")
            except Exception as e:
                self._respond(500, f"授权失败: {e}")
                log.error(f"OAuth 回调处理失败: {e}")

        # Gmail 授权入口
        elif parsed.path == "/auth/gmail":
            params = parse_qs(parsed.query)
            redirect = f"https://oa.idays.gq/api/gmail/oauth/callback"
            url = (f"{GMAIL_AUTH_URL}?client_id={GMAIL_CLIENT_ID}"
                   f"&redirect_uri={redirect}&response_type=code"
                   f"&scope={GMAIL_SCOPES.replace(' ', '%20')}"
                   f"&access_type=offline&prompt=consent")
            self._redirect(url)

        # Gmail OAuth 回调
        elif parsed.path == "/api/gmail/oauth/callback":
            params = parse_qs(parsed.query)
            code = params.get("code", [None])[0]
            if not code:
                self._respond(400, "缺少 code 参数")
                return
            try:
                redirect = f"https://oa.idays.gq/api/gmail/oauth/callback"
                r = httpx.post(GMAIL_TOKEN_URL, data={
                    "client_id": GMAIL_CLIENT_ID,
                    "client_secret": GMAIL_CLIENT_SECRET,
                    "code": code,
                    "redirect_uri": redirect,
                    "grant_type": "authorization_code",
                }, timeout=15)
                d = r.json()
                if "refresh_token" not in d:
                    raise RuntimeError(d.get("error_description", d))
                # 获取邮箱地址
                me = httpx.get("https://www.googleapis.com/gmail/v1/users/me/profile",
                               headers={"Authorization": f"Bearer {d['access_token']}"}, timeout=10)
                email = me.json().get("emailAddress", "")
                # 找到对应账号的 label
                label = email
                for acc in cfg.get("accounts", []):
                    for mb in (acc.get("mailboxes") or []):
                        if mb.get("email") == email:
                            label = mb.get("label", email)
                _gmail_tokens[email] = {
                    "access_token": d["access_token"],
                    "refresh_token": d["refresh_token"],
                    "expiry": time.time() + d.get("expires_in", 3600) - 60,
                    "label": label,
                }
                _save_gmail_token(email, d["refresh_token"])
                _gmail_watch(email)
                self._respond(200, f"✅ Gmail 授权成功！{email} 已启用 Push 监控。")
                send_tg(f"✅ Gmail Push 已启用：`{email}`")
            except Exception as e:
                self._respond(500, f"授权失败: {e}")
                log.error(f"Gmail OAuth 回调失败: {e}")

        # Gmail Pub/Sub Push 接收
        elif parsed.path == "/api/gmail/push":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            self._respond(200, "ok")
            try:
                data = json.loads(body)
                threading.Thread(target=_process_gmail_push, args=(data,), daemon=True).start()
            except Exception as e:
                log.error(f"[Gmail Push] 解析推送失败: {e}")
        else:
            self._respond(404, "Not Found")

    def _redirect(self, url):
        self.send_response(302)
        self.send_header("Location", url)
        self.end_headers()

    def _respond(self, code, msg):
        body = msg.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)


def _exchange_code(code: str) -> tuple[str, str]:
    """返回 (refresh_token, email)"""
    data = {
        "client_id":    OAUTH_CLIENT_ID,
        "grant_type":   "authorization_code",
        "code":         code,
        "redirect_uri": OAUTH_REDIRECT,
        "scope":        "https://graph.microsoft.com/Mail.Read https://graph.microsoft.com/Mail.ReadWrite https://graph.microsoft.com/User.Read offline_access",
        "token_endpoint_auth_method": "none",
    }
    if OAUTH_CLIENT_SECRET:
        data["client_secret"] = OAUTH_CLIENT_SECRET
        data.pop("token_endpoint_auth_method", None)
    r = httpx.post(OUTLOOK_TOKEN_URL, data=data, timeout=15)
    d = r.json()
    if "refresh_token" not in d:
        raise RuntimeError(d.get("error_description", d))
    # 用 access_token 获取邮箱地址
    email = ""
    try:
        me = httpx.get("https://graph.microsoft.com/v1.0/me",
                       headers={"Authorization": f"Bearer {d['access_token']}"},
                       params={"$select": "mail,userPrincipalName"}, timeout=10)
        me_data = me.json()
        email = me_data.get("mail") or me_data.get("userPrincipalName", "")
    except Exception:
        pass
    return d["refresh_token"], email


def _save_outlook_account(refresh_token: str, email: str):
    """更新已有账号的 token，不存在则追加"""
    with open(CONFIG_FILE) as f:
        content = f.read()

    # 用 yaml 解析判断是否已存在
    data = yaml.safe_load(content)
    exists = False
    for entry in data.get("accounts", []):
        if entry.get("type") == "outlook":
            for mb in entry.get("mailboxes", []):
                if mb.get("email") == email:
                    exists = True
                    break

    if exists:
        # 替换该邮箱对应的 refresh_token（找到 email 行后的第一个 refresh_token）
        import re as _re
        pattern = rf'(email:\s*["\']?{_re.escape(email)}["\']?\s*\n\s+refresh_token:\s*)[^\n]+'
        new_content = _re.sub(pattern, rf'\g<1>"{refresh_token}"', content, count=1)
        with open(CONFIG_FILE, "w") as f:
            f.write(new_content)
        return

    # 不存在则追加
    new_entry = (
        f"      - label: \"{email}\"\n"
        f"        email: \"{email}\"\n"
        f"        refresh_token: \"{refresh_token}\"\n"
    )
    if "type: outlook" in content:
        content = content.rstrip() + "\n" + new_entry
    else:
        content = content.rstrip() + "\n  - type: outlook\n    mailboxes:\n" + new_entry

    with open(CONFIG_FILE, "w") as f:
        f.write(content)


def _save_gmail_token(email: str, refresh_token: str):
    """保存 Gmail refresh_token 到 config.yaml，不破坏原有格式"""
    with open(CONFIG_FILE) as f:
        lines = f.readlines()

    new_lines = []
    i = 0
    inserted = False
    while i < len(lines):
        line = lines[i]
        new_lines.append(line)
        # 找到匹配的 email 行
        if not inserted and re.search(rf'email:\s*["\']?{re.escape(email)}["\']?\s*$', line.rstrip()):
            indent = len(line) - len(line.lstrip())
            spaces = " " * indent
            # 检查下一行是否已有 gmail_refresh_token
            if i + 1 < len(lines) and "gmail_refresh_token:" in lines[i + 1]:
                i += 1  # 跳过旧的 token 行
                new_lines.append(f'{spaces}gmail_refresh_token: "{refresh_token}"\n')
            else:
                new_lines.append(f'{spaces}gmail_refresh_token: "{refresh_token}"\n')
            inserted = True
        i += 1

    with open(CONFIG_FILE, "w") as f:
        f.writelines(new_lines)


def start_oauth_server():
    server = HTTPServer(("0.0.0.0", OAUTH_PORT), OAuthHandler)
    log.info(f"OAuth 回调服务已启动，授权入口: http://0.0.0.0:{OAUTH_PORT}/auth/outlook")
    server.serve_forever()


# ── 主循环 ────────────────────────────────────────────────────────────────────
def main():
    if OAUTH_ENABLED:
        t = threading.Thread(target=start_oauth_server, daemon=True)
        t.start()

    # 支持新格式（按 type 分组）和旧格式（flat list）
    raw = cfg.get("accounts", [])
    accounts = []
    for entry in raw:
        if "mailboxes" in entry:
            for mb in (entry["mailboxes"] or []):
                accounts.append({**mb, "type": entry["type"]})
        else:
            accounts.append(entry)

    # 去重：同邮箱保留最后一条
    seen = {}
    for acc in accounts:
        seen[acc.get("email", "")] = acc
    accounts = list(seen.values())
    log.info(f"加载 {len(accounts)} 个账号")

    def _group(t):
        return "\n".join(f"`{a['email']}`" for a in accounts if a.get("type","").lower()==t and a.get("email"))

    gmail_list   = _group("gmail")
    qq_list      = _group("qq")
    outlook_list = _group("outlook")

    parts = []
    if gmail_list:   parts.append(f"📧 Gmail：\n{gmail_list}")
    if qq_list:      parts.append(f"📧 QQ：\n{qq_list}")
    if outlook_list: parts.append(f"📧 Outlook：\n{outlook_list}")

    auth_url = OAUTH_REDIRECT.replace("/api/emails/oauth/outlook/callback", "/auth/outlook")
    if OAUTH_ENABLED:
        parts.append(f"➕ [添加 Outlook 账号]({auth_url})")
    if GMAIL_PUSH_ENABLED:
        parts.append(f"➕ [添加 Gmail Push]({auth_url.replace('/auth/outlook', '/auth/gmail')})")

    send_tg(f"✅ 监控已启动，共 {len(accounts)} 个账号\n\n" + "\n\n".join(parts))

    # 加载已有 Gmail Push token 并注册 watch
    if GMAIL_PUSH_ENABLED:
        for acc in accounts:
            if acc.get("type") == "gmail" and acc.get("gmail_refresh_token"):
                email = acc["email"]
                _gmail_tokens[email] = {
                    "access_token": "",
                    "refresh_token": acc["gmail_refresh_token"],
                    "expiry": 0,
                    "label": acc.get("label", email),
                }
                _gmail_watch(email)
        threading.Thread(target=_renew_gmail_watches, daemon=True).start()

    first_run = True
    while True:
        _skip = first_run
        def poll_one(acc, skip=_skip):
            t = acc.get("type", "").lower()
            try:
                if t == "gmail":
                    return poll_gmail(acc, skip_existing=skip)
                elif t == "qq":
                    return poll_qq(acc, skip_existing=skip)
                elif t == "outlook":
                    return poll_outlook(acc, skip_existing=skip)
            except Exception as e:
                log.error(f"[{acc.get('email')}] {e}")
            return []

        all_items = []
        with ThreadPoolExecutor(max_workers=min(len(accounts), 10)) as ex:
            futures = {ex.submit(poll_one, acc): acc for acc in accounts}
            for f in as_completed(futures):
                all_items.extend(f.result() or [])

        for item in all_items:
            body_raw = item.get("body", "")
            plain = html_to_text(body_raw)
            is_html = "<" in body_raw and ">" in body_raw

            if item.get("code"):
                text = (f"`{item['code']}`\n\n"
                        f"📬 *{item['label']}*\n"
                        f"发件人: {item['from']}\n"
                        f"时间: {item.get('date', '')}\n"
                        f"主题: {item['subject']}")
                log.info(f"[{item['label']}] 验证码: {item['code']}")
                send_tg(text)
                if FORWARD_ALL and is_html and body_raw:
                    send_tg_document(f"📎 {item['subject'][:60]}", f"{item['subject'][:40]}.html", body_raw)
            else:
                caption = (f"📩 *{item['label']}*\n"
                           f"发件人: {item['from']}\n"
                           f"时间: {item.get('date', '')}\n"
                           f"主题: {item['subject']}")
                log.info(f"[{item['label']}] 转发邮件: {item['subject']}")
                if plain and len(plain) >= 50:
                    text = caption + f"\n\n{plain[:1500]}"
                    if len(plain) > 1500:
                        text += "\n…（内容已截断）"
                    send_tg(text)
                    if is_html and len(plain) > 1500:
                        send_tg_document(caption, f"{item['subject'][:40]}.html", body_raw)
                else:
                    send_tg(caption + "\n\n📎 邮件以图片为主，已附原始文件")
                    if is_html and body_raw:
                        send_tg_document(caption, f"{item['subject'][:40]}.html", body_raw)
        first_run = False
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
