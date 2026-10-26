import base64
import email.utils
import html
import re
from email.message import EmailMessage
from html.parser import HTMLParser
from urllib.parse import urlencode
import requests
from django.conf import settings
from rest_framework.exceptions import APIException, PermissionDenied
from .security import decrypt_token, encrypt_token
from crm.models import MailboxSecret

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://gmail.googleapis.com/gmail/v1/users/me"

class GmailError(APIException):
    status_code = 502
    default_detail = "Gmail request failed."
    default_code = "gmail_error"
    def __init__(self, detail=None, status_code=None, code="gmail_error"):
        if status_code: self.status_code = status_code
        self.default_code = code
        super().__init__(detail or self.default_detail, code=code)

def oauth_configured():
    return all([settings.GOOGLE_CLIENT_ID, settings.GOOGLE_CLIENT_SECRET, settings.GOOGLE_REDIRECT_URI])

def authorization_url(state, mode, login_hint=""):
    if not oauth_configured(): raise APIException("Google OAuth is not configured yet.")
    scopes = ["openid", "email", "profile", "https://www.googleapis.com/auth/gmail.readonly"]
    if mode == "manage": scopes = ["openid", "email", "profile", "https://www.googleapis.com/auth/gmail.modify"]
    params = {"client_id":settings.GOOGLE_CLIENT_ID, "redirect_uri":settings.GOOGLE_REDIRECT_URI, "response_type":"code",
        "scope":" ".join(scopes), "access_type":"offline", "prompt":"consent", "include_granted_scopes":"true", "state":state}
    if login_hint: params["login_hint"] = login_hint
    return AUTH_URL + "?" + urlencode(params)

def exchange_code(code):
    response = requests.post(TOKEN_URL, data={"code":code, "client_id":settings.GOOGLE_CLIENT_ID,
        "client_secret":settings.GOOGLE_CLIENT_SECRET, "redirect_uri":settings.GOOGLE_REDIRECT_URI,
        "grant_type":"authorization_code"}, timeout=20)
    if not response.ok: raise GmailError("Google OAuth token exchange failed.", status_code=400, code="oauth_exchange_failed")
    return response.json()

def _api(connection, method, path, *, params=None, json=None, data=None, timeout=25):
    try:
        secret = connection.secret
        refresh = decrypt_token(secret.encrypted_refresh_token)
    except MailboxSecret.DoesNotExist as exc:
        raise GmailError("The mailbox needs to be connected again.", status_code=409, code="missing_refresh_token") from exc
    try:
        response = requests.post(TOKEN_URL, data={"client_id":settings.GOOGLE_CLIENT_ID,
            "client_secret":settings.GOOGLE_CLIENT_SECRET, "refresh_token":refresh, "grant_type":"refresh_token"}, timeout=20)
    except requests.Timeout as exc:
        raise GmailError("Google token refresh timed out before the Gmail operation.", status_code=503, code="token_refresh_timeout") from exc
    except requests.RequestException as exc:
        raise GmailError("Google token refresh could not reach Google.", status_code=503, code="token_refresh_unavailable") from exc
    if response.status_code in (400, 401):
        connection.status = connection.Status.REAUTH
        connection.save(update_fields=["status", "updated_at"])
        raise GmailError("Gmail authorization expired or was revoked.", status_code=409, code="reauthorization_required")
    if not response.ok: raise GmailError("Could not refresh Gmail authorization.", status_code=502, code="token_refresh_failed")
    access = response.json()["access_token"]
    try:
        result = requests.request(method, f"{API}/{path}", headers={"Authorization":f"Bearer {access}"},
            params=params, json=json, data=data, timeout=timeout)
    except requests.Timeout as exc:
        raise GmailError("Gmail request timed out; its outcome may be unknown.", status_code=504, code="gmail_timeout") from exc
    if not result.ok:
        error = result.json().get("error", {}) if result.headers.get("content-type", "").startswith("application/json") else {}
        if result.status_code in (401, 403) and error.get("status") == "UNAUTHENTICATED":
            connection.status = connection.Status.REAUTH; connection.save(update_fields=["status", "updated_at"])
        raise GmailError("Gmail rejected the request.", status_code=502 if result.status_code >= 500 else 400,
            code=f"gmail_{result.status_code}")
    return result.json() if result.content else {}

def profile(connection): return _api(connection, "GET", "profile")
def list_messages(connection, query="", page_token=None, max_results=25, label_ids=None):
    params = {"maxResults":max(1, min(int(max_results), 50))}
    if query: params["q"] = query[:500]
    if page_token: params["pageToken"] = page_token
    if label_ids: params["labelIds"] = label_ids
    return _api(connection, "GET", "messages", params=params)
def list_threads(connection, query="", page_token=None, max_results=25):
    params = {"maxResults":max(1, min(int(max_results), 50))}
    if query: params["q"] = query[:500]
    if page_token: params["pageToken"] = page_token
    return _api(connection, "GET", "threads", params=params)
