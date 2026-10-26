import hashlib
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone

from .models import (
    Client, DepartmentAccess, EmailNotification, Membership, Task, VAAgentAvailability, Workspace,
    VAClientAssignment, VAClientChecklistCompletion, VAClientChecklistRun, VAClientPortalAccess,
    VAClientProfile, VAClientRequest, VAQualityReview, VARecurringTask, VATaskChecklist,
    VAAutomationEvent, VAFollowUp, VAActionApproval, VAAppointment,
)


def _dedupe_event(workspace, client, event_key, event_type, payload=None):
    try:
        with transaction.atomic():
            return VAAutomationEvent.objects.create(workspace=workspace, client=client,
                event_key=event_key[:220], event_type=event_type, payload=payload or {})
    except IntegrityError:
        return None


def _notify(user, workspace, client, event_key, kind, message):
    if not user: return
    with transaction.atomic():
        event=_dedupe_event(workspace,client,f"notify:{event_key}:{user.pk}",kind,{"user_id":str(user.pk)})
        if event:
            EmailNotification.objects.create(workspace=workspace,user=user,notification_type=kind,
                message=message[:240],reference_id=str(client.pk if client else ""))


def start_onboarding_checklists(assignment):
    if not assignment.is_active: return
    templates=VATaskChecklist.objects.filter(workspace=assignment.workspace,is_active=True,auto_start_on_assignment=True).filter(
        Q(client__isnull=True)|Q(client=assignment.client))
    for checklist in templates:
        run,created=VAClientChecklistRun.objects.get_or_create(workspace=assignment.workspace,checklist=checklist,
            client=assignment.client,assignment=assignment,defaults={"assigned_to":assignment.agent,"created_by":assignment.assigned_by})
        if created:
            VAClientChecklistCompletion.objects.bulk_create([
                VAClientChecklistCompletion(workspace=assignment.workspace,run=run,item=item)
                for item in checklist.items.all()
            ],ignore_conflicts=True)
            _notify(assignment.agent,assignment.workspace,assignment.client,
                f"onboarding:{run.pk}","va_onboarding_started",f"Onboarding checklist started: {checklist.name}")


def seed_default_onboarding_checklist(workspace):
    checklist,created=VATaskChecklist.objects.get_or_create(workspace=workspace,name="VA Client Onboarding",
        defaults={"description":"Standard checks before the VA team begins ongoing client work.","auto_start_on_assignment":True})
    if not created: return checklist
    titles=("Confirm client profile and primary contact","Confirm client timezone and working hours",
        "Connect the approved client mailbox","Verify mailbox access level and assigned agents",
        "Collect company information and key contacts","Collect client instructions and SOPs",
        "Record communication style and approval rules","Confirm escalation contacts and urgent channel",
        "Test approved workflow access","Confirm onboarding completion with the client")
    VAClientChecklistItem.objects.bulk_create([VAClientChecklistItem(workspace=workspace,checklist=checklist,title=title,position=index)
        for index,title in enumerate(titles)])
    return checklist


def _follow_up_hours(client):
    try:
        value=int(client.va_profile.operational_preferences.get("auto_follow_up_hours",24))
        return min(max(value,1),720)
    except (AttributeError,TypeError,ValueError):
        return 24


def _auto_follow_up(*, workspace, client, task=None, request=None, assigned_to=None, actor=None):
    if assigned_to is None:
        assigned_to=VAClientAssignment.objects.filter(workspace=workspace,client=client,is_active=True).values_list("agent",flat=True).first()
    if task:
        lookup={"workspace":workspace,"source_task":task,"status__in":[VAFollowUp.Status.OPEN,VAFollowUp.Status.WAITING]}
        if VAFollowUp.objects.filter(**lookup).exists(): return
        title=f"Follow up: {task.title}"
        reason=task.description[:500]
    else:
        lookup={"workspace":workspace,"source_request":request,"status__in":[VAFollowUp.Status.OPEN,VAFollowUp.Status.WAITING]}
        if VAFollowUp.objects.filter(**lookup).exists(): return
        title=f"Follow up: {request.title}"
        reason=request.description[:500]
    due=timezone.now()+timedelta(hours=_follow_up_hours(client))
    followup=VAFollowUp.objects.create(workspace=workspace,client=client,source_task=task,source_request=request,
        assigned_to=assigned_to,created_by=actor,category=VAFollowUp.Category.CLIENT,title=title,
        reason=reason,next_action="Check for the requested client information and resume the work.",due_at=due,
        status=VAFollowUp.Status.WAITING)
    _notify(assigned_to,workspace,client,f"waiting:{followup.pk}","va_waiting_follow_up_created",
        f"A client follow-up was scheduled: {title}")


def _cancel_auto_follow_up(source_task=None, source_request=None):
    qs=VAFollowUp.objects.filter(status__in=[VAFollowUp.Status.OPEN,VAFollowUp.Status.WAITING])
    qs=qs.filter(source_task=source_task) if source_task else qs.filter(source_request=source_request)
    qs.update(status=VAFollowUp.Status.CANCELLED,updated_at=timezone.now())


