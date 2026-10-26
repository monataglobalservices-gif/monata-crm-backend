# MONATA Virtual Assistance: Build Specification

## Automation currently implemented

The persistent `run_email_worker` is also the scheduler for CRM-side Virtual Assistance automations. Keep it running under a process supervisor; no scheduled action runs while the worker is stopped.

- A standard VA onboarding checklist is seeded for each new workspace and starts when an active VA client assignment is created. Existing workspaces receive the same template through migration.
- Moving an assigned VA task or client request to “waiting for client” creates a follow-up. The due interval defaults to 24 hours and is configurable per client with the manager-maintained profile preference `auto_follow_up_hours` (1–720). Resuming the work cancels its open generated follow-up.
- The worker sends in-app notices for due follow-ups and SLA warnings/breaches. SLA thresholds come from active client-specific or default `VASLATarget` records.
- Completed VA tasks and requests enter the manager QA queue by deterministic sampling. `VA_QA_SAMPLE_PERCENT` defaults to 10 and can be set from 0 to 100.
- Recurring VA tasks are created by the worker. If an assigned agent is unavailable, the task is assigned to that client’s configured active backup where available.
- Appointment rules requiring client approval make appointments tentative until approval. Rejection or expiration cancels them. Appointment reminder emails require manager configuration and separate customer opt-in.
- Daily and weekly summary emails are sent only after customer opt-in using `POST /api/v1/va/portal/preferences/`. The same endpoint supports retrieval and updating of report opt-ins/schedules and appointment reminder email consent. Report queries exclude private tasks, appointments, and time entries.
- Client email windows can be configured by a manager in the profile’s `email_send_window` preference. Scheduled email jobs defer until the configured local-time window. Malformed windows defer the job and should be corrected by the manager.

These automations cover records and services already present in the backend. External calendar synchronization, WhatsApp delivery, and upload malware scanning remain unavailable until their providers, credentials/scopes, and storage services are configured.

## Purpose

Give MONATA agents one workspace for managing a client's day-to-day administrative work. Email, tasks, follow-ups, appointments, instructions, and service reporting should form one connected workflow. This specification covers the Virtual Assistance department only; other MONATA departments can share selected client and contact records while keeping their tools and permissions separate.

## Product boundaries

- Gmail remains the source of truth for mailbox content. MONATA stores connection and workflow metadata, not a mirrored inbox.
- Client records and contacts may be shared across departments when explicitly permitted. VA tasks, notes, files, activity, and reports are department-scoped.
- Client-visible information must be explicitly marked and authorized. Private agent notes, internal team notes, and client notes are distinct visibility levels.
- Agents act within an assigned client and authorized resources. A CRM membership alone does not grant Gmail access.
- The backend is the current deliverable. A frontend can consume the API later; this document describes the intended screens and their API needs.

## Roles and visibility

| Role | Can see | Can do |
| --- | --- | --- |
| VA agent | Assigned client work, permitted contacts, client instructions, assigned mailbox, tasks, follow-ups, and own time entries | Update assigned work; create tasks, follow-ups, and communication logs; use mailbox capabilities granted by the client; record time |
| VA manager | VA work for managed clients, assignments, workload, service usage, and reports | Assign agents; manage client instructions; review work and time; create reports; manage department settings |
| Client | Their own client-visible tasks, appointments, shared notes/files, approvals, and service reports | Provide instructions and files; approve or reject actions that require approval; view agreed reports |
| Workspace owner/admin | Workspace membership, administration, audit data, and access configuration | Manage workspace users and settings; grant department access; review audit information |

Access is always scoped to the active workspace and the user's active membership. Client portal access is limited to the matching client record. Email permissions continue to be enforced by mailbox-specific access and client approvals.

## Navigation and screens

### 1. Virtual Assistance Daily Desk

**Purpose:** Give an agent one prioritized start-of-day view across assigned clients.

**Screen sections:**

- Per-client summary: urgent items, overdue work, due-today tasks, upcoming appointments, pending client responses, and email approvals.
- Prioritized action list with a reason for each item, due date, client, and direct link to the related record.
- Filters for client, urgency, due date, assignment, and work type.
- Quick actions to open a task, follow-up, message, appointment, or approval.

