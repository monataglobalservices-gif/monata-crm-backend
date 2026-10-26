import base64
import hashlib
import html
import json
import re
import email.utils
from datetime import timedelta
from urllib.parse import urlencode
from uuid import UUID
import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import EmailMultiAlternatives
from django.db import IntegrityError, transaction
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.exceptions import APIException, PermissionDenied, ValidationError, NotFound
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from crm.models import (Workspace, Membership, Client, DepartmentAccess, MailboxConnection, MailboxSecret, MailboxInvitation, OAuthTransaction,
    MailboxAgentAccess, EmailAutomationRule, EmailApproval, ScheduledEmail, EmailSendAttempt, ScheduledEmailEvent,
    MailAuditEvent, RecurringEmailSchedule, EmailNotification)
from . import google
from .security import random_secret, digest_secret, encrypt_token
from .logic import (audit, event, notify, canonical_content, normalize_recipients, get_access,
    validate_rule_templates, validate_rule_message)

API_PREFIX = "/api/v1/mail"

class EmailDeliveryUnavailable(APIException):
    status_code = 503
    default_detail = "Invitation email delivery is unavailable. Check SMTP configuration and try again."
    default_code = "email_delivery_unavailable"

def _html_page(title, content):
    response=HttpResponse(f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'><title>{html.escape(title)}</title><style>body{{font:16px system-ui;max-width:760px;margin:48px auto;padding:0 20px;color:#17243a}}section{{border:1px solid #ddd;border-radius:12px;padding:24px}}button{{padding:12px 20px;margin:8px;border:0;border-radius:8px;background:#163355;color:white;font-size:16px;cursor:pointer}}button.secondary{{background:#666}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#f5f6f8;padding:16px;border-radius:8px}}.muted{{color:#555}}</style></head><body><section>{content}</section></body></html>", content_type="text/html; charset=utf-8")
    response["Cache-Control"]="no-store"; response["Referrer-Policy"]="no-referrer"; response["X-Content-Type-Options"]="nosniff"
    response["Content-Security-Policy"]="default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    return response

class MailAuthView(APIView):
    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        memberships = Membership.objects.filter(user=request.user, is_active=True).select_related("workspace")
        workspace_id = request.headers.get("X-Workspace-ID") or request.query_params.get("workspace")
        membership = memberships.filter(workspace_id=workspace_id).first() if workspace_id else memberships.order_by("created_at").first()
        if not membership: raise PermissionDenied("You do not belong to an active workspace.")
        request.workspace, request.membership = membership.workspace, membership

    def require_role(self, request, *roles):
        if request.membership.role not in roles: raise PermissionDenied("Your workspace role cannot perform this action.")

    def connection(self, request, connection_id):
        connection = get_object_or_404(MailboxConnection, id=connection_id, workspace=request.workspace)
        return connection

    def assigned_connection(self, request, connection_id, permission="can_read"):
        connection = self.connection(request, connection_id)
        access = get_access(connection, request.user, permission)
        return connection, access

def _mailbox_data(connection):
    return {"id":str(connection.id), "client_id":str(connection.client_id), "client_name":connection.client.name,
        "email_address":connection.email_address, "provider":connection.provider, "status":connection.status,
        "access_mode":connection.access_mode, "granted_scopes":connection.granted_scopes,
        "connected_at":connection.connected_at, "sends_paused":connection.sends_paused}

def _mail_job_data(job):
    return {"id":str(job.id), "client_id":str(job.client_id), "connection_id":str(job.connection_id),
        "created_by":str(job.created_by_id) if job.created_by_id else None, "status":job.status,
        "recipients":job.recipient_data, "subject":job.subject, "body_text":job.body_text,
        "scheduled_at_utc":job.scheduled_at_utc, "timezone":job.timezone, "content_hash":job.content_hash,
        "attempt_count":job.attempt_count, "provider_message_id":job.provider_message_id,
        "last_error_code":job.last_error_code, "sent_at":job.sent_at, "created_at":job.created_at}

def _public_api(path): return f"{settings.PUBLIC_API_BASE_URL.rstrip('/')}{path}"
def _approval_url(token): return _public_api(f"{API_PREFIX}/approvals/{token}/")
def _rule_activation_url(token): return _public_api(f"{API_PREFIX}/rules/activate/{token}/")

def _send_mailbox_invitation(invitation, url):
    if not settings.EMAIL_HOST or not settings.DEFAULT_FROM_EMAIL:
        raise EmailDeliveryUnavailable("Invitation email is not configured. Set EMAIL_HOST and DEFAULT_FROM_EMAIL in the environment.")
    mode = "read-only access to the Gmail inbox" if invitation.access_mode == "read" else (
        "read access, draft preparation, label management, and sending messages only after your approval")
    subject = f"{invitation.created_by.get_full_name() or invitation.created_by.email} requests Gmail access for {invitation.client.name}"
    text = (
        f"Hello,\n\n{invitation.created_by.get_full_name() or invitation.created_by.email} at MONATA Global Services "
        f"is requesting {mode} for {invitation.expected_email} on behalf of {invitation.client.name}.\n\n"
        "Review the request and choose whether to accept or decline. If you accept, Google will show the exact permissions "
        "before granting access. Your mailbox will not connect unless you accept here and authorize Google. "
        "Declining closes this invitation. MONATA will never ask for your Google password.\n\n"
        f"Review request: {url}\n\nThis link expires {invitation.expires_at.isoformat()}."
    )
    html_body = (
        "<!doctype html><html><body style='margin:0;background:#f3f6fa;font-family:Arial,sans-serif;color:#17243a'>"
        "<div style='max-width:600px;margin:32px auto;padding:0 16px'><div style='background:#fff;border:1px solid #e2e8f0;border-radius:16px;padding:32px'>"
        "<div style='font-size:13px;letter-spacing:.12em;font-weight:700;color:#46607d'>MONATA GLOBAL SERVICES</div>"
        "<h1 style='font-size:25px;margin:18px 0 12px'>Gmail access request</h1>"
        f"<p><strong>{html.escape(invitation.created_by.get_full_name() or invitation.created_by.email)}</strong> is requesting access to help with "
        f"<strong>{html.escape(invitation.client.name)}</strong>.</p>"
        f"<div style='background:#f5f8fc;border-radius:10px;padding:16px;margin:20px 0'><div style='font-size:13px;color:#52647a'>MAILBOX</div>"
        f"<div style='font-weight:700;margin-top:5px'>{html.escape(invitation.expected_email)}</div><div style='margin-top:12px;font-size:13px;color:#52647a'>REQUESTED ACCESS</div>"
        f"<div style='margin-top:5px'>{html.escape(mode)}</div></div>"
        "<p>Review the request and choose <strong>Accept</strong> or <strong>Decline</strong>. Accepting takes you to Google, where you can review and grant the requested permissions. "
        "The mailbox connects only after both steps. Declining closes the request.</p>"
        f"<p style='text-align:center;margin:28px 0'><a href='{html.escape(url, quote=True)}' style='display:inline-block;background:#174a78;color:#fff;text-decoration:none;padding:14px 24px;border-radius:9px;font-weight:700'>Review request</a></p>"
        "<p style='font-size:13px;color:#607086'>MONATA will never ask for your Google password. This one-use request expires "
        f"{html.escape(timezone.localtime(invitation.expires_at).strftime('%B %d, %Y at %I:%M %p %Z'))}.</p>"
        "</div><p style='text-align:center;color:#76869a;font-size:12px'>If you were not expecting this request, you can ignore this email.</p></div></body></html>"
    )
    message = EmailMultiAlternatives(subject, text, settings.DEFAULT_FROM_EMAIL, [invitation.expected_email])
    message.attach_alternative(html_body, "text/html")
    try:
        if message.send(fail_silently=False) != 1:
            raise RuntimeError("Email backend did not confirm delivery.")
    except Exception as exc:
        raise EmailDeliveryUnavailable("Could not send the invitation email. Check SMTP settings and try again.") from exc

def _new_approval(job, actor):
    raw = random_secret()
    approval = EmailApproval.objects.create(workspace=job.workspace, scheduled_email=job, requested_by=actor,
        content_hash=job.content_hash, token_hash=digest_secret(raw), expires_at=max(timezone.now()+timedelta(hours=settings.MAIL_APPROVAL_TTL_HOURS),(job.scheduled_at_utc or timezone.now())+timedelta(days=1)))
    job.approval = approval; job.status = ScheduledEmail.Status.APPROVAL
    job.save(update_fields=["approval", "status", "updated_at"])
    event(job, "approval_requested", actor)
    notify(actor, job.workspace, "approval_requested", f"Client approval requested for: {job.subject}", job.id)
    return raw

def _follow_up_due(rule, now):
    from datetime import datetime, time as dt_time, timezone as dt_timezone
    from zoneinfo import ZoneInfo
    zone=ZoneInfo(rule.timezone); local_now=now.astimezone(zone); eligible=[]
    allowed=set(rule.allowed_weekdays or range(7))
    for offset in range(1,367):
        day=local_now.date()+timedelta(days=offset)
        if day.weekday() in allowed and day.isoformat() not in rule.excluded_dates:
            eligible.append(day)
            if len(eligible)>=max(1,rule.wait_business_days): break
    if not eligible: raise ValidationError("Could not find a valid follow-up date under this rule.")
    send_time=rule.allowed_start or dt_time(10,0)
    if rule.allowed_end and send_time>=rule.allowed_end: raise ValidationError("The rule's permitted sending window does not fit its follow-up time.")
    candidate=datetime.combine(eligible[-1],send_time,zone)
    if candidate.astimezone(dt_timezone.utc).astimezone(zone).replace(tzinfo=None)!=candidate.replace(tzinfo=None):
        candidate=datetime.combine(eligible[-1]+timedelta(days=1),send_time,zone)
    return candidate.astimezone(dt_timezone.utc)

class MailboxListView(MailAuthView):
    def get(self, request):
        if request.membership.role in (Membership.Role.OWNER, Membership.Role.ADMIN):
            rows=MailboxConnection.objects.filter(workspace=request.workspace).select_related("client").order_by("-created_at")
        else:
            rows=MailboxConnection.objects.filter(workspace=request.workspace, agent_access__user=request.user,
                agent_access__is_active=True).select_related("client").distinct().order_by("-created_at")
        return Response([_mailbox_data(row) for row in rows])

class MailboxInvitationView(MailAuthView):
    @transaction.atomic
    def post(self, request):
        has_va_access = DepartmentAccess.objects.filter(workspace=request.workspace, user=request.user,
            department=DepartmentAccess.Department.VIRTUAL_ASSISTANCE, is_active=True).exists()
        if request.membership.role not in (Membership.Role.OWNER, Membership.Role.ADMIN) and not has_va_access:
            raise PermissionDenied("Virtual Assistance department access is required to request a client mailbox connection.")
        client=get_object_or_404(Client, id=request.data.get("client_id"), workspace=request.workspace)
        expected_email=str(request.data.get("email", client.email)).strip().lower()
        from django.core.validators import validate_email
        try: validate_email(expected_email)
        except Exception as exc: raise ValidationError({"email":"Provide the exact Gmail address the client should connect."}) from exc
        mode=request.data.get("access_mode", "read")
        if mode not in ("read", "manage"): raise ValidationError({"access_mode":"Choose read or manage."})
        raw=random_secret()
        invitation=MailboxInvitation.objects.create(workspace=request.workspace, client=client, expected_email=expected_email, created_by=request.user,
            token_hash=digest_secret(raw), access_mode=mode,
            expires_at=timezone.now()+timedelta(hours=settings.MAIL_INVITATION_TTL_HOURS))
        connect_url=_public_api(f"{API_PREFIX}/connect/{raw}/")
        _send_mailbox_invitation(invitation, connect_url)
        audit(request.workspace, "mailbox_invitation_created", actor=request.user, client=client,
            target_type="mailbox_invitation", target_id=invitation.id, metadata={"access_mode":mode,"email_sent":True})
        return Response({"id":str(invitation.id), "client_name":client.name, "email":expected_email, "access_mode":mode,
            "expires_at":invitation.expires_at, "email_sent":True}, status=201)

class MailboxConnectView(APIView):
    permission_classes=[AllowAny]
    authentication_classes=[]

    def _consent_page(self, invitation, token, error=""):
        mode = "read-only access to the inbox" if invitation.access_mode == "read" else (
            "read access, draft preparation, label management, and sending only after your approval")
        action = "Continue to Google" if invitation.status == MailboxInvitation.Status.ACCEPTED else "Accept and continue to Google"
        error_html = f"<p style='color:#a32121;font-weight:600'>{html.escape(error)}</p>" if error else ""
        body = (
            "<div style='font-size:13px;letter-spacing:.12em;font-weight:700;color:#46607d'>MONATA GLOBAL SERVICES</div>"
            "<h1>Review Gmail access request</h1>"
            f"{error_html}<p><strong>{html.escape(invitation.created_by.get_full_name() or invitation.created_by.email)}</strong> requests access for "
            f"<strong>{html.escape(invitation.client.name)}</strong>.</p>"
            f"<div style='background:#f5f8fc;border-radius:10px;padding:16px;margin:20px 0'><div style='font-size:13px;color:#52647a'>GMAIL ACCOUNT</div>"
            f"<p style='font-weight:700'>{html.escape(invitation.expected_email)}</p><div style='font-size:13px;color:#52647a'>REQUESTED ACCESS</div>"
            f"<p>{html.escape(mode)}</p></div>"
            "<p>Accepting takes you to Google to review the exact permissions. Your Gmail account connects only if you grant those permissions. "
            "Declining closes this invitation, and no mailbox will be connected. MONATA will never ask for your Google password.</p>"
            f"<form method='post' action='{API_PREFIX}/connect/{html.escape(token, quote=True)}/'>"
            f"<label>Confirm the invited email address <input name='email' type='email' required autocomplete='email' value='{html.escape(invitation.expected_email, quote=True)}' style='box-sizing:border-box;width:100%;padding:12px;margin:8px 0 16px;border:1px solid #cbd5e1;border-radius:8px'></label><br>"
            f"<button name='decision' value='accept'>{action}</button>"
            "<button class='secondary' name='decision' value='decline'>Decline request</button></form>"
            f"<p class='muted'>This request expires {html.escape(timezone.localtime(invitation.expires_at).strftime('%B %d, %Y at %I:%M %p %Z'))}.</p>"
        )
        return _html_page("Review Gmail access request", body)

    def get(self, request, token):
        invitation=MailboxInvitation.objects.filter(token_hash=digest_secret(token), status__in=[MailboxInvitation.Status.PENDING,MailboxInvitation.Status.ACCEPTED],
            expires_at__gt=timezone.now()).select_related("client", "created_by").first()
        if not invitation:
            declined=MailboxInvitation.objects.filter(token_hash=digest_secret(token),status=MailboxInvitation.Status.DECLINED).exists()
            if declined:
                return _html_page("Request declined", "<h1>You declined this Gmail access request.</h1><p>No mailbox was connected. You can close this page.</p>")
            return _html_page("Invitation unavailable", "<h1>This Gmail invitation is expired or has already been used.</h1>")
        return self._consent_page(invitation, token)

    def post(self, request, token):
        decision=str(request.data.get("decision", ""))
        email=str(request.data.get("email", "")).strip().lower()
        if decision not in ("accept", "decline"):
            return _html_page("Invalid response", "<h1>Choose Accept or Decline.</h1>")
        with transaction.atomic():
            invitation=MailboxInvitation.objects.select_for_update().filter(token_hash=digest_secret(token),
                status__in=[MailboxInvitation.Status.PENDING,MailboxInvitation.Status.ACCEPTED],
                expires_at__gt=timezone.now()).select_related("client", "created_by").first()
            if not invitation:
                return _html_page("Invitation unavailable", "<h1>This Gmail invitation is expired or has already been used.</h1>")
            if email != invitation.expected_email.lower():
                return self._consent_page(invitation, token, "Enter the invited email address to confirm your response.")
            if decision == "decline":
                invitation.status=MailboxInvitation.Status.DECLINED
                invitation.responded_at=timezone.now()
                invitation.responded_by_email=email
                invitation.save(update_fields=["status","responded_at","responded_by_email","updated_at"])
                audit(invitation.workspace,"mailbox_invitation_declined",client=invitation.client,
                    target_type="mailbox_invitation",target_id=invitation.id,metadata={"email":email})
                notify(invitation.created_by, invitation.workspace, "mailbox_invitation_declined",
                    f"{email} declined the Gmail access request for {invitation.client.name}.", str(invitation.id))
                return _html_page("Request declined", "<h1>Gmail access request declined</h1><p>No mailbox was connected. The requesting MONATA agent has been notified.</p>")
            if invitation.status == MailboxInvitation.Status.PENDING:
                invitation.status=MailboxInvitation.Status.ACCEPTED
                invitation.responded_at=timezone.now()
                invitation.responded_by_email=email
                invitation.save(update_fields=["status","responded_at","responded_by_email","updated_at"])
                audit(invitation.workspace,"mailbox_invitation_accepted",client=invitation.client,
                    target_type="mailbox_invitation",target_id=invitation.id,metadata={"email":email})
                notify(invitation.created_by, invitation.workspace, "mailbox_invitation_accepted",
                    f"{email} accepted the Gmail access request for {invitation.client.name}; Google authorization is still required.", str(invitation.id))
            if not google.oauth_configured():
                return _html_page("Gmail setup incomplete", "<h1>Gmail connection is not configured yet.</h1><p>Your acceptance is recorded, but Google OAuth is not ready. Contact MONATA to complete setup.</p>")
            raw_state=random_secret()
            OAuthTransaction.objects.create(invitation=invitation,state_hash=digest_secret(raw_state),
                expires_at=timezone.now()+timedelta(minutes=10))
        return HttpResponseRedirect(google.authorization_url(raw_state, invitation.access_mode, invitation.expected_email))

class GmailOAuthCallbackView(APIView):
    permission_classes=[AllowAny]
    authentication_classes=[]
    def get(self, request):
        state=request.query_params.get("state", "")
        if request.query_params.get("error"):
            return _html_page("Gmail connection cancelled", "<h1>Gmail was not connected.</h1><p>You can close this page or ask MONATA for a new connection link.</p>")
        with transaction.atomic():
            tx=OAuthTransaction.objects.select_for_update().filter(state_hash=digest_secret(state), used_at__isnull=True,
                expires_at__gt=timezone.now()).select_related("invitation__workspace", "invitation__client", "invitation__created_by").first()
            if not tx: return _html_page("Connection unavailable", "<h1>This OAuth request expired or was already used.</h1>")
            tx.used_at=timezone.now(); tx.save(update_fields=["used_at", "updated_at"])
            invite=tx.invitation
        try:
            tokens=google.exchange_code(request.query_params.get("code", ""))
            access=tokens.get("access_token"); refresh=tokens.get("refresh_token")
            if not access or not refresh: raise ValidationError("Google did not provide offline access. Create a new invitation and reconnect.")
            profile=requests.get("https://gmail.googleapis.com/gmail/v1/users/me/profile", headers={"Authorization":f"Bearer {access}"}, timeout=20)
            if not profile.ok: raise ValidationError("Could not identify the authorized Gmail account.")
            mailbox=profile.json(); email=mailbox.get("emailAddress", "")
            if email.lower() != invite.expected_email.lower(): raise ValidationError("The Google account does not match the email address selected by MONATA. Request a new link for the correct account.")
            granted=tokens.get("scope", "").split()
            needed="https://www.googleapis.com/auth/gmail.modify" if invite.access_mode == "manage" else "https://www.googleapis.com/auth/gmail.readonly"
            if needed not in granted: raise ValidationError("Google did not grant the required Gmail permission.")
            with transaction.atomic():
                invite=MailboxInvitation.objects.select_for_update().get(id=invite.id)
                if invite.status != MailboxInvitation.Status.ACCEPTED or invite.expires_at <= timezone.now(): raise ValidationError("This invitation is no longer valid or was declined.")
                connection=MailboxConnection.objects.create(workspace=invite.workspace, client=invite.client, provider_account_id=email,
                    email_address=email, access_mode=invite.access_mode, granted_scopes=granted, connected_by=invite.created_by, connected_at=timezone.now())
                MailboxSecret.objects.create(connection=connection, encrypted_refresh_token=encrypt_token(refresh))
                MailboxAgentAccess.objects.create(connection=connection, user=invite.created_by, can_read=True,
                    can_draft=invite.access_mode == "manage", can_send_approved=invite.access_mode == "manage",
                    can_manage_labels=invite.access_mode == "manage",client_approved_at=timezone.now(),client_approved_by_email=invite.expected_email)
                invite.status=MailboxInvitation.Status.CONNECTED; invite.used_at=timezone.now(); invite.save(update_fields=["status","used_at","updated_at"])
                audit(invite.workspace, "mailbox_connected", actor=invite.created_by, connection=connection, metadata={"access_mode":invite.access_mode,"scopes":granted})
                notify(invite.created_by, invite.workspace, "mailbox_connected",
                    f"{email} connected Gmail for {invite.client.name}.", str(connection.id))
        except IntegrityError:
            return _html_page("Mailbox already connected", "<h1>This Gmail account is already connected to this MONATA workspace.</h1>")
        except Exception as exc:
            return _html_page("Could not connect Gmail", f"<h1>Gmail connection failed.</h1><p>{html.escape(str(getattr(exc, 'detail', 'Please try again or request a new invitation.')))}</p>")
        return _html_page("Gmail connected", f"<h1>Gmail connected</h1><p>{html.escape(email)} is now connected for {html.escape(invite.client.name)}.</p><p>The assigned MONATA agent can now open the mailbox in MONATA CRM.</p>")

class MailboxAgentView(MailAuthView):
    def get(self, request, connection_id):
        self.require_role(request, Membership.Role.OWNER, Membership.Role.ADMIN)
        connection=self.connection(request, connection_id)
        accesses=MailboxAgentAccess.objects.filter(connection=connection, is_active=True).select_related("user")
        return Response([{"user_id":str(a.user_id),"email":a.user.email,"can_read":a.can_read,"can_draft":a.can_draft,
            "can_send_approved":a.can_send_approved,"can_send_routine":a.can_send_routine,"can_manage_labels":a.can_manage_labels,
            "can_manage_rules":a.can_manage_rules,"expires_at":a.expires_at,"client_approved_at":a.client_approved_at,
            "client_approved_by_email":a.client_approved_by_email} for a in accesses])
    def post(self, request, connection_id):
        self.require_role(request, Membership.Role.OWNER, Membership.Role.ADMIN)
        connection=self.connection(request, connection_id)
        user=get_user_model().objects.filter(email__iexact=request.data.get("email", ""), memberships__workspace=request.workspace, memberships__is_active=True).exclude(memberships__workspace=request.workspace,memberships__role=Membership.Role.CLIENT).distinct().first()
        if not user: raise ValidationError({"email":"User must have an active membership in this workspace."})
        fields={k:request.data.get(k, False) is True for k in ("can_read","can_draft","can_send_approved","can_send_routine","can_manage_labels","can_manage_rules")}
        if not fields["can_read"]: raise ValidationError({"can_read":"Mailbox access must include read permission."})
        if connection.access_mode == "read" and any(fields[k] for k in ("can_draft","can_send_approved","can_send_routine","can_manage_labels")):
            raise ValidationError("This connection is read-only; reconnect with mailbox management permission first.")
        raw=random_secret()
        access,created=MailboxAgentAccess.objects.update_or_create(connection=connection,user=user,defaults={**fields,"is_active":False,
            "client_approved_at":None,"client_approved_by_email":"","approval_token_hash":digest_secret(raw),"approval_expires_at":timezone.now()+timedelta(hours=settings.MAIL_INVITATION_TTL_HOURS)})
        audit(request.workspace,"mailbox_agent_approval_requested",actor=request.user,connection=connection,
            target_type="mailbox_agent_access",target_id=access.id,metadata={"agent_id":str(user.id),**fields})
        return Response({"user_id":str(user.id),"email":user.email,"permissions":fields,"active":False,
            "client_approval_url":_public_api(f"{API_PREFIX}/agent-approvals/{raw}/")},status=201 if created else 200)
    def delete(self, request, connection_id):
        self.require_role(request, Membership.Role.OWNER, Membership.Role.ADMIN)
        connection=self.connection(request, connection_id); user_id=request.query_params.get("user_id")
        access=get_object_or_404(MailboxAgentAccess,connection=connection,user_id=user_id)
        access.is_active=False; access.save(update_fields=["is_active","updated_at"])
        audit(request.workspace,"mailbox_agent_unassigned",actor=request.user,connection=connection,target_type="mailbox_agent_access",target_id=access.id)
        for job in ScheduledEmail.objects.filter(connection=connection,assigned_agent_id=user_id,status__in=[ScheduledEmail.Status.SCHEDULED,ScheduledEmail.Status.APPROVAL]):
            job.status=ScheduledEmail.Status.CANCELLED; job.cancelled_at=timezone.now(); job.save(update_fields=["status","cancelled_at","updated_at"])
            job.approvals.filter(status=EmailApproval.Status.PENDING).update(status=EmailApproval.Status.INVALIDATED)
        return Response(status=204)

class MailboxControlView(MailAuthView):
    def post(self, request, connection_id, action):
        self.require_role(request, Membership.Role.OWNER, Membership.Role.ADMIN)
        connection=self.connection(request,connection_id)
        if action == "pause":
            connection.sends_paused=True; connection.save(update_fields=["sends_paused","updated_at"])
            audit(request.workspace,"mailbox_sends_paused",actor=request.user,connection=connection)
        elif action == "resume":
            if connection.status != MailboxConnection.Status.ACTIVE: raise ValidationError("Reconnect this mailbox before resuming sends.")
            connection.sends_paused=False; connection.save(update_fields=["sends_paused","updated_at"])
            audit(request.workspace,"mailbox_sends_resumed",actor=request.user,connection=connection)
        elif action == "disconnect":
            from .google import revoke
            revoke(connection); connection.status=MailboxConnection.Status.REVOKED; connection.revoked_at=timezone.now(); connection.sends_paused=True
            connection.save(update_fields=["status","revoked_at","sends_paused","updated_at"])
            MailboxSecret.objects.filter(connection=connection).delete()
            MailboxAgentAccess.objects.filter(connection=connection).update(is_active=False)
            ScheduledEmail.objects.filter(connection=connection,status__in=[ScheduledEmail.Status.SCHEDULED,ScheduledEmail.Status.APPROVAL]).update(status=ScheduledEmail.Status.CANCELLED,cancelled_at=timezone.now())
            audit(request.workspace,"mailbox_disconnected",actor=request.user,connection=connection)
        else: raise NotFound("Unknown mailbox action.")
        return Response(_mailbox_data(connection))

class MailboxAgentApprovalView(APIView):
    permission_classes=[AllowAny]; authentication_classes=[]
    def get(self,request,token):
        access=MailboxAgentAccess.objects.filter(approval_token_hash=digest_secret(token),is_active=False,approval_expires_at__gt=timezone.now()).select_related("connection__client","user").first()
        if not access: return _html_page("Agent access unavailable","<h1>This agent authorization link is invalid or already used.</h1>")
        perms=[label for key,label in (("can_read","read email"),("can_draft","create drafts"),("can_send_approved","send client-approved messages"),("can_send_routine","send messages under client-approved rules"),("can_manage_labels","organize labels"),("can_manage_rules","manage send rules")) if getattr(access,key)]
        body=f"<h1>Authorize MONATA agent access</h1><p>Client: <b>{html.escape(access.connection.client.name)}</b></p><p>Mailbox: <b>{html.escape(access.connection.email_address)}</b></p><p>Agent: <b>{html.escape(access.user.email)}</b></p><p>Requested permissions: {html.escape(', '.join(perms))}</p><p>Link expires: {html.escape(access.approval_expires_at.isoformat())}</p><form method='post' action='{API_PREFIX}/agent-approvals/{token}/'><label>Client contact email <input name='email' type='email' autocomplete='email'></label><button name='decision' value='approve'>Authorize this agent</button><button class='secondary' name='decision' value='reject'>Reject</button></form>"
        return _html_page("Authorize MONATA agent",body)
    def post(self,request,token):
        with transaction.atomic():
            access=MailboxAgentAccess.objects.select_for_update().filter(approval_token_hash=digest_secret(token),is_active=False,approval_expires_at__gt=timezone.now()).select_related("connection__client","user").first()
            if not access: raise ValidationError("This authorization link is invalid or already used.")
            decision=request.data.get("decision")
            if decision=="approve":
                if access.connection.status != MailboxConnection.Status.ACTIVE: raise ValidationError("This mailbox is no longer active.")
                if access.connection.client.email and str(request.data.get("email", "")).lower()!=access.connection.client.email.lower(): raise PermissionDenied("Enter the client's registered contact email to approve this agent.")
                access.is_active=True; access.client_approved_at=timezone.now(); access.client_approved_by_email=str(request.data.get("email", "")).lower()
                event_type="mailbox_agent_client_approved"
            elif decision=="reject":
                access.is_active=False; access.client_approved_at=None; access.client_approved_by_email=str(request.data.get("email", "")).lower(); event_type="mailbox_agent_client_rejected"
            else: raise ValidationError({"decision":"Choose approve or reject."})
            access.approval_token_hash=""; access.approval_expires_at=None; access.save(update_fields=["is_active","client_approved_at","client_approved_by_email","approval_token_hash","approval_expires_at","updated_at"])
            audit(access.connection.workspace,event_type,connection=access.connection,client=access.connection.client,
                target_type="mailbox_agent_access",target_id=access.id,metadata={"agent_id":str(access.user_id)})
        return _html_page("Agent access updated", "<h1>Agent authorized</h1><p>The MONATA agent can access the approved mailbox permissions.</p>" if decision=="approve" else "<h1>Agent denied</h1><p>This agent cannot access the mailbox.</p>")

class GmailMessagesView(MailAuthView):
    def get(self, request, connection_id):
        connection,_=self.assigned_connection(request,connection_id,"can_read")
        query=request.query_params.get("q", ""); page=request.query_params.get("page_token")
        try:
            labels=request.query_params.getlist("label")
            if not query and not labels: labels=["INBOX"]
            result=google.list_messages(connection,query,page,request.query_params.get("limit",25),labels)
            return Response({"messages":result.get("messages",[]),"next_page_token":result.get("nextPageToken"),"result_size_estimate":result.get("resultSizeEstimate",0)})
        except google.GmailError: raise

class GmailMessageView(MailAuthView):
    def get(self, request, connection_id, message_id):
        connection,_=self.assigned_connection(request,connection_id,"can_read")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}",message_id): raise ValidationError("Invalid Gmail message ID.")
        return Response(google.parse_message(google.get_message(connection,message_id)))

