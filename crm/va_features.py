"""Operational Virtual Assistance features that extend the core VA work loop."""
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import (
    AuditLog, Client, DepartmentAccess, Membership, Project, Task, VAAgentAvailability,
    VAClientAssignment, VAClientInstruction, VAClientProfile, VAClientRequest, VAClientRule,
    VAHandover, VAQualityReview, VARecurringTask, VARequestTemplate, VASLATarget,
    VATaskChecklist, VATaskChecklistItem, VATimeEntry, VAResearchOption, VAFollowUp, ScheduledEmail, EmailApproval,
    VAClientChecklistRun, VAClientChecklistCompletion, MailboxConnection, MailboxAgentAccess, VAAppointment, VAActionApproval,
)
from .va import _client_ids, _context, _audit


class FeatureSerializer(serializers.ModelSerializer):
    class Meta:
        fields = "__all__"
        read_only_fields = ("id", "workspace", "created_at", "updated_at", "created_by", "authored_by", "reviewer", "last_run_at", "automation_key")

    def validate(self, attrs):
        request = self.context["request"]
        for name, value in attrs.items():
            related = value if isinstance(value, (list, tuple)) else [value]
            for obj in related:
                ws_id = getattr(obj, "workspace_id", None)
                if ws_id and ws_id != request.workspace.id:
                    raise serializers.ValidationError({name: "Related records must belong to the active workspace."})
        model = getattr(self.Meta, "model", None)
        if model is VARecurringTask:
            project = attrs.get("project", getattr(self.instance, "project", None))
            if project and (project.service_id is None or project.service.category != "virtual_assistance"):
                raise serializers.ValidationError({"project": "Recurring VA work must use a Virtual Assistance project."})
        if model is VAQualityReview:
            for key in ("communication_score", "accuracy_score", "sop_score"):
                value = attrs.get(key, getattr(self.instance, key, None))
                if value is not None and value > 10:
                    raise serializers.ValidationError({key: "Score must be between 0 and 10."})
            scores=[attrs.get(key,getattr(self.instance,key,None)) for key in ("communication_score","accuracy_score","sop_score")]
            if any(score is not None for score in scores) and not all(score is not None for score in scores):
                raise serializers.ValidationError("Provide all three QA scores together.")
            if all(score is not None for score in scores): attrs["status"]=VAQualityReview.Status.REVIEWED
        if model is VATimeEntry:
            client=attrs.get("client",getattr(self.instance,"client",None))
            task=attrs.get("task",getattr(self.instance,"task",None))
            if task and client and task.project.client_id != client.id:
                raise serializers.ValidationError({"task":"Task must belong to this client."})
            start=attrs.get("started_at",getattr(self.instance,"started_at",None)); end=attrs.get("ended_at",getattr(self.instance,"ended_at",None))
            if start and end and end<=start: raise serializers.ValidationError({"ended_at":"Time entry must end after it starts."})
        if model is VAClientChecklistCompletion:
            run=attrs.get("run",getattr(self.instance,"run",None)); item=attrs.get("item",getattr(self.instance,"item",None))
            if run and item and run.checklist_id != item.checklist_id:
                raise serializers.ValidationError({"item":"Checklist item must belong to this run's checklist."})
        if model is VAClientChecklistRun:
            checklist=attrs.get("checklist",getattr(self.instance,"checklist",None)); client=attrs.get("client",getattr(self.instance,"client",None))
            if checklist and client and checklist.client_id and checklist.client_id != client.id:
                raise serializers.ValidationError({"checklist":"Checklist is configured for a different client."})
        if model is VAAppointment:
            client=attrs.get("client",getattr(self.instance,"client",None))
            contact=attrs.get("contact",getattr(self.instance,"contact",None))
            task=attrs.get("task",getattr(self.instance,"task",None))
            starts=attrs.get("starts_at",getattr(self.instance,"starts_at",None)); ends=attrs.get("ends_at",getattr(self.instance,"ends_at",None))
            if contact and client and contact.client_id != client.id: raise serializers.ValidationError({"contact":"Contact must belong to this client."})
            if task and client and task.project.client_id != client.id: raise serializers.ValidationError({"task":"Task must belong to this client."})
            if starts and ends and ends <= starts: raise serializers.ValidationError({"ends_at":"Appointment must end after it starts."})
            status=attrs.get("status",getattr(self.instance,"status",VAAppointment.Status.TENTATIVE))
            if client and status==VAAppointment.Status.CONFIRMED:
                requires_approval=client.va_rules.filter(is_active=True,category__in=["appointments","appointment","calendar"],requires_approval=True).exists()
                if requires_approval:
                    approved=bool(self.instance and self.instance.approvals.filter(status=VAActionApproval.Status.APPROVED).exists())
                    if not self.instance:
                        attrs["status"]=VAAppointment.Status.TENTATIVE
                    elif not approved:
                        raise serializers.ValidationError({"status":"Client approval is required before confirming this appointment."})
        if not request.va_manager and model in (VAAppointment, VATimeEntry) and "is_client_visible" in attrs and attrs["is_client_visible"]:
            raise PermissionDenied("Only a VA manager can mark work as client visible.")
        profile = model is VAClientProfile
        if profile:
            backup=attrs.get("backup_agent",getattr(self.instance,"backup_agent",None))
            if backup and not Membership.objects.filter(workspace=request.workspace,user=backup,is_active=True).exists():
                raise serializers.ValidationError({"backup_agent":"Backup agent must be an active workspace member."})
            if backup and not (DepartmentAccess.objects.filter(workspace=request.workspace,user=backup,department=DepartmentAccess.Department.VIRTUAL_ASSISTANCE,is_active=True).exists() or Membership.objects.filter(workspace=request.workspace,user=backup,is_active=True,role__in=[Membership.Role.OWNER,Membership.Role.ADMIN]).exists()):
                raise serializers.ValidationError({"backup_agent":"Backup agent must have Virtual Assistance access."})
        if model is VARequestTemplate:
            fields=attrs.get("fields",getattr(self.instance,"fields",[])) or []
            if not isinstance(fields,list): raise serializers.ValidationError({"fields":"Fields must be a list of field definitions."})
            seen=set()
            for field in fields:
                if not isinstance(field,dict) or not field.get("name") or field.get("type","text") not in ("text","textarea","email","number","date","select","boolean"):
                    raise serializers.ValidationError({"fields":"Each field needs a name and a supported type."})
                if field["name"] in seen: raise serializers.ValidationError({"fields":"Field names must be unique."})
                seen.add(field["name"])
        if not request.va_manager:
            client=attrs.get("client")
            if model is VAResearchOption:
                related_request=attrs.get("request",getattr(self.instance,"request",None))
                client=related_request.client if related_request else None
            elif model is VAClientChecklistCompletion:
                run=attrs.get("run",getattr(self.instance,"run",None)); client=run.client if run else None
            if client and not VAClientAssignment.objects.filter(workspace=request.workspace,client=client,agent=request.user,is_active=True).exists():
                raise PermissionDenied("You are not assigned to this client.")
        return attrs


