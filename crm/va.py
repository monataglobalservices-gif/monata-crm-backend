from django.db.models import Q
from django.utils import timezone
from rest_framework import serializers, viewsets
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import (
    AuditLog, Client, Contact, DepartmentAccess, EmailApproval, Membership, Task,
    VAClientAssignment, VAClientInstruction, VAFollowUp, VANote,
    VAClientPortalAccess, VAClientRequest, VAActionApproval,
)


def _context(request):
    workspace_id = request.headers.get("X-Workspace-ID") or request.query_params.get("workspace")
    memberships = Membership.objects.filter(user=request.user, is_active=True, workspace__is_active=True)
    membership = memberships.filter(workspace_id=workspace_id).first() if workspace_id else None
    if not workspace_id:
        membership = memberships.filter(
            department_access__department=DepartmentAccess.Department.VIRTUAL_ASSISTANCE,
            department_access__is_active=True,
        ).order_by("created_at").first()
        if not membership:
            membership = memberships.filter(role__in=[Membership.Role.OWNER, Membership.Role.ADMIN]).order_by("created_at").first()
    if not membership:
        raise PermissionDenied("Select an active workspace where you have Virtual Assistance access.")
    access = DepartmentAccess.objects.filter(
        workspace=membership.workspace, user=request.user,
        department=DepartmentAccess.Department.VIRTUAL_ASSISTANCE, is_active=True,
    ).first()
    manager = membership.role in (Membership.Role.OWNER, Membership.Role.ADMIN) or bool(
        access and access.role == DepartmentAccess.Role.MANAGER
    )
    if not manager and not access:
        raise PermissionDenied("Virtual Assistance department access is required.")
    request.workspace = membership.workspace
    request.membership = membership
    request.va_manager = manager
    return membership.workspace, manager


def _client_ids(request):
    assignments = VAClientAssignment.objects.filter(
        workspace=request.workspace, is_active=True,
    )
    if not request.va_manager:
        assignments = assignments.filter(agent=request.user)
    return assignments.values_list("client_id", flat=True)


class VAViewSet(viewsets.ModelViewSet):
    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        _context(request)

    def perform_create(self, serializer):
        obj = serializer.save(workspace=self.request.workspace)
        AuditLog.objects.create(workspace=self.request.workspace, actor=self.request.user,
                                action="created", object_type=obj.__class__.__name__, object_id=str(obj.pk))

    def perform_update(self, serializer):
        obj = serializer.save()
        AuditLog.objects.create(workspace=self.request.workspace, actor=self.request.user,
                                action="updated", object_type=obj.__class__.__name__, object_id=str(obj.pk))

    def perform_destroy(self, instance):
        AuditLog.objects.create(workspace=self.request.workspace, actor=self.request.user,
                                action="deleted", object_type=instance.__class__.__name__, object_id=str(instance.pk))
        instance.delete()


def _audit(request, action, obj):
    AuditLog.objects.create(workspace=request.workspace, actor=request.user, action=action,
                            object_type=obj.__class__.__name__, object_id=str(obj.pk))


class WorkspaceSerializer(serializers.ModelSerializer):
    workspace = serializers.UUIDField(read_only=True)
    class Meta:
        fields = "__all__"
        read_only_fields = ("id", "workspace", "created_at", "updated_at")

    def validate(self, attrs):
        workspace = self.context["request"].workspace
        for field, value in attrs.items():
            values = value if isinstance(value, (list, tuple)) else [value]
            for item in values:
                related_workspace = getattr(item, "workspace_id", None)
                if related_workspace and related_workspace != workspace.id:
                    raise serializers.ValidationError({field: "Related records must belong to the active workspace."})
        return attrs