class GmailThreadView(MailAuthView):
    def get(self, request, connection_id, thread_id):
        connection,_=self.assigned_connection(request,connection_id,"can_read")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}",thread_id): raise ValidationError("Invalid Gmail thread ID.")
        result=google.get_thread(connection,thread_id)
        return Response({"id":result.get("id"),"history_id":result.get("historyId"),"messages":[google.parse_message(m) for m in result.get("messages",[])]})

class GmailLabelsView(MailAuthView):
    def get(self, request, connection_id):
        connection,_=self.assigned_connection(request,connection_id,"can_read")
        return Response(google.get_labels(connection).get("labels",[]))
    def post(self, request, connection_id):
        connection,_=self.assigned_connection(request,connection_id,"can_manage_labels")
        message_id=request.data.get("message_id", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}",message_id): raise ValidationError({"message_id":"Invalid Gmail message ID."})
        add=request.data.get("add",[]); remove=request.data.get("remove",[])
        if not isinstance(add,list) or not isinstance(remove,list) or len(add)+len(remove)>20: raise ValidationError("Provide at most 20 label changes.")
        return Response(google.modify_message(connection,message_id,add,remove))

class GmailArchiveView(MailAuthView):
    def post(self, request, connection_id, message_id):
        connection,_=self.assigned_connection(request,connection_id,"can_manage_labels")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}",message_id): raise ValidationError("Invalid Gmail message ID.")
        return Response(google.archive_message(connection,message_id))

