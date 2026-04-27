"""SSH key parsing helpers for the optional SSH-tunnel to the Oracle EBS database.

Vendored from `component-elasticsearch/src/client/ssh_utils.py` so the writer
matches the canonical Keboola pattern used by other DB-targeting components.
"""

import base64
import binascii
import contextlib
from io import StringIO

import paramiko


class SSHException(Exception):
    """Raised when the user's SSH private key cannot be parsed."""


def get_private_key(input_key: str, private_key_password: str | None) -> paramiko.RSAKey:
    """Return a paramiko RSAKey from the user-supplied private key string.

    Accepts both raw PEM and base64-encoded PEM (Keboola's `ssh-editor` widget
    sometimes emits base64-encoded keys).
    """
    key = _get_decoded_key(input_key)
    try:
        if private_key_password:
            return paramiko.RSAKey.from_private_key(StringIO(key), password=private_key_password)
        return paramiko.RSAKey.from_private_key(StringIO(key))
    except paramiko.ssh_exception.SSHException as pkey_error:
        raise SSHException("Invalid private key") from pkey_error


def _get_decoded_key(input_key: str) -> str:
    """Return a usable PEM string, accepting raw or base64-encoded input."""
    b64_decoded_input_key = ""
    with contextlib.suppress(binascii.Error):
        b64_decoded_input_key = base64.b64decode(input_key, validate=True).decode("utf-8")

    is_valid_b64, message_b64 = _validate_ssh_private_key(b64_decoded_input_key)
    is_valid, message = _validate_ssh_private_key(input_key)
    if is_valid_b64:
        return b64_decoded_input_key
    if is_valid:
        return input_key
    raise SSHException("\n".join([message, message_b64]))


def _validate_ssh_private_key(ssh_private_key: str) -> tuple[bool, str]:
    if "\n" not in ssh_private_key:
        return False, "SSH Private key is invalid; make sure it has \\n characters as new lines."
    return True, ""