class DepartmentAccessSerializer(serializers.ModelSerializer):
    user_email = serializers.EmailField(source="user.email", read_only=True)
    class Meta:
        model = DepartmentAccess
        model = DepartmentAccess
        fields = ("id", "user", "user_email", "department", "role", "is_active", "created_at")
        read_only_fields = ("id", "created_at")

    def validate(self, attrs):
        request = self.context["request"]
        if attrs.get("department", DepartmentAccess.Department.VIRTUAL_ASSISTANCE) != DepartmentAccess.Department.VIRTUAL_ASSISTANCE:
            raise serializers.ValidationError({"department": "This endpoint manages Virtual Assistance access only."})
        user = attrs.get("user", getattr(self.instance, "user", None))
        if user and not Membership.objects.filter(workspace=request.workspace, user=user, is_active=True).exists():
            raise serializers.ValidationError({"user": "The user must have an active membership in this workspace."})
        return attrs


class ClientAssignmentSerializer(WorkspaceSerializer):
    class Meta(WorkspaceSerializer.Meta):
        model = VAClientAssignment

    def validate(self, attrs):
        attrs = super().validate(attrs)
        request = self.context["request"]
        client = attrs.get("client", getattr(self.instance, "client", None))
        agent = attrs.get("agent", getattr(self.instance, "agent", None))
        if client and client.workspace_id != request.workspace.id:
            raise serializers.ValidationError({"client": "Client must belong to the active workspace."})
        if agent and not DepartmentAccess.objects.filter(
            workspace=request.workspace, user=agent, department=DepartmentAccess.Department.VIRTUAL_ASSISTANCE,
            is_active=True,
        ).exists() and not Membership.objects.filter(
            workspace=request.workspace, user=agent, role__in=[Membership.Role.OWNER, Membership.Role.ADMIN], is_active=True,
        ).exists():
            raise serializers.ValidationError({"agent": "Assign only a workspace member with Virtual Assistance access."})
        return attrs


class InstructionSerializer(WorkspaceSerializer):
    class Meta(WorkspaceSerializer.Meta):
        model = VAClientInstruction


class FollowUpSerializer(WorkspaceSerializer):
    class Meta(WorkspaceSerializer.Meta):
        model = VAFollowUp

    def validate(self, attrs):
        attrs = super().validate(attrs)
        request = self.context["request"]
        client = attrs.get("client", getattr(self.instance, "client", None))
        contact = attrs.get("contact", getattr(self.instance, "contact", None))
        assignee = attrs.get("assigned_to", getattr(self.instance, "assigned_to", None))
        if client and client.id not in set(_client_ids(request)):
            raise serializers.ValidationError({"client": "You are not assigned to this client."})
        if contact and client and contact.client_id != client.id:
            raise serializers.ValidationError({"contact": "Contact must belong to the selected client."})
        if assignee and client:
            is_assigned = VAClientAssignment.objects.filter(
                workspace=request.workspace, client=client, agent=assignee, is_active=True,
            ).exists()
            is_admin = Membership.objects.filter(workspace=request.workspace, user=assignee,
                role__in=[Membership.Role.OWNER, Membership.Role.ADMIN], is_active=True).exists()
            if not is_assigned and not is_admin:
                raise serializers.ValidationError({"assigned_to": "The assignee must be assigned to this client."})
        return attrs

    def update(self, instance, validated_data):
        if validated_data.get("status") == VAFollowUp.Status.COMPLETED:
            validated_data["completed_at"] = timezone.now()
        elif validated_data.get("status") and validated_data["status"] != VAFollowUp.Status.COMPLETED:
            validated_data["completed_at"] = None
        return super().update(instance, validated_data)


class NoteSerializer(WorkspaceSerializer):
    class Meta(WorkspaceSerializer.Meta):
        model = VANote

    def validate(self, attrs):
        attrs = super().validate(attrs)
        request = self.context["request"]
        client = attrs.get("client", getattr(self.instance, "client", None))
        contact = attrs.get("contact", getattr(self.instance, "contact", None))
        if client and client.id not in set(_client_ids(request)):
            raise serializers.ValidationError({"client": "You are not assigned to this client."})
        if contact and client and contact.client_id != client.id:
            raise serializers.ValidationError({"contact": "Contact must belong to the selected client."})
        if self.instance and self.instance.visibility == VANote.Visibility.PRIVATE and self.instance.author_id != request.user.id:
            raise PermissionDenied("Private agent notes can only be edited by their author.")
        if self.instance and self.instance.author_id != request.user.id and not request.va_manager:
            raise PermissionDenied("Only the note author or a department manager can edit this note.")
        return attrs