**Rules:** Counts and action items are calculated from authorized records. A suggested action never sends an email or changes a calendar without the permissions and approval required by that feature.

### 2. Email

**Purpose:** Work with a client's authorized Gmail account without making Gmail contents a second system of record.

**Screens:** Inbox/search, message and thread view, drafts, scheduled messages, approvals, and automation rules.

**Actions:** Read and search; create and edit drafts; reply or forward; schedule or cancel; archive and manage supported labels; request client approval; manage authorized templates and rules. Attachments remain subject to configured size and security controls.

**Current project status:** Gmail connections, mailbox assignments, messages/threads, drafts, scheduled sends, client approval links, automation rules, recurrence, audit, and a persistent worker already exist in the backend. The README notes that outgoing attachment composition is not enabled.

### 3. Tasks

**Purpose:** Track administrative work from creation through completion.

**Views:** My tasks, client tasks, due today, overdue, waiting for client, scheduled, and completed.

**Task fields:** Client, title, description, status, priority, due date, assignee, source, related contact, related email/thread, appointment, and optional completion details.

**Actions:** Create, assign, update status, set due date, add a note, link related records, and mark complete. “Waiting for client” must be a first-class state with an optional next reminder date.

### 4. Follow-ups

**Purpose:** Make commitments and next actions visible and time-bound.

**Fields:** Client, contact, category, reason, last-contact date, next action, due date/time, owner, status, and linked task/email/appointment.

**Categories:** Client, customer, supplier, lead, payment, document, appointment, and internal.

**Actions:** Create from a contact, email, or task; reschedule; complete; snooze; or convert an authorized follow-up into an approved scheduled email. Automated sends must use existing client-authorized rules and must not bypass mailbox approval checks.

### 5. Contacts and relationship view

**Purpose:** Show the operational context around a person.

**Contact detail sections:** Contact details, company/client, last interaction, next follow-up, notes visible to the current user, related tasks, appointments, and linked email references that the agent is authorized to access.

**Actions:** Add or update contact details, add a note, create a follow-up/task, and navigate to related records. Prevent cross-workspace access and validate that related records belong to the same client/workspace.

### 6. Calendar

**Purpose:** Manage authorized client appointments and coordinate meeting requests.

**Screens:** Calendar day/week/month views, appointment detail, and availability selection.

**Fields:** Client, title, start/end with timezone, attendees, location or meeting link, reminder settings, recurrence details, status, and related contacts/tasks/email thread.

**Actions:** Check availability, create, reschedule, cancel, add attendees, and prepare invitations. Calendar provider and OAuth scopes must be selected before implementation. Until then, the backend should keep calendar integration behind a provider boundary and avoid implying external synchronization.

### 7. Notes and client instructions / SOPs

**Notes visibility:** Private agent, internal team, or client-visible. Visibility is explicit and shown before saving.

**Client instructions:** Organized by topic (email, appointments, expenses, customers, escalation, and other), with instruction text, effective dates, author, and active status. Changes are auditable. Agents see active instructions in the Daily Desk and relevant task/email/calendar workflows.

**Safety:** Never expose private or internal notes through client endpoints or reports. Client-visible notes require an explicit visibility value and client-scoped authorization.

### 8. Files

**Purpose:** Store or reference client work documents and link them to tasks, emails, contacts, and clients.

**Requirements:** Workspace/client scoped storage; upload size and file-type limits; authorization on download; metadata and audit trail; attachment scanning/quarantine before broad production use. Do not store file contents in public static directories. The repository currently has no private scanning/quarantine service, so file uploads should not be enabled for production until one is selected.

### 9. Communication log

**Purpose:** Record calls and other offline interactions without building a phone system.

**Fields:** Client, contact, date/time, duration, purpose, summary, outcome, follow-up required, and linked next action. Visibility follows the note and client access rules.

### 10. Time tracking and service allocation

**Purpose:** Give MONATA operational visibility into work delivered against an agreed client allocation.

