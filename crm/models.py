import uuid
from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone
from .mail.fields import EncryptedTextField

class User(AbstractUser):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True)
    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["username"]

class Timestamped(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    class Meta:
        abstract = True

class Workspace(Timestamped):
    name = models.CharField(max_length=160)
    slug = models.SlugField(max_length=80, unique=True)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="owned_workspaces")
    plan = models.CharField(max_length=30, default="starter")
    is_active = models.BooleanField(default=True)
    def __str__(self): return self.name

class Membership(Timestamped):
    class Role(models.TextChoices):
        OWNER="owner", "Owner"; ADMIN="admin", "Admin"; SALES="sales", "Sales agent"; PROJECT="project_manager", "Project manager"
        SERVICE="service_agent", "Service agent"; MARKETING="marketing", "Marketing specialist"; FINANCE="finance", "Finance officer"; CLIENT="client", "Client user"
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=24, choices=Role.choices, default=Role.ADMIN)
    is_active = models.BooleanField(default=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["workspace", "user"], name="unique_workspace_member")]

class TenantModel(Timestamped):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE)
    class Meta: abstract = True

class Client(TenantModel):
    name = models.CharField(max_length=180)
    company = models.CharField(max_length=180, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=40, blank=True)
    website = models.URLField(blank=True)
    notes = models.TextField(blank=True)
    status = models.CharField(max_length=20, default="active")
    def __str__(self): return self.name

class Contact(TenantModel):
    client = models.ForeignKey(Client, null=True, blank=True, on_delete=models.SET_NULL, related_name="contacts")
    first_name = models.CharField(max_length=80)
    last_name = models.CharField(max_length=80, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=40, blank=True)
    title = models.CharField(max_length=100, blank=True)
    notes = models.TextField(blank=True)

class Lead(TenantModel):
    class Stage(models.TextChoices): NEW="new", "New"; CONTACTED="contacted", "Contacted"; QUALIFIED="qualified", "Qualified"; PROPOSAL="proposal", "Proposal"; WON="won", "Won"; LOST="lost", "Lost"
    name = models.CharField(max_length=160)
    company = models.CharField(max_length=180, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=40, blank=True)
    source = models.CharField(max_length=100, blank=True)
    stage = models.CharField(max_length=20, choices=Stage.choices, default=Stage.NEW)
    estimated_value = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    notes = models.TextField(blank=True)

class Service(TenantModel):
    name = models.CharField(max_length=140)
    category = models.CharField(max_length=32, choices=[
        ("virtual_assistance", "Virtual assistance"),
        ("digital_development", "Digital development"),
        ("social_media", "Social media"),
        ("digital_marketing", "Digital marketing"),
        ("customer_support", "Customer support"),
    ], default="virtual_assistance")
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

class Deal(TenantModel):
    class Stage(models.TextChoices): NEW="new", "New"; CONTACTED="contacted", "Contacted"; MEETING="meeting", "Meeting"; PROPOSAL="proposal", "Proposal"; WON="won", "Won"; LOST="lost", "Lost"
    name = models.CharField(max_length=180)
    lead = models.ForeignKey(Lead, null=True, blank=True, on_delete=models.SET_NULL, related_name="deals")
    client = models.ForeignKey(Client, null=True, blank=True, on_delete=models.SET_NULL, related_name="deals")
    service = models.ForeignKey(Service, null=True, blank=True, on_delete=models.SET_NULL)
    stage = models.CharField(max_length=20, choices=Stage.choices, default=Stage.NEW)
    value = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    expected_close = models.DateField(null=True, blank=True)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)

class Activity(TenantModel):
    class Kind(models.TextChoices): NOTE="note", "Note"; CALL="call", "Call"; EMAIL="email", "Email"; MEETING="meeting", "Meeting"; TASK="task", "Task"
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.NOTE)
    subject = models.CharField(max_length=180)
    body = models.TextField(blank=True)
    due_at = models.DateTimeField(null=True, blank=True)
    client = models.ForeignKey(Client, null=True, blank=True, on_delete=models.CASCADE)
    lead = models.ForeignKey(Lead, null=True, blank=True, on_delete=models.CASCADE)
    deal = models.ForeignKey(Deal, null=True, blank=True, on_delete=models.CASCADE)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)

class Project(TenantModel):
    class Status(models.TextChoices): INTAKE="intake", "Intake"; ACTIVE="active", "Active"; REVIEW="review", "Review"; COMPLETED="completed", "Completed"; ON_HOLD="on_hold", "On hold"
    name = models.CharField(max_length=180)
    client = models.ForeignKey(Client, on_delete=models.PROTECT, related_name="projects")
    service = models.ForeignKey(Service, null=True, blank=True, on_delete=models.SET_NULL)
    description = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.INTAKE)
    start_date = models.DateField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    manager = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)