class GmailAttachmentView(MailAuthView):
    def get(self, request, connection_id, message_id, attachment_id):
        connection,_=self.assigned_connection(request,connection_id,"can_read")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}",message_id) or not re.fullmatch(r"[A-Za-z0-9_-]{1,500}",attachment_id): raise ValidationError("Invalid Gmail attachment reference.")
        message=google.get_message(connection,message_id); parsed=google.parse_message(message)
        meta=next((a for a in parsed["attachments"] if a["attachment_id"]==attachment_id),None)
        if not meta: raise NotFound("Attachment not found on this message.")
        if meta["size"] > settings.MAIL_MAX_ATTACHMENT_BYTES: raise ValidationError("Attachment is larger than the configured download limit.")
        encoded=google.attachment(connection,message_id,attachment_id).get("data","")
        raw=base64.urlsafe_b64decode(encoded + "==")
        if len(raw)>settings.MAIL_MAX_ATTACHMENT_BYTES: raise ValidationError("Attachment is larger than the configured download limit.")
        filename=re.sub(r"[^A-Za-z0-9._-]", "_", meta["filename"])[:150] or "attachment.bin"
        response=HttpResponse(raw,content_type="application/octet-stream")
        response["Content-Disposition"]=f'attachment; filename="{filename}"'; response["X-Content-Type-Options"]="nosniff"
        response["Cache-Control"]="private, no-store"; response["Referrer-Policy"]="no-referrer"
        audit(request.workspace,"gmail_attachment_downloaded",actor=request.user,connection=connection,target_type="gmail_message",target_id=message_id,metadata={"filename":filename,"size":len(raw)})
        return response