def serialize_feature(obj, many=False):
    model = obj.model if many else obj.__class__
    serializer = type(f"{model.__name__}OutputSerializer", (FeatureSerializer,), {
        "Meta": type("Meta", (), {"model": model, "fields": "__all__", "read_only_fields": FeatureSerializer.Meta.read_only_fields})
    })
    return serializer(obj, many=many).data


class FeatureViewSet(viewsets.ModelViewSet):
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    model = None
    client_path = "client_id"
    manager_write = True
    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        _context(request)
    def get_serializer_class(self):
        readonly=FeatureSerializer.Meta.read_only_fields
        if self.model is VAQualityReview:
            readonly=readonly+("status",)
        return type(f"{self.model.__name__}FeatureSerializer", (FeatureSerializer,), {
            "Meta": type("Meta", (), {"model": self.model, "fields": "__all__", "read_only_fields": readonly})
        })
    def get_queryset(self):
        qs = self.model.objects.filter(workspace=self.request.workspace)
        if self.client_path:
            if self.model is VARequestTemplate:
                qs = qs.filter(Q(client__isnull=True) | Q(client_id__in=_client_ids(self.request)))
            else:
                qs = qs.filter(**{f"{self.client_path}__in": _client_ids(self.request)})
        if not self.request.va_manager:
            readable_for_agents=(VAClientProfile,VAClientRule,VARequestTemplate,VARecurringTask,VATaskChecklist,VATaskChecklistItem,VASLATarget,VAClientChecklistRun,VAClientChecklistCompletion)
            if self.manager_write and self.model not in readable_for_agents:
                qs = qs.none()
            elif self.model is VATimeEntry:
                qs = qs.filter(agent=self.request.user)
            elif self.model is VAAgentAvailability:
                qs = qs.filter(agent=self.request.user)
            elif self.model is VAQualityReview:
                qs = qs.filter(agent=self.request.user)
            elif self.model is VAClientChecklistRun:
                qs = qs.filter(Q(assigned_to=self.request.user)|Q(assigned_to__isnull=True))
            elif self.model is VAClientChecklistCompletion:
                qs = qs.filter(Q(run__assigned_to=self.request.user)|Q(run__assigned_to__isnull=True))
        if self.model is VATaskChecklistItem:
            qs=qs.filter(Q(checklist__client__isnull=True)|Q(checklist__client_id__in=_client_ids(self.request)))
        if self.model is VATaskChecklist:
            qs=qs.filter(Q(client__isnull=True)|Q(client_id__in=_client_ids(self.request)))
        return qs
    def _may_write(self):
        if self.request.va_manager or not self.manager_write: return
        raise PermissionDenied("Only a VA manager can manage this resource.")
    def perform_create(self, serializer):
        if self.manager_write: self._may_write()
        extras = {"workspace": self.request.workspace}
        if self.model is VARequestTemplate: extras["created_by"] = self.request.user
        if self.model is VAClientRule: extras["authored_by"] = self.request.user
        if self.model is VAClientInstruction: extras["authored_by"] = self.request.user
        if self.model is VAQualityReview: extras["reviewer"] = self.request.user
        if self.model is VATimeEntry: extras["agent"] = self.request.user
        if self.model is VAAppointment: extras["created_by"] = self.request.user
        if self.model is VAClientChecklistRun:
            extras["created_by"] = self.request.user
            if not self.request.va_manager: extras["assigned_to"] = self.request.user
        if self.model is VAClientChecklistCompletion:
            extras["completed_by"] = self.request.user
            extras["completed_at"] = timezone.now()
        _audit(self.request, "created", serializer.save(**extras))
    def perform_update(self, serializer):
        if self.manager_write: self._may_write()
        _audit(self.request, "updated", serializer.save())
    def perform_destroy(self, instance):
        if self.manager_write: self._may_write()
        _audit(self.request, "deleted", instance)
        instance.delete()

    @action(detail=True, methods=["post"], url_path="stop")
    def stop(self, request, pk=None):
        if self.model is not VATimeEntry: raise ValidationError("Stop is only available for time entries.")
        entry=self.get_object()
        if entry.ended_at is None:
            entry.ended_at=timezone.now(); entry.save(update_fields=["ended_at","updated_at"])
            _audit(request,"time_entry_stopped",entry)
        return Response(self.get_serializer(entry).data)


