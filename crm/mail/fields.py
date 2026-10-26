from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models

class EncryptedTextField(models.TextField):
    """Encrypt message content before it reaches the database driver."""
    prefix = "fernet1:"
    def _fernet(self):
        key=getattr(settings,"GMAIL_TOKEN_ENCRYPTION_KEY","")
        if not key: raise ImproperlyConfigured("GMAIL_TOKEN_ENCRYPTION_KEY is required to read or write scheduled email content.")
        try: return Fernet(key.encode("ascii"))
        except Exception as exc: raise ImproperlyConfigured("GMAIL_TOKEN_ENCRYPTION_KEY is invalid.") from exc
    def get_prep_value(self, value):
        if value is None: return None
        value=str(value)
        return self.prefix + self._fernet().encrypt(value.encode("utf-8")).decode("ascii")
    def from_db_value(self, value, expression, connection):
        if value is None: return value
        if not value.startswith(self.prefix): raise ImproperlyConfigured("Unencrypted scheduled email content was found; encrypt it before serving mail data.")
        try: return self._fernet().decrypt(value[len(self.prefix):].encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError) as exc: raise ImproperlyConfigured("Stored email content cannot be decrypted with the configured key.") from exc
    def to_python(self, value): return value