class GmailDraftView(MailAuthView):
    def get(self,request,connection_id):
        connection,_=self.assigned_connection(request,connection_id,"can_read")
        result=google.list_drafts(connection,request.query_params.get("page_token"),request.query_params.get("limit",25))
        return Response({"drafts":result.get("drafts",[]),"next_page_token":result.get("nextPageToken")})
    def post(self, request, connection_id):
        connection,_=self.assigned_connection(request,connection_id,"can_draft")
        recipient_data=request.data.get("recipients",{})
        thread_id=""
        reply_to=request.data.get("reply_to_message_id")
        if reply_to:
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}",str(reply_to)): raise ValidationError({"reply_to_message_id":"Invalid Gmail message ID."})
            original=google.get_message(connection,str(reply_to)); payload=original.get("payload",{})
            headers={h["name"].lower():h["value"] for h in payload.get("headers",[])}
            if not recipient_data.get("to"):
                recipient_data={**recipient_data,"to":[address for _,address in email.utils.getaddresses([headers.get("reply-to") or headers.get("from","")]) if address]}
            references=" ".join(filter(None,[headers.get("references", ""),headers.get("message-id", "")]))[:2000]
            recipient_data={**recipient_data,"thread_headers":{"In-Reply-To":headers.get("message-id", ""),"References":references}}
            thread_id=original.get("threadId", "")
        recipients=normalize_recipients(recipient_data); subject=str(request.data.get("subject", "")).strip(); body=str(request.data.get("body_text", ""))
        if reply_to and not subject: subject=headers.get("subject", "") if str(headers.get("subject", "")).lower().startswith("re:") else "Re: " + headers.get("subject", "")
        if not subject or "\r" in subject or "\n" in subject or len(subject)>998 or len(body)>100000: raise ValidationError("A valid single-line subject is required and message body must be under 100 KB.")
        draft=google.create_draft(connection,recipients,subject,body,thread_id)
        audit(request.workspace,"gmail_draft_created",actor=request.user,connection=connection,target_type="gmail_draft",target_id=draft.get("id", ""))
        return Response({"draft_id":draft.get("id"),"message_id":draft.get("message",{}).get("id")},status=201)
    def put(self,request,connection_id,draft_id):
        connection,_=self.assigned_connection(request,connection_id,"can_draft")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}",draft_id): raise ValidationError("Invalid Gmail draft ID.")
        recipients=normalize_recipients(request.data.get("recipients",{})); subject=str(request.data.get("subject", "")).strip(); body=str(request.data.get("body_text", ""))
        if not subject or "\r" in subject or "\n" in subject or len(subject)>998 or len(body)>100000: raise ValidationError("A valid single-line subject is required and message body must be under 100 KB.")
        draft=google.update_draft(connection,draft_id,recipients,subject,body,request.data.get("thread_id"))
        audit(request.workspace,"gmail_draft_updated",actor=request.user,connection=connection,target_type="gmail_draft",target_id=draft.get("id",draft_id))
        return Response({"draft_id":draft.get("id"),"message_id":draft.get("message",{}).get("id")})