def feature_view(model, *, client_path="client_id", manager_write=True):
    return type(f"{model.__name__}ViewSet", (FeatureViewSet,), {
        "model": model, "client_path": client_path, "manager_write": manager_write,
        "basename": model.__name__.lower(),
    })


class ClientContextView(APIView):
    def get(self, request, client_id):
        workspace, manager = _context(request)
        if not VAClientAssignment.objects.filter(workspace=workspace, client_id=client_id, is_active=True,
                **({} if manager else {"agent": request.user})).exists():
            raise PermissionDenied("You are not assigned to this client.")
        client = Client.objects.get(workspace=workspace, id=client_id)
        profile = VAClientProfile.objects.filter(workspace=workspace, client=client).first()
        rules = list(client.va_rules.filter(is_active=True).values("id", "category", "title", "rule", "requires_approval", "approval_threshold"))
        instructions = list(VAClientInstruction.objects.filter(workspace=workspace, client=client, is_active=True).values("id", "topic", "title", "instruction", "is_pinned"))
        counts = {"open_requests": VAClientRequest.objects.filter(workspace=workspace, client=client).exclude(status__in=["completed", "declined"]).count(),
                  "open_tasks": Task.objects.filter(workspace=workspace, project__client=client, project__service__category="virtual_assistance").exclude(status=Task.Status.DONE).count(),
                  "open_follow_ups": VAFollowUp.objects.filter(workspace=workspace, client=client).exclude(status__in=["completed", "cancelled"]).count()}
        data = {"client": {"id": str(client.id), "name": client.name, "company": client.company, "email": client.email, "status": client.status},
                "profile": serialize_feature(profile) if profile else None, "rules": rules, "instructions": instructions, "counts": counts}
        return Response(data)

    def put(self, request, client_id):
        workspace, manager = _context(request)
        if not manager: raise PermissionDenied("Only a VA manager can update client operating preferences.")
        client = Client.objects.get(workspace=workspace, id=client_id)
        obj, _ = VAClientProfile.objects.get_or_create(workspace=workspace, client=client)
        serializer_class = type("VAClientProfileSerializer", (FeatureSerializer,), {"Meta": type("Meta", (), {"model": VAClientProfile, "fields": "__all__", "read_only_fields": FeatureSerializer.Meta.read_only_fields})})
        serializer = serializer_class(obj, data=request.data, partial=True, context={"request": request})
        serializer.is_valid(raise_exception=True); serializer.save(workspace=workspace, client=client)
        _audit(request, "client_profile_updated", obj)
        return Response(serializer.data)


class WorkloadView(APIView):
    def get(self, request):
        workspace, manager = _context(request)
        if not manager: raise PermissionDenied("Only a VA manager can view team workload.")
        agents = DepartmentAccess.objects.filter(workspace=workspace, department=DepartmentAccess.Department.VIRTUAL_ASSISTANCE,
            is_active=True).select_related("user")
        today = timezone.localdate(); rows=[]
        for access in agents:
            user=access.user
            tasks=Task.objects.filter(workspace=workspace, project__service__category="virtual_assistance", assignee=user).exclude(status=Task.Status.DONE)
            requests=VAClientRequest.objects.filter(workspace=workspace, assigned_to=user).exclude(status__in=["completed", "declined"])
            followups=VAFollowUp.objects.filter(workspace=workspace, assigned_to=user).exclude(status__in=["completed", "cancelled"])
            rows.append({"agent_id":str(user.id), "email":user.email, "active":VAAgentAvailability.objects.filter(workspace=workspace,agent=user,is_available=True).exists() or not VAAgentAvailability.objects.filter(workspace=workspace,agent=user).exists(),
                "active_tasks":tasks.count(), "due_today":tasks.filter(due_date=today).count()+requests.filter(requested_due_at__date=today).count(),
                "overdue":tasks.filter(due_date__lt=today).count()+requests.filter(requested_due_at__lt=timezone.now()).count(), "open_requests":requests.count(), "open_follow_ups":followups.count(),
                "tracked_minutes":0})
        # Use stored intervals for hours; an open timer contributes elapsed time to now.
        for row in rows:
            entries=VATimeEntry.objects.filter(workspace=workspace,agent_id=row["agent_id"],started_at__date=today)
            row["tracked_minutes"]=sum(int(((e.ended_at or timezone.now())-e.started_at).total_seconds()/60) for e in entries)
        return Response({"date":today, "agents":rows})


