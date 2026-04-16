# Generated manually — adds operator_token field to IntakeWorkspace.
# Two-phase approach: add nullable field, backfill unique tokens, then
# make it non-null + unique to avoid duplicate-key errors on existing rows.

import secrets

from django.db import migrations, models


def backfill_operator_tokens(apps, schema_editor):
    """Assign a unique token to every existing workspace."""
    Workspace = apps.get_model("company_intake", "IntakeWorkspace")
    for ws in Workspace.objects.filter(operator_token=""):
        ws.operator_token = secrets.token_urlsafe()
        ws.save(update_fields=["operator_token"])


class Migration(migrations.Migration):

    dependencies = [
        ("company_intake", "0006_alter_workspacesource_status"),
    ]

    operations = [
        # 1. Add the field as nullable, non-unique, with blank default
        migrations.AddField(
            model_name="intakeworkspace",
            name="operator_token",
            field=models.CharField(
                default="",
                help_text="Bearer token required for workspace admin operations.",
                max_length=64,
                blank=True,
            ),
        ),
        # 2. Backfill a unique token for each existing row
        migrations.RunPython(
            backfill_operator_tokens,
            reverse_code=migrations.RunPython.noop,
        ),
        # 3. Now alter to the final state: non-blank default + unique
        migrations.AlterField(
            model_name="intakeworkspace",
            name="operator_token",
            field=models.CharField(
                default=secrets.token_urlsafe,
                help_text="Bearer token required for workspace admin operations.",
                max_length=64,
                unique=True,
            ),
        ),
    ]
