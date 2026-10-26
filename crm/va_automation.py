"""Scheduled VA workflow automation invoked by the supervised mail worker."""
from datetime import datetime, time, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone

from .models import (
    ScheduledEmail, Task, VAActionApproval, VAAppointment, VAClientAssignment,
    VAClientProfile, VAClientRequest, VAReportDelivery, VASLATarget,
    VATimeEntry, VAFollowUp,
)
from .signals import _notify


def _local_profile(client):
    return VAClientProfile.objects.filter(workspace=client.workspace, client=client).first()


def _run_sla_alerts(now, limit=300):
    rows=VAClientRequest.objects.filter(status__in=[VAClientRequest.Status.SUBMITTED,VAClientRequest.Status.IN_PROGRESS,VAClientRequest.Status.WAITING]).select_related("client","assigned_to")[:limit]
    for item in rows:
        target=VASLATarget.objects.filter(workspace=item.workspace,client=item.client,category=item.priority,is_active=True).first()
        if not target: target=VASLATarget.objects.filter(workspace=item.workspace,client=item.client,category="normal",is_active=True).first()
        if not target: continue
        if not item.first_response_at:
            deadline=item.created_at+timedelta(minutes=target.response_minutes); stage="response"
        elif target.resolution_minutes and item.status!=VAClientRequest.Status.WAITING:
            deadline=item.created_at+timedelta(minutes=target.resolution_minutes); stage="resolution"
        else: continue
        remaining=(deadline-now).total_seconds()
        sla_minutes=target.response_minutes if stage=="response" else target.resolution_minutes
        warning_window=min(60*60,max(15*60,sla_minutes*60//5))
        if remaining<0: level="breached"
        elif remaining<=warning_window: level="warning"
        else: continue
        recipients=[item.assigned_to] if item.assigned_to_id else [row.agent for row in VAClientAssignment.objects.filter(workspace=item.workspace,client=item.client,is_active=True).select_related("agent")[:5]]
        for recipient in recipients:
            _notify(recipient,item.workspace,item.client,f"sla:{item.pk}:{stage}:{level}",f"va_sla_{level}",
                f"{item.client.name}: {stage} SLA {level} for {item.title}")


def _run_due_followups(now, limit=200):
    candidates=VAFollowUp.objects.filter(status__in=[VAFollowUp.Status.OPEN,VAFollowUp.Status.WAITING],due_at__lte=now,reminder_sent_at__isnull=True).select_related("workspace","client","assigned_to")[:limit]
    for candidate in candidates:
        with transaction.atomic():
            row=VAFollowUp.objects.select_for_update().filter(pk=candidate.pk,reminder_sent_at__isnull=True).first()
            if not row: continue
            row.reminder_sent_at=now; row.save(update_fields=["reminder_sent_at","updated_at"])
            _notify(row.assigned_to,row.workspace,row.client,f"followup-due:{row.pk}","va_follow_up_due",f"Follow-up is due: {row.title}")


def _expire_va_approvals(now, limit=200):
    rows=VAActionApproval.objects.filter(status=VAActionApproval.Status.PENDING,expires_at__lte=now).select_related("workspace","client","requested_by","appointment")[:limit]
    for approval in rows:
        with transaction.atomic():
            row=VAActionApproval.objects.select_for_update().filter(pk=approval.pk,status=VAActionApproval.Status.PENDING,expires_at__lte=now).first()
            if not row: continue
            row.status=VAActionApproval.Status.EXPIRED; row.save(update_fields=["status","updated_at"])
            if row.appointment_id and row.appointment.status==VAAppointment.Status.TENTATIVE:
                row.appointment.status=VAAppointment.Status.CANCELLED
                row.appointment.save(update_fields=["status","updated_at"])
            _notify(row.requested_by,row.workspace,row.client,f"approval-expired:{row.pk}","va_approval_expired",f"Client approval expired: {row.title}")


def _appointment_reminders(now, limit=200):
    appointments=VAAppointment.objects.filter(status__in=[VAAppointment.Status.TENTATIVE,VAAppointment.Status.CONFIRMED],
        reminder_sent_at__isnull=True,starts_at__gt=now).select_related("workspace","client","created_by")[:limit]
    for appointment in appointments:
        profile=_local_profile(appointment.client)
        prefs=profile.operational_preferences if profile else {}
        if not prefs.get("appointment_reminders_enabled",False): continue
        try: lead_minutes=min(max(int(prefs.get("appointment_reminder_minutes",60)),5),10080)
        except (TypeError,ValueError): lead_minutes=60
        if appointment.starts_at-now>timedelta(minutes=lead_minutes): continue
        with transaction.atomic():
            row=VAAppointment.objects.select_for_update().filter(pk=appointment.pk,reminder_sent_at__isnull=True).first()
            if not row: continue
            row.reminder_sent_at=now; row.save(update_fields=["reminder_sent_at","updated_at"])
            _notify(row.created_by,row.workspace,row.client,f"appointment:{row.pk}","va_appointment_reminder",f"Appointment reminder: {row.title}")
            if prefs.get("appointment_reminders_to_client") and row.client.email and settings.EMAIL_HOST:
                when=row.starts_at.astimezone(ZoneInfo(row.timezone)).strftime("%A, %d %B %Y at %H:%M %Z")
                try:
                    send_mail(f"Reminder: {row.title}",f"Your appointment is scheduled for {when}.",settings.DEFAULT_FROM_EMAIL,[row.client.email],fail_silently=False)
                except Exception:
                    _notify(row.created_by,row.workspace,row.client,f"appointment-mail-failed:{row.pk}","va_appointment_notice_failed","The client appointment reminder email could not be delivered.")


def _parse_local_time(value, default):
    try: return time.fromisoformat(value)
    except (TypeError,ValueError): return time.fromisoformat(default)


def _report_period(profile, now, kind):
    try: zone=ZoneInfo(profile.timezone)
    except (ZoneInfoNotFoundError,AttributeError): zone=ZoneInfo(settings.TIME_ZONE)
    local_now=now.astimezone(zone); prefs=profile.operational_preferences or {}
    if kind==VAReportDelivery.ReportType.DAILY:
        send_at=_parse_local_time(prefs.get("daily_summary_time"),"17:00")
        start_day=local_now.date()
        due=local_now.timetz().replace(tzinfo=None)>=send_at
        period_start=start_day
        period_end=datetime.combine(start_day+timedelta(days=1),time.min,zone).astimezone(dt_timezone.utc)
    else:
        send_at=_parse_local_time(prefs.get("weekly_report_time"),"16:00")
        try: weekday=int(prefs.get("weekly_report_weekday",4))
        except (TypeError,ValueError): weekday=4
        weekday=min(max(weekday,0),6)
        target_day=local_now.date()-timedelta(days=(local_now.weekday()-weekday)%7)
        due=local_now.weekday()>weekday or (local_now.weekday()==weekday and local_now.timetz().replace(tzinfo=None)>=send_at)
        period_start=target_day-timedelta(days=target_day.weekday()+7)
        period_end=datetime.combine(target_day-timedelta(days=target_day.weekday()),time.min,zone).astimezone(dt_timezone.utc)
        if not due: return None
    if not due: return None
    return period_start,period_end,local_now


def _report_body(client, period_start, period_end, kind):
    profile=_local_profile(client)
    try: zone=ZoneInfo(profile.timezone if profile else settings.TIME_ZONE)
    except ZoneInfoNotFoundError: zone=ZoneInfo(settings.TIME_ZONE)
    start_dt=datetime.combine(period_start,time.min,zone).astimezone(dt_timezone.utc)
    now=period_end if hasattr(period_end,"tzinfo") else timezone.now()
    completed=Task.objects.filter(workspace=client.workspace,project__client=client,project__service__category="virtual_assistance",
        status=Task.Status.DONE,is_client_visible=True,updated_at__gte=start_dt,updated_at__lte=now).count()
    requests=VAClientRequest.objects.filter(workspace=client.workspace,client=client,created_at__gte=start_dt,created_at__lte=now)
    appointments=VAAppointment.objects.filter(workspace=client.workspace,client=client,is_client_visible=True,starts_at__gte=start_dt,starts_at__lte=now).exclude(status=VAAppointment.Status.CANCELLED).count()
    emails=ScheduledEmail.objects.filter(workspace=client.workspace,client=client,status=ScheduledEmail.Status.SENT,sent_at__gte=start_dt,sent_at__lte=now).count()
    entries=VATimeEntry.objects.filter(workspace=client.workspace,client=client,is_client_visible=True,ended_at__isnull=False,started_at__gte=start_dt,started_at__lte=now)
    minutes=sum(int((entry.ended_at-entry.started_at).total_seconds()/60) for entry in entries)
    label="Daily" if kind==VAReportDelivery.ReportType.DAILY else "Weekly"
    return (f"{label} Virtual Assistance summary for {client.name}\n\n"
        f"Completed tasks: {completed}\nRequests received: {requests.count()}\nRequests completed: {requests.filter(status=VAClientRequest.Status.COMPLETED,completed_at__gte=start_dt).count()}\n"
        f"Emails sent: {emails}\nAppointments managed: {appointments}\nTracked client-visible time: {minutes} minutes\n\n"
        "This summary includes only work recorded in MONATA and marked client-visible where applicable.")


def _deliver_reports(now, limit=100):
    if not settings.EMAIL_HOST or not settings.DEFAULT_FROM_EMAIL: return
    for profile in VAClientProfile.objects.select_related("workspace","client")[:limit]:
        if not profile.client.email: continue
        prefs=profile.operational_preferences or {}
        for kind,pref_key in ((VAReportDelivery.ReportType.DAILY,"daily_summary_enabled"),(VAReportDelivery.ReportType.WEEKLY,"weekly_report_enabled")):
            if not prefs.get(pref_key,False): continue
            period=_report_period(profile,now,kind)
            if not period: continue
            period_start,period_end,local_now=period
            delivery,created=VAReportDelivery.objects.get_or_create(workspace=profile.workspace,client=profile.client,
                report_type=kind,period_start=period_start,defaults={"recipient":profile.client.email})
            if delivery.status=="sent" or delivery.attempts>=5 or delivery.next_attempt_at>now: continue
            with transaction.atomic():
                delivery=VAReportDelivery.objects.select_for_update().get(pk=delivery.pk)
                if delivery.status=="sent" or delivery.attempts>=5 or delivery.next_attempt_at>now: continue
                try:
                    body=_report_body(profile.client,period_start,period_end,kind)
                    title="Daily VA summary" if kind==VAReportDelivery.ReportType.DAILY else "Weekly VA activity report"
                    send_mail(f"{title} — {profile.client.name}",body,settings.DEFAULT_FROM_EMAIL,[delivery.recipient],fail_silently=False)
                    delivery.status="sent"; delivery.sent_at=now; delivery.last_error=""
                except Exception as exc:
                    delivery.status="failed"; delivery.last_error=str(exc)[:200]
                    delivery.next_attempt_at=now+timedelta(minutes=min(60,2**(delivery.attempts+1)))
                delivery.attempts+=1; delivery.save(update_fields=["status","sent_at","last_error","next_attempt_at","attempts","updated_at"])


def preferred_email_send_time(job, now):
    """Return the next permitted send time, or None when the client's window is open."""
    profile=_local_profile(job.client)
    window=(profile.operational_preferences or {}).get("email_send_window") if profile else None
    if not isinstance(window,dict): return None
    try:
        zone=ZoneInfo(window.get("timezone") or profile.timezone)
        starts=time.fromisoformat(window["start"]); ends=time.fromisoformat(window["end"])
        weekdays={int(day) for day in window.get("weekdays",[0,1,2,3,4])}
        if not weekdays or any(day<0 or day>6 for day in weekdays) or starts>=ends: return now+timedelta(hours=1)
    except (ValueError,TypeError,KeyError,ZoneInfoNotFoundError):
        return now+timedelta(hours=1)
    local_now=now.astimezone(zone); local_time=local_now.timetz().replace(tzinfo=None)
    if local_now.weekday() in weekdays and starts<=local_time<ends: return None
    for offset in range(0,8):
        day=local_now.date()+timedelta(days=offset)
        if day.weekday() not in weekdays: continue
        candidate=datetime.combine(day,starts,zone)
        if candidate<=local_now: continue
        return candidate.astimezone(dt_timezone.utc)
    return None


def run_va_automations(now=None):
    now=now or timezone.now()
    _expire_va_approvals(now)
    _run_sla_alerts(now)
    _run_due_followups(now)
    _appointment_reminders(now)
    _deliver_reports(now)