class DepartmentAccessViewSet(VAViewSet):
    serializer_class = DepartmentAccessSerializer
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    def get_queryset(self):
        if not self.request.va_manager:
            return DepartmentAccess.objects.filter(workspace=self.request.workspace, user=self.request.user,
                department=DepartmentAccess.Department.VIRTUAL_ASSISTANCE)
        return DepartmentAccess.objects.filter(workspace=self.request.workspace,
            department=DepartmentAccess.Department.VIRTUAL_ASSISTANCE).select_related("user")
    def perform_create(self, serializer):
        if not self.request.va_manager:
            raise PermissionDenied("Only a department manager can grant access.")
        _audit(self.request, "created", serializer.save(workspace=self.request.workspace))
    def perform_update(self, serializer):
        if not self.request.va_manager:
            raise PermissionDenied("Only a department manager can change access.")
        _audit(self.request, "updated", serializer.save())
    def perform_destroy(self, instance):
        if not self.request.va_manager:
            raise PermissionDenied("Only a department manager can revoke access.")
        _audit(self.request, "deleted", instance)
        instance.delete()


class ClientAssignmentViewSet(VAViewSet):
    serializer_class = ClientAssignmentSerializer
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    def get_queryset(self):
        rows = VAClientAssignment.objects.filter(workspace=self.request.workspace).select_related("client", "agent", "assigned_by")
        if not self.request.va_manager:
            rows = rows.filter(agent=self.request.user)
        return rows
    def perform_create(self, serializer):
        if not self.request.va_manager:
            raise PermissionDenied("Only a department manager can assign clients.")
        _audit(self.request, "created", serializer.save(workspace=self.request.workspace, assigned_by=self.request.user))
    def perform_update(self, serializer):
        if not self.request.va_manager:
            raise PermissionDenied("Only a department manager can change assignments.")
        _audit(self.request, "updated", serializer.save())
    def perform_destroy(self, instance):
        if not self.request.va_manager:
            raise PermissionDenied("Only a department manager can remove assignments.")
        instance.is_active = False
        instance.save(update_fields=["is_active", "updated_at"])
        _audit(self.request, "deactivated", instance)


class InstructionViewSet(VAViewSet):
    serializer_class = InstructionSerializer
    def get_queryset(self):
        rows = VAClientInstruction.objects.filter(workspace=self.request.workspace, client_id__in=_client_ids(self.request))
        if self.request.query_params.get("client"):
            rows = rows.filter(client_id=self.request.query_params["client"])
        if self.request.query_params.get("active") == "true":
            today = timezone.localdate()
            rows = rows.filter(is_active=True).filter(
                (Q(effective_from__isnull=True) | Q(effective_from__lte=today)) &
                (Q(effective_until__isnull=True) | Q(effective_until__gte=today))
            )
        return rows.select_related("client", "authored_by")
    def perform_create(self, serializer):
        if not self.request.va_manager:
            raise PermissionDenied("Only a department manager can publish client instructions.")
        _audit(self.request, "created", serializer.save(workspace=self.request.workspace, authored_by=self.request.user))
    def perform_update(self, serializer):
        if not self.request.va_manager:
            raise PermissionDenied("Only a department manager can change client instructions.")
        _audit(self.request, "updated", serializer.save())
    def perform_destroy(self, instance):
        if not self.request.va_manager:
            raise PermissionDenied("Only a department manager can remove client instructions.")
        super().perform_destroy(instance)


class FollowUpViewSet(VAViewSet):
    serializer_class = FollowUpSerializer
    def get_queryset(self):
        rows = VAFollowUp.objects.filter(workspace=self.request.workspace, client_id__in=_client_ids(self.request))
        if not self.request.va_manager:
            rows = rows.filter(assigned_to=self.request.user)
        if self.request.query_params.get("status"):
            rows = rows.filter(status=self.request.query_params["status"])
        return rows.select_related("client", "contact", "assigned_to", "created_by")
    def perform_create(self, serializer):
        serializer.save(workspace=self.request.workspace, created_by=self.request.user,
                        assigned_to=serializer.validated_data.get("assigned_to") or self.request.user)


