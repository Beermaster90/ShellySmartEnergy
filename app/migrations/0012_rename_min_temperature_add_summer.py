from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('app', '0011_alter_shellydevice_status'),
    ]

    operations = [
        migrations.RenameField(
            model_name='shellytemperature',
            old_name='min_temperature',
            new_name='min_temperature_winter',
        ),
        migrations.AlterField(
            model_name='shellytemperature',
            name='min_temperature_winter',
            field=models.DecimalField(
                decimal_places=2,
                default=0,
                help_text='Minimum temperature threshold Sep 1 – Mar 31',
                max_digits=5,
                verbose_name='Min temperature (winter)',
            ),
        ),
        migrations.AddField(
            model_name='shellytemperature',
            name='min_temperature_summer',
            field=models.DecimalField(
                decimal_places=2,
                default=0,
                help_text='Minimum temperature threshold Apr 1 – Aug 31',
                max_digits=5,
                verbose_name='Min temperature (summer)',
            ),
        ),
    ]
