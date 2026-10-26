from django.contrib import admin
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from crm.views import RegisterView, WorkspaceViewSet, MembershipViewSet, ClientViewSet, ContactViewSet, LeadViewSet, DealViewSet, ActivityViewSet, ProjectViewSet, TaskViewSet, TicketViewSet, TicketMessageViewSet, ServiceViewSet, CampaignViewSet, ContentItemViewSet, ProposalViewSet, ContractViewSet, InvoiceViewSet, PaymentViewSet, AuditLogViewSet, MeView
from crm.mail.api import (MailboxListView, MailboxInvitationView, MailboxConnectView, GmailOAuthCallbackView,
    MailboxAgentView, MailboxControlView, GmailMessagesView, GmailMessageView, GmailThreadView, GmailLabelsView,
    GmailArchiveView, GmailAttachmentView, GmailDraftView, ScheduledEmailListCreateView, ScheduledEmailDetailView,
    ApprovalPortalView, EmailRuleListCreateView, EmailRuleActivationView, EmailRuleControlView, EmailRuleUpdateView,
    RecurringEmailListCreateView, RecurringEmailControlView, EmailAuditView, EmailNotificationView, MailboxAgentApprovalView)
from crm.va import (DailyDeskView, DepartmentAccessViewSet, ClientAssignmentViewSet,
    InstructionViewSet, FollowUpViewSet, NoteViewSet, ClientPortalAccessViewSet,
    ClientPortalRequestView, ClientRequestViewSet, WorkInboxView)
from crm.va_features import (ClientContextView, WorkloadView, AvailabilityView, HandoverView,
    ReportView, PortalReportView, PortalPreferencesView, PortalAppointmentsView, VAActionApprovalView, PortalActionApprovalView, ConvertRequestToTaskView, TaskFromEmailView, SLAMonitorView, PortalTemplatesView,
    feature_view)
from crm.models import (VAAgentAvailability, VAClientProfile, VAClientRule, VARequestTemplate, VARecurringTask,
    VATaskChecklist, VATaskChecklistItem, VAResearchOption, VAQualityReview, VASLATarget, VATimeEntry,
    VAClientChecklistRun, VAClientChecklistCompletion, VAAppointment)

router = DefaultRouter()
router.register("workspaces", WorkspaceViewSet, basename="workspace")
router.register("memberships", MembershipViewSet, basename="membership")
router.register("clients", ClientViewSet, basename="client")
router.register("contacts", ContactViewSet, basename="contact")
router.register("leads", LeadViewSet, basename="lead")
router.register("deals", DealViewSet, basename="deal")
router.register("activities", ActivityViewSet, basename="activity")
router.register("services", ServiceViewSet, basename="service")
router.register("projects", ProjectViewSet, basename="project")
router.register("tasks", TaskViewSet, basename="task")
router.register("tickets", TicketViewSet, basename="ticket")
router.register("ticket-messages", TicketMessageViewSet, basename="ticket-message")
router.register("campaigns", CampaignViewSet, basename="campaign")
router.register("content-items", ContentItemViewSet, basename="content-item")
router.register("proposals", ProposalViewSet, basename="proposal")
router.register("contracts", ContractViewSet, basename="contract")
router.register("invoices", InvoiceViewSet, basename="invoice")
router.register("payments", PaymentViewSet, basename="payment")
router.register("audit-logs", AuditLogViewSet, basename="audit-log")
router.register("va/department-access", DepartmentAccessViewSet, basename="va-department-access")
router.register("va/assignments", ClientAssignmentViewSet, basename="va-assignment")
router.register("va/instructions", InstructionViewSet, basename="va-instruction")
router.register("va/follow-ups", FollowUpViewSet, basename="va-follow-up")
router.register("va/notes", NoteViewSet, basename="va-note")
router.register("va/portal-access", ClientPortalAccessViewSet, basename="va-portal-access")
router.register("va/requests", ClientRequestViewSet, basename="va-client-request")
router.register("va/profiles", feature_view(VAClientProfile), basename="va-profile")
router.register("va/rules", feature_view(VAClientRule), basename="va-rule")
router.register("va/request-templates", feature_view(VARequestTemplate), basename="va-request-template")
router.register("va/recurring-tasks", feature_view(VARecurringTask, client_path="project__client_id"), basename="va-recurring-task")
router.register("va/checklists", feature_view(VATaskChecklist), basename="va-checklist")
router.register("va/checklist-items", feature_view(VATaskChecklistItem, client_path="checklist__client_id"), basename="va-checklist-item")
router.register("va/research-options", feature_view(VAResearchOption, client_path="request__client_id", manager_write=False), basename="va-research-option")
router.register("va/quality-reviews", feature_view(VAQualityReview), basename="va-quality-review")
router.register("va/sla-targets", feature_view(VASLATarget), basename="va-sla-target")
router.register("va/time-entries", feature_view(VATimeEntry, manager_write=False), basename="va-time-entry")
router.register("va/checklist-runs", feature_view(VAClientChecklistRun), basename="va-checklist-run")
router.register("va/checklist-completions", feature_view(VAClientChecklistCompletion, client_path="run__client_id", manager_write=False), basename="va-checklist-completion")
router.register("va/appointments", feature_view(VAAppointment, manager_write=False), basename="va-appointment")

