from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('company_intake', '0002_workspacesource_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='workspacesource',
            name='notes',
            field=models.TextField(blank=True, default=''),
        ),
    ]
