from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from zoneinfo import ZoneInfo
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone
from django.conf import settings
from crm.models import (MailboxConnection, MailboxAgentAccess, Membership, ScheduledEmail, EmailApproval,
    EmailSendAttempt, ScheduledEmailEvent, EmailAutomationRule, RecurringEmailSchedule, MailboxInvitation,
    VARecurringTask, Task, VAAgentAvailability, VAClientAssignment)
from .google import find_sent_by_idempotency, GmailError
from .providers import provider_for
from .logic import (event, audit, notify, validate_rule_message, canonical_content, reserve_send_capacity)
from crm.va_automation import run_va_automations, preferred_email_send_time

def next_occurrence(*, timezone_name, local_time, starts_on, ends_on, weekdays, after):
    zone=ZoneInfo(timezone_name); local_after=after.astimezone(zone)
    day=max(starts_on,local_after.date())
    for offset in range(0,367):
        date=day+timedelta(days=offset)
        if ends_on and date > ends_on: return None
        if weekdays and date.weekday() not in weekdays: continue
        candidate=datetime.combine(date,local_time,zone)
        # Skip nonexistent local times at daylight-saving transitions; choose the first occurrence if ambiguous.
        roundtrip=candidate.astimezone(dt_timezone.utc).astimezone(zone)
        if roundtrip.replace(tzinfo=None) != candidate.replace(tzinfo=None): continue
        if candidate.astimezone(dt_timezone.utc) <= after: continue
        return candidate.astimezone(dt_timezone.utc)
    return None

def _send_success(job, provider_id):
    with transaction.atomic():
        locked=ScheduledEmail.objects.select_for_update().get(id=job.id)
        if locked.status != ScheduledEmail.Status.SENDING: return
        locked.status=ScheduledEmail.Status.SENT; locked.provider_message_id=provider_id or ""; locked.sent_at=timezone.now(); locked.last_error_code=""
        locked.save(update_fields=["status","provider_message_id","sent_at","last_error_code","updated_at"])
        EmailSendAttempt.objects.create(workspace=locked.workspace,scheduled_email=locked,attempt_number=locked.attempt_count,
            outcome="sent",provider_message_id=provider_id or "")
        event(locked,"sent",None,{"provider_message_id":provider_id or ""})
        audit(locked.workspace,"scheduled_email_sent",connection=locked.connection,client=locked.client,
            target_type="scheduled_email",target_id=locked.id,metadata={"provider_message_id":provider_id or ""})
        notify(locked.assigned_agent,locked.workspace,"email_sent",f"Gmail accepted: {locked.subject}",locked.id)

def _finish_failure(job, *, code, message, retry=False, ambiguous=False):
    now=timezone.now()
    if ambiguous:
        try:
            matches=find_sent_by_idempotency(job.connection,job.idempotency_key)
            if matches:
                _send_success(job,matches[0].get("id", "")); return
        except Exception:
            pass
    with transaction.atomic():
        locked=ScheduledEmail.objects.select_for_update().get(id=job.id)
        if locked.status != ScheduledEmail.Status.SENDING: return
        allowed_retry=retry and locked.attempt_count <= settings.MAIL_MAX_RETRIES
        if ambiguous:
            locked.status=ScheduledEmail.Status.NEEDS_ATTENTION
        elif allowed_retry:
            locked.status=ScheduledEmail.Status.SCHEDULED
            locked.scheduled_at_utc=now+timedelta(seconds=min(3600,30*(2**max(0,locked.attempt_count-1))))
        else:
            locked.status=ScheduledEmail.Status.FAILED
        locked.last_error_code=code[:100]; locked.claimed_at=None
        locked.save(update_fields=["status","scheduled_at_utc","last_error_code","claimed_at","updated_at"])
        outcome="needs_attention" if ambiguous else "retry_scheduled" if allowed_retry else "failed"
        EmailSendAttempt.objects.create(workspace=locked.workspace,scheduled_email=locked,attempt_number=locked.attempt_count,
            outcome=outcome,error_code=code[:100],retryable=bool(allowed_retry))
        event(locked,outcome,None,{"error_code":code[:100]})
        audit(locked.workspace,"scheduled_email_"+outcome,connection=locked.connection,client=locked.client,
            target_type="scheduled_email",target_id=locked.id,metadata={"error_code":code[:100]})
        notify(locked.assigned_agent,locked.workspace,"email_"+outcome,f"Email {outcome.replace('_',' ')}: {locked.subject}",locked.id)

