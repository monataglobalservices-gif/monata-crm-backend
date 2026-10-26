from django.contrib.auth import get_user_model
from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import generics, mixins, serializers, status, viewsets
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from .models import Workspace, Membership, Client, Contact, Lead, Deal, Activity, Service, Project, Task, Ticket, TicketMessage, Campaign, ContentItem, Proposal, Contract, Invoice, Payment, AuditLog
from .serializers import (RegisterSerializer, WorkspaceSerializer, MembershipSerializer, UserSerializer, ClientSerializer, ContactSerializer, LeadSerializer, DealSerializer, ActivitySerializer, ServiceSerializer, ProjectSerializer, TaskSerializer, TicketSerializer, TicketMessageSerializer, CampaignSerializer, ContentItemSerializer, ProposalSerializer, ContractSerializer, InvoiceSerializer, PaymentSerializer, AuditLogSerializer)

class RegisterView(generics.GenericAPIView):
    permission_classes = [AllowAny]
    serializer_class = RegisterSerializer
    @transaction.atomic
    def post(self, request):
        data = self.get_serializer(data=request.data); data.is_valid(raise_exception=True)
        values = data.validated_data
        user = get_user_model().objects.create_user(email=values["email"], username=values["email"], password=values["password"], first_name=values.get("first_name", ""), last_name=values.get("last_name", ""))
        workspace = Workspace.objects.create(name=values["workspace_name"], slug=values["workspace_slug"], owner=user)
        Membership.objects.create(workspace=workspace, user=user, role=Membership.Role.OWNER)
        from rest_framework_simplejwt.tokens import RefreshToken
        token = RefreshToken.for_user(user)
        return Response({"user":UserSerializer(user).data, "workspace":WorkspaceSerializer(workspace).data, "access":str(token.access_token), "refresh":str(token)}, status=status.HTTP_201_CREATED)

class MeView(APIView):
    def get(self, request): return Response({"user":UserSerializer(request.user).data, "workspaces":WorkspaceSerializer(Workspace.objects.filter(memberships__user=request.user, memberships__is_active=True), many=True).data})

class WorkspaceViewSet(mixins.ListModelMixin, mixins.CreateModelMixin, viewsets.GenericViewSet):
    serializer_class = WorkspaceSerializer
    def get_queryset(self): return Workspace.objects.filter(memberships__user=self.request.user, memberships__is_active=True).distinct()
    def perform_create(self, serializer):
        workspace = serializer.save(owner=self.request.user)
        Membership.objects.create(workspace=workspace, user=self.request.user, role=Membership.Role.OWNER)

class MembershipViewSet(mixins.ListModelMixin, mixins.CreateModelMixin, mixins.DestroyModelMixin, viewsets.GenericViewSet):
    serializer_class = MembershipSerializer
    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        member = Membership.objects.filter(user=request.user, is_active=True).select_related("workspace").filter(workspace_id=request.headers.get("X-Workspace-ID") or request.query_params.get("workspace")).first() if (request.headers.get("X-Workspace-ID") or request.query_params.get("workspace")) else Membership.objects.filter(user=request.user, is_active=True, role__in=[Membership.Role.OWNER, Membership.Role.ADMIN]).select_related("workspace").first()
        if not member: raise PermissionDenied("An owner or administrator membership is required.")
        if member.role not in (Membership.Role.OWNER, Membership.Role.ADMIN): raise PermissionDenied("Only workspace owners and administrators can manage members.")
        request.workspace = member.workspace
    def get_queryset(self): return Membership.objects.filter(workspace=self.request.workspace).exclude(role=Membership.Role.OWNER).select_related("user")
    def get_serializer_context(self): return {**super().get_serializer_context(), "request":self.request}
    def perform_destroy(self, instance):
        if instance.user_id == self.request.user.id: raise ValidationError("You cannot remove your own membership.")
        instance.delete()

ROLE_ACCESS = {
    "lead": {"owner","admin","sales"}, "deal": {"owner","admin","sales"},
    "client": {"owner","admin","sales","project_manager","service_agent","marketing","finance"},
    "contact": {"owner","admin","sales"}, "activity": {"owner","admin","sales","project_manager","service_agent"},
    "service": {"owner","admin"}, "project": {"owner","admin","project_manager","service_agent"},
    "task": {"owner","admin","project_manager","service_agent"}, "ticket": {"owner","admin","service_agent"},
    "ticket-message": {"owner","admin","service_agent"}, "campaign": {"owner","admin","marketing"},
    "content-item": {"owner","admin","marketing"}, "proposal": {"owner","admin","sales","finance"},
    "contract": {"owner","admin","sales","finance"}, "invoice": {"owner","admin","finance"},
    "payment": {"owner","admin","finance"}, "audit-log": {"owner","admin"},
}

