"""Transparently encrypted model fields (Fernet / AES-128-CBC + HMAC).

Stored credentials — Shelly and Tuya API keys, the ENTSO-E key — are encrypted
at rest so a leaked database file does not hand over working keys to your relays
and charger. Encryption/decryption is transparent: model code still reads and
writes plaintext.

The encryption key lives OUTSIDE the database — in DJANGO_FIELD_ENCRYPTION_KEY or
a 0600 file on the persistent /data volume — so the ciphertext and its key are
never in the same place. Losing that key makes the encrypted values
unrecoverable, so it must be backed up alongside the database.

Reads use decrypt-or-passthrough: a value that isn't a valid token is returned
as-is and assumed to be legacy plaintext, so switching a field to encrypted is
seamless and the one-off data migration can run idempotently.
"""

import os
import threading
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import models

_fernet = None
_lock = threading.Lock()


def _key_path() -> Path:
    configured = os.environ.get("DJANGO_FIELD_ENCRYPTION_KEY_FILE")
    if configured:
        return Path(configured).expanduser()
    if Path("/data").exists():
        return Path("/data/.field_encryption_key")
    data_dir = Path(settings.BASE_DIR) / "data"
    return (data_dir if data_dir.exists() else Path(settings.BASE_DIR)) / ".field_encryption_key"


def _load_or_create_key() -> bytes:
    env_key = os.environ.get("DJANGO_FIELD_ENCRYPTION_KEY")
    if env_key:
        return env_key.encode()

    path = _key_path()
    if path.exists():
        key = path.read_text().strip()
        if not key:
            raise RuntimeError(f"Field encryption key file is empty: {path}")
        return key.encode()

    # First run: generate and persist beside the database on /data.
    key = Fernet.generate_key()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(key)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return key


def get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        with _lock:
            if _fernet is None:
                _fernet = Fernet(_load_or_create_key())
    return _fernet


class EncryptedFieldMixin:
    """Encrypt on the way to the DB, decrypt on the way out.

    Note: encryption is non-deterministic, so these fields cannot be used in
    exact-match query filters — only read/written as attributes.
    """

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if value is None or value == "":
            return value
        return get_fernet().encrypt(str(value).encode()).decode()

    def from_db_value(self, value, expression, connection):
        if value is None or value == "":
            return value
        try:
            return get_fernet().decrypt(value.encode()).decode()
        except (InvalidToken, ValueError):
            # Not a valid token — legacy plaintext written before encryption.
            return value


class EncryptedCharField(EncryptedFieldMixin, models.CharField):
    pass


class EncryptedTextField(EncryptedFieldMixin, models.TextField):
    pass