def _authorize(job):
    connection=job.connection
    if connection.status != MailboxConnection.Status.ACTIVE or connection.sends_paused: raise PermissionError("mailbox_paused")
    if not Membership.objects.filter(workspace=job.workspace,user=job.assigned_agent,is_active=True,workspace__is_active=True).exists(): raise PermissionError("agent_membership_inactive")
    access=MailboxAgentAccess.objects.filter(connection=connection,user=job.assigned_agent,is_active=True).first()
    if not access or (access.expires_at and access.expires_at <= timezone.now()): raise PermissionError("agent_mailbox_access_revoked")
    if job.rule_id:
        if not access.can_send_routine: raise PermissionError("agent_routine_send_disabled")
        rule=job.rule
        if job.rule_version != rule.version: raise PermissionError("automation_rule_changed")
        validate_rule_message(rule,job.recipient_data,job.subject,job.body_text,timezone.now())
    else:
        if not access.can_send_approved: raise PermissionError("agent_approved_send_disabled")
        approval=job.approval
        if not approval or approval.status != EmailApproval.Status.APPROVED: raise PermissionError("client_approval_missing")
        if approval.expires_at <= timezone.now() or approval.content_hash != job.content_hash: raise PermissionError("client_approval_expired_or_changed")
    if canonical_content(job.recipient_data,job.subject,job.body_text,job.provider_thread_id) != job.content_hash: raise PermissionError("message_content_hash_mismatch")

def _has_inbound_reply(job):
    if not job.rule_id or job.rule.trigger_type != "no_reply_follow_up": return False
    if not job.wait_for_reply_to_message_id or not job.provider_thread_id: raise PermissionError("follow_up_reference_missing")
    thread=provider_for(job.connection).get_thread(job.connection,job.provider_thread_id)
    messages=thread.get("messages",[])
    original=next((m for m in messages if m.get("id")==job.wait_for_reply_to_message_id),None)
    if not original: raise PermissionError("follow_up_reference_not_in_thread")
    baseline=int(original.get("internalDate",0))
    for message in messages:
        if message.get("id")==job.wait_for_reply_to_message_id: continue
        if int(message.get("internalDate",0))>baseline and "SENT" not in message.get("labelIds",[]): return True
    return False

def _suppress_follow_up(job):
    with transaction.atomic():
        locked=ScheduledEmail.objects.select_for_update().get(id=job.id)
        if locked.status != ScheduledEmail.Status.SENDING: return
        locked.status=ScheduledEmail.Status.CANCELLED; locked.cancelled_at=timezone.now(); locked.last_error_code="reply_received"
        locked.save(update_fields=["status","cancelled_at","last_error_code","updated_at"])
        EmailSendAttempt.objects.create(workspace=locked.workspace,scheduled_email=locked,attempt_number=locked.attempt_count,
            outcome="suppressed_reply_received",error_code="reply_received")
        event(locked,"follow_up_suppressed_reply_received",None,{"reference_message_id":locked.wait_for_reply_to_message_id})
        audit(locked.workspace,"follow_up_suppressed_reply_received",connection=locked.connection,client=locked.client,
            target_type="scheduled_email",target_id=locked.id)
        notify(locked.assigned_agent,locked.workspace,"follow_up_suppressed","Follow-up cancelled because the client replied.",locked.id)

def _process_job(job):
    next_allowed=preferred_email_send_time(job,timezone.now())
    if next_allowed:
        with transaction.atomic():
            locked=ScheduledEmail.objects.select_for_update().get(id=job.id)
            if locked.status==ScheduledEmail.Status.SENDING:
                locked.status=ScheduledEmail.Status.SCHEDULED; locked.scheduled_at_utc=next_allowed; locked.claimed_at=None
                locked.last_error_code="deferred_to_client_send_window"
                locked.save(update_fields=["status","scheduled_at_utc","claimed_at","last_error_code","updated_at"])
                event(locked,"deferred_to_client_send_window",None,{"next_allowed_at":next_allowed.isoformat()})
        return
    try:
        _authorize(job)
    except Exception as exc:
        code=getattr(exc,"default_code",None) or (str(exc)[:100] if isinstance(exc,PermissionError) else "authorization_failed")
        _finish_failure(job,code=code,message="Sending authorization failed.")
        return
    try:
        if _has_inbound_reply(job):
            _suppress_follow_up(job); return
    except GmailError as exc:
        code=getattr(exc,"default_code","gmail_error")
        _finish_failure(job,code=code,message="Could not check for a client reply.",retry=True)
        return
    except Exception:
        _finish_failure(job,code="reply_check_failed",message="Could not check for a client reply.",retry=True)
        return
    try:
        reserve_send_capacity(job.connection,job.rule,job.recipient_data)
    except Exception as exc:
        code=getattr(exc,"default_code","daily_limit_reached")
        _finish_failure(job,code=code,message="Sending limits prevented the email.")
        return
    try:
        result=provider_for(job.connection).send_message(job.connection,job)
        _send_success(job,result.get("id", ""))
    except GmailError as exc:
        code=getattr(exc,"default_code","gmail_error")
        ambiguous=(code=="gmail_timeout" or code.startswith("gmail_5"))
        _finish_failure(job,code=code,message="Gmail send failed.",retry=(code=="gmail_429" or code.startswith("token_refresh_")),ambiguous=ambiguous)
    except Exception:
        _finish_failure(job,code="unexpected_worker_error",message="Unexpected worker error.",ambiguous=True)

