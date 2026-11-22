"""One provider config in macOS Keychain; no plaintext or backend fallback.

Secrets go directly to the native Security framework through keyring's explicit
macOS backend, never through command-line arguments, shell commands or files.
The API key, base URL and model travel together to avoid restoring a key against
the wrong provider. Importing this module does not access the user's Keychain.
"""

import hashlib
import json
from pathlib import Path
import sys


SERVICE = "com.etsy.dashboard.ai-provider"
ACCOUNT = hashlib.sha256(str(Path(__file__).resolve().parent).encode()).hexdigest()[:24]


class CredentialStoreError(RuntimeError):
    """Safe public error; never retain a backend error or credential payload."""

    def __init__(self, message, *, mutation_completed=False):
        super().__init__(message)
        self.mutation_completed = mutation_completed


class _UpdatingMacOSBackend:
    """Use SecItemUpdate instead of keyring 25's delete-then-add replacement."""

    def __init__(self, backend, api):
        self.backend = backend
        self.api = api

    def get_password(self, service, account):
        return self.backend.get_password(service, account)

    def delete_password(self, service, account):
        return self.backend.delete_password(service, account)

    def set_password(self, service, account, password):
        from ctypes import c_char_p, c_int32, c_long, c_void_p

        api = self.api
        update = api._sec.SecItemUpdate
        update.restype = c_int32
        update.argtypes = (c_void_p, c_void_p)
        release = api._found.CFRelease
        release.restype = None
        release.argtypes = (c_void_p,)
        identity = {"kSecClass": api.k_("kSecClassGenericPassword"),
                    "kSecAttrService": service, "kSecAttrAccount": account}
        # SecItemUpdate requires CFData, not the CFString used by keyring's
        # legacy add helper. Keep the bytes in memory, never in argv or files.
        create_data = api._found.CFDataCreate
        create_data.restype = c_void_p
        create_data.argtypes = (c_void_p, c_void_p, c_long)
        encoded = password.encode("utf-8")
        data = create_data(None, c_char_p(encoded), len(encoded))
        query = api.create_query(**identity)
        attrs = api.create_query(kSecValueData=c_void_p(data))
        try:
            status = update(query, attrs)
            if status == api.error.item_not_found:
                new_item = api.create_query(**identity, kSecValueData=c_void_p(data))
                try:
                    status = api.SecItemAdd(new_item, None)
                    if status == -25299:  # errSecDuplicateItem: another process added it.
                        status = update(query, attrs)
                finally:
                    release(new_item)
            api.Error.raise_for_status(status)
        finally:
            release(attrs)
            release(query)
            release(data)


def _backend():
    if sys.platform != "darwin":
        raise CredentialStoreError("Lưu key an toàn hiện yêu cầu macOS Keychain.")
    try:
        # Explicit backend: never auto-select a third-party plaintext fallback.
        from keyring.backends.macOS import Keyring, api
        return _UpdatingMacOSBackend(Keyring(), api)
    except (ImportError, RuntimeError):
        raise CredentialStoreError("Không tải được macOS Keychain; hãy kiểm tra thư viện keyring.") from None


def _validated_record(value):
    if not isinstance(value, dict) or value.get("version") != 1:
        raise CredentialStoreError("Cấu hình API trong Keychain không hợp lệ; hãy lưu lại key.")
    limits = {"api_key": 8192, "base_url": 2048, "model": 256}
    result = {}
    for field, limit in limits.items():
        item = value.get(field)
        if not isinstance(item, str) or not item.strip() or len(item) > limit:
            raise CredentialStoreError("Cấu hình API trong Keychain không hợp lệ; hãy lưu lại key.")
        result[field] = item.strip()
    return result


def _backend_error_types():
    """Known OS/library failures, loaded lazily for dependency-free unit tests."""
    standard = (OSError, RuntimeError, ValueError, TypeError)
    try:
        from keyring.errors import KeyringError
        from keyring.backends.macOS.api import Error
    except (ImportError, OSError, RuntimeError):
        return standard
    return (*standard, KeyringError, Error)


def load_config():
    backend = _backend()
    try:
        raw = backend.get_password(SERVICE, ACCOUNT)
    except _backend_error_types():
        raise CredentialStoreError("Không đọc được Keychain; hãy mở khoá Keychain và thử lại.") from None
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        raise CredentialStoreError("Cấu hình API trong Keychain không hợp lệ; hãy lưu lại key.") from None
    return _validated_record(value)


def save_config(config):
    record = _validated_record({**config, "version": 1})
    backend = _backend()
    encoded = json.dumps({"version": 1, **record}, separators=(",", ":"))
    try:
        backend.set_password(SERVICE, ACCOUNT, encoded)
    except _backend_error_types():
        raise CredentialStoreError("Không lưu được Keychain; hãy mở khoá Keychain và thử lại.") from None
    try:
        verified = backend.get_password(SERVICE, ACCOUNT)
    except _backend_error_types():
        raise CredentialStoreError(
            "Đã ghi cấu hình vào Keychain nhưng chưa xác minh được. Cấu hình mới đang dùng; hãy thử lưu lại.",
            mutation_completed=True,
        ) from None
    if verified != encoded:
        raise CredentialStoreError(
            "Không xác minh được key đã lưu trong Keychain; cấu hình mới đang dùng, hãy thử lưu lại.",
            mutation_completed=True,
        )


def delete_config():
    backend = _backend()
    try:
        if backend.get_password(SERVICE, ACCOUNT) is not None:
            backend.delete_password(SERVICE, ACCOUNT)
    except _backend_error_types():
        raise CredentialStoreError("Không xoá được key trong Keychain; hãy mở khoá Keychain và thử lại.") from None
    try:
        if backend.get_password(SERVICE, ACCOUNT) is not None:
            raise CredentialStoreError("Keychain chưa xác nhận xoá key; key runtime đã xoá, hãy thử lại.", mutation_completed=True)
    except CredentialStoreError:
        raise
    except _backend_error_types():
        raise CredentialStoreError(
            "Đã yêu cầu xoá Keychain nhưng chưa xác minh được; key runtime đã xoá, hãy thử lại.", mutation_completed=True,
        ) from None
