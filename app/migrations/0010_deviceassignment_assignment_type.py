from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('app', '0009_shellydevice_minimum_run_hours_per_day'),
    ]

    operations = [
        migrations.AddField(
            model_name='deviceassignment',
            name='assignment_type',
            field=models.CharField(
                choices=[
                    ('cheapest', 'Cheapest Hours'),
                    ('threshold', 'Price Threshold'),
                    ('forced_min', 'Forced — Min Temperature'),
                    ('manual', 'Manual'),
                ],
                default='cheapest',
                help_text='Reason this period was assigned',
                max_length=20,
            ),
        ),
    ]