def _claim_due_job(now):
    with transaction.atomic():
        candidate=ScheduledEmail.objects.filter(status=ScheduledEmail.Status.SCHEDULED,scheduled_at_utc__lte=now,
            connection__status=MailboxConnection.Status.ACTIVE,connection__sends_paused=False).filter(
            Q(rule__isnull=True)|Q(rule__status=EmailAutomationRule.Status.ACTIVE)).order_by("scheduled_at_utc","created_at").first()
        if not candidate: return None
        changed=ScheduledEmail.objects.filter(id=candidate.id,status=ScheduledEmail.Status.SCHEDULED,scheduled_at_utc__lte=now).update(
            status=ScheduledEmail.Status.SENDING,claimed_at=now,attempt_count=F("attempt_count")+1)
        if changed != 1: return None
        return candidate.id

def _recover_stale_claims(now):
    stale=ScheduledEmail.objects.filter(status=ScheduledEmail.Status.SENDING,claimed_at__lt=now-timedelta(minutes=5))
    for job in stale.select_related("workspace","connection","client","assigned_agent")[:100]:
        _finish_failure(job,code="worker_interrupted_outcome_unknown",message="Worker stopped during send.",ambiguous=True)

def _expire_permissions(now):
    MailboxInvitation.objects.filter(status__in=[MailboxInvitation.Status.PENDING,MailboxInvitation.Status.ACCEPTED],expires_at__lte=now).update(status=MailboxInvitation.Status.EXPIRED)
    MailboxAgentAccess.objects.filter(is_active=False,approval_expires_at__lte=now).update(approval_token_hash="",approval_expires_at=None)
    for approval in EmailApproval.objects.filter(status=EmailApproval.Status.PENDING,expires_at__lte=now).select_related("scheduled_email__workspace","scheduled_email__connection","scheduled_email__client","scheduled_email__assigned_agent")[:100]:
        with transaction.atomic():
            locked=EmailApproval.objects.select_for_update().get(id=approval.id)
            if locked.status != EmailApproval.Status.PENDING or locked.expires_at>now: continue
            locked.status=EmailApproval.Status.EXPIRED; locked.save(update_fields=["status","updated_at"])
            job=ScheduledEmail.objects.select_for_update().get(id=locked.scheduled_email_id)
            if job.status==ScheduledEmail.Status.APPROVAL:
                job.status=ScheduledEmail.Status.FAILED; job.last_error_code="client_approval_expired"; job.save(update_fields=["status","last_error_code","updated_at"])
                event(job,"approval_expired")
                notify(job.assigned_agent,job.workspace,"approval_expired",f"Client approval expired: {job.subject}",job.id)
    for rule in EmailAutomationRule.objects.filter(status=EmailAutomationRule.Status.ACTIVE,expires_at__lte=now).select_related("workspace","connection","client")[:100]:
        with transaction.atomic():
            locked=EmailAutomationRule.objects.select_for_update().get(id=rule.id)
            if locked.status != EmailAutomationRule.Status.ACTIVE or locked.expires_at>now: continue
            locked.status=EmailAutomationRule.Status.EXPIRED; locked.version+=1; locked.save(update_fields=["status","version","updated_at"])
            ScheduledEmail.objects.filter(rule=locked,status=ScheduledEmail.Status.SCHEDULED).update(status=ScheduledEmail.Status.CANCELLED,cancelled_at=now)
            RecurringEmailSchedule.objects.filter(rule=locked,status=RecurringEmailSchedule.Status.ACTIVE).update(status=RecurringEmailSchedule.Status.PAUSED)
            audit(locked.workspace,"email_rule_expired",connection=locked.connection,client=locked.client,target_type="email_rule",target_id=locked.id,metadata={"version":locked.version})

