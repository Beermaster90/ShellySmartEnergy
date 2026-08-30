"""Encrypt credential values that were stored as plaintext before encryption.

Re-saves the affected fields so EncryptedField.get_prep_value writes ciphertext.
Idempotent: values already encrypted decrypt to plaintext on read and re-encrypt
on save. Reversing is a no-op — decrypt-on-read keeps the app working either way.
"""

from django.db import migrations


FIELDS = {
    "ShellyDevice": ["shelly_api_key"],
    "ShellyTemperature": ["shelly_api_key"],
    "EVCharger": ["tuya_client_id", "tuya_client_secret", "tuya_device_id"],
    "AppSetting": ["value"],
}


def encrypt_existing(apps, schema_editor):
    for model_name, fields in FIELDS.items():
        model = apps.get_model("app", model_name)
        for obj in model.objects.all().iterator():
            # Reading already decrypted (or passed through) plaintext; saving
            # these fields re-writes them as ciphertext.
            obj.save(update_fields=fields)


def noop_reverse(apps, schema_editor):
    # Nothing to do: encrypted values remain readable via decrypt-on-read.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("app", "0022_alter_appsetting_value_and_more"),
    ]

    operations = [
        migrations.RunPython(encrypt_existing, noop_reverse),
    ]
