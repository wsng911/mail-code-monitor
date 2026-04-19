"""
Mail-Monitor v1 — 纯 IMAP IDLE 版本
支持 Gmail / QQ邮箱 / Outlook(OAuth2)
"""
import os, re, time, imaplib, email as email_lib, logging, httpx, yaml, html, threading, base64, json
from html.parser import HTMLParser
from email.header import decode_header
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
FORWARD_ALL   = cfg.get("forward_all", False)

CODE_RE = re.compile(r'\b\d{6}\b')

# ── 验证码提取 ────────────────────────────────────────────────────────────────
def find_code(text: str) -> str | None:
    for m in CODE_RE.finditer(text or ""):
        c = m.group()
        if len(set(c)) == 1: continue
        if c in ("123456", "654321", "000000"): continue
        if c.endswith("0000"): continue
        return c
    return None

# ── HTML → 纯文本 ─────────────────────────────────────────────────────────────
class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts, self._skip = [], False
    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script"): self._skip = True
    def handle_endtag(self, tag):
        if tag in ("style", "script"): self._skip = False
        if tag in ("p", "br", "div", "tr", "li"): self._parts.append("\n")
    def handle_data(self, data):
        if not self._skip: self._parts.append(data)
    def get_text(self):
        return re.sub(r'\n{3,}', '\n\n', "".join(self._parts)).strip()

def html_to_text(raw: str) -> str:
    try:
        p = _TextExtractor(); p.feed(html.unescape(raw)); return p.get_text()
    except Exception:
        return re.sub(r'<[^>]+>', '', html.unescape(raw)).strip()

# ── 工具函数 ──────────────────────────────────────────────────────────────────
def _esc(text: str) -> str:
    for c in r'\_*[]()~`>#+-=|{}.!':
        text = text.replace(c, f'\\{c}')
    return text

def _safe_filename(subject: str, max_len: int = 30) -> str:
    name = re.sub(r'[\\/:*?"<>|]', '', subject).strip()
    return (name[:max_len].rstrip() if len(name) > max_len else name) or "邮件"

def decode_subject(msg) -> str:
    raw, enc = decode_header(msg.get("Subject", ""))[0]
    return raw.decode(enc or "utf-8") if isinstance(raw, bytes) else (raw or "")

def decode_from(msg) -> str:
    parts = decode_header(msg.get("From", ""))
    result = []
    for raw, enc in parts:
        result.append(raw.decode(enc or "utf-8", errors="replace") if isinstance(raw, bytes) else (raw or ""))
    return "".join(result)

def extract_to_email(msg) -> str:
    to = msg.get("Delivered-To") or msg.get("To", "")
    m = re.search(r'[\w.+%-]+@[\w.-]+', to)
    return m.group(0) if m else ""

def parse_date(msg) -> str:
    try:
        return parsedate_to_datetime(msg.get("Date", "")).astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""

def extract_imap_body(msg) -> tuple[str, str]:
    plain = html_body = None
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain" and plain is None:
                plain = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="replace")
            elif ct == "text/html" and html_body is None:
                html_body = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="replace")
        return plain or html_to_text(html_body or ""), html_body or ""
    payload = msg.get_payload(decode=True)
    decoded = payload.decode(msg.get_content_charset() or "utf-8", errors="replace") if payload else ""
    if "html" in msg.get_content_type():
        return html_to_text(decoded), decoded
    return decoded, ""

def wrap_html(html_body: str, subject="", from_="", to="", date="") -> str:
    def row(label, val):
        return f"<tr><td style='color:#888;white-space:nowrap;padding:2px 12px 2px 0'>{label}</td><td style='word-break:break-all'>{html.escape(val)}</td></tr>" if val else ""
    header = (
        "<div style='font-family:sans-serif;font-size:13px;background:#f5f5f5;color:#333;"
        "border-bottom:2px solid #ddd;padding:12px 16px;margin-bottom:12px'>"
        f"<div style='font-size:15px;font-weight:bold;color:#111;margin-bottom:8px'>{html.escape(subject)}</div>"
        "<table style='border-collapse:collapse'>"
        + row("发件人", from_) + row("收件人", to) + row("时间", date)
        + "</table></div>"
    )
    if "<body" in html_body.lower():
        return re.sub(r'(<body[^>]*>)', r'\1' + header, html_body, count=1, flags=re.IGNORECASE)
    return header + html_body