**Actions:** Start/stop a timer from a task, or add a manual entry with a reason. Entries include client, task, agent, start/end, duration, and approval state if required by policy.

**Manager view:** Period allocation, used time, remaining time, and entries grouped by client and task. Define rounding, edits, approvals, and whether clients see detailed entries before implementation; do not silently change historical entries.

### 11. Reminders

**Purpose:** Surface personal and client-work reminders in the Daily Desk.

**Fields:** Owner, client, text, due time/timezone, recurrence, related record, delivery channels, and completion state. Initial delivery is an in-app notification. Email/mobile/calendar delivery is a later integration and must respect user preferences.

### 12. Reports

**Purpose:** Explain the service delivered during a chosen period.

**Weekly/monthly report:** Emails handled (where auditable), appointments managed, tasks completed, calls logged, documents processed, tracked time, and a list of completed work. Each metric needs a defined source and period boundary. Client reports include only client-visible/approved information and never private notes or unrelated mailbox contents.

### 13. VA Copilot (later)

Copilot may summarize authorized assigned work and suggest next actions after the data model, permissions, and audit trail are stable. Suggestions require source links and agent review. Copilot cannot independently send messages, modify appointments, or expose records outside the agent's access.

## First release scope

Build the smallest useful VA operational loop before external calendar or file integrations:

1. Department membership/access structure that keeps VA tools separate from future departments.
2. Client assignment and client instructions.
3. Task improvements for assignment, priority, due dates, waiting-for-client status, and links to contacts/email references.
4. Follow-ups with categories, due dates, and Daily Desk visibility.
5. Notes with private/internal/client-visible access enforced by the API.
6. Daily Desk endpoint aggregating assigned tasks, follow-ups, pending approvals, and upcoming due work.
7. Audit events for assignment, instruction changes, visibility changes, and completion.

Calendar, client file uploads, time allocation/billing rules, client-facing reports, and Copilot are subsequent releases. Existing Gmail remains available as a connected workflow and should be integrated via references/links rather than duplicated message storage.

## Project fit and data model direction

The current project already has `Workspace`, `Membership`, `Client`, `Contact`, `Activity`, `Task`, and a Gmail/mail workflow in `crm/models.py`. Most CRM records are workspace-scoped. Mailbox access is separately represented by mailbox connection and agent access records, with approval and audit models.

### Implemented first slice

The first backend slice is now implemented under `/api/v1/`:

- `GET/POST/PATCH/DELETE /va/department-access/` manages VA agent and manager access for active workspace members.
- `GET/POST/PATCH/DELETE /va/assignments/` assigns workspace clients to VA agents; deletion deactivates an assignment.
- `GET/POST/PATCH/DELETE /va/instructions/` manages scoped client instructions. `?client={id}` and `?active=true` filter the list.
- `GET/POST/PATCH/DELETE /va/follow-ups/` manages categorized, dated client follow-ups; filters include `client` and `status`.
- `GET/POST/PATCH/DELETE /va/notes/` manages private, internal, and client-visible notes. Private notes are returned only to their author.
- `GET /va/daily-desk/` summarizes VA project tasks, follow-ups, and pending email approvals.
- `GET /va/work-inbox/` combines active client requests, open VA project tasks, follow-ups, and mailbox-authorized pending email approvals.
- `GET/POST/PATCH/DELETE /va/portal-access/` lets VA managers grant or revoke access for users with active client memberships.
- `GET/POST /va/portal/requests/` lets authenticated client portal users submit requests and see only their own requests for clients they can access.
- `GET/PATCH /va/requests/` lets assigned agents and managers triage submitted requests, claim unassigned work, assign it, and update its status.

The CRM service category list now matches the five MONATA departments: `virtual_assistance`, `digital_development`, `social_media`, `digital_marketing`, and `customer_support`. Tasks also have a `waiting_for_client` status, and the Daily Desk includes that state in its count.

All routes use `X-Workspace-ID` (or the existing `workspace` query parameter) when a user belongs to multiple workspaces. Workspace owners/admins and VA department managers can manage VA access and assignments. VA agents need active department access and an active client assignment for client-scoped records. Agent email approval summaries also require active read access to the corresponding mailbox. Client request portal access is separate from VA agent access: a portal user needs an active client membership and an explicit client-to-user portal grant. Client-visible note labels remain metadata until a dedicated client note endpoint is added.