class NoteViewSet(VAViewSet):
    serializer_class = NoteSerializer
    def get_queryset(self):
        rows = VANote.objects.filter(workspace=self.request.workspace, client_id__in=_client_ids(self.request))
        rows = rows.filter(~Q(visibility=VANote.Visibility.PRIVATE) | Q(author=self.request.user))
        if self.request.query_params.get("client"):
            rows = rows.filter(client_id=self.request.query_params["client"])
        return rows.select_related("client", "contact", "author").distinct()
    def perform_create(self, serializer):
        serializer.save(workspace=self.request.workspace, author=self.request.user)


class DailyDeskView(APIView):
    def get(self, request):
        workspace, manager = _context(request)
        client_ids = _client_ids(request)
        today = timezone.localdate()
        tasks = Task.objects.filter(workspace=workspace, project__client_id__in=client_ids,
            project__service__category="virtual_assistance").exclude(status=Task.Status.DONE)
        if not manager:
            tasks = tasks.filter(assignee=request.user)
        followups = VAFollowUp.objects.filter(workspace=workspace, client_id__in=client_ids,
            status__in=[VAFollowUp.Status.OPEN, VAFollowUp.Status.WAITING])
        approvals = EmailApproval.objects.filter(workspace=workspace, status=EmailApproval.Status.PENDING,
            scheduled_email__client_id__in=client_ids)
        va_approvals = VAActionApproval.objects.filter(workspace=workspace,client_id__in=client_ids,status=VAActionApproval.Status.PENDING)
        if not manager:
            followups = followups.filter(assigned_to=request.user)
            approvals = approvals.filter(scheduled_email__assigned_agent=request.user,
                scheduled_email__connection__agent_access__user=request.user,
                scheduled_email__connection__agent_access__is_active=True,
                scheduled_email__connection__agent_access__can_read=True).distinct()
            va_approvals = va_approvals.filter(requested_by=request.user)
        overdue_tasks = tasks.filter(due_date__lt=today)
        due_today_tasks = tasks.filter(due_date=today)
        urgent_tasks = tasks.filter(priority="urgent")
        overdue_followups = followups.filter(due_at__date__lt=today)
        due_today_followups = followups.filter(due_at__date=today)
        task_rows = list(tasks.filter(Q(due_date__lte=today) | Q(priority="urgent")).select_related(
            "project", "project__client", "assignee").order_by("due_date")[:25])
        followup_rows = list(followups.filter(due_at__date__lte=today).select_related("client", "contact", "assigned_to").order_by("due_at")[:25])
        actions = [{"type": "task", "id": str(x.id), "title": x.title, "client": x.project.client.name,
                    "priority": x.priority, "due_at": x.due_date.isoformat() if x.due_date else None, "status": x.status} for x in task_rows]
        actions += [{"type": "follow_up", "id": str(x.id), "title": x.title, "client": x.client.name,
                     "priority": "normal", "due_at": x.due_at.isoformat() if x.due_at else None, "status": x.status} for x in followup_rows]
        approval_rows = approvals.select_related("scheduled_email", "scheduled_email__client").order_by("created_at")[:25]
        actions += [{"type": "email_approval", "id": str(x.id), "title": "Email approval needed",
                     "client": x.scheduled_email.client.name, "priority": "high",
                     "due_at": x.expires_at.isoformat(), "status": x.status} for x in approval_rows]
        va_approval_rows=va_approvals.select_related("client").order_by("created_at")[:25]
        actions += [{"type":"va_approval","id":str(x.id),"title":x.title,"client":x.client.name,
                     "priority":"high","due_at":x.expires_at.isoformat() if x.expires_at else None,"status":x.status} for x in va_approval_rows]
        actions.sort(key=lambda x: (x["due_at"] is None, x["due_at"] or ""))
        return Response({"date": today, "counts": {
            "urgent_tasks": urgent_tasks.count(), "due_today_tasks": due_today_tasks.count(),
            "overdue_tasks": overdue_tasks.count(), "open_follow_ups": followups.count(),
            "due_today_follow_ups": due_today_followups.count(), "overdue_follow_ups": overdue_followups.count(),
            "waiting_for_client": followups.filter(status=VAFollowUp.Status.WAITING).count() + tasks.filter(status=Task.Status.WAITING).count(),
            "pending_email_approvals": approvals.count(), "pending_va_approvals":va_approvals.count(),
        }, "next_actions": actions[:25]})


