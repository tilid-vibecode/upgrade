# Generated manually — adds mutual-exclusivity constraint for scope FKs
# and an index on (prototype_workspace, file_category).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        (
            "media_storage",
            "0003_rename_media_stora_prototy_ee9afd_idx_media_stora_prototy_84cd0b_idx_and_more",
        ),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="mediafile",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        organization__isnull=False,
                        discussion__isnull=True,
                        prototype_workspace__isnull=True,
                    )
                    | models.Q(
                        organization__isnull=True,
                        discussion__isnull=False,
                        prototype_workspace__isnull=True,
                    )
                    | models.Q(
                        organization__isnull=True,
                        discussion__isnull=True,
                        prototype_workspace__isnull=False,
                    )
                    | models.Q(
                        organization__isnull=True,
                        discussion__isnull=True,
                        prototype_workspace__isnull=True,
                    )
                ),
                name="media_file_single_scope",
            ),
        ),
        migrations.AddIndex(
            model_name="mediafile",
            index=models.Index(
                fields=["prototype_workspace", "file_category"],
                name="media_stora_prototy_filcat_idx",
            ),
        ),
    ]