class ScheduledEmailListCreateView(MailAuthView):
    def get(self, request):
        rows=ScheduledEmail.objects.filter(workspace=request.workspace,assigned_agent=request.user).select_related("connection","client").order_by("-created_at")
        rows=rows.filter(connection__agent_access__user=request.user,connection__agent_access__is_active=True,
            connection__agent_access__can_read=True).distinct()
        if request.query_params.get("connection"): rows=rows.filter(connection_id=request.query_params["connection"])
        return Response([_mail_job_data(row) for row in rows[:200]])
    @transaction.atomic
    def post(self, request):
        connection_id=request.data.get("connection_id")
        connection=self.connection(request,connection_id)
        recipient_data=request.data.get("recipients",{}); thread_id=""
        reply_to=request.data.get("reply_to_message_id")
        if reply_to:
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}",str(reply_to)): raise ValidationError({"reply_to_message_id":"Invalid Gmail message ID."})
            original=google.get_message(connection,str(reply_to)); payload=original.get("payload",{})
            headers={h["name"].lower():h["value"] for h in payload.get("headers",[])}
            if not recipient_data.get("to"):
                recipient_data={**recipient_data,"to":[address for _,address in email.utils.getaddresses([headers.get("reply-to") or headers.get("from","")]) if address]}
            refs=" ".join(filter(None,[headers.get("references", ""),headers.get("message-id", "")]))[:2000]
            recipient_data={**recipient_data,"thread_headers":{"In-Reply-To":headers.get("message-id", ""),"References":refs}}
            thread_id=original.get("threadId", "")
        recipient=normalize_recipients(recipient_data)
        subject=str(request.data.get("subject", "")).strip(); body=str(request.data.get("body_text", ""))
        if reply_to and not subject: subject=headers.get("subject", "") if str(headers.get("subject", "")).lower().startswith("re:") else "Re: " + headers.get("subject", "")
        if not subject or "\r" in subject or "\n" in subject or len(subject)>998 or len(body)>100000: raise ValidationError("A valid single-line subject is required and message body must be under 100 KB.")
        timezone_name=str(request.data.get("timezone", "Africa/Nairobi"))
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(timezone_name)
        except Exception as exc: raise ValidationError({"timezone":"Unknown time zone."}) from exc
        due=request.data.get("scheduled_at")
        due=parse_datetime(due) if isinstance(due,str) else timezone.now()
        if due is None or timezone.is_naive(due): raise ValidationError({"scheduled_at":"Provide an ISO-8601 timestamp with timezone, or omit for send as soon as approved."})
        if due < timezone.now()-timedelta(seconds=30) or due > timezone.now()+timedelta(days=366): raise ValidationError({"scheduled_at":"Choose a time from now through the next 366 days."})
        rule=None; wait_for_reply_to=""
        if request.data.get("rule_id"):
            rule=get_object_or_404(EmailAutomationRule, id=request.data["rule_id"], workspace=request.workspace, connection=connection, client=connection.client)
            get_access(connection,request.user,"can_send_routine")
            if rule.trigger_type=="no_reply_follow_up":
                if not reply_to: raise ValidationError({"reply_to_message_id":"A no-reply follow-up must reference the original Gmail message."})
                if request.data.get("scheduled_at"): raise ValidationError("No-reply follow-ups use the client's approved business-day delay.")
                wait_for_reply_to=str(reply_to); due=_follow_up_due(rule,timezone.now()); timezone_name=rule.timezone
            validate_rule_message(rule,recipient,subject,body,due)
        else:
            get_access(connection,request.user,"can_send_approved")
        job=ScheduledEmail.objects.create(workspace=request.workspace,client=connection.client,connection=connection,
            created_by=request.user,assigned_agent=request.user,rule=rule,rule_version=rule.version if rule else None,
            recipient_data=recipient,subject=subject,body_text=body,provider_thread_id=thread_id,wait_for_reply_to_message_id=wait_for_reply_to,scheduled_at_utc=due,timezone=timezone_name,
            content_hash=canonical_content(recipient,subject,body,thread_id),status=ScheduledEmail.Status.SCHEDULED if rule else ScheduledEmail.Status.DRAFT)
        event(job,"created",request.user,{"rule_id":str(rule.id) if rule else None})
        audit(request.workspace,"scheduled_email_created",actor=request.user,connection=connection,target_type="scheduled_email",target_id=job.id,
            metadata={"status":job.status,"rule_id":str(rule.id) if rule else None})
        data=_mail_job_data(job)
        if not rule:
            raw=_new_approval(job,request.user); data["approval_url"]=_approval_url(raw)
        return Response(data,status=201)

class ScheduledEmailDetailView(MailAuthView):
    def get(self,request,job_id):
        job=get_object_or_404(ScheduledEmail, id=job_id, workspace=request.workspace)
        get_access(job.connection,request.user,"can_read")
        data=_mail_job_data(job)
        data["attempts"]=[{"attempt_number":x.attempt_number,"outcome":x.outcome,"error_code":x.error_code,"retryable":x.retryable,"provider_message_id":x.provider_message_id,"created_at":x.created_at} for x in job.attempts.order_by("attempt_number")]
        data["events"]=[{"event_type":x.event_type,"actor_id":str(x.actor_id) if x.actor_id else None,"safe_metadata":x.safe_metadata,"created_at":x.created_at} for x in job.events.order_by("created_at")]
        return Response(data)
    def patch(self,request,job_id):
        job=get_object_or_404(ScheduledEmail, id=job_id, workspace=request.workspace)
        if job.status not in (ScheduledEmail.Status.DRAFT,ScheduledEmail.Status.APPROVAL,ScheduledEmail.Status.SCHEDULED): raise ValidationError("This email can no longer be edited.")
        connection,_=self.assigned_connection(request,job.connection_id,"can_draft")
        old_hash=job.content_hash
        if "recipients" in request.data: job.recipient_data=normalize_recipients(request.data["recipients"])
        if "subject" in request.data: job.subject=str(request.data["subject"]).strip()
        if "body_text" in request.data: job.body_text=str(request.data["body_text"])
        if "timezone" in request.data: job.timezone=str(request.data["timezone"])
        if "scheduled_at" in request.data:
            due=parse_datetime(request.data["scheduled_at"])
            if not due or timezone.is_naive(due): raise ValidationError({"scheduled_at":"Use an ISO-8601 timestamp with timezone."})
            job.scheduled_at_utc=due
            if job.approval_id and job.approval.status == EmailApproval.Status.APPROVED:
                job.approval.expires_at=max(job.approval.expires_at,due+timedelta(days=1)); job.approval.save(update_fields=["expires_at","updated_at"])
        if not job.subject or "\r" in job.subject or "\n" in job.subject or len(job.subject)>998 or len(job.body_text)>100000: raise ValidationError("Invalid subject or body length.")
        job.content_hash=canonical_content(job.recipient_data,job.subject,job.body_text,job.provider_thread_id)
        approval_link=None
        if job.content_hash != old_hash:
            job.approvals.filter(status=EmailApproval.Status.PENDING).update(status=EmailApproval.Status.INVALIDATED)
            job.approval=None
            if job.rule_id:
                validate_rule_message(job.rule,job.recipient_data,job.subject,job.body_text,job.scheduled_at_utc)
                job.rule_version=job.rule.version; job.status=ScheduledEmail.Status.SCHEDULED
            else:
                get_access(connection,request.user,"can_send_approved")
                job.status=ScheduledEmail.Status.DRAFT
        job.save()
        event(job,"edited",request.user,{"content_changed":job.content_hash != old_hash})
        if job.content_hash != old_hash and not job.rule_id:
            raw=_new_approval(job,request.user); approval_link=_approval_url(raw)
        response=_mail_job_data(job)
        if approval_link: response["approval_url"]=approval_link
        return Response(response)
    def post(self,request,job_id,action):
        job=get_object_or_404(ScheduledEmail, id=job_id, workspace=request.workspace)
        get_access(job.connection,request.user,"can_draft")
        if action == "cancel":
            if job.status in (ScheduledEmail.Status.SENDING,ScheduledEmail.Status.SENT): raise ValidationError("Sending has started; this email cannot be cancelled safely.")
            if job.status in (ScheduledEmail.Status.CANCELLED,ScheduledEmail.Status.FAILED): raise ValidationError("This email is already closed.")
            job.status=ScheduledEmail.Status.CANCELLED; job.cancelled_at=timezone.now(); job.save(update_fields=["status","cancelled_at","updated_at"])
            job.approvals.filter(status=EmailApproval.Status.PENDING).update(status=EmailApproval.Status.INVALIDATED)
            event(job,"cancelled",request.user); audit(request.workspace,"scheduled_email_cancelled",actor=request.user,connection=job.connection,target_type="scheduled_email",target_id=job.id)
        elif action == "retry":
            if job.status == ScheduledEmail.Status.NEEDS_ATTENTION and not request.data.get("confirm_not_sent"):
                raise ValidationError({"confirm_not_sent":"The previous send outcome is unknown. Confirm manually that it was not sent before retrying."})
            if job.status not in (ScheduledEmail.Status.FAILED,ScheduledEmail.Status.NEEDS_ATTENTION): raise ValidationError("Only failed or needs-attention messages can be retried.")
            if job.rule_id:
                validate_rule_message(job.rule,job.recipient_data,job.subject,job.body_text,timezone.now())
                job.rule_version=job.rule.version
            elif not job.approval or job.approval.status != EmailApproval.Status.APPROVED or job.approval.content_hash != job.content_hash or job.approval.expires_at <= timezone.now():
                raw=_new_approval(job,request.user); event(job,"retry_requires_approval",request.user)
                return Response({**_mail_job_data(job),"approval_url":_approval_url(raw)},status=202)
            job.idempotency_key=__import__("uuid").uuid4(); job.status=ScheduledEmail.Status.SCHEDULED
            job.scheduled_at_utc=timezone.now(); job.last_error_code=""; job.claimed_at=None; job.save()
            event(job,"retry_queued",request.user,{"manual_confirmation":bool(request.data.get("confirm_not_sent"))})
        else: raise NotFound("Unknown scheduled-email action.")
        return Response(_mail_job_data(job))