class ClientPortalAccessSerializer(serializers.ModelSerializer):
    user_email = serializers.EmailField(source="user.email", read_only=True)
    client_name = serializers.CharField(source="client.name", read_only=True)
    class Meta:
        model = VAClientPortalAccess
        fields = ("id", "client", "client_name", "user", "user_email", "is_active", "created_at")
        read_only_fields = ("id", "created_at")

    def validate(self, attrs):
        request = self.context["request"]
        client = attrs.get("client", getattr(self.instance, "client", None))
        user = attrs.get("user", getattr(self.instance, "user", None))
        if client and client.id not in set(_client_ids(request)):
            raise serializers.ValidationError({"client": "Assign this client to Virtual Assistance before granting portal access."})
        if user and not Membership.objects.filter(workspace=request.workspace, user=user,
            role=Membership.Role.CLIENT, is_active=True).exists():
            raise serializers.ValidationError({"user": "The portal user must have an active client membership in this workspace."})
        return attrs


class ClientPortalAccessViewSet(VAViewSet):
    serializer_class = ClientPortalAccessSerializer
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    def get_queryset(self):
        if not self.request.va_manager:
            return VAClientPortalAccess.objects.none()
        return VAClientPortalAccess.objects.filter(workspace=self.request.workspace).select_related("client", "user")
    def perform_create(self, serializer):
        if not self.request.va_manager:
            raise PermissionDenied("Only a VA manager can grant client portal access.")
        _audit(self.request, "created", serializer.save(workspace=self.request.workspace, granted_by=self.request.user))
    def perform_update(self, serializer):
        if not self.request.va_manager:
            raise PermissionDenied("Only a VA manager can change client portal access.")
        _audit(self.request, "updated", serializer.save())
    def perform_destroy(self, instance):
        if not self.request.va_manager:
            raise PermissionDenied("Only a VA manager can revoke client portal access.")
        instance.is_active = False
        instance.save(update_fields=["is_active", "updated_at"])
        _audit(self.request, "portal_access_revoked", instance)


def _client_portal_context(request):
    workspace_id = request.headers.get("X-Workspace-ID") or request.query_params.get("workspace")
    memberships = Membership.objects.filter(user=request.user, is_active=True, workspace__is_active=True,
        role=Membership.Role.CLIENT)
    membership = memberships.filter(workspace_id=workspace_id).first() if workspace_id else memberships.filter(
        workspace__va_portal_access__user=request.user, workspace__va_portal_access__is_active=True
    ).order_by("created_at").first()
    if not membership:
        raise PermissionDenied("An active client portal membership is required.")
    accesses = VAClientPortalAccess.objects.filter(workspace=membership.workspace, user=request.user, is_active=True,
        client__va_assignments__is_active=True).select_related("client").distinct()
    if not accesses.exists():
        raise PermissionDenied("No active Virtual Assistance portal access was found.")
    request.workspace = membership.workspace
    request.portal_accesses = accesses
    return membership.workspace, accesses


class ClientPortalRequestSerializer(serializers.ModelSerializer):
    class Meta:
        model = VAClientRequest
        fields = ("id", "client", "category", "title", "description", "priority", "requested_due_at", "status", "created_at", "updated_at")
        read_only_fields = ("id", "status", "created_at", "updated_at")

    def validate_client(self, client):
        request = self.context["request"]
        if not request.portal_accesses.filter(client=client).exists():
            raise serializers.ValidationError("You do not have portal access to this client.")
        if not VAClientAssignment.objects.filter(workspace=request.workspace, client=client, is_active=True).exists():
            raise serializers.ValidationError("This client does not currently have an active VA assignment.")
        return client


