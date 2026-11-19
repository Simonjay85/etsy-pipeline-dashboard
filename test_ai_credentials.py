import builtins
import ctypes
import importlib.util
import json
import sys
import types
from unittest.mock import MagicMock

import pytest

import ai_credentials


API_KEY = "sk-test-secret-do-not-leak"
BASE_URL = "https://provider.invalid/private-endpoint"
MODEL = "provider/private-model"
BACKEND_ERROR = "backend failure containing sk-backend-secret"

VALID_CONFIG = {
    "api_key": API_KEY,
    "base_url": BASE_URL,
    "model": MODEL,
}

LIMITS = {
    "api_key": 8192,
    "base_url": 2048,
    "model": 256,
}


class MockBackend:
    """In-memory keyring backend that records every adapter interaction."""

    def __init__(self, stored=None):
        self.stored = stored
        self.calls = []
        self.read_results = []
        self.read_error = None
        self.set_error = None
        self.delete_error = None
        self.keep_after_delete = False

    def get_password(self, service, account):
        self.calls.append(("get_password", service, account))
        if self.read_error is not None:
            raise self.read_error
        if self.read_results:
            return self.read_results.pop(0)
        return self.stored

    def set_password(self, service, account, password):
        self.calls.append(("set_password", service, account, password))
        if self.set_error is not None:
            raise self.set_error
        self.stored = password

    def delete_password(self, service, account):
        self.calls.append(("delete_password", service, account))
        if self.delete_error is not None:
            raise self.delete_error
        if not self.keep_after_delete:
            self.stored = None


class MockNativeAPI:
    """Minimal keyring.backends.macOS.api double for SecItem calls."""

    def __init__(self, update_status=0, add_status=0):
        self.error = types.SimpleNamespace(item_not_found=-25300)
        self._sec = types.SimpleNamespace(
            SecItemUpdate=MagicMock(return_value=update_status)
        )
        self._found = types.SimpleNamespace(
            CFDataCreate=MagicMock(return_value=100),
            CFRelease=MagicMock(),
        )
        self.k_ = MagicMock(side_effect=lambda name: f"native:{name}")
        self.create_query = MagicMock(
            side_effect=lambda **kwargs: MagicMock(**kwargs)
        )
        self.SecItemAdd = MagicMock(return_value=add_status)
        self.Error = types.SimpleNamespace(raise_for_status=MagicMock())


@pytest.fixture
def backend(monkeypatch):
    mock_backend = MockBackend()
    monkeypatch.setattr(ai_credentials, "_backend", lambda: mock_backend)
    return mock_backend


def _payload(**overrides):
    payload = {"version": 1, **VALID_CONFIG}
    payload.update(overrides)
    return payload


def _assert_safe_error(exc_info, *secrets):
    error = exc_info.value
    assert isinstance(error, ai_credentials.CredentialStoreError)
    assert str(error)
    assert error.__cause__ is None
    assert all(secret not in str(error) for secret in secrets)


def test_backend_rejects_non_macos_without_trying_a_fallback(monkeypatch):
    monkeypatch.setattr(ai_credentials.sys, "platform", "linux")
    original_import = builtins.__import__

    def reject_keyring_imports(name, *args, **kwargs):
        if name == "keyring" or name.startswith("keyring."):
            raise AssertionError("a non-macOS backend fallback was attempted")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_keyring_imports)

    with pytest.raises(ai_credentials.CredentialStoreError) as exc_info:
        ai_credentials._backend()

    _assert_safe_error(exc_info)


def test_backend_uses_only_the_explicit_native_macos_keyring(monkeypatch):
    monkeypatch.setattr(ai_credentials.sys, "platform", "darwin")
    native_instances = []
    native_api = MockNativeAPI()

    class FakeNativeKeyring:
        def __init__(self):
            native_instances.append(self)

    keyring_module = types.ModuleType("keyring")
    keyring_module.__path__ = []
    keyring_module.get_keyring = lambda: pytest.fail("generic keyring fallback was used")

    backends_module = types.ModuleType("keyring.backends")
    backends_module.__path__ = []
    macos_module = types.ModuleType("keyring.backends.macOS")
    macos_module.Keyring = FakeNativeKeyring
    macos_module.api = native_api

    keyring_module.backends = backends_module
    backends_module.macOS = macos_module
    monkeypatch.setitem(sys.modules, "keyring", keyring_module)
    monkeypatch.setitem(sys.modules, "keyring.backends", backends_module)
    monkeypatch.setitem(sys.modules, "keyring.backends.macOS", macos_module)

    result = ai_credentials._backend()

    assert isinstance(result, ai_credentials._UpdatingMacOSBackend)
    assert result.backend is native_instances[0]
    assert result.api is native_api
    assert len(native_instances) == 1


