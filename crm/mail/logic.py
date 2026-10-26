import hashlib
import json
import re
from datetime import timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from django.conf import settings
from django.core.validators import validate_email
from django.utils import timezone
from django.db import transaction
from rest_framework.exceptions import ValidationError, PermissionDenied
from crm.models import MailAuditEvent, ScheduledEmailEvent, EmailNotification, MailboxAgentAccess, MailboxConnection, EmailAutomationRule, Membership, MailboxSendUsage, RuleSendUsage, RuleRecipientUsage

PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")
SAFE_PLACEHOLDERS = {"first_name", "appointment_date", "service_name", "meeting_link"}

def audit(workspace, event, actor=None, connection=None, client=None, target_type="", target_id="", metadata=None):
    MailAuditEvent.objects.create(workspace=workspace, client=client or getattr(connection, "client", None), connection=connection,
        actor=actor, event_type=event, target_type=target_type, target_id=str(target_id), safe_metadata=metadata or {})

def event(job, event_type, actor=None, metadata=None):
    ScheduledEmailEvent.objects.create(workspace=job.workspace, scheduled_email=job, actor=actor,
        event_type=event_type, safe_metadata=metadata or {})

def notify(user, workspace, kind, message, reference=""):
    if user: EmailNotification.objects.create(workspace=workspace, user=user, notification_type=kind, message=message[:240], reference_id=str(reference))