class ClientPortalRequestView(APIView):
    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        _client_portal_context(request)

    def get(self, request):
        rows = VAClientRequest.objects.filter(workspace=request.workspace,
            client_id__in=request.portal_accesses.values_list("client_id", flat=True), requested_by=request.user)
        client_id = request.query_params.get("client")
        if client_id:
            rows = rows.filter(client_id=client_id)
        rows = rows.select_related("client", "assigned_to").order_by("-created_at")[:100]
        return Response(ClientPortalRequestSerializer(rows, many=True, context={"request": request}).data)

    def post(self, request):
        serializer = ClientPortalRequestSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        item = serializer.save(workspace=request.workspace, requested_by=request.user)
        _audit(request, "client_request_submitted", item)
        return Response(ClientPortalRequestSerializer(item, context={"request": request}).data, status=201)


class ClientRequestStaffSerializer(serializers.ModelSerializer):
    client_name = serializers.CharField(source="client.name", read_only=True)
    requester_email = serializers.EmailField(source="requested_by.email", read_only=True)
    assigned_to_email = serializers.EmailField(source="assigned_to.email", read_only=True)
    class Meta:
        model = VAClientRequest
        fields = ("id", "client", "client_name", "requested_by", "requester_email", "assigned_to", "assigned_to_email",
            "category", "title", "description", "priority", "requested_due_at", "status", "staff_note", "first_response_at", "completed_at", "created_at", "updated_at")
        read_only_fields = ("id", "client", "requested_by", "category", "title", "description", "priority", "requested_due_at", "first_response_at", "completed_at", "created_at", "updated_at")

    def validate(self, attrs):
        request = self.context["request"]
        assignee = attrs.get("assigned_to", self.instance.assigned_to if self.instance else None)
        if not request.va_manager and assignee not in (None, request.user):
            raise PermissionDenied("Agents can claim unassigned requests but cannot reassign another agent's work.")
        if assignee and self.instance:
            assigned = VAClientAssignment.objects.filter(workspace=request.workspace, client=self.instance.client,
                agent=assignee, is_active=True).exists()
            is_admin = Membership.objects.filter(workspace=request.workspace, user=assignee,
                role__in=[Membership.Role.OWNER, Membership.Role.ADMIN], is_active=True).exists()
            if not assigned and not is_admin:
                raise serializers.ValidationError({"assigned_to": "The agent must be assigned to this client."})
        return attrs

    def update(self, instance, validated_data):
        if instance.status == VAClientRequest.Status.SUBMITTED and validated_data.get("status") in (VAClientRequest.Status.IN_PROGRESS, VAClientRequest.Status.WAITING, VAClientRequest.Status.COMPLETED):
            validated_data["first_response_at"] = timezone.now()
        if validated_data.get("status") == VAClientRequest.Status.COMPLETED:
            validated_data["completed_at"] = timezone.now()
        elif validated_data.get("status") and validated_data["status"] != VAClientRequest.Status.COMPLETED:
            validated_data["completed_at"] = None
        return super().update(instance, validated_data)


class ClientRequestViewSet(VAViewSet):
    serializer_class = ClientRequestStaffSerializer
    http_method_names = ["get", "patch", "head", "options"]
    def get_queryset(self):
        rows = VAClientRequest.objects.filter(workspace=self.request.workspace, client_id__in=_client_ids(self.request))
        if not self.request.va_manager:
            rows = rows.filter(Q(assigned_to=self.request.user) | Q(assigned_to__isnull=True))
        if self.request.query_params.get("status"):
            rows = rows.filter(status=self.request.query_params["status"])
        return rows.select_related("client", "requested_by", "assigned_to").order_by("-priority", "created_at")
    def perform_update(self, serializer):
        if not self.request.va_manager and serializer.validated_data.get("assigned_to") is None:
            serializer.validated_data["assigned_to"] = self.request.user
        _audit(self.request, "client_request_updated", serializer.save())