class Task(TenantModel):
    class Status(models.TextChoices): TODO="todo", "To do"; IN_PROGRESS="in_progress", "In progress"; REVIEW="review", "Review"; WAITING="waiting_for_client", "Waiting for client"; DONE="done", "Done"
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="tasks")
    title = models.CharField(max_length=180)
    description = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.TODO)
    priority = models.CharField(max_length=12, choices=[("low","Low"),("normal","Normal"),("high","High"),("urgent","Urgent")], default="normal")
    due_date = models.DateField(null=True, blank=True)
    assignee = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    source_request = models.ForeignKey("VAClientRequest", null=True, blank=True, on_delete=models.SET_NULL, related_name="generated_tasks")
    source_email_thread_id = models.CharField(max_length=255, blank=True)
    source_recurring_task = models.ForeignKey("VARecurringTask", null=True, blank=True, on_delete=models.SET_NULL, related_name="generated_tasks")
    is_client_visible = models.BooleanField(default=False)

class DepartmentAccess(Timestamped):
    class Department(models.TextChoices):
        VIRTUAL_ASSISTANCE = "virtual_assistance", "Virtual assistance"
        DIGITAL_DEVELOPMENT = "digital_development", "Digital development"
        SOCIAL_MEDIA = "social_media", "Social media"
        DIGITAL_MARKETING = "digital_marketing", "Digital marketing"
        CUSTOMER_SUPPORT = "customer_support", "Customer support"
    class Role(models.TextChoices):
        MANAGER = "manager", "Department manager"
        AGENT = "agent", "Department agent"
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="department_access")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="department_access")
    department = models.CharField(max_length=32, choices=Department.choices)
    role = models.CharField(max_length=12, choices=Role.choices, default=Role.AGENT)
    is_active = models.BooleanField(default=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["workspace", "user", "department"], name="unique_department_user_access")]

class VAClientAssignment(Timestamped):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="va_assignments")
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="va_assignments")
    agent = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="va_client_assignments")
    assigned_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="va_assignments_created")
    is_active = models.BooleanField(default=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["workspace", "client", "agent"], name="unique_va_client_agent_assignment")]

class VAClientInstruction(Timestamped):
    class Topic(models.TextChoices):
        EMAIL = "email", "Email"
        APPOINTMENTS = "appointments", "Appointments"
        EXPENSES = "expenses", "Expenses"
        CUSTOMERS = "customers", "Customers"
        ESCALATION = "escalation", "Escalation"
        OTHER = "other", "Other"
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="va_instructions")
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="va_instructions")
    topic = models.CharField(max_length=20, choices=Topic.choices, default=Topic.OTHER)
    title = models.CharField(max_length=160)
    instruction = models.TextField()
    is_active = models.BooleanField(default=True)
    is_pinned = models.BooleanField(default=False)
    effective_from = models.DateField(null=True, blank=True)
    effective_until = models.DateField(null=True, blank=True)
    authored_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="va_instructions_authored")

class VAFollowUp(Timestamped):
    class Category(models.TextChoices):
        CLIENT = "client", "Client"
        CUSTOMER = "customer", "Customer"
        SUPPLIER = "supplier", "Supplier"
        LEAD = "lead", "Lead"
        PAYMENT = "payment", "Payment"
        DOCUMENT = "document", "Document"
        APPOINTMENT = "appointment", "Appointment"
        INTERNAL = "internal", "Internal"
    class Status(models.TextChoices):
        OPEN = "open", "Open"
        WAITING = "waiting_for_client", "Waiting for client"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="va_follow_ups")
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="va_follow_ups")
    source_task = models.ForeignKey("Task", null=True, blank=True, on_delete=models.CASCADE, related_name="va_follow_ups")
    source_request = models.ForeignKey("VAClientRequest", null=True, blank=True, on_delete=models.CASCADE, related_name="va_follow_ups")
    contact = models.ForeignKey(Contact, null=True, blank=True, on_delete=models.SET_NULL, related_name="va_follow_ups")
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="va_follow_ups")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="va_follow_ups_created")
    category = models.CharField(max_length=16, choices=Category.choices, default=Category.CLIENT)
    title = models.CharField(max_length=180)
    reason = models.TextField(blank=True)
    next_action = models.TextField(blank=True)
    due_at = models.DateTimeField()
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.OPEN)
    completed_at = models.DateTimeField(null=True, blank=True)
    email_thread_id = models.CharField(max_length=255, blank=True)
    reminder_sent_at = models.DateTimeField(null=True, blank=True)

