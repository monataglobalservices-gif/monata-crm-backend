from django.contrib.auth import get_user_model
from rest_framework import serializers
from .models import (Workspace, Membership, Client, Contact, Lead, Deal, Activity, Service, Project, Task, Ticket, TicketMessage, Campaign, ContentItem, Proposal, Contract, Invoice, Payment, AuditLog)

class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = get_user_model()
        fields = ("id", "email", "first_name", "last_name")
        read_only_fields = fields

class RegisterSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, min_length=10)
    first_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    last_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    workspace_name = serializers.CharField(max_length=160)
    workspace_slug = serializers.SlugField(max_length=80)
    def validate_email(self, value):
        if get_user_model().objects.filter(email__iexact=value).exists(): raise serializers.ValidationError("An account with this email already exists.")
        return value
    def validate_workspace_slug(self, value):
        if Workspace.objects.filter(slug__iexact=value).exists(): raise serializers.ValidationError("That workspace URL is already in use.")
        return value

class WorkspaceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Workspace
        fields = ("id", "name", "slug", "plan", "is_active", "created_at", "updated_at")
        read_only_fields = ("id", "plan", "is_active", "created_at", "updated_at")
    def validate_slug(self, value):
        if Workspace.objects.filter(slug__iexact=value).exists(): raise serializers.ValidationError("That workspace URL is already in use.")
        return value

class MembershipSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(source="user.email", read_only=True)
    user_email = serializers.EmailField(write_only=True)
    class Meta:
        model = Membership
        fields = ("id", "email", "user_email", "role", "is_active", "created_at")
        read_only_fields = ("id", "email", "created_at")
    def validate_user_email(self, value):
        user = get_user_model().objects.filter(email__iexact=value).first()
        if not user: raise serializers.ValidationError("That email does not have an account yet.")
        if Membership.objects.filter(workspace=self.context["request"].workspace, user=user).exists():
            raise serializers.ValidationError("That account is already a member of this workspace.")
        return value
    def validate_role(self, value):
        if value == Membership.Role.OWNER: raise serializers.ValidationError("Workspace ownership cannot be assigned here.")
        return value
    def create(self, validated_data):
        email = validated_data.pop("user")["email"]
        user = get_user_model().objects.get(email__iexact=email)
        return Membership.objects.create(workspace=self.context["request"].workspace, user=user, **validated_data)

class TenantSerializer(serializers.ModelSerializer):
    workspace = serializers.UUIDField(read_only=True)
    class Meta:
        fields = "__all__"
        read_only_fields = ("id", "workspace", "created_at", "updated_at")
    def validate(self, attrs):
        workspace = getattr(self.context.get("request"), "workspace", None)
        for value in attrs.values():
            candidates = value if isinstance(value, (list, tuple)) else [value]
            for obj in candidates:
                related_workspace = getattr(obj, "workspace_id", None)
                if related_workspace and workspace and str(related_workspace) != str(workspace.id):
                    raise serializers.ValidationError("Related records must belong to the active workspace.")
        return attrs

def serializer_for(model):
    class Meta:
        fields = "__all__"
        read_only_fields = ("id", "workspace", "created_at", "updated_at")
    return type(f"{model.__name__}Serializer", (TenantSerializer,), {"Meta": type("Meta", (), {"model": model, **Meta.__dict__})})

ClientSerializer = serializer_for(Client)
ContactSerializer = serializer_for(Contact)
LeadSerializer = serializer_for(Lead)
DealSerializer = serializer_for(Deal)
ActivitySerializer = serializer_for(Activity)
ServiceSerializer = serializer_for(Service)
ProjectSerializer = serializer_for(Project)
TaskSerializer = serializer_for(Task)
TicketSerializer = serializer_for(Ticket)
TicketMessageSerializer = serializer_for(TicketMessage)
CampaignSerializer = serializer_for(Campaign)
ContentItemSerializer = serializer_for(ContentItem)
ProposalSerializer = serializer_for(Proposal)
ContractSerializer = serializer_for(Contract)
InvoiceSerializer = serializer_for(Invoice)
PaymentSerializer = serializer_for(Payment)
AuditLogSerializer = serializer_for(AuditLog)
