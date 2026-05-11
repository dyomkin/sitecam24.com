#!/usr/bin/env python3
import html
import json
import subprocess
import time
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs


HOST = "127.0.0.1"
PORT = 8088
RECIPIENT = "hello@sitecam24.com"
SENDER = "SiteCam24 <noreply@sitecam24.com>"
MAX_BODY_BYTES = 16 * 1024
MIN_FILL_SECONDS = 3
MAX_FILL_SECONDS = 2 * 60 * 60
RATE_WINDOW_SECONDS = 10 * 60
RATE_LIMIT = 3

RATE_BUCKETS = {}


def clean(value, limit=2000):
    return " ".join((value or "").replace("\x00", "").strip().split())[:limit]


def json_response(handler, status, payload):
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def client_ip(handler):
    forwarded = handler.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return handler.client_address[0]


def rate_limited(ip):
    now = time.time()
    attempts = [stamp for stamp in RATE_BUCKETS.get(ip, []) if now - stamp < RATE_WINDOW_SECONDS]
    if len(attempts) >= RATE_LIMIT:
        RATE_BUCKETS[ip] = attempts
        return True
    attempts.append(now)
    RATE_BUCKETS[ip] = attempts
    return False


def build_email(fields, ip, elapsed):
    msg = EmailMessage()
    msg["To"] = RECIPIENT
    msg["From"] = SENDER
    msg["Reply-To"] = fields["email"]
    msg["Subject"] = "New SiteCam24 contact form message"

    text = f"""New contact form message from sitecam24.com

Name: {fields["name"]}
Company: {fields["company"] or "-"}
Email: {fields["email"]}
Language: {fields["language"]}
IP: {ip}
Fill time: {elapsed:.1f}s

Project:
{fields["project"]}
"""

    safe_project = html.escape(fields["project"]).replace("\n", "<br>")
    html_body = f"""<h2>New SiteCam24 contact form message</h2>
<p><strong>Name:</strong> {html.escape(fields["name"])}</p>
<p><strong>Company:</strong> {html.escape(fields["company"] or "-")}</p>
<p><strong>Email:</strong> {html.escape(fields["email"])}</p>
<p><strong>Language:</strong> {html.escape(fields["language"])}</p>
<p><strong>IP:</strong> {html.escape(ip)}</p>
<p><strong>Fill time:</strong> {elapsed:.1f}s</p>
<p><strong>Project:</strong><br>{safe_project}</p>"""

    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")
    return msg


class ContactHandler(BaseHTTPRequestHandler):
    server_version = "SiteCam24Contact/1.0"

    def do_POST(self):
        if self.path != "/api/contact":
            json_response(self, 404, {"ok": False, "message": "Not found"})
            return

        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0 or length > MAX_BODY_BYTES:
            json_response(self, 400, {"ok": False, "message": "Invalid request"})
            return

        raw_body = self.rfile.read(length).decode("utf-8", errors="replace")
        data = parse_qs(raw_body, keep_blank_values=True)
        get = lambda key: data.get(key, [""])[0]
        ip = client_ip(self)

        if rate_limited(ip):
            json_response(self, 429, {"ok": False, "message": "Too many requests"})
            return

        if clean(get("website")):
            json_response(self, 400, {"ok": False, "message": "Invalid request"})
            return

        try:
            started_ms = int(get("form_started_at"))
        except ValueError:
            started_ms = 0

        elapsed = time.time() - (started_ms / 1000)
        if elapsed < MIN_FILL_SECONDS or elapsed > MAX_FILL_SECONDS:
            json_response(self, 400, {"ok": False, "message": "Invalid form timing"})
            return

        if clean(get("captcha")) != "7":
            json_response(self, 400, {"ok": False, "message": "Wrong captcha answer"})
            return

        fields = {
            "name": clean(get("name"), 200),
            "company": clean(get("company"), 200),
            "email": clean(get("email"), 320),
            "project": clean(get("project"), 4000),
            "language": clean(get("language"), 20),
        }

        if not fields["name"] or "@" not in fields["email"] or not fields["project"]:
            json_response(self, 400, {"ok": False, "message": "Required fields are missing"})
            return

        message = build_email(fields, ip, elapsed)
        try:
            subprocess.run(["/usr/sbin/sendmail", "-t", "-oi"], input=message.as_bytes(), check=True)
        except subprocess.CalledProcessError:
            json_response(self, 500, {"ok": False, "message": "Mail delivery failed"})
            return

        json_response(self, 200, {"ok": True})

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)


if __name__ == "__main__":
    ThreadingHTTPServer((HOST, PORT), ContactHandler).serve_forever()