class WorkInboxView(APIView):
    def get(self, request):
        workspace, manager = _context(request)
        client_ids = _client_ids(request)
        requests = VAClientRequest.objects.filter(workspace=workspace, client_id__in=client_ids,
            status__in=[VAClientRequest.Status.SUBMITTED, VAClientRequest.Status.IN_PROGRESS, VAClientRequest.Status.WAITING])
        tasks = Task.objects.filter(workspace=workspace, project__client_id__in=client_ids,
            project__service__category="virtual_assistance").exclude(status=Task.Status.DONE)
        followups = VAFollowUp.objects.filter(workspace=workspace, client_id__in=client_ids,
            status__in=[VAFollowUp.Status.OPEN, VAFollowUp.Status.WAITING])
        approvals = EmailApproval.objects.filter(workspace=workspace, status=EmailApproval.Status.PENDING,
            scheduled_email__client_id__in=client_ids)
        va_approvals = VAActionApproval.objects.filter(workspace=workspace,client_id__in=client_ids,status=VAActionApproval.Status.PENDING)
        if not manager:
            requests = requests.filter(Q(assigned_to=request.user) | Q(assigned_to__isnull=True))
            tasks = tasks.filter(Q(assignee=request.user) | Q(assignee__isnull=True))
            followups = followups.filter(Q(assigned_to=request.user) | Q(assigned_to__isnull=True))
            approvals = approvals.filter(scheduled_email__assigned_agent=request.user,
                scheduled_email__connection__agent_access__user=request.user,
                scheduled_email__connection__agent_access__is_active=True,
                scheduled_email__connection__agent_access__can_read=True).distinct()
            va_approvals = va_approvals.filter(requested_by=request.user)
        actions = []
        for item in requests.select_related("client").order_by("-priority", "created_at")[:100]:
            actions.append({"type":"client_request", "id":str(item.id), "client_id":str(item.client_id),
                "client":item.client.name, "title":item.title, "priority":item.priority,
                "status":item.status, "created_at":item.created_at.isoformat(), "due_at":item.requested_due_at.isoformat() if item.requested_due_at else None})
        for item in tasks.select_related("project__client").order_by("due_date")[:100]:
            actions.append({"type":"task", "id":str(item.id), "client_id":str(item.project.client_id),
                "client":item.project.client.name, "title":item.title, "priority":item.priority,
                "status":item.status, "created_at":item.created_at.isoformat(), "due_at":item.due_date.isoformat() if item.due_date else None})
        for item in followups.select_related("client").order_by("due_at")[:100]:
            actions.append({"type":"follow_up", "id":str(item.id), "client_id":str(item.client_id),
                "client":item.client.name, "title":item.title, "priority":"normal",
                "status":item.status, "created_at":item.created_at.isoformat(), "due_at":item.due_at.isoformat()})
        for item in approvals.select_related("scheduled_email__client").order_by("created_at")[:100]:
            job=item.scheduled_email
            actions.append({"type":"email_approval", "id":str(item.id), "client_id":str(job.client_id),
                "client":job.client.name, "title":"Email approval needed", "priority":"high",
                "status":item.status, "created_at":item.created_at.isoformat(), "due_at":item.expires_at.isoformat()})
        for item in va_approvals.select_related("client").order_by("created_at")[:100]:
            actions.append({"type":"va_approval","id":str(item.id),"client_id":str(item.client_id),"client":item.client.name,
                "title":item.title,"priority":"high","status":item.status,"created_at":item.created_at.isoformat(),
                "due_at":item.expires_at.isoformat() if item.expires_at else None})
        actions.sort(key=lambda item: (item["priority"] not in ("urgent", "high", "important"), item["due_at"] is None, item["due_at"] or item["created_at"]))
        return Response({"counts":{"client_requests":requests.count(), "tasks":tasks.count(),
            "follow_ups":followups.count(), "email_approvals":approvals.count(), "va_approvals":va_approvals.count(), "total":len(actions)},
            "items":actions[:100]})