class AvailabilityView(APIView):
    def post(self, request):
        workspace, manager = _context(request)
        agent_id = request.data.get("agent") if manager else request.user.id
        if not manager and agent_id != request.user.id: raise PermissionDenied()
        if not Membership.objects.filter(workspace=workspace,user_id=agent_id,is_active=True).exists(): raise ValidationError({"agent":"Agent must be an active workspace member."})
        obj,_=VAAgentAvailability.objects.get_or_create(workspace=workspace,agent_id=agent_id)
        serializer_class=type("AvailabilitySerializer",(FeatureSerializer,),{"Meta":type("Meta",(),{"model":VAAgentAvailability,"fields":"__all__","read_only_fields":FeatureSerializer.Meta.read_only_fields})})
        serializer=serializer_class(obj,data=request.data,partial=True,context={"request":request}); serializer.is_valid(raise_exception=True)
        serializer.save(workspace=workspace,agent_id=agent_id)
        reassigned=0
        if not obj.is_available and manager:
            assignments=VAClientAssignment.objects.filter(workspace=workspace,agent_id=agent_id,is_active=True).select_related("client")
            for assignment in assignments:
                backup=assignment.client.va_profile.backup_agent if hasattr(assignment.client,"va_profile") else None
                if not backup: continue
                Task.objects.filter(workspace=workspace,project__client=assignment.client,project__service__category="virtual_assistance",assignee_id=agent_id).exclude(status=Task.Status.DONE).update(assignee=backup)
                VAFollowUp.objects.filter(workspace=workspace,client=assignment.client,assigned_to_id=agent_id).exclude(status__in=["completed","cancelled"]).update(assigned_to=backup)
                VAClientRequest.objects.filter(workspace=workspace,client=assignment.client,assigned_to_id=agent_id).exclude(status__in=["completed","declined"]).update(assigned_to=backup)
                VAClientAssignment.objects.update_or_create(workspace=workspace,client=assignment.client,agent=backup,defaults={"assigned_by":request.user,"is_active":True})
                assignment.is_active=False; assignment.save(update_fields=["is_active","updated_at"])
                reassigned += 1
        _audit(request,"availability_updated",obj)
        return Response({**serialize_feature(obj),"clients_reassigned":reassigned})


class HandoverView(APIView):
    def get(self, request):
        workspace, manager=_context(request)
        rows=VAHandover.objects.filter(workspace=workspace)
        if not manager: rows=rows.filter(to_agent=request.user)
        return Response(serialize_feature(rows.order_by("-created_at")[:100],many=True))
    def post(self, request):
        workspace, manager=_context(request)
        if not manager: raise PermissionDenied("Only a VA manager can hand over client ownership.")
        client=Client.objects.get(workspace=workspace,id=request.data.get("client"))
        target=request.data.get("to_agent")
        assignment=VAClientAssignment.objects.filter(workspace=workspace,client=client,is_active=True,agent_id=target).first()
        if not assignment: raise ValidationError({"to_agent":"The receiving agent must be assigned to this client."})
        tasks=Task.objects.filter(workspace=workspace,project__client=client,project__service__category="virtual_assistance").exclude(status=Task.Status.DONE)
        requests=VAClientRequest.objects.filter(workspace=workspace,client=client).exclude(status__in=["completed","declined"])
        followups=VAFollowUp.objects.filter(workspace=workspace,client=client).exclude(status__in=["completed","cancelled"])
        snapshot={"open_tasks":tasks.count(),"urgent_tasks":tasks.filter(priority="urgent").count(),"waiting_tasks":tasks.filter(status=Task.Status.WAITING).count(),"open_requests":requests.count(),"waiting_follow_ups":followups.filter(status=VAFollowUp.Status.WAITING).count(),"open_follow_ups":followups.count(),"upcoming_appointments":VAAppointment.objects.filter(workspace=workspace,client=client,starts_at__gte=timezone.now()).exclude(status=VAAppointment.Status.CANCELLED).count(),"scheduled_emails":ScheduledEmail.objects.filter(workspace=workspace,client=client,status=ScheduledEmail.Status.SCHEDULED).count(),"pending_email_approvals":EmailApproval.objects.filter(workspace=workspace,status=EmailApproval.Status.PENDING,scheduled_email__client=client).count(),
            "instructions":list(VAClientInstruction.objects.filter(workspace=workspace,client=client,is_active=True).values("id","title","instruction")),"rules":list(client.va_rules.filter(is_active=True).values("id","title","rule")),"last_activity":timezone.now().isoformat()}
        current=VAClientAssignment.objects.filter(workspace=workspace,client=client,is_active=True).first()
        handover=VAHandover.objects.create(workspace=workspace,client=client,to_agent_id=target,from_agent_id=current.agent_id if current else None,created_by=request.user,summary=request.data.get("summary",""),snapshot=snapshot)
        Task.objects.filter(workspace=workspace,project__client=client,project__service__category="virtual_assistance").exclude(status=Task.Status.DONE).update(assignee_id=target)
        VAClientRequest.objects.filter(workspace=workspace,client=client).exclude(status__in=["completed","declined"]).update(assigned_to_id=target)
        VAFollowUp.objects.filter(workspace=workspace,client=client).exclude(status__in=["completed","cancelled"]).update(assigned_to_id=target)
        VAClientAssignment.objects.filter(workspace=workspace,client=client,is_active=True).update(is_active=False)
        VAClientAssignment.objects.update_or_create(workspace=workspace,client=client,agent_id=target,defaults={"assigned_by":request.user,"is_active":True})
        _audit(request,"client_handover",handover)
        return Response(serialize_feature(handover),status=201)