class VANote(Timestamped):
    class Visibility(models.TextChoices):
        PRIVATE = "private", "Private agent"
        INTERNAL = "internal", "Internal team"
        CLIENT = "client", "Client visible"
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="va_notes")
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="va_notes")
    contact = models.ForeignKey(Contact, null=True, blank=True, on_delete=models.SET_NULL, related_name="va_notes")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="va_notes_authored")
    title = models.CharField(max_length=180, blank=True)
    body = models.TextField()
    visibility = models.CharField(max_length=12, choices=Visibility.choices, default=Visibility.PRIVATE)
    is_pinned = models.BooleanField(default=False)

class VAClientPortalAccess(Timestamped):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="va_portal_access")
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="va_portal_access")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="va_portal_access")
    granted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="va_portal_access_granted")
    is_active = models.BooleanField(default=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["workspace", "client", "user"], name="unique_va_client_portal_user")]

class VAClientRequest(Timestamped):
    class Category(models.TextChoices):
        EMAIL = "email_management", "Email management"
        CALENDAR = "calendar", "Calendar"
        RESEARCH = "research", "Research"
        DATA_ENTRY = "data_entry", "Data entry"
        DOCUMENT = "document_preparation", "Document preparation"
        TRAVEL = "travel", "Travel"
        PERSONAL = "personal_assistance", "Personal assistance"
        OTHER = "other", "Other"
    class Priority(models.TextChoices):
        NORMAL = "normal", "Normal"
        IMPORTANT = "important", "Important"
        URGENT = "urgent", "Urgent"
    class Status(models.TextChoices):
        SUBMITTED = "submitted", "Submitted"
        IN_PROGRESS = "in_progress", "In progress"
        WAITING = "waiting_for_client", "Waiting for client"
        COMPLETED = "completed", "Completed"
        DECLINED = "declined", "Declined"
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="va_client_requests")
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="va_client_requests")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="va_client_requests")
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="va_requests_assigned")
    category = models.CharField(max_length=24, choices=Category.choices, default=Category.OTHER)
    title = models.CharField(max_length=180)
    description = models.TextField()
    priority = models.CharField(max_length=12, choices=Priority.choices, default=Priority.NORMAL)
    requested_due_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.SUBMITTED)
    first_response_at = models.DateTimeField(null=True, blank=True)
    staff_note = models.TextField(blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    class Meta:
        indexes = [models.Index(fields=["workspace", "status", "created_at"])]


class VAClientProfile(Timestamped):
    """Structured operating context for a client receiving VA services."""
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="va_client_profiles")
    client = models.OneToOneField(Client, on_delete=models.CASCADE, related_name="va_profile")
    timezone = models.CharField(max_length=64, default="Africa/Nairobi")
    language = models.CharField(max_length=64, default="English")
    communication_channel = models.CharField(max_length=40, default="email")
    tone = models.CharField(max_length=80, blank=True)
    working_hours = models.JSONField(default=dict, blank=True)
    operational_preferences = models.JSONField(default=dict, blank=True)
    retainer_minutes = models.PositiveIntegerField(null=True, blank=True)
    backup_agent = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="va_backup_clients")
    status = models.CharField(max_length=20, default="healthy")


class VAClientRule(Timestamped):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="va_client_rules")
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="va_rules")
    category = models.CharField(max_length=32, default="other")
    title = models.CharField(max_length=160)
    rule = models.TextField()
    requires_approval = models.BooleanField(default=False)
    approval_threshold = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    is_active = models.BooleanField(default=True)
    authored_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)


class VARequestTemplate(Timestamped):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="va_request_templates")
    client = models.ForeignKey(Client, null=True, blank=True, on_delete=models.CASCADE, related_name="va_request_templates")
    name = models.CharField(max_length=140)
    category = models.CharField(max_length=24, choices=VAClientRequest.Category.choices, default=VAClientRequest.Category.OTHER)
    description = models.TextField(blank=True)
    fields = models.JSONField(default=list, blank=True)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)