urlpatterns = [path("admin/", admin.site.urls), path("api/v1/auth/register/", RegisterView.as_view()),
    path("api/v1/va/daily-desk/", DailyDeskView.as_view()),
    path("api/v1/va/work-inbox/", WorkInboxView.as_view()),
    path("api/v1/va/clients/<uuid:client_id>/context/", ClientContextView.as_view()),
    path("api/v1/va/workload/", WorkloadView.as_view()),
    path("api/v1/va/agent-availability/", AvailabilityView.as_view()),
    path("api/v1/va/handover/", HandoverView.as_view()),
    path("api/v1/va/reports/", ReportView.as_view()),
    path("api/v1/va/sla-monitor/", SLAMonitorView.as_view()),
    path("api/v1/va/requests/<uuid:request_id>/convert-to-task/", ConvertRequestToTaskView.as_view()),
    path("api/v1/va/tasks/from-email/", TaskFromEmailView.as_view()),
    path("api/v1/va/portal/templates/", PortalTemplatesView.as_view()),
    path("api/v1/va/portal/reports/", PortalReportView.as_view()),
    path("api/v1/va/portal/preferences/", PortalPreferencesView.as_view()),
    path("api/v1/va/portal/appointments/", PortalAppointmentsView.as_view()),
    path("api/v1/va/approvals/", VAActionApprovalView.as_view()),
    path("api/v1/va/portal/approvals/", PortalActionApprovalView.as_view()),
    path("api/v1/va/portal/requests/", ClientPortalRequestView.as_view()),
    path("api/v1/auth/token/", TokenObtainPairView.as_view()), path("api/v1/auth/token/refresh/", TokenRefreshView.as_view()),
    path("api/v1/auth/me/", MeView.as_view()), path("api/v1/", include(router.urls)),
    path("api/v1/mail/connections/", MailboxListView.as_view()),
    path("api/v1/mail/invitations/", MailboxInvitationView.as_view()),
    path("api/v1/mail/connect/<str:token>/", MailboxConnectView.as_view()),
    path("api/v1/mail/oauth/callback/", GmailOAuthCallbackView.as_view()),
    path("api/v1/mail/mailboxes/<uuid:connection_id>/agents/", MailboxAgentView.as_view()),
    path("api/v1/mail/mailboxes/<uuid:connection_id>/messages/", GmailMessagesView.as_view()),
    path("api/v1/mail/mailboxes/<uuid:connection_id>/messages/<str:message_id>/", GmailMessageView.as_view()),
    path("api/v1/mail/mailboxes/<uuid:connection_id>/threads/<str:thread_id>/", GmailThreadView.as_view()),
    path("api/v1/mail/mailboxes/<uuid:connection_id>/labels/", GmailLabelsView.as_view()),
    path("api/v1/mail/mailboxes/<uuid:connection_id>/messages/<str:message_id>/archive/", GmailArchiveView.as_view()),
    path("api/v1/mail/mailboxes/<uuid:connection_id>/messages/<str:message_id>/attachments/<str:attachment_id>/", GmailAttachmentView.as_view()),
    path("api/v1/mail/mailboxes/<uuid:connection_id>/drafts/", GmailDraftView.as_view()),
    path("api/v1/mail/mailboxes/<uuid:connection_id>/drafts/<str:draft_id>/", GmailDraftView.as_view()),
    path("api/v1/mail/mailboxes/<uuid:connection_id>/<str:action>/", MailboxControlView.as_view()),
    path("api/v1/mail/agent-approvals/<str:token>/", MailboxAgentApprovalView.as_view()),
    path("api/v1/mail/scheduled/", ScheduledEmailListCreateView.as_view()),
    path("api/v1/mail/scheduled/<uuid:job_id>/", ScheduledEmailDetailView.as_view()),
    path("api/v1/mail/scheduled/<uuid:job_id>/<str:action>/", ScheduledEmailDetailView.as_view()),
    path("api/v1/mail/approvals/<str:token>/", ApprovalPortalView.as_view()),
    path("api/v1/mail/rules/", EmailRuleListCreateView.as_view()),
    path("api/v1/mail/rules/<uuid:rule_id>/", EmailRuleUpdateView.as_view()),
    path("api/v1/mail/rules/<uuid:rule_id>/<str:action>/", EmailRuleControlView.as_view()),
    path("api/v1/mail/rules/activate/<str:token>/", EmailRuleActivationView.as_view()),
    path("api/v1/mail/recurrences/", RecurringEmailListCreateView.as_view()),
    path("api/v1/mail/recurrences/<uuid:schedule_id>/<str:action>/", RecurringEmailControlView.as_view()),
    path("api/v1/mail/audit/", EmailAuditView.as_view()),
    path("api/v1/mail/notifications/", EmailNotificationView.as_view()),
    path("api/v1/mail/notifications/<uuid:notification_id>/read/", EmailNotificationView.as_view())]