class ApprovalPortalView(APIView):
    permission_classes=[AllowAny]; authentication_classes=[]
    def get(self,request,token):
        approval=EmailApproval.objects.filter(token_hash=digest_secret(token)).select_related("scheduled_email__client").first()
        if approval and approval.status == EmailApproval.Status.APPROVED and approval.scheduled_email.status == ScheduledEmail.Status.SCHEDULED and approval.expires_at > timezone.now():
            job=approval.scheduled_email
            body=f"<h1>Approved MONATA email</h1><p>To: {html.escape(', '.join(job.recipient_data.get('to',[])))}</p><p>Subject: {html.escape(job.subject)}</p><p>Scheduled: {html.escape(job.scheduled_at_utc.isoformat() if job.scheduled_at_utc else '')}</p><p>The message has not been sent yet. You can cancel it here.</p><form method='post' action='{API_PREFIX}/approvals/{token}/'><label>Authorized contact email <input name='email' type='email' autocomplete='email'></label><button class='secondary' name='decision' value='cancel'>Cancel scheduled email</button></form>"
            return _html_page("Cancel approved email",body)
        if not approval or approval.status != EmailApproval.Status.PENDING or approval.expires_at <= timezone.now():
            return _html_page("Approval unavailable","<h1>This approval link has expired or has already been used.</h1>")
        job=approval.scheduled_email
        if job.content_hash != approval.content_hash: return _html_page("Approval changed","<h1>This message changed after the approval link was created. Ask MONATA for a new approval link.</h1>")
        recipients=html.escape("To: " + ", ".join(job.recipient_data.get("to",[])) + "\nCC: " + ", ".join(job.recipient_data.get("cc",[])) + "\nBCC: " + ", ".join(job.recipient_data.get("bcc",[])))
        body=f"<h1>Review email for MONATA client: {html.escape(job.client.name)}</h1><p><b>From:</b> {html.escape(job.connection.email_address)}</p><p><b>{recipients}</b></p><p><b>Subject:</b> {html.escape(job.subject)}</p><p><b>Scheduled:</b> {html.escape(job.scheduled_at_utc.isoformat() if job.scheduled_at_utc else 'Immediately after approval')} ({html.escape(job.timezone)})</p><pre>{html.escape(job.body_text)}</pre><p>This approval applies only to this exact message and recipient list.</p><form method='post' action='{API_PREFIX}/approvals/{token}/'><label>Authorized contact email <input name='email' type='email' autocomplete='email'></label><button name='decision' value='approve'>Approve email</button><button class='secondary' name='decision' value='reject'>Reject</button></form>"
        return _html_page("Review email approval",body)
    def post(self,request,token):
        decision=request.data.get("decision")
        if decision not in ("approve","reject","cancel"): raise ValidationError({"decision":"Choose approve, reject or cancel."})
        approver_email=str(request.data.get("email", "")).strip().lower()
        comment=str(request.data.get("comment", ""))[:500]
        with transaction.atomic():
            approval=EmailApproval.objects.select_for_update().filter(token_hash=digest_secret(token)).select_related("scheduled_email__connection","scheduled_email__client").first()
            if not approval or approval.expires_at <= timezone.now(): raise ValidationError("This approval has expired or has already been used.")
            job=ScheduledEmail.objects.select_for_update().get(id=approval.scheduled_email_id)
            if job.content_hash != approval.content_hash: approval.status=EmailApproval.Status.INVALIDATED; approval.save(update_fields=["status","updated_at"]); raise ValidationError("The message changed after approval was requested.")
            if job.client.email and approver_email != job.client.email.lower(): raise PermissionDenied("Enter the client's registered contact email to approve this message.")
            if decision=="cancel":
                if approval.status != EmailApproval.Status.APPROVED or job.status != ScheduledEmail.Status.SCHEDULED: raise ValidationError("This email can no longer be cancelled.")
                job.status=ScheduledEmail.Status.CANCELLED; job.cancelled_at=timezone.now(); approval.status=EmailApproval.Status.INVALIDATED
                approval.comment=comment or "Cancelled by the client"; approval.save(); job.save()
                event(job,"client_cancelled",None,{"approver_email":approver_email})
                audit(job.workspace,"client_cancelled_scheduled_email",connection=job.connection,client=job.client,target_type="scheduled_email",target_id=job.id,metadata={"approver_email":approver_email})
                notify(job.assigned_agent,job.workspace,"client_cancelled","Client cancelled an approved scheduled email.",job.id)
                return _html_page("Scheduled email cancelled","<h1>Email cancelled</h1><p>MONATA will not send this message.</p>")
            if approval.status != EmailApproval.Status.PENDING: raise ValidationError("This approval has already been decided.")
            approval.approved_by_email=approver_email; approval.comment=comment; approval.decided_at=timezone.now()
            if decision == "approve":
                if job.connection.status != MailboxConnection.Status.ACTIVE or job.connection.sends_paused: raise ValidationError("This mailbox is paused or disconnected; approval cannot be accepted now.")
                approval.status=EmailApproval.Status.APPROVED; approval.expires_at=max(approval.expires_at,(job.scheduled_at_utc or timezone.now())+timedelta(days=1)); job.status=ScheduledEmail.Status.SCHEDULED
                job.scheduled_at_utc=max(job.scheduled_at_utc or timezone.now(), timezone.now())
            else:
                approval.status=EmailApproval.Status.REJECTED; job.status=ScheduledEmail.Status.CANCELLED; job.cancelled_at=timezone.now()
            approval.save(); job.save(); event(job,"client_approved" if decision=="approve" else "client_rejected",None,{"approver_email":approver_email})
            notify(job.assigned_agent,job.workspace,"client_approved" if decision=="approve" else "client_rejected",f"Client {decision}d: {job.subject}",job.id)
            audit(job.workspace,"email_approval_decided",connection=job.connection,client=job.client,target_type="scheduled_email",target_id=job.id,metadata={"decision":decision,"approver_email":approver_email})
        return _html_page("Email approval recorded", "<h1>Email approved</h1><p>The message is queued for the scheduled time.</p>" if decision=="approve" else "<h1>Email rejected</h1><p>MONATA will not send this message.</p>")