class VARecurringTask(Timestamped):
    class Cadence(models.TextChoices): DAILY="daily", "Daily"; WEEKLY="weekly", "Weekly"; MONTHLY="monthly", "Monthly"; QUARTERLY="quarterly", "Quarterly"
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="va_recurring_tasks")
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="va_recurring_tasks")
    title = models.CharField(max_length=180)
    description = models.TextField(blank=True)
    cadence = models.CharField(max_length=12, choices=Cadence.choices)
    next_run_at = models.DateTimeField()
    assignee = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    priority = models.CharField(max_length=12, default="normal")
    is_active = models.BooleanField(default=True)
    last_run_at = models.DateTimeField(null=True, blank=True)


class VATaskChecklist(Timestamped):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="va_checklists")
    client = models.ForeignKey(Client, null=True, blank=True, on_delete=models.CASCADE, related_name="va_checklists")
    name = models.CharField(max_length=140)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    auto_start_on_assignment = models.BooleanField(default=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)


class VATaskChecklistItem(Timestamped):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="va_checklist_items")
    checklist = models.ForeignKey(VATaskChecklist, on_delete=models.CASCADE, related_name="items")
    title = models.CharField(max_length=180)
    position = models.PositiveIntegerField(default=0)
    is_required = models.BooleanField(default=True)


class VAClientChecklistRun(Timestamped):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="va_checklist_runs")
    checklist = models.ForeignKey(VATaskChecklist, on_delete=models.PROTECT, related_name="runs")
    assignment = models.ForeignKey(VAClientAssignment, null=True, blank=True, on_delete=models.SET_NULL, related_name="checklist_runs")
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="va_checklist_runs")
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="va_checklist_runs_created")
    completed_at = models.DateTimeField(null=True, blank=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["checklist", "assignment"], name="unique_va_checklist_run_assignment")]


class VAClientChecklistCompletion(Timestamped):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="va_checklist_completions")
    run = models.ForeignKey(VAClientChecklistRun, on_delete=models.CASCADE, related_name="completions")
    item = models.ForeignKey(VATaskChecklistItem, on_delete=models.PROTECT, related_name="completions")
    completed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    completed_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=500, blank=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["run", "item"], name="unique_va_checklist_run_item_completion")]


class VAAppointment(Timestamped):
    class Status(models.TextChoices): TENTATIVE="tentative", "Tentative"; CONFIRMED="confirmed", "Confirmed"; CANCELLED="cancelled", "Cancelled"; COMPLETED="completed", "Completed"
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="va_appointments")
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="va_appointments")
    contact = models.ForeignKey(Contact, null=True, blank=True, on_delete=models.SET_NULL, related_name="va_appointments")
    task = models.ForeignKey(Task, null=True, blank=True, on_delete=models.SET_NULL, related_name="va_appointments")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    title = models.CharField(max_length=180)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    timezone = models.CharField(max_length=64, default="Africa/Nairobi")
    attendees = models.JSONField(default=list, blank=True)
    location = models.CharField(max_length=255, blank=True)
    meeting_url = models.URLField(blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.TENTATIVE)
    recurrence = models.JSONField(default=dict, blank=True)
    is_client_visible = models.BooleanField(default=False)
    reminder_sent_at = models.DateTimeField(null=True, blank=True)


class VAActionApproval(Timestamped):
    class ActionType(models.TextChoices): APPOINTMENT="appointment", "Appointment"; DOCUMENT="document", "Document"; PURCHASE="purchase", "Purchase"; OTHER="other", "Other"
    class Status(models.TextChoices): PENDING="pending", "Pending"; APPROVED="approved", "Approved"; REJECTED="rejected", "Rejected"; CHANGES="changes_requested", "Changes requested"; EXPIRED="expired", "Expired"
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="va_action_approvals")
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="va_action_approvals")
    appointment = models.ForeignKey(VAAppointment, null=True, blank=True, on_delete=models.SET_NULL, related_name="approvals")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="va_approvals_requested")
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="va_approvals_decided")
    action_type = models.CharField(max_length=16, choices=ActionType.choices, default=ActionType.OTHER)
    title = models.CharField(max_length=180)
    summary = models.TextField(blank=True)
    details = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.PENDING)
    decision_comment = models.CharField(max_length=500, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)




class VAResearchOption(Timestamped):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="va_research_options")
    request = models.ForeignKey(VAClientRequest, on_delete=models.CASCADE, related_name="research_options")
    name = models.CharField(max_length=180)
    price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True)
    location = models.CharField(max_length=180, blank=True)
    contact = models.CharField(max_length=180, blank=True)
    url = models.URLField(blank=True)
    notes = models.TextField(blank=True)
    position = models.PositiveIntegerField(default=0)
    selected_by_client = models.BooleanField(default=False)