def test_backend_import_failure_is_a_safe_error_without_fallback(monkeypatch):
    monkeypatch.setattr(ai_credentials.sys, "platform", "darwin")
    original_import = builtins.__import__

    def fail_native_import(name, *args, **kwargs):
        if name == "keyring.backends.macOS":
            raise ImportError(f"missing native backend {BACKEND_ERROR}")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_native_import)

    with pytest.raises(ai_credentials.CredentialStoreError) as exc_info:
        ai_credentials._backend()

    _assert_safe_error(exc_info, BACKEND_ERROR)


def test_backend_runtime_failure_is_sanitized(monkeypatch):
    monkeypatch.setattr(ai_credentials.sys, "platform", "darwin")

    class FailingNativeKeyring:
        def __init__(self):
            raise RuntimeError(BACKEND_ERROR)

    macos_module = types.ModuleType("keyring.backends.macOS")
    macos_module.Keyring = FailingNativeKeyring
    macos_module.api = MockNativeAPI()
    monkeypatch.setitem(sys.modules, "keyring.backends.macOS", macos_module)

    with pytest.raises(ai_credentials.CredentialStoreError) as exc_info:
        ai_credentials._backend()

    _assert_safe_error(exc_info, BACKEND_ERROR)


def test_module_import_does_not_access_keychain(monkeypatch):
    imported_keyring_modules = []
    original_import = builtins.__import__

    def reject_keyring_imports(name, *args, **kwargs):
        if name == "keyring" or name.startswith("keyring."):
            imported_keyring_modules.append(name)
            raise AssertionError("module import accessed Keychain dependencies")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_keyring_imports)
    module_name = "ai_credentials_import_probe"
    module_path = ai_credentials.__file__
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None
    assert spec.loader is not None
    imported_module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, imported_module)

    spec.loader.exec_module(imported_module)

    assert imported_keyring_modules == []
    assert imported_module.SERVICE == ai_credentials.SERVICE
    assert imported_module.ACCOUNT == ai_credentials.ACCOUNT


def test_updating_backend_delegates_get_and_delete_to_native_keyring():
    native_backend = MagicMock()
    native_backend.get_password.return_value = "stored-record"
    wrapper = ai_credentials._UpdatingMacOSBackend(native_backend, MockNativeAPI())

    assert wrapper.get_password(ai_credentials.SERVICE, ai_credentials.ACCOUNT) == (
        "stored-record"
    )
    wrapper.delete_password(ai_credentials.SERVICE, ai_credentials.ACCOUNT)

    native_backend.get_password.assert_called_once_with(
        ai_credentials.SERVICE, ai_credentials.ACCOUNT
    )
    native_backend.delete_password.assert_called_once_with(
        ai_credentials.SERVICE, ai_credentials.ACCOUNT
    )


def test_updating_backend_existing_item_uses_update_without_add_or_delete():
    native_backend = MagicMock()
    api = MockNativeAPI(update_status=0)
    wrapper = ai_credentials._UpdatingMacOSBackend(native_backend, api)

    wrapper.set_password(ai_credentials.SERVICE, ai_credentials.ACCOUNT, API_KEY)

    api._sec.SecItemUpdate.assert_called_once()
    api.SecItemAdd.assert_not_called()
    native_backend.delete_password.assert_not_called()
    api.Error.raise_for_status.assert_called_once_with(0)

    data_call = api._found.CFDataCreate.call_args
    assert data_call.args[0] is None
    assert isinstance(data_call.args[1], ctypes.c_char_p)
    assert data_call.args[1].value == API_KEY.encode("utf-8")
    assert data_call.args[2] == len(API_KEY.encode("utf-8"))
    attrs = api.create_query.call_args_list[1].kwargs
    assert isinstance(attrs["kSecValueData"], ctypes.c_void_p)
    assert attrs["kSecValueData"].value == 100