def _sample_quality(*, workspace, client, agent, task=None, request=None):
    percent=getattr(settings,"VA_QA_SAMPLE_PERCENT",10)
    if not agent or percent<=0: return
    obj_type="task" if task else "request"; obj=task or request
    digest=hashlib.sha256(f"{obj_type}:{obj.pk}".encode()).hexdigest()
    if int(digest[:8],16)%100>=percent: return
    key=f"auto:{obj_type}:{obj.pk}"
    VAQualityReview.objects.get_or_create(automation_key=key,defaults={
        "workspace":workspace,"client":client,"agent":agent,"task":task,"request":request,"status":VAQualityReview.Status.PENDING,
    })


@receiver(pre_save, sender=Task)
def task_pre_save(sender, instance, **kwargs):
    instance._va_previous_status=sender.objects.filter(pk=instance.pk).values_list("status",flat=True).first() if instance.pk else None


@receiver(post_save, sender=Task)
def task_post_save(sender, instance, created, **kwargs):
    previous=getattr(instance,"_va_previous_status",None)
    if not instance.project_id or not instance.project.service_id or instance.project.service.category!="virtual_assistance": return
    if not VAClientAssignment.objects.filter(workspace=instance.workspace,client=instance.project.client,agent=instance.assignee,is_active=True).exists(): return
    if previous==instance.status: return
    if instance.status==Task.Status.WAITING:
        _auto_follow_up(workspace=instance.workspace,client=instance.project.client,task=instance,assigned_to=instance.assignee,actor=instance.assignee)
    elif previous==Task.Status.WAITING:
        _cancel_auto_follow_up(source_task=instance)
    if instance.status==Task.Status.DONE and previous!=Task.Status.DONE:
        _sample_quality(workspace=instance.workspace,client=instance.project.client,agent=instance.assignee,task=instance)


@receiver(pre_save, sender=VAClientRequest)
def request_pre_save(sender, instance, **kwargs):
    previous=sender.objects.filter(pk=instance.pk).values("status","first_response_at").first() if instance.pk else None
    instance._va_previous_status=previous["status"] if previous else None
    if previous and not previous["first_response_at"] and previous["status"]==VAClientRequest.Status.SUBMITTED and instance.status in (VAClientRequest.Status.IN_PROGRESS,VAClientRequest.Status.WAITING,VAClientRequest.Status.COMPLETED):
        instance.first_response_at=timezone.now()
    if instance.status==VAClientRequest.Status.COMPLETED and previous and previous["status"]!=VAClientRequest.Status.COMPLETED:
        instance.completed_at=timezone.now()


@receiver(post_save, sender=VAClientRequest)
def request_post_save(sender, instance, created, **kwargs):
    previous=getattr(instance,"_va_previous_status",None)
    if previous==instance.status: return
    if instance.status==VAClientRequest.Status.WAITING:
        _auto_follow_up(workspace=instance.workspace,client=instance.client,request=instance,assigned_to=instance.assigned_to,actor=instance.assigned_to)
    elif previous==VAClientRequest.Status.WAITING:
        _cancel_auto_follow_up(source_request=instance)
    if instance.status==VAClientRequest.Status.COMPLETED and previous!=VAClientRequest.Status.COMPLETED:
        _sample_quality(workspace=instance.workspace,client=instance.client,agent=instance.assigned_to,request=instance)


@receiver(post_save, sender=VAClientAssignment)
def assignment_post_save(sender, instance, **kwargs):
    if instance.is_active:
        start_onboarding_checklists(instance)


@receiver(post_save, sender=Workspace)
def workspace_onboarding_template_post_save(sender, instance, created, **kwargs):
    if created: seed_default_onboarding_checklist(instance)


@receiver(post_save, sender=VAClientChecklistCompletion)
def checklist_completion_post_save(sender, instance, **kwargs):
    run=instance.run
    if not run.checklist.items.filter(is_required=True).exclude(id__in=VAClientChecklistCompletion.objects.filter(run=run,completed_at__isnull=False).values("item_id")).exists():
        if not run.completed_at:
            run.completed_at=timezone.now(); run.save(update_fields=["completed_at","updated_at"])
    elif run.completed_at:
        run.completed_at=None; run.save(update_fields=["completed_at","updated_at"])


@receiver(post_save, sender=VAAppointment)
def appointment_approval_post_save(sender, instance, created, **kwargs):
    if not created: return
    if not instance.client.va_rules.filter(is_active=True,category__in=["appointments","appointment","calendar"],requires_approval=True).exists(): return
    approval,created=VAActionApproval.objects.get_or_create(workspace=instance.workspace,appointment=instance,
        defaults={"client":instance.client,"requested_by":instance.created_by,"action_type":VAActionApproval.ActionType.APPOINTMENT,
            "title":instance.title,"summary":"This appointment requires client approval before confirmation.",
            "details":{"starts_at":instance.starts_at.isoformat(),"ends_at":instance.ends_at.isoformat(),"timezone":instance.timezone}})
    if not created: return
    _notify(instance.created_by,instance.workspace,instance.client,f"appointment-approval:{approval.pk}","va_approval_requested",
        f"Client approval requested for appointment: {instance.title}")
    users=Membership.objects.filter(workspace=instance.workspace,is_active=True,
        user__va_portal_access__client=instance.client,user__va_portal_access__is_active=True).select_related("user").distinct()
    for membership in users:
        _notify(membership.user,instance.workspace,instance.client,f"appointment-approval:{approval.pk}",
            "va_client_approval_requested",f"MONATA needs your approval for: {instance.title}")
