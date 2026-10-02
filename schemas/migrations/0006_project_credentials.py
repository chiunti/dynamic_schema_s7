import uuid

from django.db import migrations, models
from django.utils import timezone


class Migration(migrations.Migration):
    dependencies = [
        ('schemas', '0005_example_seed'),
    ]

    operations = [
        migrations.CreateModel(
            name='ProjectAPICredential',
            fields=[
                ('id', models.UUIDField(primary_key=True, serialize=False, editable=False,
                                        default=uuid.uuid4, db_default=models.Func(function='gen_random_uuid'))),
                ('name', models.CharField(max_length=255)),
                ('token_digest', models.CharField(max_length=64, unique=True)),
                ('can_read', models.BooleanField(default=False)),
                ('can_import', models.BooleanField(default=False)),
                ('can_publish', models.BooleanField(default=False)),
                ('expires_at', models.DateField()),
                ('revoked_at', models.DateTimeField(null=True, blank=True)),
                ('created_at', models.DateTimeField(default=timezone.now)),
                ('project', models.ForeignKey(on_delete=models.CASCADE, related_name='api_credentials',
                                              to='schemas.project')),
            ],
            options={
                'db_table': 'schema_project_api_credentials',
                'verbose_name': 'Project API credential',
                'verbose_name_plural': 'Project API credentials',
            },
        ),
        migrations.AddIndex(
            model_name='projectapicredential',
            index=models.Index(fields=['project', 'revoked_at'], name='idx_project_tokens_active'),
        ),
    ]
