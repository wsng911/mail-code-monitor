"""
邮箱验证码监控 - 多账号，支持 Gmail(应用密码) + Outlook(OAuth2)
"""
import os, re, time, imaplib, email as email_lib, logging, httpx, yaml, html
from html.parser import HTMLParser
from email.header import decode_header

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

CODE_RE = re.compile(r'\b\d{6}\b')

def find_code(text: str) -> str | None:
    for m in CODE_RE.finditer(text or ""):
        c = m.group()
        # 排除全同数字(111111)和简单交替(123456/654321/121212等常见误判)
        if len(set(c)) == 1:
            continue
        if c in ("123456", "654321", "000000"):
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
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="replace")
    payload = msg.get_payload(decode=True)
    return payload.decode(msg.get_content_charset() or "utf-8", errors="replace") if payload else ""

def decode_subject(msg) -> str:
    raw, enc = decode_header(msg.get("Subject", ""))[0]
    return raw.decode(enc or "utf-8") if isinstance(raw, bytes) else (raw or "")

def parse_date(msg) -> str:
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(msg.get("Date", "")).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""

# ── 通用 IMAP 轮询（Gmail / QQ 等应用密码方案）───────────────────────────────
def _poll_imap(acc: dict, host: str) -> list[dict]:
    results = []
    try:
        imap = imaplib.IMAP4_SSL(host, 993)
        imap.login(acc["email"], acc["app_pass"])
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
            if code or FORWARD_ALL:
                results.append({"label": acc.get("label", acc["email"]), "subject": subject,
                                 "from": str(msg.get("From", "")), "code": code, "body": body, "date": date})
            imap.store(uid, "+FLAGS", "\\Seen")
        imap.logout()
    except Exception as e:
        log.error(f"[IMAP:{acc['email']}] {e}")
    return results

# ── Gmail（应用专用密码）─────────────────────────────────────────────────────
def poll_gmail(acc: dict) -> list[dict]:
    return _poll_imap(acc, "imap.gmail.com")

# ── QQ 邮箱（授权码）─────────────────────────────────────────────────────────
def poll_qq(acc: dict) -> list[dict]:
    return _poll_imap(acc, "imap.qq.com")
    return results

# ── Outlook（OAuth2，Graph + IMAP fallback）──────────────────────────────────
_outlook_tokens: dict[str, dict] = {}  # email -> {access_token, expiry, token_type}

def _outlook_refresh(acc: dict) -> dict:
    client_id = acc.get("client_id") or OUTLOOK_DEFAULT_CLIENT_ID
    for scope in [
        "https://graph.microsoft.com/.default offline_access",
        "https://outlook.office.com/IMAP.AccessAsUser.All offline_access",
    ]:
        r = httpx.post(OUTLOOK_TOKEN_URL, data={
            "client_id": client_id, "grant_type": "refresh_token",
            "refresh_token": acc["refresh_token"], "scope": scope,
        }, timeout=15)
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

def poll_outlook(acc: dict) -> list[dict]:
    """acc: {email, refresh_token, client_id(可选), label(可选)}"""
    results = []
    try:
        token, token_type = _outlook_get_token(acc)
        label = acc.get("label", acc["email"])
        if token_type == "imap":
            results = _outlook_imap(acc, token, label)
        else:
            results = _outlook_graph(acc, token, label)
    except Exception as e:
        log.error(f"[Outlook:{acc['email']}] {e}")
    return results

def _outlook_graph(acc: dict, token: str, label: str) -> list[dict]:
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
        subject = msg.get("subject", "")
        sender  = msg.get("from", {}).get("emailAddress", {}).get("address", "")
        body    = msg.get("body", {}).get("content", "")
        raw_dt  = msg.get("receivedDateTime", "")
        try:
            from datetime import datetime, timezone
            date = datetime.fromisoformat(raw_dt.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
        except Exception:
            date = raw_dt[:16]
        code    = find_code(body) or find_code(subject)
        if code or FORWARD_ALL:
            results.append({"label": label, "subject": subject, "from": sender, "code": code, "body": body, "date": date})
        httpx.patch(f"https://graph.microsoft.com/v1.0/me/messages/{msg['id']}",
                    json={"isRead": True}, headers=headers, timeout=10)
    return results

def _outlook_imap(acc: dict, token: str, label: str) -> list[dict]:
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
                if code or FORWARD_ALL:
                    results.append({"label": label, "subject": subject,
                                    "from": str(msg.get("From", "")), "code": code, "body": body, "date": date})
                imap.store(uid, "+FLAGS", "\\Seen")
        imap.logout()
    except Exception as e:
        log.error(f"[Outlook IMAP:{acc['email']}] {e}")
    return results

# ── 主循环 ────────────────────────────────────────────────────────────────────
def main():
    # 支持新格式（按 type 分组）和旧格式（flat list）
    raw = cfg.get("accounts", [])
    accounts = []
    for entry in raw:
        if "mailboxes" in entry:
            for mb in entry["mailboxes"]:
                accounts.append({**mb, "type": entry["type"]})
        else:
            accounts.append(entry)
    log.info(f"加载 {len(accounts)} 个账号")
    email_list = "\n".join(
        f"`{acc['email']}`" for acc in accounts if acc.get("email")
    )
    version = os.environ.get("APP_VERSION", "dev")
    send_tg(f"✅ 监控已启动 `v{version}`，共 {len(accounts)} 个账号\n\n{email_list}")

    while True:
        for acc in accounts:
            t = acc.get("type", "").lower()
            try:
                if t == "gmail":
                    items = poll_gmail(acc)
                elif t == "qq":
                    items = poll_qq(acc)
                elif t == "outlook":
                    items = poll_outlook(acc)
                else:
                    log.warning(f"未知账号类型: {t}")
                    continue
                for item in items:
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
                        # forward_all 时始终发完整 HTML 附件
                        if FORWARD_ALL and is_html and body_raw:
                            send_tg_document(text, f"{item['subject'][:40]}.html", body_raw)
                    else:
                        # forward_all 普通邮件
                        caption = (f"📩 *{item['label']}*\n"
                                   f"发件人: {item['from']}\n"
                                   f"时间: {item.get('date', '')}\n"
                                   f"主题: {item['subject']}")
                        log.info(f"[{item['label']}] 转发邮件: {item['subject']}")
                        if plain and len(plain) >= 50:
                            # 有足够文字：推文字，文字超长时额外附 HTML
                            text = caption + f"\n\n{plain[:1500]}"
                            if len(plain) > 1500:
                                text += "\n…（内容已截断）"
                            send_tg(text)
                            if is_html and len(plain) > 1500:
                                send_tg_document(caption, f"{item['subject'][:40]}.html", body_raw)
                        else:
                            # 文字太少（图片为主）：推说明 + 发 HTML 附件
                            send_tg(caption + "\n\n📎 邮件以图片为主，已附原始文件")
                            if is_html and body_raw:
                                send_tg_document(caption, f"{item['subject'][:40]}.html", body_raw)
            except Exception as e:
                log.error(f"账号 {acc.get('email')} 轮询异常: {e}")
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