Request attachments are intentionally not accepted yet; add them only after private storage and malware scanning are available. Request templates, approval center, recurring VA work, and structured client preferences remain later increments.

## Additional operational backend features

The proposal's remaining operational capabilities now have backend support under the VA API. Every staff route requires an authenticated workspace membership with VA department access; records are filtered to the agent's assigned VA clients. Workspace owners/admins and VA managers can configure department-wide resources.

| Capability | Backend surface | Notes |
| --- | --- | --- |
| Turn email/request into a task | `POST /va/tasks/from-email/`; `POST /va/requests/{id}/convert-to-task/` | Saves a Gmail thread ID or source request on the task, after confirming client assignment and mailbox read access. Gmail remains the source of message content. |
| Request templates | `/va/request-templates/`; client portal `GET/POST /va/portal/templates/` | JSON field schemas support text, textarea, email, number, date, select and boolean. Required values are checked when submitted. |
| Handover | `GET/POST /va/handover/` | Captures current tasks, waiting work, requests, follow-ups, instructions, rules and mailbox counts, then reassigns open VA work and updates the client assignment. |
| Client context/preferences | `GET/PUT /va/clients/{client_id}/context/`; `/va/profiles/` | Stores timezone, language, communication channel, tone, working hours, preferences, retainer minutes, health status and backup agent. Context also returns active client rules, instructions and work counts. |
| Client rules | `/va/rules/` | Stores operational rules and approval thresholds for agents to consult in the client context. Existing email/calendar/purchase workflows must enforce a rule before performing the corresponding action. |
| VA Approval Center | `GET/POST /va/approvals/`; client `GET/POST /va/portal/approvals/` | Records approval requests for appointments, documents, purchases and other actions. An accepted or rejected appointment approval confirms or cancels its tentative appointment; other action types are recorded for staff to carry out after the decision. Email continues to use its existing content-bound email approval flow. |
| Personal assistant queue | Existing `/va/portal/requests/`, `/va/requests/`, and work inbox | Personal assistance and research are request categories; they share the request triage lifecycle. |
| Research workspace | `/va/research-options/` | Stores comparable options with price, currency, location, contact, URL, notes and selection state against a research request. |
| Recurring VA work | `/va/recurring-tasks/` | The email worker creates due VA tasks on daily, weekly, monthly or quarterly cadence. Use `run_email_worker` in deployment to process them. |
| Checklists | `/va/checklists/`, `/va/checklist-items/`, `/va/checklist-runs/`, `/va/checklist-completions/` | Supports reusable checklist templates, per-client runs and auditable item completion. Creating a run is explicit; onboarding does not yet trigger one automatically. |
| Quality control | `/va/quality-reviews/` | Managers record communication, accuracy and SOP scores from 0 to 10 with feedback and improvement flag. Reviews are not yet sampled or calculated automatically. |
| SLA monitoring | `/va/sla-targets/`, `GET /va/sla-monitor/` | Per-client response and resolution targets. First response is timestamped on triage; monitor reports on-track, at-risk and breached request targets using wall-clock minutes. |
| Workload | `GET /va/workload/` | Counts active tasks, due-today/overdue work, open requests/follow-ups and tracked minutes per VA agent. |
| Offline coverage | `POST /va/agent-availability/` | A manager can mark an agent unavailable; open work moves to each client's configured backup agent and client assignment updates. Agents can update their own availability, while manager action performs reassignment. |
| Preferences | Client profile/context routes above | Preferences are stored and returned with client context. Provider actions such as sending email or creating an external calendar event must still apply the relevant preferences. |
| Daily/weekly client reports | `GET /va/reports/?period=day|week|month`; portal `GET /va/portal/reports/` | Aggregates completed VA tasks, requests, follow-ups, sent scheduled email counts, appointments and tracked minutes. Client reports include only tasks, appointments and time entries explicitly marked client-visible; request summaries are scoped to that client's portal grant. |
| Calendar records | `/va/appointments/`; portal `GET /va/portal/appointments/` | Stores timezone-aware appointments, attendees, location/link, status and recurrence metadata. Appointments can be attached to a client approval request. It does not synchronize with Google Calendar or another provider. |
| Waiting room | Existing waiting request/task/follow-up statuses and `/va/work-inbox/` | Follow-ups carry the next due date. Agent must create the follow-up when moving work to waiting; no automatic follow-up rule is inferred. |