class ReportView(APIView):
    def get(self, request):
        workspace, manager=_context(request)
        client_id=request.query_params.get("client")
        if client_id and str(client_id) not in {str(i) for i in _client_ids(request)}: raise PermissionDenied("You are not assigned to this client.")
        client_filter={"project__client_id":client_id} if client_id else {"project__client_id__in":_client_ids(request)}
        follow_filter={"client_id":client_id} if client_id else {"client_id__in":_client_ids(request)}
        period=request.query_params.get("period","week")
        days=1 if period=="day" else (30 if period=="month" else 7)
        since=timezone.now()-timedelta(days=days)
        tasks=Task.objects.filter(workspace=workspace,project__service__category="virtual_assistance",**client_filter)
        completed=tasks.filter(status=Task.Status.DONE,updated_at__gte=since)
        requests=VAClientRequest.objects.filter(workspace=workspace,created_at__gte=since,**follow_filter)
        followups=VAFollowUp.objects.filter(workspace=workspace,created_at__gte=since,**follow_filter)
        entries=VATimeEntry.objects.filter(workspace=workspace,started_at__gte=since,**({"client_id":client_id} if client_id else {"client_id__in":_client_ids(request)}))
        minutes=sum(int(((e.ended_at or timezone.now())-e.started_at).total_seconds()/60) for e in entries)
        mail_filter={"client_id":client_id} if client_id else {"client_id__in":_client_ids(request)}
        emails_sent=ScheduledEmail.objects.filter(workspace=workspace,status=ScheduledEmail.Status.SENT,sent_at__gte=since,**mail_filter).count()
        appointment_filter={"client_id":client_id} if client_id else {"client_id__in":_client_ids(request)}
        appointments=VAAppointment.objects.filter(workspace=workspace,starts_at__gte=since,**appointment_filter).exclude(status=VAAppointment.Status.CANCELLED).count()
        return Response({"period":period,"from":since,"to":timezone.now(),"client":client_id,"tasks_completed":completed.count(),"requests_received":requests.count(),"requests_completed":requests.filter(status="completed",completed_at__gte=since).count(),"follow_ups_completed":followups.filter(status="completed",completed_at__gte=since).count(),"emails_sent":emails_sent,"appointments_managed":appointments,"tracked_minutes":minutes,"pending_client_actions":requests.filter(status="waiting_for_client").count()+followups.filter(status="waiting_for_client").count(),"completed_items":list(completed.values("id","title","updated_at")[:100])})


class ConvertRequestToTaskView(APIView):
    def post(self, request, request_id):
        workspace, _=_context(request)
        item=VAClientRequest.objects.get(workspace=workspace,id=request_id,client_id__in=_client_ids(request))
        existing=item.generated_tasks.first()
        if existing: return Response({"task_id":str(existing.id),"created":False})
        project=Project.objects.filter(workspace=workspace,client=item.client,service__category="virtual_assistance").first()
        if not project: raise ValidationError("Create a Virtual Assistance project for this client before converting requests to tasks.")
        task=Task.objects.create(workspace=workspace,project=project,title=item.title,description=item.description,priority={"normal":"normal","important":"high","urgent":"urgent"}.get(item.priority,"normal"),assignee=item.assigned_to or request.user,due_date=item.requested_due_at.date() if item.requested_due_at else None,source_request=item)
        _audit(request,"client_request_converted_to_task",task)
        return Response({"task_id":str(task.id),"created":True},status=201)