def test_updating_backend_not_found_adds_once_after_update():
    native_backend = MagicMock()
    api = MockNativeAPI(update_status=-25300, add_status=0)
    wrapper = ai_credentials._UpdatingMacOSBackend(native_backend, api)

    wrapper.set_password(ai_credentials.SERVICE, ai_credentials.ACCOUNT, API_KEY)

    api._sec.SecItemUpdate.assert_called_once()
    api.SecItemAdd.assert_called_once()
    api.Error.raise_for_status.assert_called_once_with(0)
    native_backend.delete_password.assert_not_called()
    assert api.create_query.call_count == 3


def test_updating_backend_update_error_raises_without_fallback_add():
    native_backend = MagicMock()
    api = MockNativeAPI(update_status=-50)
    api.Error.raise_for_status.side_effect = RuntimeError(BACKEND_ERROR)
    wrapper = ai_credentials._UpdatingMacOSBackend(native_backend, api)

    with pytest.raises(RuntimeError, match="sk-backend-secret"):
        wrapper.set_password(ai_credentials.SERVICE, ai_credentials.ACCOUNT, API_KEY)

    api._sec.SecItemUpdate.assert_called_once()
    api.SecItemAdd.assert_not_called()


def test_updating_backend_duplicate_after_not_found_retries_update_once():
    native_backend = MagicMock()
    api = MockNativeAPI(update_status=-25300, add_status=-25299)
    api._sec.SecItemUpdate.side_effect = [-25300, 0]
    wrapper = ai_credentials._UpdatingMacOSBackend(native_backend, api)
    wrapper.set_password(ai_credentials.SERVICE, ai_credentials.ACCOUNT, API_KEY)
    assert api._sec.SecItemUpdate.call_count == 2
    api.SecItemAdd.assert_called_once()
    api.Error.raise_for_status.assert_called_once_with(0)
    native_backend.delete_password.assert_not_called()
    native_backend.delete_password.assert_not_called()


def test_load_missing_config_returns_none_and_does_not_create_a_record(backend):
    assert ai_credentials.load_config() is None

    assert backend.calls == [("get_password", ai_credentials.SERVICE, ai_credentials.ACCOUNT)]
    assert backend.stored is None


def test_load_valid_config_returns_the_three_trimmed_fields(backend):
    backend.stored = json.dumps(
        {
            "version": 1,
            "api_key": f"  {API_KEY}  ",
            "base_url": f"  {BASE_URL}  ",
            "model": f"  {MODEL}  ",
        }
    )

    assert ai_credentials.load_config() == VALID_CONFIG


@pytest.mark.parametrize(
    "raw",
    [
        "not-json",
        json.dumps(["not", "an", "object"]),
        json.dumps(_payload(version=2)),
        json.dumps(_payload(version="1")),
        json.dumps(_payload(version=None)),
    ],
)
def test_load_rejects_malformed_or_wrong_version_records_safely(backend, raw):
    backend.stored = raw

    with pytest.raises(ai_credentials.CredentialStoreError) as exc_info:
        ai_credentials.load_config()

    _assert_safe_error(exc_info, API_KEY, BASE_URL, MODEL)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("api_key", None),
        ("api_key", [API_KEY]),
        ("base_url", 123),
        ("base_url", {"url": BASE_URL}),
        ("model", False),
        ("model", [MODEL]),
        ("api_key", ""),
        ("base_url", "   "),
        ("model", ""),
    ],
)
def test_load_rejects_wrong_types_and_empty_values_safely(backend, field, value):
    backend.stored = json.dumps(_payload(**{field: value}))

    with pytest.raises(ai_credentials.CredentialStoreError) as exc_info:
        ai_credentials.load_config()

    _assert_safe_error(exc_info, API_KEY, BASE_URL, MODEL)


@pytest.mark.parametrize("field, limit", LIMITS.items())
def test_load_rejects_values_over_each_schema_limit(backend, field, limit):
    backend.stored = json.dumps(_payload(**{field: "x" * (limit + 1)}))

    with pytest.raises(ai_credentials.CredentialStoreError) as exc_info:
        ai_credentials.load_config()

    _assert_safe_error(exc_info, API_KEY, BASE_URL, MODEL)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("api_key", None),
        ("base_url", []),
        ("model", ""),
    ],
)
def test_save_rejects_invalid_schema_before_backend_access(backend, field, value):
    config = {**VALID_CONFIG, field: value}

    with pytest.raises(ai_credentials.CredentialStoreError) as exc_info:
        ai_credentials.save_config(config)

    _assert_safe_error(exc_info, API_KEY, BASE_URL, MODEL)
    assert backend.calls == []