def _materialize_recurring(now, limit=50):
    for schedule in RecurringEmailSchedule.objects.filter(status=RecurringEmailSchedule.Status.ACTIVE,next_run_at__lte=now).select_related("rule","connection","client","workspace","created_by")[:limit]:
        with transaction.atomic():
            schedule=RecurringEmailSchedule.objects.select_for_update().select_related("rule","connection","client","workspace","created_by").get(id=schedule.id)
            if schedule.status != RecurringEmailSchedule.Status.ACTIVE or schedule.next_run_at > now: continue
            rule=schedule.rule
            try:
                validate_rule_message(rule,schedule.recipient_data,schedule.subject,schedule.body_text,schedule.next_run_at)
                access=MailboxAgentAccess.objects.filter(connection=schedule.connection,user=schedule.created_by,is_active=True,can_send_routine=True).first()
                if not access: raise PermissionError("recurrence_agent_access_revoked")
                due=schedule.next_run_at
                job=ScheduledEmail.objects.create(workspace=schedule.workspace,client=schedule.client,connection=schedule.connection,
                    created_by=schedule.created_by,assigned_agent=schedule.created_by,rule=rule,rule_version=rule.version,
                    status=ScheduledEmail.Status.SCHEDULED,recipient_data=schedule.recipient_data,subject=schedule.subject,
                    body_text=schedule.body_text,scheduled_at_utc=due,timezone=schedule.timezone,
                    content_hash=canonical_content(schedule.recipient_data,schedule.subject,schedule.body_text))
                event(job,"recurrence_created",schedule.created_by,{"recurrence_id":str(schedule.id)})
                next_time=next_occurrence(timezone_name=schedule.timezone,local_time=schedule.local_time,starts_on=schedule.starts_on,
                    ends_on=schedule.ends_on,weekdays=schedule.weekdays,after=due)
                if next_time: schedule.next_run_at=next_time
                else: schedule.status=RecurringEmailSchedule.Status.COMPLETED
                schedule.save(update_fields=["next_run_at","status","updated_at"])
            except Exception as exc:
                schedule.status=RecurringEmailSchedule.Status.PAUSED; schedule.save(update_fields=["status","updated_at"])
                notify(schedule.created_by,schedule.workspace,"recurrence_paused",f"Recurring email paused: {str(exc)[:150]}",schedule.id)

def _advance_va_recurrence(value, cadence):
    if cadence == VARecurringTask.Cadence.DAILY: return value + timedelta(days=1)
    if cadence == VARecurringTask.Cadence.WEEKLY: return value + timedelta(weeks=1)
    months = 3 if cadence == VARecurringTask.Cadence.QUARTERLY else 1
    month_index=value.year*12+value.month-1+months
    year,month=divmod(month_index,12); month+=1
    from calendar import monthrange
    return value.replace(year=year,month=month,day=min(value.day,monthrange(year,month)[1]))

def _materialize_va_tasks(now, limit=50):
    due_ids=list(VARecurringTask.objects.filter(is_active=True,next_run_at__lte=now,
        project__service__category="virtual_assistance").values_list("id",flat=True)[:limit])
    for recurrence_id in due_ids:
        with transaction.atomic():
            recurrence=VARecurringTask.objects.select_for_update().select_related("project").filter(
                id=recurrence_id,is_active=True,next_run_at__lte=now).first()
            if not recurrence: continue
            assignee=recurrence.assignee
            if assignee and VAAgentAvailability.objects.filter(workspace=recurrence.workspace,agent=assignee,is_available=False).exists():
                profile=getattr(recurrence.project.client,"va_profile",None)
                assignee=profile.backup_agent if profile else None
                if assignee and not VAClientAssignment.objects.filter(workspace=recurrence.workspace,client=recurrence.project.client,agent=assignee,is_active=True).exists():
                    assignee=None
            Task.objects.create(workspace=recurrence.workspace,project=recurrence.project,title=recurrence.title,
                description=recurrence.description,priority=recurrence.priority,assignee=assignee,
                due_date=recurrence.next_run_at.date(),source_recurring_task=recurrence)
            due=recurrence.next_run_at
            while due<=now: due=_advance_va_recurrence(due,recurrence.cadence)
            recurrence.last_run_at=now; recurrence.next_run_at=due
            recurrence.save(update_fields=["last_run_at","next_run_at","updated_at"])

def run_once(batch_size=20):
    now=timezone.now(); _expire_permissions(now); _recover_stale_claims(now); _materialize_recurring(now); _materialize_va_tasks(now); run_va_automations(now)
    processed=0
    for _ in range(batch_size):
        job_id=_claim_due_job(now)
        if not job_id: break
        job=ScheduledEmail.objects.select_related("connection__client","client","workspace","assigned_agent","approval","rule").get(id=job_id)
        _process_job(job); processed+=1
    return processed