class TaskFromEmailView(APIView):
    def post(self,request):
        workspace,_=_context(request)
        client=Client.objects.filter(workspace=workspace,id=request.data.get("client"),id__in=_client_ids(request)).first()
        if not client: raise PermissionDenied("You are not assigned to this client.")
        thread_id=str(request.data.get("email_thread_id","")).strip()
        if not thread_id: raise ValidationError({"email_thread_id":"An email thread reference is required."})
        priority=request.data.get("priority","normal")
        if priority not in ("low","normal","high","urgent"): raise ValidationError({"priority":"Choose low, normal, high, or urgent."})
        connections=MailboxConnection.objects.filter(workspace=workspace,client=client,status=MailboxConnection.Status.ACTIVE,
            agent_access__user=request.user,agent_access__is_active=True,agent_access__can_read=True)
        if not connections.exists(): raise PermissionDenied("No authorized mailbox access is available for this client.")
        project=Project.objects.filter(workspace=workspace,client=client,service__category="virtual_assistance").first()
        if not project: raise ValidationError("Create a Virtual Assistance project for this client before creating email tasks.")
        from rest_framework.fields import DateField
        due=DateField().run_validation(request.data.get("due_date")) if request.data.get("due_date") else None
        assignee_id=request.data.get("assignee") or request.user.id
        if not VAClientAssignment.objects.filter(workspace=workspace,client=client,agent_id=assignee_id,is_active=True).exists() and not Membership.objects.filter(workspace=workspace,user_id=assignee_id,is_active=True,role__in=[Membership.Role.ADMIN,Membership.Role.OWNER]).exists():
            raise ValidationError({"assignee":"Assignee must be assigned to this client."})
        task=Task.objects.create(workspace=workspace,project=project,title=request.data.get("title") or "Follow up on email",
            description=request.data.get("description", ""),priority=priority,
            due_date=due,assignee_id=assignee_id,source_email_thread_id=thread_id)
        _audit(request,"task_created_from_email",task)
        return Response({"task_id":str(task.id),"email_thread_id":thread_id},status=201)


class SLAMonitorView(APIView):
    def get(self, request):
        workspace, manager = _context(request)
        requests = VAClientRequest.objects.filter(workspace=workspace, client_id__in=_client_ids(request),
            status__in=["submitted", "in_progress", "waiting_for_client"])
        if not manager: requests = requests.filter(Q(assigned_to=request.user) | Q(assigned_to__isnull=True))
        now = timezone.now(); result=[]
        for item in requests.select_related("client").order_by("created_at")[:500]:
            target = VASLATarget.objects.filter(workspace=workspace, client=item.client, category=item.priority, is_active=True).first()
            if not target: target = VASLATarget.objects.filter(workspace=workspace, client=item.client, category="normal", is_active=True).first()
            if not target: continue
            is_response_pending=item.first_response_at is None
            if is_response_pending:
                due=item.created_at+timedelta(minutes=target.response_minutes)
                sla_kind="response"
            elif target.resolution_minutes:
                due=item.created_at+timedelta(minutes=target.resolution_minutes)
                sla_kind="resolution"
            else:
                continue
            remaining = int((due-now).total_seconds())
            result.append({"request_id":str(item.id),"client":item.client.name,"title":item.title,"status":item.status,
                "target_minutes":target.response_minutes if sla_kind=="response" else target.resolution_minutes,"sla_kind":sla_kind,"response_due_at":due,"remaining_seconds":remaining,
                "state":"breached" if remaining < 0 else ("warning" if remaining <= 3600 else "on_track")})
        return Response({"items":result,"breached":sum(x["state"]=="breached" for x in result),"at_risk":sum(x["state"]=="warning" for x in result)})


class PortalTemplatesView(APIView):
    """Client portal request forms; schema is metadata and values are validated against it."""
    def get(self, request):
        from .va import _client_portal_context
        workspace, accesses = _client_portal_context(request)
        client_id=request.query_params.get("client")
        if client_id and not accesses.filter(client_id=client_id).exists(): raise PermissionDenied("No portal access to this client.")
        rows=VARequestTemplate.objects.filter(workspace=workspace,is_active=True).filter(Q(client__isnull=True)|Q(client_id__in=accesses.values_list("client_id",flat=True)))
        if client_id: rows=rows.filter(Q(client__isnull=True)|Q(client_id=client_id))
        return Response(serialize_feature(rows.order_by("name"),many=True))

    def post(self, request):
        from .va import _client_portal_context
        workspace, accesses = _client_portal_context(request)
        template=VARequestTemplate.objects.filter(workspace=workspace,id=request.data.get("template"),is_active=True).first()
        if not template: raise ValidationError({"template":"Choose an active request template."})
        client=accesses.filter(client_id=request.data.get("client")).first()
        if not client: raise ValidationError({"client":"Choose a client you can access."})
        if template.client_id and template.client_id != client.client_id: raise ValidationError({"template":"This form is not available for the selected client."})
        answers=request.data.get("answers",{})
        if not isinstance(answers,dict): raise ValidationError({"answers":"Answers must be an object."})
        schema=template.fields if isinstance(template.fields,list) else []
        errors={}
        for field in schema:
            name=field.get("name")
            value=answers.get(name)
            if field.get("required") and (value is None or str(value).strip()==""): errors[name]="This field is required."
        if errors: raise ValidationError({"answers":errors})
        title=request.data.get("title") or template.name
        description="\n".join(f"{field.get('label') or field.get('name')}: {answers.get(field.get('name'), '')}" for field in schema)
        if request.data.get("notes"): description += "\n\nAdditional details: " + str(request.data["notes"])
        from rest_framework.fields import DateTimeField
        due_value=request.data.get("requested_due_at")
        due=DateTimeField().run_validation(due_value) if due_value else None
        priority=request.data.get("priority","normal")
        if priority not in VAClientRequest.Priority.values: raise ValidationError({"priority":"Choose normal, important, or urgent."})
        item=VAClientRequest.objects.create(workspace=workspace,client=client.client,requested_by=request.user,category=template.category,
            title=title,description=description,priority=priority,requested_due_at=due)
        _audit(request,"client_request_submitted_from_template",item)
        return Response({"id":str(item.id),"title":item.title,"status":item.status,"created_at":item.created_at},status=201)