class VAQualityReview(Timestamped):
    class Status(models.TextChoices): PENDING="pending", "Pending review"; REVIEWED="reviewed", "Reviewed"
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="va_quality_reviews")
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="va_quality_reviews")
    task = models.ForeignKey(Task, null=True, blank=True, on_delete=models.SET_NULL, related_name="va_quality_reviews")
    request = models.ForeignKey(VAClientRequest, null=True, blank=True, on_delete=models.SET_NULL, related_name="quality_reviews")
    agent = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="va_quality_reviews")
    reviewer = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="va_reviews_created")
    communication_score = models.PositiveSmallIntegerField(null=True, blank=True)
    accuracy_score = models.PositiveSmallIntegerField(null=True, blank=True)
    sop_score = models.PositiveSmallIntegerField(null=True, blank=True)
    feedback = models.TextField(blank=True)
    needs_improvement = models.BooleanField(default=False)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    automation_key = models.CharField(max_length=80, unique=True, null=True, blank=True)


class VAReportDelivery(Timestamped):
    class ReportType(models.TextChoices): DAILY="daily", "Daily summary"; WEEKLY="weekly", "Weekly report"
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="va_report_deliveries")
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="va_report_deliveries")
    report_type = models.CharField(max_length=8, choices=ReportType.choices)
    period_start = models.DateField()
    recipient = models.EmailField()
    status = models.CharField(max_length=12, default="queued")
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    sent_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=200, blank=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["workspace", "client", "report_type", "period_start"], name="unique_va_report_period_delivery")]


class VAAutomationEvent(Timestamped):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="va_automation_events")
    client = models.ForeignKey(Client, null=True, blank=True, on_delete=models.CASCADE, related_name="va_automation_events")
    event_key = models.CharField(max_length=220, unique=True)
    event_type = models.CharField(max_length=48)
    payload = models.JSONField(default=dict, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)


class VASLATarget(Timestamped):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="va_sla_targets")
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="va_sla_targets")
    category = models.CharField(max_length=32, default="normal")
    response_minutes = models.PositiveIntegerField()
    resolution_minutes = models.PositiveIntegerField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["workspace", "client", "category"], name="unique_va_client_sla_category")]


class VAAgentAvailability(Timestamped):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="va_agent_availability")
    agent = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="va_availability")
    is_available = models.BooleanField(default=True)
    unavailable_until = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=240, blank=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["workspace", "agent"], name="unique_va_agent_availability")]


class VATimeEntry(Timestamped):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="va_time_entries")
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="va_time_entries")
    task = models.ForeignKey(Task, null=True, blank=True, on_delete=models.SET_NULL, related_name="va_time_entries")
    agent = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="va_time_entries")
    started_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)
    description = models.CharField(max_length=240, blank=True)
    is_billable = models.BooleanField(default=True)
    is_client_visible = models.BooleanField(default=False)


class VAHandover(Timestamped):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="va_handovers")
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="va_handovers")
    from_agent = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="va_handovers_out")
    to_agent = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="va_handovers_in")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="va_handovers_created")
    summary = models.TextField(blank=True)
    snapshot = models.JSONField(default=dict)

class Ticket(TenantModel):
    class Status(models.TextChoices): NEW="new", "New"; OPEN="open", "Open"; PENDING="pending", "Pending"; RESOLVED="resolved", "Resolved"; CLOSED="closed", "Closed"
    class Priority(models.TextChoices): LOW="low", "Low"; NORMAL="normal", "Normal"; HIGH="high", "High"; URGENT="urgent", "Urgent"
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="tickets")
    subject = models.CharField(max_length=180)
    description = models.TextField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.NEW)
    priority = models.CharField(max_length=12, choices=Priority.choices, default=Priority.NORMAL)
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    due_at = models.DateTimeField(null=True, blank=True)

class TicketMessage(TenantModel):
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="messages")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    message = models.TextField()
    is_internal = models.BooleanField(default=False)

class Campaign(TenantModel):
    name = models.CharField(max_length=180)
    client = models.ForeignKey(Client, null=True, blank=True, on_delete=models.SET_NULL, related_name="campaigns")
    channel = models.CharField(max_length=60, blank=True)
    status = models.CharField(max_length=20, choices=[("planning","Planning"),("active","Active"),("paused","Paused"),("completed","Completed")], default="planning")
    budget = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    starts_on = models.DateField(null=True, blank=True)
    ends_on = models.DateField(null=True, blank=True)
    report = models.TextField(blank=True)