# ── Telegram ──────────────────────────────────────────────────────────────────
def send_tg(text: str) -> bool:
    try:
        r = httpx.post(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                       json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "MarkdownV2"}, timeout=10)
        if r.status_code == 200: return True
        log.error(f"TG 推送失败: {r.text}")
        # 降级纯文本重试
        r2 = httpx.post(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                        json={"chat_id": TG_CHAT_ID, "text": re.sub(r'[\\`*_\[\]()~>#+=|{}.!\-]', '', text)}, timeout=10)
        return r2.status_code == 200
    except Exception as e:
        log.error(f"TG 推送异常: {e}"); return False

def send_tg_document(filename: str, content: str):
    try:
        r = httpx.post(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendDocument",
                       data={"chat_id": TG_CHAT_ID},
                       files={"document": (filename, content.encode("utf-8"), "text/html")}, timeout=30)
        if r.status_code != 200:
            log.error(f"TG 附件推送失败: {r.text}")
    except Exception as e:
        log.error(f"TG 附件推送异常: {e}")

# ── Outlook OAuth2 token 刷新 ─────────────────────────────────────────────────
OUTLOOK_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
_outlook_tokens: dict[str, dict] = {}

def _outlook_refresh(acc: dict) -> dict:
    client_id = acc.get("client_id") or "7feada80-d946-4d06-b134-73afa3524fb7"
    payload = {
        "client_id": client_id,
        "grant_type": "refresh_token",
        "refresh_token": acc["refresh_token"],
        "scope": "https://outlook.office.com/IMAP.AccessAsUser.All offline_access",
    }
    if acc.get("client_secret"):
        payload["client_secret"] = acc["client_secret"]
    r = httpx.post(OUTLOOK_TOKEN_URL, data=payload, timeout=15)
    d = r.json()
    if r.status_code == 200 and "access_token" in d:
        if d.get("refresh_token"):
            acc["refresh_token"] = d["refresh_token"]
        return {"access_token": d["access_token"],
                "expiry": time.time() + d.get("expires_in", 3600) - 60}
    raise RuntimeError(f"Outlook token 刷新失败: {acc['email']} {d}")

def _outlook_get_token(acc: dict) -> str:
    email = acc["email"]
    cached = _outlook_tokens.get(email)
    if not cached or time.time() >= cached["expiry"]:
        _outlook_tokens[email] = _outlook_refresh(acc)
    return _outlook_tokens[email]["access_token"]

# ── 邮件处理 ──────────────────────────────────────────────────────────────────
def process_message(msg, label: str, email_addr: str):
    subject  = decode_subject(msg)
    body, html_body = extract_imap_body(msg)
    date     = parse_date(msg)
    to_addr  = extract_to_email(msg) or label
    sender   = decode_from(msg)
    code     = find_code(body) or find_code(subject)
    plain    = html_to_text(body)
    is_html  = bool(html_body)

    if not (code or FORWARD_ALL):
        return

    if code:
        text = (f"`{code}`\n\n"
                f">{_esc('📬')} *{_esc(to_addr)}*\n"
                f">{_esc('发件人')}: {_esc(sender)}\n"
                f">{_esc('时间')}: {_esc(date)}\n"
                f">{_esc('主题')}: {_esc(subject)}")
        log.info(f"[{label}] 验证码: {code}")
        if send_tg(text) and FORWARD_ALL and is_html:
            send_tg_document(f"{_safe_filename(subject)}.html",
                             wrap_html(html_body, subject=subject, from_=sender, to=to_addr, date=date))
    else:
        header = (f">{_esc('📩')} *{_esc(to_addr)}*\n"
                  f">{_esc('发件人')}: {_esc(sender)}\n"
                  f">{_esc('时间')}: {_esc(date)}\n"
                  f">{_esc('主题')}: {_esc(subject)}")
        log.info(f"[{label}] 转发: {subject}")
        if plain and len(plain) >= 50:
            if send_tg(header + f"\n\n||{_esc(plain[:1500])}||") and is_html and len(plain) > 1500:
                send_tg_document(f"{_safe_filename(subject)}.html",
                                 wrap_html(html_body, subject=subject, from_=sender, to=to_addr, date=date))
        else:
            if send_tg(header + f"\n\n{_esc('📎 邮件以图片为主，已附原始文件')}") and is_html:
                send_tg_document(f"{_safe_filename(subject)}.html",
                                 wrap_html(html_body, subject=subject, from_=sender, to=to_addr, date=date))