class PortalReportView(APIView):
    def get(self,request):
        from .va import _client_portal_context
        workspace,accesses=_client_portal_context(request)
        client_id=request.query_params.get("client")
        if client_id:
            if not accesses.filter(client_id=client_id).exists(): raise PermissionDenied("No portal access to this client.")
            client_ids=[client_id]
        else:
            client_ids=list(accesses.values_list("client_id",flat=True))
        period=request.query_params.get("period","week")
        days=1 if period=="day" else (30 if period=="month" else 7)
        since=timezone.now()-timedelta(days=days)
        tasks=Task.objects.filter(workspace=workspace,project__client_id__in=client_ids,
            project__service__category="virtual_assistance",is_client_visible=True,updated_at__gte=since,status=Task.Status.DONE)
        requests=VAClientRequest.objects.filter(workspace=workspace,client_id__in=client_ids,created_at__gte=since)
        followups=VAFollowUp.objects.filter(workspace=workspace,client_id__in=client_ids,status=VAFollowUp.Status.WAITING)
        appointments=VAAppointment.objects.filter(workspace=workspace,client_id__in=client_ids,is_client_visible=True,starts_at__gte=since)
        entries=VATimeEntry.objects.filter(workspace=workspace,client_id__in=client_ids,is_client_visible=True,started_at__gte=since,ended_at__isnull=False)
        minutes=sum(int((entry.ended_at-entry.started_at).total_seconds()/60) for entry in entries)
        mail_filter={"client_id__in":client_ids}
        emails=ScheduledEmail.objects.filter(workspace=workspace,status=ScheduledEmail.Status.SENT,sent_at__gte=since,**mail_filter).count()
        return Response({"period":period,"from":since,"to":timezone.now(),"client":client_id,"tasks_completed":tasks.count(),
            "requests_received":requests.count(),"requests_completed":requests.filter(status="completed").count(),
            "follow_ups_completed":0,"pending_client_actions":requests.filter(status="waiting_for_client").count()+followups.count(),
            "emails_sent":emails,"appointments_managed":appointments.count(),"tracked_minutes":minutes,
            "completed_items":list(tasks.values("id","title","updated_at")[:100])})


class PortalPreferencesView(APIView):
    """Let the customer choose automated report and appointment email delivery."""
    allowed={"daily_summary_enabled","weekly_report_enabled","appointment_reminders_to_client",
        "daily_summary_time","weekly_report_time","weekly_report_weekday"}
    def _profile(self,request):
        from .va import _client_portal_context
        workspace,accesses=_client_portal_context(request)
        client_id=request.data.get("client") or request.query_params.get("client")
        access=accesses.filter(client_id=client_id).select_related("client").first() if client_id else None
        if not access: raise ValidationError({"client":"Select one of your clients."})
        profile,_=VAClientProfile.objects.get_or_create(workspace=workspace,client=access.client)
        return access.client,profile
    def get(self,request):
        client,profile=self._profile(request)
        prefs=profile.operational_preferences or {}
        return Response({"client_id":str(client.id),"timezone":profile.timezone,
            **{key:prefs.get(key,False if key.endswith("enabled") or key=="appointment_reminders_to_client" else None) for key in self.allowed}})
    def post(self,request):
        client,profile=self._profile(request)
        unknown=set(request.data)-self.allowed-{"client"}
        if unknown: raise ValidationError({key:"This preference cannot be changed in the client portal." for key in unknown})
        prefs=dict(profile.operational_preferences or {})
        for key,value in request.data.items():
            if key=="client": continue
            if key in ("daily_summary_enabled","weekly_report_enabled","appointment_reminders_to_client"):
                if not isinstance(value,bool): raise ValidationError({key:"Provide true or false."})
                prefs[key]=value
            elif key in ("daily_summary_time","weekly_report_time"):
                from datetime import time as time_type
                try: prefs[key]=time_type.fromisoformat(value).strftime("%H:%M")
                except (TypeError,ValueError): raise ValidationError({key:"Use 24-hour HH:MM time."})
            elif key=="weekly_report_weekday":
                try: day=int(value)
                except (TypeError,ValueError): raise ValidationError({key:"Use a weekday number from 0 (Monday) to 6 (Sunday)."})
                if day<0 or day>6: raise ValidationError({key:"Use a weekday number from 0 (Monday) to 6 (Sunday)."})
                prefs[key]=day
        profile.operational_preferences=prefs; profile.save(update_fields=["operational_preferences","updated_at"])
        _audit(request,"client_automation_preferences_updated",profile)
        return Response({"client_id":str(client.id),**{key:prefs.get(key) for key in self.allowed}})