class ContentItem(TenantModel):
    class Status(models.TextChoices): DRAFT="draft", "Draft"; REVIEW="review", "Review"; APPROVED="approved", "Approved"; SCHEDULED="scheduled", "Scheduled"; PUBLISHED="published", "Published"
    campaign = models.ForeignKey(Campaign, null=True, blank=True, on_delete=models.CASCADE, related_name="content_items")
    title = models.CharField(max_length=180)
    channel = models.CharField(max_length=60, blank=True)
    copy = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    scheduled_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)

class Proposal(TenantModel):
    class Status(models.TextChoices): DRAFT="draft", "Draft"; SENT="sent", "Sent"; ACCEPTED="accepted", "Accepted"; REJECTED="rejected", "Rejected"; EXPIRED="expired", "Expired"
    client = models.ForeignKey(Client, null=True, blank=True, on_delete=models.SET_NULL, related_name="proposals")
    deal = models.ForeignKey(Deal, null=True, blank=True, on_delete=models.SET_NULL, related_name="proposals")
    title = models.CharField(max_length=180)
    summary = models.TextField(blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, default="USD")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    valid_until = models.DateField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)

class Contract(TenantModel):
    class Status(models.TextChoices): DRAFT="draft", "Draft"; ACTIVE="active", "Active"; COMPLETED="completed", "Completed"; TERMINATED="terminated", "Terminated"
    client = models.ForeignKey(Client, on_delete=models.PROTECT, related_name="contracts")
    proposal = models.ForeignKey(Proposal, null=True, blank=True, on_delete=models.SET_NULL, related_name="contracts")
    title = models.CharField(max_length=180)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    starts_on = models.DateField(null=True, blank=True)
    ends_on = models.DateField(null=True, blank=True)
    renewal_date = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)

class Invoice(TenantModel):
    class Status(models.TextChoices): DRAFT="draft", "Draft"; SENT="sent", "Sent"; PARTIAL="partial", "Partially paid"; PAID="paid", "Paid"; OVERDUE="overdue", "Overdue"; VOID="void", "Void"
    client = models.ForeignKey(Client, on_delete=models.PROTECT, related_name="invoices")
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.SET_NULL, related_name="invoices")
    number = models.CharField(max_length=50)
    currency = models.CharField(max_length=3, default="USD")
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    issued_at = models.DateField(null=True, blank=True)
    due_at = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["workspace", "number"], name="unique_invoice_number_per_workspace")]

class Payment(TenantModel):
    class Status(models.TextChoices): PENDING="pending", "Pending"; COMPLETED="completed", "Completed"; FAILED="failed", "Failed"; REFUNDED="refunded", "Refunded"
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name="payments")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3, default="USD")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    method = models.CharField(max_length=50, blank=True)
    reference = models.CharField(max_length=120, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)

class MailboxConnection(TenantModel):
    class Status(models.TextChoices): ACTIVE="active", "Active"; PAUSED="paused", "Paused"; REAUTH="reauthorization_required", "Reauthorization required"; REVOKED="revoked", "Revoked"
    client = models.ForeignKey(Client, on_delete=models.PROTECT, related_name="mailboxes")
    provider = models.CharField(max_length=20, default="gmail")
    provider_account_id = models.CharField(max_length=255)
    email_address = models.EmailField()
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.ACTIVE)
    access_mode = models.CharField(max_length=20, choices=[("read", "Read only"), ("manage", "Mailbox management")], default="read")
    granted_scopes = models.JSONField(default=list)
    connected_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="mailboxes_connected")
    connected_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    sends_paused = models.BooleanField(default=False)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["workspace", "provider", "provider_account_id"], name="unique_mailbox_account_per_workspace")]

class MailboxSecret(Timestamped):
    connection = models.OneToOneField(MailboxConnection, on_delete=models.CASCADE, related_name="secret")
    encrypted_refresh_token = models.TextField()
    key_version = models.CharField(max_length=40, default="fernet-v1")

class MailboxInvitation(Timestamped):
    class Status(models.TextChoices): PENDING="pending", "Pending"; ACCEPTED="accepted", "Accepted"; DECLINED="declined", "Declined"; CONNECTED="connected", "Connected"; REVOKED="revoked", "Revoked"; EXPIRED="expired", "Expired"
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE)
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="mailbox_invitations")
    expected_email = models.EmailField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    token_hash = models.CharField(max_length=64, unique=True)
    access_mode = models.CharField(max_length=20, choices=[("read", "Read only"), ("manage", "Mailbox management")], default="read")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    responded_at = models.DateTimeField(null=True, blank=True)
    responded_by_email = models.EmailField(blank=True)

