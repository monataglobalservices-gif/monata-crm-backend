from django.db import migrations


ITEMS = (
    "Confirm client profile and primary contact",
    "Confirm client timezone and working hours",
    "Connect the approved client mailbox",
    "Verify mailbox access level and assigned agents",
    "Collect company information and key contacts",
    "Collect client instructions and SOPs",
    "Record communication style and approval rules",
    "Confirm escalation contacts and urgent channel",
    "Test approved workflow access",
    "Confirm onboarding completion with the client",
)


def seed_onboarding(apps, schema_editor):
    Workspace = apps.get_model("crm", "Workspace")
    Checklist = apps.get_model("crm", "VATaskChecklist")
    Item = apps.get_model("crm", "VATaskChecklistItem")
    Assignment = apps.get_model("crm", "VAClientAssignment")
    Run = apps.get_model("crm", "VAClientChecklistRun")
    Completion = apps.get_model("crm", "VAClientChecklistCompletion")
    for workspace in Workspace.objects.all().iterator():
        checklist, _ = Checklist.objects.get_or_create(workspace_id=workspace.pk, name="VA Client Onboarding", defaults={
            "description": "Standard checks before the VA team begins ongoing client work.",
            "auto_start_on_assignment": True,
        })
        for index, title in enumerate(ITEMS):
            Item.objects.get_or_create(workspace_id=workspace.pk, checklist_id=checklist.pk, position=index,
                defaults={"title": title, "is_required": True})
        for assignment in Assignment.objects.filter(workspace_id=workspace.pk, is_active=True).iterator():
            run, _ = Run.objects.get_or_create(workspace_id=workspace.pk, checklist_id=checklist.pk,
                client_id=assignment.client_id, assignment_id=assignment.pk,
                defaults={"assigned_to_id": assignment.agent_id, "created_by_id": assignment.assigned_by_id})
            Completion.objects.bulk_create([
                Completion(workspace_id=workspace.pk, run_id=run.pk, item_id=item.pk)
                for item in Item.objects.filter(checklist_id=checklist.pk)
            ], ignore_conflicts=True)


class Migration(migrations.Migration):
    dependencies = [("crm", "0016_task_source_recurring_task")]
    operations = [migrations.RunPython(seed_onboarding, migrations.RunPython.noop)]