def get_thread(connection, thread_id): return _api(connection, "GET", f"threads/{thread_id}", params={"format":"full"})
def get_message(connection, message_id): return _api(connection, "GET", f"messages/{message_id}", params={"format":"full"})
def get_labels(connection): return _api(connection, "GET", "labels")
def list_drafts(connection, page_token=None, max_results=25):
    params={"maxResults":max(1,min(int(max_results),50))}
    if page_token: params["pageToken"]=page_token
    return _api(connection,"GET","drafts",params=params)
def update_draft(connection, draft_id, recipients, subject, body, thread_id=None):
    thread_headers=recipients.get("thread_headers")
    raw=_make_mime(connection.email_address,recipients,subject,body,"draft-"+__import__("secrets").token_hex(12),thread_headers)
    message={"raw":raw}
    if thread_id: message["threadId"]=thread_id
    return _api(connection,"PUT",f"drafts/{draft_id}",json={"id":draft_id,"message":message})
def modify_message(connection, message_id, add=(), remove=()):
    return _api(connection, "POST", f"messages/{message_id}/modify", json={"addLabelIds":list(add), "removeLabelIds":list(remove)})
def archive_message(connection, message_id): return modify_message(connection, message_id, remove=["INBOX"])
def attachment(connection, message_id, attachment_id): return _api(connection, "GET", f"messages/{message_id}/attachments/{attachment_id}")

def _make_mime(sender, recipients, subject, body, message_id, thread_headers=None):
    msg = EmailMessage()
    msg["From"] = sender
    for field in ("to", "cc", "bcc"):
        if recipients.get(field): msg[field.title()] = ", ".join(recipients[field])
    msg["Subject"] = subject
    msg["Message-ID"] = f"<{message_id}@mail.monataglobal.com>"
    if thread_headers:
        if thread_headers.get("In-Reply-To"): msg["In-Reply-To"] = thread_headers["In-Reply-To"]
        if thread_headers.get("References"): msg["References"] = thread_headers["References"]
    msg.set_content(body)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")

def create_draft(connection, recipients, subject, body, thread_id=None):
    thread_headers=recipients.get("thread_headers")
    raw = _make_mime(connection.email_address, recipients, subject, body, "draft-" + __import__("secrets").token_hex(12), thread_headers)
    message={"raw":raw}
    if thread_id: message["threadId"]=thread_id
    return _api(connection, "POST", "drafts", json={"message":message})

def send(connection, scheduled_email):
    raw = _make_mime(connection.email_address, scheduled_email.recipient_data, scheduled_email.subject,
        scheduled_email.body_text, str(scheduled_email.idempotency_key), scheduled_email.recipient_data.get("thread_headers"))
    body = {"raw":raw}
    if scheduled_email.provider_thread_id: body["threadId"] = scheduled_email.provider_thread_id
    return _api(connection, "POST", "messages/send", json=body, timeout=40)

def find_sent_by_idempotency(connection, idempotency_key):
    mid = f"{idempotency_key}@mail.monataglobal.com"
    return list_messages(connection, query=f"in:sent rfc822msgid:{mid}", max_results=3).get("messages", [])

def revoke(connection):
    token = decrypt_token(connection.secret.encrypted_refresh_token)
    try: requests.post("https://oauth2.googleapis.com/revoke", data={"token":token}, timeout=15)
    except requests.RequestException: pass

class _TextOnly(HTMLParser):
    def __init__(self): super().__init__(); self.parts=[]; self.skip=0
    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"): self.skip += 1
        elif tag in ("br", "p", "div", "li"): self.parts.append("\n")
    def handle_endtag(self, tag):
        if tag in ("script", "style", "head") and self.skip: self.skip -= 1
        elif tag in ("p", "div", "li"): self.parts.append("\n")
    def handle_data(self, data):
        if not self.skip: self.parts.append(data)

def parse_message(message):
    payload = message.get("payload", {}); headers = {h["name"].lower():h["value"] for h in payload.get("headers", [])}
    candidates=[]; attachments=[]
    def visit(part):
        filename=part.get("filename")
        body=part.get("body", {})
        if filename:
            attachments.append({"attachment_id":body.get("attachmentId"), "filename":filename, "mime_type":part.get("mimeType", "application/octet-stream"), "size":body.get("size",0)})
        elif part.get("mimeType") == "text/plain" and body.get("data"):
            candidates.append(body["data"])
        elif part.get("mimeType") == "text/html" and body.get("data") and not candidates:
            candidates.append(("html", body["data"]))
        for child in part.get("parts", []): visit(child)
    visit(payload)
    body_text=""
    if candidates:
        candidate=candidates[0]
        if isinstance(candidate, tuple):
            raw=base64.urlsafe_b64decode(candidate[1] + "==").decode("utf-8", "replace")
            parser=_TextOnly(); parser.feed(raw); body_text="".join(parser.parts)
        else: body_text=base64.urlsafe_b64decode(candidate + "==").decode("utf-8", "replace")
    return {"id":message.get("id"), "thread_id":message.get("threadId"), "snippet":message.get("snippet", ""),
        "from":headers.get("from", ""), "to":headers.get("to", ""), "cc":headers.get("cc", ""),
        "subject":headers.get("subject", ""), "date":headers.get("date", ""), "body_text":body_text,
        "label_ids":message.get("labelIds", []), "attachments":attachments}