class OAuthTransaction(Timestamped):
    invitation = models.ForeignKey(MailboxInvitation, on_delete=models.CASCADE)
    state_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)

class MailboxAgentAccess(Timestamped):
    connection = models.ForeignKey(MailboxConnection, on_delete=models.CASCADE, related_name="agent_access")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="mailbox_access")
    can_read = models.BooleanField(default=True)
    can_draft = models.BooleanField(default=False)
    can_send_approved = models.BooleanField(default=False)
    can_send_routine = models.BooleanField(default=False)
    can_manage_labels = models.BooleanField(default=False)
    can_manage_rules = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    client_approved_at = models.DateTimeField(null=True, blank=True)
    client_approved_by_email = models.EmailField(blank=True)
    approval_token_hash = models.CharField(max_length=64, blank=True)
    approval_expires_at = models.DateTimeField(null=True, blank=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["connection", "user"], name="unique_mailbox_user_access")]

class EmailAutomationRule(TenantModel):
    class Status(models.TextChoices): DRAFT="draft", "Draft"; ACTIVE="active", "Active"; PAUSED="paused", "Paused"; EXPIRED="expired", "Expired"; REVOKED="revoked", "Revoked"
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="email_rules")
    connection = models.ForeignKey(MailboxConnection, on_delete=models.CASCADE, related_name="automation_rules")
    name = models.CharField(max_length=120)
    purpose = models.CharField(max_length=80)
    trigger_type = models.CharField(max_length=24, choices=[("routine", "Scheduled routine"),("no_reply_follow_up", "Follow up if no reply")], default="routine")
    wait_business_days = models.PositiveSmallIntegerField(default=0)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    allowed_emails = models.JSONField(default=list, blank=True)
    allowed_domains = models.JSONField(default=list, blank=True)
    approved_templates = models.JSONField(default=list)
    max_sends_per_day = models.PositiveSmallIntegerField(default=10)
    max_sends_per_recipient_per_day = models.PositiveSmallIntegerField(default=2)
    timezone = models.CharField(max_length=64, default="Africa/Nairobi")
    allowed_weekdays = models.JSONField(default=list)
    allowed_start = models.TimeField(null=True, blank=True)
    allowed_end = models.TimeField(null=True, blank=True)
    excluded_dates = models.JSONField(default=list, blank=True)
    expires_at = models.DateTimeField()
    version = models.PositiveIntegerField(default=1)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="email_rules_created")
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by_email = models.EmailField(blank=True)
    activation_token_hash = models.CharField(max_length=64, blank=True)
    class Meta:
        indexes = [models.Index(fields=["workspace", "client", "status"])]

class RecurringEmailSchedule(Timestamped):
    class Status(models.TextChoices): ACTIVE="active", "Active"; PAUSED="paused", "Paused"; COMPLETED="completed", "Completed"; CANCELLED="cancelled", "Cancelled"
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE)
    client = models.ForeignKey(Client, on_delete=models.CASCADE)
    connection = models.ForeignKey(MailboxConnection, on_delete=models.CASCADE)
    rule = models.ForeignKey(EmailAutomationRule, on_delete=models.PROTECT, related_name="recurrences")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    timezone = models.CharField(max_length=64)
    local_time = models.TimeField()
    starts_on = models.DateField()
    ends_on = models.DateField(null=True, blank=True)
    weekdays = models.JSONField(default=list)
    recipient_data = models.JSONField(default=dict)
    subject = models.CharField(max_length=998)
    body_text = EncryptedTextField()
    next_run_at = models.DateTimeField()
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.ACTIVE)

class EmailApproval(Timestamped):
    class Status(models.TextChoices): PENDING="pending", "Pending"; APPROVED="approved", "Approved"; REJECTED="rejected", "Rejected"; EXPIRED="expired", "Expired"; INVALIDATED="invalidated", "Invalidated"
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE)
    scheduled_email = models.ForeignKey("ScheduledEmail", on_delete=models.CASCADE, related_name="approvals")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="email_approvals_requested")
    approved_by_email = models.EmailField(blank=True)
    content_hash = models.CharField(max_length=64)
    token_hash = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    expires_at = models.DateTimeField()
    decided_at = models.DateTimeField(null=True, blank=True)
    comment = models.CharField(max_length=500, blank=True)

