from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("app", "0018_shellydevice_overrides")]

    operations = [
        migrations.AddField(
            model_name="shellydevice",
            name="force_off",
            field=models.BooleanField(
                default=False,
                help_text="Keep the device off while preserving its normal assignments",
            ),
        ),
    ]
