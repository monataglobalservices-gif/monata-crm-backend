# MONATA ONE API

Django REST backend for MONATA's multi-workspace CRM and Gmail-enabled Virtual Assistant module. Existing CRM modules cover clients, contacts, leads, deals, activities, services, projects, tasks, support tickets, marketing campaigns, proposals, contracts, invoices and payment records. The mail module adds client-authorized Gmail connections, mailbox permissions, inbox and thread access, drafts, per-message approvals, scheduled sends, client-authorized automation rules, recurring schedules and a persistent worker.

Gmail remains the source of truth. Inbox content is fetched live and is not mirrored into the CRM database. Scheduled message bodies and OAuth refresh tokens are encrypted at rest using the configured Fernet key.

## Local setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Local development uses SQLite unless `DATABASE_URL` is set. Use PostgreSQL for deployments and multiple workers. Set `DJANGO_SECRET_KEY`, `DJANGO_DEBUG=false`, exact trusted hosts and CORS origins before deployment.

Generate the encryption key with:

```powershell
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Put the output in `GMAIL_TOKEN_ENCRYPTION_KEY`. Store this key in a secret manager in production, back it up securely, and keep it separate from the database. Losing it makes stored Gmail refresh tokens and scheduled message bodies unreadable.

## Configure Gmail OAuth

Create a Google Cloud project, enable the Gmail API, configure the OAuth consent screen, and create a Web application OAuth client. Set these variables in `.env`:

```text
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
GOOGLE_REDIRECT_URI=https://api.example.com/api/v1/mail/oauth/callback/
PUBLIC_API_BASE_URL=https://api.example.com
GMAIL_TOKEN_ENCRYPTION_KEY=...
EMAIL_HOST=smtp.example.com
EMAIL_PORT=587
EMAIL_HOST_USER=notifications@example.com
EMAIL_HOST_PASSWORD=...
EMAIL_USE_TLS=true
EMAIL_USE_SSL=false
DEFAULT_FROM_EMAIL=MONATA <notifications@example.com>
```

Register the exact redirect URI in Google Cloud. The backend requests `gmail.readonly` for read-only mailbox connections and `gmail.modify` for connections that can draft, send after approval, archive and manage labels. It does not request permanent-delete access. Google classifies these Gmail scopes as restricted; public use may require OAuth verification and a security assessment when restricted data is processed on MONATA servers. Resolve Google’s requirements before connecting real client mailboxes broadly.

The repository has no Google OAuth client credentials, so Gmail connection routes remain unavailable until configured. The backend never asks clients for their Google password. Invitations identify the expected Gmail address, and the OAuth callback rejects a different Google account.

The SMTP settings above send Gmail connection invitations. The customer receives a consent page with **Accept** and **Decline** choices. Accepting starts Google OAuth; a mailbox is created only after Google grants the requested scope. Declining closes the invitation and prevents the OAuth callback from connecting it. Configure a real SMTP host and sender address before using `POST /api/v1/mail/invitations/`; the endpoint reports an error instead of pretending an invitation was sent when SMTP is missing.

## Run the scheduled-email worker

Run this in a separate process from Django. Jobs are persisted in the database, so the worker can restart without relying on an agent’s browser or computer:

```powershell
python manage.py run_email_worker --poll-seconds 5 --batch-size 20
```

For a one-pass worker invocation, add `--once`. Keep the worker supervised in deployment. PostgreSQL is recommended when running multiple worker processes because job claiming and quota reservation use database transactions and row locks.

Messages are individually approved by default. Routine sends require a separately client-authorized rule with recipient allowlists, exact approved templates, time windows, expiry and send limits. A rule can also create a no-reply follow-up; the worker checks the Gmail thread again immediately before dispatch. If a client has replied, it suppresses the follow-up. Retries are bounded. When Gmail may have accepted a message but the response was lost, the job moves to `needs_attention` and is not blindly resent.

The worker reports notification records through the API. Gmail connection invitations are emailed directly to the client's address through configured SMTP. Other workflow approval links (including message and automation-rule approvals) are returned to the agent to share through an approved channel.

## API overview

Authenticated CRM routes use `/api/v1/`. Login uses `POST /auth/token/`; use the selected workspace membership with `X-Workspace-ID` when needed. Mail operations use these routes:

| Route | Purpose |
| --- | --- |
| `GET /mail/connections/` | List mailbox connections visible to the user |
| `POST /mail/invitations/` | Email a one-use client Gmail invitation; returns delivery status |
| `GET /mail/connect/{token}/` | Client reviews the requested access and continues to Google OAuth |
| `GET /mail/mailboxes/{id}/messages/` | Search/list messages; defaults to the Inbox |
| `GET /mail/mailboxes/{id}/messages/{message_id}/` | Read a message and safe plain-text rendering |
| `GET /mail/mailboxes/{id}/threads/{thread_id}/` | Read a conversation thread |
| `GET/POST /mail/mailboxes/{id}/drafts/` | List Gmail drafts or create a draft/reply |
| `PUT /mail/mailboxes/{id}/drafts/{draft_id}/` | Update a Gmail draft |
| `POST /mail/scheduled/` | Create an individually approved or rule-authorized scheduled email |
| `GET /mail/scheduled/{id}/` | Read delivery attempts and safe event history |
| `PATCH /mail/scheduled/{id}/` | Edit content or reschedule; content edits invalidate approval |
| `POST /mail/scheduled/{id}/cancel/` | Cancel a queued message |
| `POST /mail/scheduled/{id}/retry/` | Retry a failed job; unknown send outcomes require explicit confirmation |
| `POST /mail/rules/` | Create a draft client-authorized routine-send rule |
| `GET /mail/rules/activate/{token}/` | Client reviews rule limits and templates |
| `POST /mail/recurrences/` | Create a recurring schedule under an active approved rule |
| `POST /mail/mailboxes/{id}/pause/` | Emergency pause for all scheduled sends on a mailbox |
| `POST /mail/mailboxes/{id}/resume/` | Resume sends after review |
| `POST /mail/mailboxes/{id}/disconnect/` | Revoke and remove the local Gmail refresh token |
| `GET /mail/audit/` | Owner/admin mail security and delivery audit history |
| `GET /mail/notifications/` | Per-user email workflow notifications |

Virtual Assistance endpoints use `/api/v1/va/`. `GET /va/work-inbox/` combines active client requests, open VA project tasks, follow-ups and mailbox-authorized pending approvals. Client users can submit requests through `/va/portal/requests/` or use request forms from `/va/portal/templates/`; they can view client-visible reports and appointments at `/va/portal/reports/` and `/va/portal/appointments/`. A client needs an active `client` workspace membership and a client-specific portal grant created at `/va/portal-access/`. VA agents and managers triage requests at `/va/requests/`.

## Virtual Assistance automation

The supervised `run_email_worker` process also runs the VA automation scheduler. It starts the default onboarding checklist when an agent is assigned to a client, creates a follow-up when VA work moves to waiting for the client (24 hours by default; configure `auto_follow_up_hours` in the manager-maintained VA client profile), and alerts agents about due follow-ups and approaching or missed response/resolution SLAs. Completed VA work is sampled for manager QA using `VA_QA_SAMPLE_PERCENT` (default 10; set 0 to disable). Recurring VA tasks are materialized by the same worker and can route around an unavailable agent to the configured active backup.

Appointment rules that require client approval keep appointments tentative until the client approves; rejection or expiration cancels the tentative appointment. Managers can enable appointment reminders and set the reminder lead time in the client profile. Clients control email consent and report schedules through `GET/POST /api/v1/va/portal/preferences/` with a client ID and one of `daily_summary_enabled`, `weekly_report_enabled`, `appointment_reminders_to_client`, `daily_summary_time`, `weekly_report_time`, or `weekly_report_weekday`. All automated client emails require explicit opt-in and configured SMTP. Only client-visible records are included in report details. Managers may configure `email_send_window` in the client profile as `{"start":"09:00","end":"17:00","weekdays":[0,1,2,3,4],"timezone":"Africa/Nairobi"}`; scheduled mail waits until that local-time window opens.

The worker must stay running under a process supervisor for scheduled automations to occur. These workflows automate CRM records and SMTP notices; they do not synchronize an external calendar, send WhatsApp messages, or scan uploaded files. Those need provider credentials, OAuth scopes and/or private storage and scanning services that are not configured in this backend.

The VA API also includes `/va/clients/{id}/context/`, `/va/rules/`, `/va/request-templates/`, `/va/research-options/`, `/va/recurring-tasks/`, checklist templates/runs/completions, `/va/quality-reviews/`, `/va/sla-targets/`, `/va/sla-monitor/`, `/va/workload/`, `/va/agent-availability/`, `/va/handover/`, `/va/approvals/`, `/va/time-entries/`, `/va/appointments/`, and `/va/reports/?period=day|week|month`. Tasks can retain their source request or Gmail thread reference. See [the VA build specification](docs/virtual-assistance-build-spec.md) for visibility rules, details and integrations that remain out of scope. Request uploads are disabled until private file storage and malware scanning are configured.

Agents need an explicit mailbox assignment. A new assignment returns a client approval link; permissions remain inactive until the client approves. The initial mailbox connection is assigned to the agent named on the consent page. Read-only connections cannot draft or send. No agent can send an individually approved email until the client approves its exact recipients, subject and body. Clients can cancel an approved message before the worker starts sending it.

## Current implementation limits

This is a backend foundation; no CRM frontend is included. The public approval and consent links use small backend-hosted pages so the flow can be exercised without a separate client portal. OAuth credentials, Google verification, domain/privacy-policy setup and a real mailbox are needed for an end-to-end Gmail connection.

Incoming attachments can be downloaded with a size limit and forced attachment disposition. Outgoing attachment composition is not enabled because this repository has no private file scanner/quarantine service. Push-based Gmail synchronization, Outlook, MFA, PostgreSQL row-level security, KMS-managed key rotation, outbound notification email, advanced trigger builders and public SaaS subscription billing are also not implemented. Complete security/isolation review, Google verification, backup/recovery setup and malware scanning before production client onboarding.
