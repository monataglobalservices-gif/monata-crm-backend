import hashlib
import os
import secrets
from cryptography.fernet import Fernet
from django.conf import settings
from rest_framework.exceptions import APIException

def random_secret():
    return secrets.token_urlsafe(40)

def digest_secret(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

def encrypt_token(value):
    key = getattr(settings, "GMAIL_TOKEN_ENCRYPTION_KEY", "")
    if not key:
        raise APIException("Gmail token encryption is not configured.")
    try:
        return Fernet(key.encode("ascii")).encrypt(value.encode("utf-8")).decode("ascii")
    except Exception as exc:
        raise APIException("Gmail token encryption key is invalid.") from exc

def decrypt_token(value):
    key = getattr(settings, "GMAIL_TOKEN_ENCRYPTION_KEY", "")
    if not key:
        raise APIException("Gmail token encryption is not configured.")
    try:
        return Fernet(key.encode("ascii")).decrypt(value.encode("ascii")).decode("utf-8")
    except Exception as exc:
        raise APIException("Gmail token could not be decrypted.") from exc