@pytest.mark.parametrize("field, limit", LIMITS.items())
def test_save_rejects_values_over_each_schema_limit_before_backend_access(
    backend, field, limit
):
    config = {**VALID_CONFIG, field: "x" * (limit + 1)}

    with pytest.raises(ai_credentials.CredentialStoreError) as exc_info:
        ai_credentials.save_config(config)

    _assert_safe_error(exc_info, API_KEY, BASE_URL, MODEL)
    assert backend.calls == []


def test_save_stores_one_versioned_tuple_and_verifies_readback(backend):
    ai_credentials.save_config(VALID_CONFIG)

    assert len(backend.calls) == 2
    assert backend.calls[0][:3] == (
        "set_password",
        ai_credentials.SERVICE,
        ai_credentials.ACCOUNT,
    )
    encoded = backend.calls[0][3]
    assert json.loads(encoded) == {"version": 1, **VALID_CONFIG}
    assert backend.calls[1] == (
        "get_password",
        ai_credentials.SERVICE,
        ai_credentials.ACCOUNT,
    )

    assert ai_credentials.load_config() == VALID_CONFIG
    assert backend.calls[-1] == (
        "get_password",
        ai_credentials.SERVICE,
        ai_credentials.ACCOUNT,
    )


def test_save_set_failure_is_sanitized(backend):
    backend.set_error = RuntimeError(BACKEND_ERROR)

    with pytest.raises(ai_credentials.CredentialStoreError) as exc_info:
        ai_credentials.save_config(VALID_CONFIG)

    _assert_safe_error(exc_info, BACKEND_ERROR, API_KEY, BASE_URL, MODEL)
    assert [call[0] for call in backend.calls] == ["set_password"]


def test_save_readback_mismatch_is_sanitized(backend):
    backend.read_results = [json.dumps(_payload(api_key="different-secret"))]

    with pytest.raises(ai_credentials.CredentialStoreError) as exc_info:
        ai_credentials.save_config(VALID_CONFIG)

    _assert_safe_error(exc_info, API_KEY, BASE_URL, MODEL)
    assert [call[0] for call in backend.calls] == ["set_password", "get_password"]


def test_load_read_failure_is_sanitized(backend):
    backend.read_error = RuntimeError(BACKEND_ERROR)

    with pytest.raises(ai_credentials.CredentialStoreError) as exc_info:
        ai_credentials.load_config()

    _assert_safe_error(exc_info, BACKEND_ERROR, API_KEY, BASE_URL, MODEL)


def test_delete_existing_config_and_verify_readback(backend):
    backend.stored = json.dumps(_payload())

    ai_credentials.delete_config()

    assert backend.stored is None
    assert backend.calls == [
        ("get_password", ai_credentials.SERVICE, ai_credentials.ACCOUNT),
        ("delete_password", ai_credentials.SERVICE, ai_credentials.ACCOUNT),
        ("get_password", ai_credentials.SERVICE, ai_credentials.ACCOUNT),
    ]


def test_delete_missing_config_is_idempotent_and_still_reads_back(backend):
    ai_credentials.delete_config()
    ai_credentials.delete_config()

    assert backend.calls == [
        ("get_password", ai_credentials.SERVICE, ai_credentials.ACCOUNT),
        ("get_password", ai_credentials.SERVICE, ai_credentials.ACCOUNT),
        ("get_password", ai_credentials.SERVICE, ai_credentials.ACCOUNT),
        ("get_password", ai_credentials.SERVICE, ai_credentials.ACCOUNT),
    ]


def test_delete_failure_is_sanitized(backend):
    backend.stored = json.dumps(_payload())
    backend.delete_error = RuntimeError(BACKEND_ERROR)

    with pytest.raises(ai_credentials.CredentialStoreError) as exc_info:
        ai_credentials.delete_config()

    _assert_safe_error(exc_info, BACKEND_ERROR, API_KEY, BASE_URL, MODEL)
    assert [call[0] for call in backend.calls] == ["get_password", "delete_password"]


def test_delete_readback_failure_is_sanitized(backend):
    backend.stored = json.dumps(_payload())
    backend.keep_after_delete = True

    with pytest.raises(ai_credentials.CredentialStoreError) as exc_info:
        ai_credentials.delete_config()

    _assert_safe_error(exc_info, API_KEY, BASE_URL, MODEL)
    assert [call[0] for call in backend.calls] == [
        "get_password",
        "delete_password",
        "get_password",
    ]