class ScheduledEmail(Timestamped):
    class Status(models.TextChoices): DRAFT="draft", "Draft"; APPROVAL="awaiting_approval", "Awaiting approval"; SCHEDULED="scheduled", "Scheduled"; SENDING="sending", "Sending"; SENT="sent", "Sent"; FAILED="failed", "Failed"; NEEDS_ATTENTION="needs_attention", "Needs attention"; CANCELLED="cancelled", "Cancelled"
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE)
    client = models.ForeignKey(Client, on_delete=models.PROTECT, related_name="scheduled_emails")
    connection = models.ForeignKey(MailboxConnection, on_delete=models.PROTECT, related_name="scheduled_emails")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="scheduled_emails_created")
    assigned_agent = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="scheduled_emails_assigned")
    rule = models.ForeignKey(EmailAutomationRule, null=True, blank=True, on_delete=models.SET_NULL, related_name="scheduled_emails")
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.DRAFT)
    recipient_data = models.JSONField(default=dict)
    subject = models.CharField(max_length=998)
    body_text = EncryptedTextField()
    scheduled_at_utc = models.DateTimeField(null=True, blank=True)
    timezone = models.CharField(max_length=64, default="Africa/Nairobi")
    content_hash = models.CharField(max_length=64)
    approval = models.ForeignKey(EmailApproval, null=True, blank=True, on_delete=models.SET_NULL, related_name="current_for")
    rule_version = models.PositiveIntegerField(null=True, blank=True)
    attempt_count = models.PositiveSmallIntegerField(default=0)
    idempotency_key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    provider_message_id = models.CharField(max_length=255, blank=True)
    provider_thread_id = models.CharField(max_length=255, blank=True)
    wait_for_reply_to_message_id = models.CharField(max_length=255, blank=True)
    last_error_code = models.CharField(max_length=100, blank=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    class Meta:
        indexes = [models.Index(fields=["workspace", "status", "scheduled_at_utc"]), models.Index(fields=["connection", "status", "scheduled_at_utc"])]

class EmailSendAttempt(Timestamped):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE)
    scheduled_email = models.ForeignKey(ScheduledEmail, on_delete=models.CASCADE, related_name="attempts")
    attempt_number = models.PositiveSmallIntegerField()
    outcome = models.CharField(max_length=30)
    provider_message_id = models.CharField(max_length=255, blank=True)
    error_code = models.CharField(max_length=100, blank=True)
    retryable = models.BooleanField(default=False)

class MailboxSendUsage(Timestamped):
    connection = models.ForeignKey(MailboxConnection, on_delete=models.CASCADE)
    usage_date = models.DateField()
    reserved_count = models.PositiveIntegerField(default=0)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["connection","usage_date"],name="unique_mailbox_daily_send_usage")]

class RuleSendUsage(Timestamped):
    rule = models.ForeignKey(EmailAutomationRule, on_delete=models.CASCADE)
    usage_date = models.DateField()
    reserved_count = models.PositiveIntegerField(default=0)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["rule","usage_date"],name="unique_rule_daily_send_usage")]

class RuleRecipientUsage(Timestamped):
    rule = models.ForeignKey(EmailAutomationRule, on_delete=models.CASCADE)
    usage_date = models.DateField()
    recipient = models.EmailField()
    reserved_count = models.PositiveIntegerField(default=0)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["rule","usage_date","recipient"],name="unique_rule_recipient_daily_usage")]

class ScheduledEmailEvent(Timestamped):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE)
    scheduled_email = models.ForeignKey(ScheduledEmail, on_delete=models.CASCADE, related_name="events")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    event_type = models.CharField(max_length=40)
    safe_metadata = models.JSONField(default=dict, blank=True)

class MailAuditEvent(Timestamped):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE)
    client = models.ForeignKey(Client, null=True, blank=True, on_delete=models.SET_NULL)
    connection = models.ForeignKey(MailboxConnection, null=True, blank=True, on_delete=models.SET_NULL)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    event_type = models.CharField(max_length=60)
    target_type = models.CharField(max_length=60, blank=True)
    target_id = models.CharField(max_length=80, blank=True)
    safe_metadata = models.JSONField(default=dict, blank=True)
    class Meta:
        indexes = [models.Index(fields=["workspace", "created_at"])]

class EmailNotification(Timestamped):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="email_notifications")
    notification_type = models.CharField(max_length=40)
    reference_id = models.CharField(max_length=80, blank=True)
    message = models.CharField(max_length=240)
    read_at = models.DateTimeField(null=True, blank=True)

class AuditLog(TenantModel):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=100)
    object_type = models.CharField(max_length=100)
    object_id = models.CharField(max_length=80)
    metadata = models.JSONField(default=dict, blank=True)
    class Meta: ordering = ["-created_at"]