def canonical_content(recipients, subject, body, provider_thread_id=""):
    headers={key:str(value) for key,value in recipients.get("thread_headers",{}).items()}
    clean_recipients = {key:sorted([x.strip().lower() for x in recipients.get(key, [])]) for key in ("to", "cc", "bcc")}
    return hashlib.sha256(json.dumps({"recipients":clean_recipients,"thread_headers":headers,"thread_id":provider_thread_id,"subject":subject,"body":body}, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()

def normalize_recipients(data):
    result = {}
    if not isinstance(data, dict): raise ValidationError({"recipients":"Expected an object with to, cc and bcc lists."})
    for field in ("to", "cc", "bcc"):
        values = data.get(field, [])
        if not isinstance(values, list) or len(values) > 25: raise ValidationError({"recipients":f"{field} must be a list of up to 25 addresses."})
        clean=[]
        for value in values:
            try: validate_email(value)
            except Exception as exc: raise ValidationError({"recipients":f"Invalid email address in {field}."}) from exc
            value=value.strip().lower()
            if value not in clean: clean.append(value)
        result[field]=clean
    if not result["to"]: raise ValidationError({"recipients":"At least one To address is required."})
    if sum(map(len, result.values())) > 25: raise ValidationError({"recipients":"A message may have at most 25 recipients."})
    if "thread_headers" in data:
        result["thread_headers"] = {k:re.sub(r"[\r\n]", "", str(v))[:2000] for k,v in data["thread_headers"].items() if k in ("In-Reply-To", "References")}
    return result

def get_access(connection, user, permission="can_read"):
    if not Membership.objects.filter(workspace=connection.workspace,user=user,is_active=True,workspace__is_active=True).exists():
        raise PermissionDenied("Your workspace membership is no longer active.")
    access = MailboxAgentAccess.objects.filter(connection=connection, user=user, is_active=True).first()
    if not access or not access.client_approved_at or (access.expires_at and access.expires_at <= timezone.now()):
        raise PermissionDenied("You are not assigned to this mailbox.")
    if not getattr(access, permission): raise PermissionDenied("Your mailbox assignment does not allow this action.")
    if connection.status != MailboxConnection.Status.ACTIVE:
        raise PermissionDenied("This mailbox is not active.")
    return access

def validate_rule_templates(templates):
    if not isinstance(templates, list) or not templates or len(templates) > 20:
        raise ValidationError({"approved_templates":"Provide between one and 20 approved subject/body templates."})
    for item in templates:
        if not isinstance(item, dict) or set(item) - {"subject", "body"} or not isinstance(item.get("subject"), str) or not isinstance(item.get("body"), str):
            raise ValidationError({"approved_templates":"Each template must contain only string subject and body values."})
        if len(item["subject"]) > 998 or len(item["body"]) > 10000:
            raise ValidationError({"approved_templates":"A template exceeds the supported size."})
        for value in (item["subject"], item["body"]):
            if any(name not in SAFE_PLACEHOLDERS for name in PLACEHOLDER_RE.findall(value)):
                raise ValidationError({"approved_templates":"A template uses an unsupported placeholder."})
        if "{" in PLACEHOLDER_RE.sub("", item["subject"] + item["body"]) or "}" in PLACEHOLDER_RE.sub("", item["subject"] + item["body"]):
            raise ValidationError({"approved_templates":"Template placeholders must use supported names like {first_name}."})
    return templates

def validate_rule_message(rule, recipients, subject, body, at_time=None):
    now = at_time or timezone.now()
    if rule.status != EmailAutomationRule.Status.ACTIVE or not rule.approved_at or rule.expires_at <= now:
        raise PermissionDenied("The client's automatic-send rule is not active.")
    if rule.connection.sends_paused or rule.connection.status != MailboxConnection.Status.ACTIVE:
        raise PermissionDenied("This mailbox is paused or disconnected.")
    if rule.connection.access_mode != "manage": raise PermissionDenied("This mailbox has read-only authorization.")
    try: local = now.astimezone(ZoneInfo(rule.timezone))
    except ZoneInfoNotFoundError as exc: raise ValidationError({"timezone":"Unknown time zone."}) from exc
    if rule.allowed_weekdays and local.weekday() not in rule.allowed_weekdays: raise PermissionDenied("The scheduled time is outside the authorized days.")
    if local.date().isoformat() in rule.excluded_dates: raise PermissionDenied("The scheduled date is excluded by the client.")
    if rule.allowed_start and rule.allowed_end and not (rule.allowed_start <= local.timetz().replace(tzinfo=None) < rule.allowed_end):
        raise PermissionDenied("The scheduled time is outside the client's allowed hours.")
    to_all = recipients.get("to", []) + recipients.get("cc", []) + recipients.get("bcc", [])
    if not to_all: raise ValidationError({"recipients":"No recipients were supplied."})
    allowed_emails={x.lower() for x in rule.allowed_emails}; allowed_domains={x.lower().lstrip("@").strip() for x in rule.allowed_domains}
    for address in to_all:
        domain=address.rsplit("@", 1)[-1]
        if address.lower() not in allowed_emails and domain not in allowed_domains:
            raise PermissionDenied("A recipient is outside the client's authorized recipient list.")
    matched=False
    for template in rule.approved_templates:
        pattern=lambda value: "^" + re.escape(value).replace(r"\{first_name\}", r"[^\r\n]{1,100}").replace(r"\{appointment_date\}", r"[^\r\n]{1,100}").replace(r"\{service_name\}", r"[^\r\n]{1,100}").replace(r"\{meeting_link\}", r"https?://[^\s<>]{1,500}") + "$"
        if re.fullmatch(pattern(template["subject"]), subject) and re.fullmatch(pattern(template["body"]), body, flags=re.DOTALL):
            matched=True; break
    if not matched: raise PermissionDenied("The message does not match a client-approved template.")
    from datetime import timezone as dt_timezone

def reserve_send_capacity(connection, rule, recipients):
    """Atomically reserve quota before calling Gmail so parallel workers cannot exceed caps."""
    now=timezone.now(); mailbox_day=now.date()
    with transaction.atomic():
        MailboxSendUsage.objects.get_or_create(connection=connection,usage_date=mailbox_day)
        mailbox=MailboxSendUsage.objects.select_for_update().get(connection=connection,usage_date=mailbox_day)
        if mailbox.reserved_count >= settings.MAIL_MAX_SENDS_PER_MAILBOX_PER_DAY:
            raise PermissionDenied("The mailbox daily send limit has been reached.")
        mailbox.reserved_count+=1; mailbox.save(update_fields=["reserved_count","updated_at"])
        if rule:
            from zoneinfo import ZoneInfo
            day=now.astimezone(ZoneInfo(rule.timezone)).date()
            RuleSendUsage.objects.get_or_create(rule=rule,usage_date=day)
            rule_usage=RuleSendUsage.objects.select_for_update().get(rule=rule,usage_date=day)
            if rule_usage.reserved_count >= rule.max_sends_per_day: raise PermissionDenied("The daily send limit for this rule has been reached.")
            rule_usage.reserved_count+=1; rule_usage.save(update_fields=["reserved_count","updated_at"])
            all_recipients=set(recipients.get("to",[])+recipients.get("cc",[])+recipients.get("bcc",[]))
            for address in sorted(all_recipients):
                RuleRecipientUsage.objects.get_or_create(rule=rule,usage_date=day,recipient=address)
                usage=RuleRecipientUsage.objects.select_for_update().get(rule=rule,usage_date=day,recipient=address)
                if usage.reserved_count >= rule.max_sends_per_recipient_per_day:
                    raise PermissionDenied("The per-recipient daily send limit has been reached.")
                usage.reserved_count+=1; usage.save(update_fields=["reserved_count","updated_at"])
