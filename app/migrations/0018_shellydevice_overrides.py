from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("app", "0017_thermostat_headroom_replace_hoped_temperature")]

    operations = [
        migrations.AddField(
            model_name="shellydevice",
            name="force_on",
            field=models.BooleanField(
                default=False,
                help_text="Continuously queue the next period so the device remains on",
            ),
        ),
        migrations.AddField(
            model_name="shellydevice",
            name="scheduled_override_until",
            field=models.DateTimeField(
                blank=True,
                help_text="Keep creating normal price-based assignments until this UTC time",
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="deviceassignment",
            name="assignment_type",
            field=models.CharField(
                choices=[
                    ("cheapest", "Cheapest Hours"),
                    ("threshold", "Price Threshold"),
                    ("forced_min", "Forced — Min Temperature"),
                    ("scheduled_override", "Scheduled Override"),
                    ("force_on", "Force Device On"),
                    ("manual", "Manual"),
                    ("removed_overheat", "Removed — Overheating"),
                    ("removed_headroom", "Removed — Headroom"),
                ],
                default="cheapest",
                help_text="Reason this period was assigned",
                max_length=20,
            ),
        ),
    ]