class EmailRuleListCreateView(MailAuthView):
    def get(self,request):
        rows=EmailAutomationRule.objects.filter(workspace=request.workspace)
        rows=rows.filter(connection__agent_access__user=request.user,connection__agent_access__is_active=True,connection__agent_access__can_manage_rules=True).distinct()
        if request.query_params.get("client"): rows=rows.filter(client_id=request.query_params["client"])
        if request.query_params.get("connection"): rows=rows.filter(connection_id=request.query_params["connection"])
        return Response([{"id":str(r.id),"client_id":str(r.client_id),"connection_id":str(r.connection_id),"name":r.name,"purpose":r.purpose,"trigger_type":r.trigger_type,"wait_business_days":r.wait_business_days,"status":r.status,"allowed_emails":r.allowed_emails,"allowed_domains":r.allowed_domains,"approved_templates":r.approved_templates,"max_sends_per_day":r.max_sends_per_day,"max_sends_per_recipient_per_day":r.max_sends_per_recipient_per_day,"timezone":r.timezone,"allowed_weekdays":r.allowed_weekdays,"allowed_start":r.allowed_start,"allowed_end":r.allowed_end,"excluded_dates":r.excluded_dates,"expires_at":r.expires_at,"version":r.version,"approved_at":r.approved_at,"approved_by_email":r.approved_by_email} for r in rows.order_by("-created_at")])
    @transaction.atomic
    def post(self,request):
        connection,_=self.assigned_connection(request,request.data.get("connection_id"),"can_manage_rules")
        if connection.access_mode != "manage": raise PermissionDenied("Mailbox management permission is required for send rules.")
        client=connection.client; data=request.data
        expires=parse_datetime(data.get("expires_at", ""))
        if not expires or timezone.is_naive(expires) or expires <= timezone.now() or expires > timezone.now()+timedelta(days=366): raise ValidationError({"expires_at":"Provide a future timezone-aware expiry within one year."})
        templates=validate_rule_templates(data.get("approved_templates"))
        trigger=str(data.get("trigger_type","routine")); wait_days=data.get("wait_business_days",0)
        if trigger not in ("routine","no_reply_follow_up"): raise ValidationError({"trigger_type":"Choose routine or no_reply_follow_up."})
        try: wait_days=int(wait_days)
        except (TypeError,ValueError) as exc: raise ValidationError({"wait_business_days":"Use a whole number from 0 to 30."}) from exc
        if trigger=="no_reply_follow_up" and not 1<=wait_days<=30: raise ValidationError({"wait_business_days":"No-reply follow-ups must wait 1 to 30 business days."})
        if trigger=="routine" and wait_days!=0: raise ValidationError({"wait_business_days":"Only no-reply follow-up rules use a business-day delay."})
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try: ZoneInfo(str(data.get("timezone", "Africa/Nairobi")))
        except ZoneInfoNotFoundError as exc: raise ValidationError({"timezone":"Unknown time zone."}) from exc
        emails=data.get("allowed_emails",[]); domains=data.get("allowed_domains",[])
        if not isinstance(emails,list) or not isinstance(domains,list) or len(emails)+len(domains)==0 or len(emails)+len(domains)>200: raise ValidationError("Add a bounded allowlist of recipient email addresses or domains.")
        from django.core.validators import validate_email
        for email in emails:
            try: validate_email(email)
            except Exception as exc: raise ValidationError("A recipient email in the allowlist is invalid.") from exc
        domains=[str(x).lower().lstrip("@").strip() for x in domains]
        if any(not re.fullmatch(r"[a-z0-9.-]{1,253}",d) or "." not in d for d in domains): raise ValidationError("A recipient domain in the allowlist is invalid.")
        weekdays=data.get("allowed_weekdays",[0,1,2,3,4,5,6])
        if not isinstance(weekdays,list) or any(type(d) is not int or d not in range(7) for d in weekdays): raise ValidationError({"allowed_weekdays":"Use weekday numbers from 0 (Monday) through 6 (Sunday)."})
        excluded=data.get("excluded_dates",[])
        if not isinstance(excluded,list) or any(not isinstance(x,str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}",x) for x in excluded): raise ValidationError({"excluded_dates":"Use ISO calendar dates such as 2026-12-25."})
        start_time=data.get("allowed_start"); end_time=data.get("allowed_end")
        if (start_time is None) != (end_time is None): raise ValidationError("Provide both allowed_start and allowed_end, or neither.")
        if start_time and str(start_time)>=str(end_time): raise ValidationError("allowed_start must be earlier than allowed_end.")
        try: per_day=max(1,min(int(data.get("max_sends_per_day",10)),100)); per_recipient=max(1,min(int(data.get("max_sends_per_recipient_per_day",2)),20))
        except (TypeError,ValueError) as exc: raise ValidationError("Send limits must be whole numbers.") from exc
        name=str(data.get("name", "")).strip()
        if not name: raise ValidationError({"name":"A rule name is required."})
        raw=random_secret(); rule=EmailAutomationRule.objects.create(workspace=request.workspace,client=client,connection=connection,
            name=name[:120],purpose=str(data.get("purpose", "routine_follow_up"))[:80],trigger_type=trigger,wait_business_days=wait_days,
            allowed_emails=[x.lower() for x in emails],allowed_domains=domains,approved_templates=templates,
            max_sends_per_day=per_day,
            max_sends_per_recipient_per_day=per_recipient,
            timezone=str(data.get("timezone", "Africa/Nairobi")),allowed_weekdays=weekdays,
            allowed_start=start_time,allowed_end=end_time,excluded_dates=excluded,
            expires_at=expires,created_by=request.user,activation_token_hash=digest_secret(raw))
        audit(request.workspace,"email_rule_created",actor=request.user,connection=connection,target_type="email_rule",target_id=rule.id,metadata={"version":rule.version})
        return Response({"id":str(rule.id),"status":rule.status,"version":rule.version,"client_activation_url":_rule_activation_url(raw)},status=201)

class EmailRuleActivationView(APIView):
    permission_classes=[AllowAny]; authentication_classes=[]
    def get(self,request,token):
        rule=EmailAutomationRule.objects.filter(activation_token_hash=digest_secret(token),status=EmailAutomationRule.Status.DRAFT).select_related("client").first()
        if not rule: return _html_page("Rule unavailable","<h1>This rule activation link is invalid or has already been used.</h1>")
        body=f"<h1>Authorize MONATA routine email</h1><p>Client: <b>{html.escape(rule.client.name)}</b></p><p>Rule: <b>{html.escape(rule.name)}</b> ({html.escape(rule.purpose)})</p><p>Trigger: {html.escape(rule.trigger_type)}; wait: {rule.wait_business_days} business day(s)</p><p>Recipients: {html.escape(', '.join(rule.allowed_emails + rule.allowed_domains))}</p><p>Limits: {rule.max_sends_per_day} per day; {rule.max_sends_per_recipient_per_day} per recipient per day</p><p>Time zone: {html.escape(rule.timezone)}; weekdays: {html.escape(', '.join(str(x) for x in rule.allowed_weekdays) if rule.allowed_weekdays else 'all days')}; allowed hours: {html.escape(str(rule.allowed_start or 'any'))}–{html.escape(str(rule.allowed_end or 'any'))}; excluded dates: {html.escape(', '.join(rule.excluded_dates) if rule.excluded_dates else 'none')}</p><p>Expires: {html.escape(rule.expires_at.isoformat())}</p><p>MONATA will only send messages matching the exact templates below and only within the recipient and time limits.</p><pre>{html.escape(json.dumps(rule.approved_templates,ensure_ascii=False,indent=2))}</pre><form method='post' action='{API_PREFIX}/rules/activate/{token}/'><label>Authorized contact email <input name='email' type='email' autocomplete='email'></label><button name='decision' value='approve'>Authorize this rule</button><button class='secondary' name='decision' value='reject'>Reject</button></form>"
        return _html_page("Authorize routine email",body)
    def post(self,request,token):
        with transaction.atomic():
            rule=EmailAutomationRule.objects.select_for_update().filter(activation_token_hash=digest_secret(token),status=EmailAutomationRule.Status.DRAFT).select_related("connection","client").first()
            if not rule or rule.expires_at <= timezone.now(): raise ValidationError("This rule activation link expired or was used.")
            if rule.connection.status != MailboxConnection.Status.ACTIVE or rule.connection.sends_paused: raise ValidationError("The connected mailbox is paused or disconnected.")
            if request.data.get("decision") == "reject":
                rule.status=EmailAutomationRule.Status.REVOKED; rule.activation_token_hash=""; rule.save(update_fields=["status","activation_token_hash","updated_at"])
            elif request.data.get("decision") == "approve":
                approver=str(request.data.get("email", "")).strip().lower()
                if rule.client.email and approver != rule.client.email.lower(): raise PermissionDenied("Enter the client's registered contact email to authorize this rule.")
                rule.status=EmailAutomationRule.Status.ACTIVE; rule.approved_at=timezone.now(); rule.approved_by_email=approver; rule.activation_token_hash=""; rule.save(update_fields=["status","approved_at","approved_by_email","activation_token_hash","updated_at"])
            else: raise ValidationError({"decision":"Choose approve or reject."})
            audit(rule.workspace,"email_rule_activated" if rule.status==EmailAutomationRule.Status.ACTIVE else "email_rule_rejected",connection=rule.connection,client=rule.client,target_type="email_rule",target_id=rule.id,metadata={"version":rule.version})
        return _html_page("Rule authorization recorded","<h1>Routine email rule authorized</h1><p>Only the limits and templates displayed on the previous page are enabled.</p>" if rule.status==EmailAutomationRule.Status.ACTIVE else "<h1>Rule rejected</h1><p>No automated messages will be sent.</p>")

class EmailRuleControlView(MailAuthView):
    def post(self,request,rule_id,action):
        rule=get_object_or_404(EmailAutomationRule, id=rule_id,workspace=request.workspace)
        get_access(rule.connection,request.user,"can_manage_rules")
        if action == "pause" and rule.status == EmailAutomationRule.Status.ACTIVE: rule.status=EmailAutomationRule.Status.PAUSED
        elif action == "resume" and rule.status == EmailAutomationRule.Status.PAUSED and rule.expires_at > timezone.now() and rule.connection.status == MailboxConnection.Status.ACTIVE: rule.status=EmailAutomationRule.Status.ACTIVE
        elif action == "revoke": rule.status=EmailAutomationRule.Status.REVOKED
        else: raise ValidationError("This rule cannot perform that action in its current state.")
        rule.version+=1; rule.save(update_fields=["status","version","updated_at"])
        if action == "revoke": ScheduledEmail.objects.filter(rule=rule,status=ScheduledEmail.Status.SCHEDULED).update(status=ScheduledEmail.Status.CANCELLED,cancelled_at=timezone.now())
        audit(request.workspace,f"email_rule_{action}d",actor=request.user,connection=rule.connection,target_type="email_rule",target_id=rule.id,metadata={"version":rule.version})
        return Response({"id":str(rule.id),"status":rule.status,"version":rule.version})

class EmailRuleUpdateView(MailAuthView):
    def patch(self,request,rule_id):
        rule=get_object_or_404(EmailAutomationRule,id=rule_id,workspace=request.workspace)
        get_access(rule.connection,request.user,"can_manage_rules")
        if rule.status in (EmailAutomationRule.Status.REVOKED,EmailAutomationRule.Status.EXPIRED): raise ValidationError("This rule is closed and cannot be edited.")
        data=request.data
        allowed={"name","purpose","trigger_type","wait_business_days","allowed_emails","allowed_domains","approved_templates","max_sends_per_day","max_sends_per_recipient_per_day","timezone","allowed_weekdays","allowed_start","allowed_end","excluded_dates","expires_at"}
        if set(data)-allowed: raise ValidationError("Unsupported rule field.")
        name=str(data.get("name",rule.name)).strip()
        templates=validate_rule_templates(data.get("approved_templates",rule.approved_templates))
        trigger=str(data.get("trigger_type",rule.trigger_type)); wait_days=data.get("wait_business_days",rule.wait_business_days)
        if trigger not in ("routine","no_reply_follow_up"): raise ValidationError({"trigger_type":"Choose routine or no_reply_follow_up."})
        try: wait_days=int(wait_days)
        except (TypeError,ValueError) as exc: raise ValidationError({"wait_business_days":"Use a whole number from 0 to 30."}) from exc
        if (trigger=="no_reply_follow_up" and not 1<=wait_days<=30) or (trigger=="routine" and wait_days!=0): raise ValidationError({"wait_business_days":"Use 1 to 30 for no-reply follow-up, or 0 for routine rules."})
        emails=data.get("allowed_emails",rule.allowed_emails); domains=data.get("allowed_domains",rule.allowed_domains)
        if not isinstance(emails,list) or not isinstance(domains,list) or not emails and not domains: raise ValidationError("A recipient allowlist is required.")
        from django.core.validators import validate_email
        for email in emails:
            try: validate_email(email)
            except Exception as exc: raise ValidationError("A recipient email in the allowlist is invalid.") from exc
        domains=[str(x).lower().lstrip("@").strip() for x in domains]
        if any(not re.fullmatch(r"[a-z0-9.-]{1,253}",d) or "." not in d for d in domains): raise ValidationError("A recipient domain in the allowlist is invalid.")
        weekdays=data.get("allowed_weekdays",rule.allowed_weekdays)
        if not isinstance(weekdays,list) or any(type(d) is not int or d not in range(7) for d in weekdays): raise ValidationError("Weekdays must be numbers from 0 to 6.")
        excluded=data.get("excluded_dates",rule.excluded_dates)
        if not isinstance(excluded,list) or any(not isinstance(x,str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}",x) for x in excluded): raise ValidationError({"excluded_dates":"Use ISO calendar dates."})
        expires=parse_datetime(data.get("expires_at", "")) if "expires_at" in data else rule.expires_at
        if not expires or timezone.is_naive(expires) or expires <= timezone.now(): raise ValidationError({"expires_at":"Choose a future timezone-aware expiry."})
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        timezone_name=str(data.get("timezone",rule.timezone))
        try: ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc: raise ValidationError({"timezone":"Unknown time zone."}) from exc
        try:
            max_day=max(1,min(int(data.get("max_sends_per_day",rule.max_sends_per_day)),100))
            max_recipient=max(1,min(int(data.get("max_sends_per_recipient_per_day",rule.max_sends_per_recipient_per_day)),20))
        except (TypeError,ValueError) as exc: raise ValidationError("Send limits must be whole numbers.") from exc
        rule.name=name[:120]; rule.purpose=str(data.get("purpose",rule.purpose))[:80]; rule.trigger_type=trigger; rule.wait_business_days=wait_days
        rule.allowed_emails=[x.lower() for x in emails]; rule.allowed_domains=domains; rule.approved_templates=templates
        rule.max_sends_per_day=max_day; rule.max_sends_per_recipient_per_day=max_recipient; rule.timezone=timezone_name
        rule.allowed_weekdays=weekdays; rule.allowed_start=data.get("allowed_start",rule.allowed_start); rule.allowed_end=data.get("allowed_end",rule.allowed_end)
        if (rule.allowed_start is None) != (rule.allowed_end is None) or (rule.allowed_start and rule.allowed_start>=rule.allowed_end): raise ValidationError("Set an ordered allowed_start/allowed_end window, or clear both.")
        rule.excluded_dates=excluded; rule.expires_at=expires
        rule.version+=1; rule.status=EmailAutomationRule.Status.DRAFT; rule.approved_at=None; rule.approved_by_email=""
        raw=random_secret(); rule.activation_token_hash=digest_secret(raw)
        rule.save()
        ScheduledEmail.objects.filter(rule=rule,status=ScheduledEmail.Status.SCHEDULED).update(status=ScheduledEmail.Status.CANCELLED,cancelled_at=timezone.now())
        audit(request.workspace,"email_rule_updated_reapproval_required",actor=request.user,connection=rule.connection,target_type="email_rule",target_id=rule.id,metadata={"version":rule.version})
        return Response({"id":str(rule.id),"status":rule.status,"version":rule.version,"client_activation_url":_rule_activation_url(raw)})

class RecurringEmailListCreateView(MailAuthView):
    def get(self,request):
        rows=RecurringEmailSchedule.objects.filter(workspace=request.workspace,connection__agent_access__user=request.user,
            connection__agent_access__is_active=True,connection__agent_access__can_read=True).select_related("rule","client").distinct()
        if request.membership.role in (Membership.Role.OWNER,Membership.Role.ADMIN):
            rows=RecurringEmailSchedule.objects.filter(workspace=request.workspace,connection__agent_access__user=request.user,
                connection__agent_access__is_active=True,connection__agent_access__can_read=True).select_related("rule","client").distinct()
        return Response([{"id":str(s.id),"client_id":str(s.client_id),"connection_id":str(s.connection_id),"rule_id":str(s.rule_id),"timezone":s.timezone,"local_time":s.local_time,"starts_on":s.starts_on,"ends_on":s.ends_on,"weekdays":s.weekdays,"recipients":s.recipient_data,"subject":s.subject,"body_text":s.body_text,"next_run_at":s.next_run_at,"status":s.status} for s in rows.order_by("next_run_at")[:100]])
    def post(self,request):
        from django.utils.dateparse import parse_date, parse_time
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        rule=get_object_or_404(EmailAutomationRule,id=request.data.get("rule_id"),workspace=request.workspace)
        connection,_=self.assigned_connection(request,rule.connection_id,"can_send_routine")
        get_access(connection,request.user,"can_manage_rules")
        tz_name=str(request.data.get("timezone",rule.timezone))
        try: zone=ZoneInfo(tz_name)
        except ZoneInfoNotFoundError as exc: raise ValidationError({"timezone":"Unknown time zone."}) from exc
        local_time=parse_time(request.data.get("local_time", "")); starts=parse_date(request.data.get("starts_on", "")); ends=parse_date(request.data.get("ends_on", "")) if request.data.get("ends_on") else None
        if not local_time or not starts or (ends and ends<starts): raise ValidationError("Provide a valid local_time, starts_on, and optional ends_on.")
        weekdays=request.data.get("weekdays",rule.allowed_weekdays)
        if not isinstance(weekdays,list) or not weekdays or any(type(x) is not int or x not in range(7) for x in weekdays): raise ValidationError({"weekdays":"Choose one or more weekday numbers from 0 to 6."})
        if rule.allowed_weekdays and any(day not in rule.allowed_weekdays for day in weekdays): raise PermissionDenied("Recurrence weekdays must be allowed by the client rule.")
        recipient=normalize_recipients(request.data.get("recipients",{})); subject=str(request.data.get("subject", "")); body=str(request.data.get("body_text", ""))
        from .worker import next_occurrence
        current=timezone.now()
        first=next_occurrence(timezone_name=tz_name,local_time=local_time,starts_on=starts,ends_on=ends,weekdays=weekdays,after=current-timedelta(seconds=1))
        if not first: raise ValidationError("There is no matching recurrence date in the next year.")
        validate_rule_message(rule,recipient,subject,body,first)
        schedule=RecurringEmailSchedule.objects.create(workspace=request.workspace,client=connection.client,connection=connection,rule=rule,
            created_by=request.user,timezone=tz_name,local_time=local_time,starts_on=starts,ends_on=ends,weekdays=weekdays,
            recipient_data=recipient,subject=subject,body_text=body,next_run_at=first)
        audit(request.workspace,"recurring_email_created",actor=request.user,connection=connection,target_type="recurring_email",target_id=schedule.id,metadata={"rule_id":str(rule.id)})
        return Response({"id":str(schedule.id),"status":schedule.status,"next_run_at":schedule.next_run_at},status=201)

class RecurringEmailControlView(MailAuthView):
    def post(self,request,schedule_id,action):
        schedule=get_object_or_404(RecurringEmailSchedule,id=schedule_id,workspace=request.workspace)
        get_access(schedule.connection,request.user,"can_manage_rules")
        if action=="pause" and schedule.status==RecurringEmailSchedule.Status.ACTIVE: schedule.status=RecurringEmailSchedule.Status.PAUSED
        elif action=="resume" and schedule.status==RecurringEmailSchedule.Status.PAUSED and schedule.rule.status==EmailAutomationRule.Status.ACTIVE: schedule.status=RecurringEmailSchedule.Status.ACTIVE
        elif action=="cancel" and schedule.status in (RecurringEmailSchedule.Status.ACTIVE,RecurringEmailSchedule.Status.PAUSED): schedule.status=RecurringEmailSchedule.Status.CANCELLED
        else: raise ValidationError("This recurrence cannot perform that action now.")
        schedule.save(update_fields=["status","updated_at"])
        audit(request.workspace,f"recurrence_{action}d",actor=request.user,connection=schedule.connection,target_type="recurring_email",target_id=schedule.id)
        return Response({"id":str(schedule.id),"status":schedule.status})

class EmailAuditView(MailAuthView):
    def get(self,request):
        self.require_role(request,Membership.Role.OWNER,Membership.Role.ADMIN)
        rows=MailAuditEvent.objects.filter(workspace=request.workspace)
        if request.query_params.get("connection"): rows=rows.filter(connection_id=request.query_params["connection"])
        return Response([{"id":str(x.id),"event_type":x.event_type,"actor_id":str(x.actor_id) if x.actor_id else None,"connection_id":str(x.connection_id) if x.connection_id else None,"client_id":str(x.client_id) if x.client_id else None,"target_type":x.target_type,"target_id":x.target_id,"safe_metadata":x.safe_metadata,"created_at":x.created_at} for x in rows.order_by("-created_at")[:250]])

class EmailNotificationView(MailAuthView):
    def get(self,request):
        rows=EmailNotification.objects.filter(workspace=request.workspace,user=request.user).order_by("-created_at")[:100]
        return Response([{"id":str(x.id),"type":x.notification_type,"message":x.message,"reference_id":x.reference_id,"read_at":x.read_at,"created_at":x.created_at} for x in rows])
    def post(self,request,notification_id):
        row=get_object_or_404(EmailNotification,id=notification_id,workspace=request.workspace,user=request.user)
        row.read_at=timezone.now(); row.save(update_fields=["read_at","updated_at"])
        return Response({"id":str(row.id),"read_at":row.read_at})