class PortalAppointmentsView(APIView):
    def get(self,request):
        from .va import _client_portal_context
        workspace,accesses=_client_portal_context(request)
        rows=VAAppointment.objects.filter(workspace=workspace,client_id__in=accesses.values_list("client_id",flat=True),
            is_client_visible=True,status__in=[VAAppointment.Status.TENTATIVE,VAAppointment.Status.CONFIRMED]).order_by("starts_at")
        if request.query_params.get("client"):
            if not accesses.filter(client_id=request.query_params["client"]).exists(): raise PermissionDenied("No portal access to this client.")
            rows=rows.filter(client_id=request.query_params["client"])
        return Response(serialize_feature(rows[:100],many=True))


class VAActionApprovalView(APIView):
    def get(self,request):
        workspace,manager=_context(request)
        rows=VAActionApproval.objects.filter(workspace=workspace,client_id__in=_client_ids(request)).select_related("client","appointment","requested_by")
        if not manager: rows=rows.filter(Q(requested_by=request.user)|Q(status=VAActionApproval.Status.PENDING))
        if request.query_params.get("status"): rows=rows.filter(status=request.query_params["status"])
        return Response(serialize_feature(rows.order_by("-created_at")[:100],many=True))
    def post(self,request):
        workspace,_=_context(request)
        client=Client.objects.filter(workspace=workspace,id=request.data.get("client"),id__in=_client_ids(request)).first()
        if not client: raise PermissionDenied("You are not assigned to this client.")
        action_type=request.data.get("action_type","other")
        if action_type not in VAActionApproval.ActionType.values: raise ValidationError({"action_type":"Choose appointment, document, purchase, or other."})
        appointment=None
        if request.data.get("appointment"):
            appointment=VAAppointment.objects.filter(workspace=workspace,client=client,id=request.data["appointment"]).first()
            if not appointment: raise ValidationError({"appointment":"Appointment must belong to this client."})
            action_type=VAActionApproval.ActionType.APPOINTMENT
        if action_type==VAActionApproval.ActionType.APPOINTMENT and not appointment: raise ValidationError({"appointment":"An appointment approval must reference an appointment."})
        details=request.data.get("details",{})
        if not isinstance(details,dict): raise ValidationError({"details":"Details must be a JSON object."})
        from rest_framework.fields import DateTimeField
        expires_at=DateTimeField().run_validation(request.data["expires_at"]) if request.data.get("expires_at") else None
        approval=VAActionApproval.objects.create(workspace=workspace,client=client,appointment=appointment,requested_by=request.user,
            action_type=action_type,title=request.data.get("title") or (appointment.title if appointment else "Approval requested"),
            summary=request.data.get("summary",""),details=details,expires_at=expires_at)
        _audit(request,"va_action_approval_requested",approval)
        return Response(serialize_feature(approval),status=201)


class PortalActionApprovalView(APIView):
    def _access(self,request):
        from .va import _client_portal_context
        return _client_portal_context(request)
    def get(self,request):
        workspace,accesses=self._access(request)
        rows=VAActionApproval.objects.filter(workspace=workspace,client_id__in=accesses.values_list("client_id",flat=True)).select_related("appointment","requested_by")
        if request.query_params.get("status"): rows=rows.filter(status=request.query_params["status"])
        else: rows=rows.filter(status=VAActionApproval.Status.PENDING)
        return Response(serialize_feature(rows.order_by("created_at")[:100],many=True))
    def post(self,request):
        workspace,accesses=self._access(request)
        with transaction.atomic():
            approval=VAActionApproval.objects.select_for_update().filter(workspace=workspace,
                client_id__in=accesses.values_list("client_id",flat=True),id=request.data.get("approval"),status=VAActionApproval.Status.PENDING).first()
            if not approval: raise ValidationError({"approval":"This approval is unavailable or has already been decided."})
            if approval.expires_at and approval.expires_at<=timezone.now():
                approval.status=VAActionApproval.Status.EXPIRED; approval.save(update_fields=["status","updated_at"])
                return Response({"detail":"This approval has expired.","status":"expired"},status=410)
            decision=request.data.get("decision")
            if decision not in ("approved","rejected","changes_requested"): raise ValidationError({"decision":"Choose approved, rejected, or changes_requested."})
            approval.status=decision; approval.decision_comment=str(request.data.get("comment",""))[:500]
            approval.decided_by=request.user; approval.decided_at=timezone.now(); approval.save(update_fields=["status","decision_comment","decided_by","decided_at","updated_at"])
            if approval.appointment_id and decision in ("approved","rejected"):
                approval.appointment.status=VAAppointment.Status.CONFIRMED if decision=="approved" else VAAppointment.Status.CANCELLED
                approval.appointment.save(update_fields=["status","updated_at"])
            return Response(serialize_feature(approval))