# ── IMAP IDLE worker ──────────────────────────────────────────────────────────
def _imap_connect(acc: dict) -> imaplib.IMAP4_SSL:
    t = acc.get("type", "").lower()
    if t == "gmail":
        imap = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        imap.login(acc["email"], acc["app_pass"])
    elif t == "qq":
        imap = imaplib.IMAP4_SSL("imap.qq.com", 993)
        imap.login(acc["email"], acc["app_pass"])
    elif t == "outlook":
        token = _outlook_get_token(acc)
        auth_str = f"user={acc['email']}\x01auth=Bearer {token}\x01\x01"
        imap = imaplib.IMAP4_SSL("outlook.office365.com", 993)
        imap.authenticate("XOAUTH2", lambda _: auth_str.encode("ascii"))
    else:
        raise ValueError(f"未知账号类型: {t}")
    return imap

def idle_worker(acc: dict):
    email  = acc["email"]
    label  = acc.get("label", email)
    log.info(f"[IDLE] {label} 启动")

    while True:
        try:
            imap = _imap_connect(acc)
            imap.select("INBOX")

            # 先处理已有未读（标已读，不推送）
            _, data = imap.search(None, "UNSEEN")
            for uid in data[0].split():
                imap.store(uid, "+FLAGS", "\\Seen")

            # IDLE 循环
            while True:
                imap.send(b"IDLE\r\n")
                imap.readline()  # "+ idling"
                imap.socket().settimeout(540)
                try:
                    line = imap.readline()
                    imap.send(b"DONE\r\n")
                    imap.readline()
                    if b"EXISTS" in line or b"RECENT" in line:
                        _, data = imap.search(None, "UNSEEN")
                        for uid in data[0].split():
                            try:
                                _, raw = imap.fetch(uid, "(RFC822)")
                                if not raw or not raw[0]: continue
                                msg = email_lib.message_from_bytes(raw[0][1])
                                imap.store(uid, "+FLAGS", "\\Seen")
                                process_message(msg, label, email)
                            except Exception as e:
                                log.error(f"[IDLE:{label}] 处理邮件失败: {e}")
                except Exception:
                    try: imap.send(b"DONE\r\n"); imap.readline()
                    except Exception: pass
                    break

        except Exception as e:
            log.error(f"[IDLE:{label}] 连接断开: {e}")
            time.sleep(15)

# ── 主入口 ────────────────────────────────────────────────────────────────────
def main():
    raw = cfg.get("accounts", [])
    accounts = []
    for entry in raw:
        if "mailboxes" in entry:
            for mb in (entry["mailboxes"] or []):
                accounts.append({**mb, "type": entry["type"]})
        else:
            accounts.append(entry)

    # 去重
    seen = {}
    for acc in accounts:
        seen[acc.get("email", "")] = acc
    accounts = list(seen.values())
    log.info(f"加载 {len(accounts)} 个账号")

    for acc in accounts:
        threading.Thread(target=idle_worker, args=(acc,), daemon=True).start()

    def _group(t):
        return "\n".join(f"`{a['email']}`" for a in accounts if a.get("type","").lower()==t and a.get("email"))

    parts = []
    for t, icon in [("gmail","📧 Gmail"), ("qq","📧 QQ"), ("outlook","📧 Outlook")]:
        g = _group(t)
        if g: parts.append(f"{icon}：\n{g}")

    send_tg(f"✅ 监控已启动，共 {len(accounts)} 个账号\n\n" + "\n\n".join(parts))

    threading.Event().wait()

if __name__ == "__main__":
    main()
