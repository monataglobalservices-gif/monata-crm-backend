from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import (User, Workspace, Membership, Client, Contact, Lead, Deal, Activity, Service, Project, Task, Ticket,
    TicketMessage, Campaign, ContentItem, Proposal, Contract, Invoice, Payment, AuditLog, MailboxConnection,
    MailboxInvitation, OAuthTransaction, MailboxAgentAccess, EmailAutomationRule, EmailApproval, ScheduledEmail,
    EmailSendAttempt, ScheduledEmailEvent, MailAuditEvent, RecurringEmailSchedule, EmailNotification,
    MailboxSendUsage, RuleSendUsage, RuleRecipientUsage)
from .models import (DepartmentAccess, VAClientAssignment, VAClientInstruction, VAFollowUp, VANote,
    VAClientPortalAccess, VAClientRequest, VAClientProfile, VAClientRule, VARequestTemplate,
    VARecurringTask, VATaskChecklist, VATaskChecklistItem, VAResearchOption, VAQualityReview,
    VASLATarget, VAAgentAvailability, VATimeEntry, VAHandover, VAClientChecklistRun, VAClientChecklistCompletion, VAAppointment, VAActionApproval)

admin.site.register(User, UserAdmin)
for model in (Workspace, Membership, Client, Contact, Lead, Deal, Activity, Service, Project, Task, Ticket, TicketMessage,
    Campaign, ContentItem, Proposal, Contract, Invoice, Payment, AuditLog, MailboxConnection, MailboxInvitation,
    OAuthTransaction, MailboxAgentAccess, EmailAutomationRule, EmailApproval, ScheduledEmail, EmailSendAttempt,
    ScheduledEmailEvent, MailAuditEvent, RecurringEmailSchedule, EmailNotification, MailboxSendUsage, RuleSendUsage,
    RuleRecipientUsage):
    admin.site.register(model)
for model in (DepartmentAccess, VAClientAssignment, VAClientInstruction, VAFollowUp, VANote, VAClientPortalAccess, VAClientRequest,
    VAClientProfile, VAClientRule, VARequestTemplate, VARecurringTask, VATaskChecklist, VATaskChecklistItem,
    VAResearchOption, VAQualityReview, VASLATarget, VAAgentAvailability, VATimeEntry, VAHandover,
    VAClientChecklistRun, VAClientChecklistCompletion, VAAppointment, VAActionApproval):
    admin.site.register(model)
