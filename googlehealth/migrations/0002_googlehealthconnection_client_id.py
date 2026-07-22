from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("googlehealth", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="googlehealthconnection",
            name="client_id",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
    ]