class TenantViewSet(viewsets.ModelViewSet):
    filterset_fields = "__all__"
    def get_queryset(self):
        if not hasattr(self.request, "workspace"): return self.model.objects.none()
        return self.model.objects.filter(workspace=self.request.workspace).select_related("workspace")
    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        memberships = Membership.objects.filter(user=request.user, is_active=True).select_related("workspace")
        workspace_id = request.headers.get("X-Workspace-ID") or request.query_params.get("workspace")
        if workspace_id:
            membership = memberships.filter(workspace_id=workspace_id).first()
        else:
            membership = memberships.order_by("created_at").first()
        if not membership: raise PermissionDenied("You do not belong to an active workspace.")
        request.workspace = membership.workspace
        request.membership = membership
        allowed = ROLE_ACCESS.get(self.basename, {"owner", "admin"})
        if membership.role not in allowed:
            raise PermissionDenied("Your workspace role cannot access this module.")
        if self.basename == "audit-logs" and request.method not in ("GET", "HEAD", "OPTIONS"):
            raise PermissionDenied("Audit logs are read only.")
    def get_serializer_context(self): return {**super().get_serializer_context(), "request":self.request}
    def perform_create(self, serializer):
        obj = serializer.save(workspace=self.request.workspace)
        AuditLog.objects.create(workspace=self.request.workspace, actor=self.request.user, action="created", object_type=obj.__class__.__name__, object_id=str(obj.pk))
    def perform_update(self, serializer):
        obj = serializer.save()
        AuditLog.objects.create(workspace=self.request.workspace, actor=self.request.user, action="updated", object_type=obj.__class__.__name__, object_id=str(obj.pk))
    def perform_destroy(self, instance):
        AuditLog.objects.create(workspace=self.request.workspace, actor=self.request.user, action="deleted", object_type=instance.__class__.__name__, object_id=str(instance.pk))
        instance.delete()

def tenant_view(model, serializer, basename, search_fields=(), ordering_fields=()):
    return type(f"{model.__name__}ViewSet", (TenantViewSet,), {"model":model,"serializer_class":serializer,"basename":basename,"search_fields":search_fields,"ordering_fields":ordering_fields})

ClientViewSet=tenant_view(Client,ClientSerializer,"clients",("name","company","email"),("created_at","name"))
ContactViewSet=tenant_view(Contact,ContactSerializer,"contacts",("first_name","last_name","email"),("created_at","last_name"))
LeadViewSet=tenant_view(Lead,LeadSerializer,"sales",("name","company","email"),("created_at","name","estimated_value"))
DealViewSet=tenant_view(Deal,DealSerializer,"sales",("name",),("created_at","stage","value","expected_close"))
ActivityViewSet=tenant_view(Activity,ActivitySerializer,"activities",("subject","body"),("created_at","due_at"))
ServiceViewSet=tenant_view(Service,ServiceSerializer,"services",("name","category"),("name","created_at"))
ProjectViewSet=tenant_view(Project,ProjectSerializer,"projects",("name","description"),("created_at","due_date","status"))
TaskViewSet=tenant_view(Task,TaskSerializer,"tasks",("title","description"),("created_at","due_date","priority"))
TicketViewSet=tenant_view(Ticket,TicketSerializer,"tickets",("subject","description"),("created_at","priority","due_at"))
TicketMessageViewSet=tenant_view(TicketMessage,TicketMessageSerializer,"ticket-messages",("message",),("created_at",))
CampaignViewSet=tenant_view(Campaign,CampaignSerializer,"campaigns",("name","channel"),("created_at","starts_on"))
ContentItemViewSet=tenant_view(ContentItem,ContentItemSerializer,"content-items",("title","copy","channel"),("created_at","scheduled_at"))
ProposalViewSet=tenant_view(Proposal,ProposalSerializer,"proposals",("title","summary"),("created_at","valid_until","amount"))
ContractViewSet=tenant_view(Contract,ContractSerializer,"contracts",("title","notes"),("created_at","renewal_date","ends_on"))
InvoiceViewSet=tenant_view(Invoice,InvoiceSerializer,"invoices",("number","notes"),("created_at","due_at","total","status"))
PaymentViewSet=tenant_view(Payment,PaymentSerializer,"payments",("reference","method"),("created_at","paid_at","amount"))
AuditLogViewSet=tenant_view(AuditLog,AuditLogSerializer,"audit-logs",("action","object_type"),("created_at",))