Migrations `0010` through `0014` add these VA operations and task provenance. The database migration has been applied locally. Private file storage/uploads and malware scanning remain disabled. External calendar synchronization, WhatsApp/mobile delivery, automatic checklist creation at onboarding, automated QA sampling, and business-hours SLA calendars still need integrations or policy decisions. Reports return only metrics backed by stored VA records; they do not invent document or time data.

The VA data migration is `crm/migrations/0005_vaclientinstruction_vafollowup_vanote_and_more.py`; department and task choice updates are in migrations `0006` and `0007`. Existing task records remain in the CRM task model and appear on the VA Daily Desk only when their project is linked to a Virtual Assistance service and a VA-assigned client.

Recommended additions should be incremental and migration-backed:

- A department/access model (or equivalent membership capability) associated with workspace membership; start with `virtual_assistance` and leave other departments as future values.
- Client assignment records connecting VA agents to specific client records.
- Client instruction records with topic, text, active/effective dates, author, and version/audit history.
- Follow-up records linked to client and optionally contact/task/mail thread.
- Notes with explicit visibility enum and client/workspace scope.
- Daily Desk as a permission-filtered aggregation API; avoid a duplicated dashboard table until performance proves necessary.
- Later models for calendar provider events, private file metadata, communication logs, time entries, and report snapshots if consistent historical reports are needed.

Before changing existing CRM endpoints, confirm whether `Task`, `Activity`, and `Membership` already provide fields/permissions that can be safely extended. Keep tenant filtering in querysets and validate every cross-model relationship at write time.

## API shape (proposed)

All authenticated endpoints remain under `/api/v1/` and use the active workspace selection already used by the project.

- `GET /va/daily-desk/` — scoped counts and prioritized action references.
- `GET, POST /va/clients/{client_id}/assignments/` and `DELETE /va/assignments/{id}/` — manager-controlled assignments.
- `GET, POST /va/clients/{client_id}/instructions/`, `PATCH /va/instructions/{id}/` — active client SOPs.
- `GET, POST /va/tasks/`, `GET, PATCH /va/tasks/{id}/` — VA task operations or a carefully extended existing task API.
- `GET, POST /va/follow-ups/`, `GET, PATCH /va/follow-ups/{id}/` — follow-up work.
- `GET, POST /va/notes/`, `GET, PATCH /va/notes/{id}/` — notes filtered by visibility and access.

The exact route design should follow existing viewset and workspace conventions after inspecting current filters and permissions. Do not expose a parallel endpoint that weakens an existing CRM permission check.

## Acceptance criteria for first release

- An agent sees only VA clients assigned to them and records in their active workspace.
- Managers can assign/reassign agents and view their team's VA work.
- Client portal users can access only their own client-visible records.
- Private agent and internal notes never appear in client responses, exports, or reports.
- Client instructions are visible to assigned agents and changes are attributable and auditable.
- Tasks and follow-ups can be created, assigned, prioritized, scheduled, completed, and linked to valid same-client records.
- Daily Desk counts match the underlying permitted tasks, follow-ups, and mail approvals, without revealing inaccessible records.
- Existing CRM and Gmail access checks continue to apply; migration does not remove current data.

## Delivery sequence

1. Inspect and document current task, membership, workspace-selection, and client-portal permission behavior.
2. Finalize the department and assignment model; add migrations and scoped APIs.
3. Implement instructions, notes, follow-ups, and task workflow.
4. Implement the Daily Desk aggregation and audit events.
5. Add calendar, time tracking, communication log, files, and reports as separate reviewed increments.
6. Add Copilot only after authorization-filtered APIs and audit behavior are established.

