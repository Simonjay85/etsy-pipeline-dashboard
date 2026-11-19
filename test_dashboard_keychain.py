"""Credential lifecycle tests: mocked Keychain, no real secrets or providers."""

import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import dashboard_app as app


class DashboardKeychainTests(unittest.TestCase):
    def setUp(self):
        self.record = None
        self.env = patch.dict(os.environ, {}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        for name in ("TOGETHER_API_KEY", "TOGETHER_API_BASE_URL", "TOGETHER_SEO_MODEL"):
            os.environ.pop(name, None)
        self.load = self._patch(app.ai_credentials, "load_config", side_effect=lambda: self.record)
        self.save = self._patch(app.ai_credentials, "save_config", side_effect=self._save)
        self.delete = self._patch(app.ai_credentials, "delete_config", side_effect=self._delete)
        self._patch(app, "_together_persisted_identity", new=None)
        self._patch(app, "_together_storage_error", new="")
        self._patch(app, "_invalidate_together_readiness")
        self.client = TestClient(app.app, base_url="http://127.0.0.1:8090")

    def _patch(self, obj, name, **kwargs):
        patcher = patch.object(obj, name, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def _save(self, config):
        self.record = dict(config)

    def _delete(self):
        self.record = None

    def _post(self, **kwargs):
        return self.client.post(
            "/api/together/settings",
            headers={"X-Dashboard-Mutation-Token": app._DASHBOARD_MUTATION_TOKEN},
            json={"base_url": "https://provider.test/v1", "model": "vendor/model", "persist": True, **kwargs},
        )

    def test_save_and_restore_after_fresh_environment_preserve_provider_tuple(self):
        response = self._post(api_key="secret-new-9876")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["persisted"])
        self.assertNotIn("secret-new-9876", response.text)
        for name in ("TOGETHER_API_KEY", "TOGETHER_API_BASE_URL", "TOGETHER_SEO_MODEL"):
            os.environ.pop(name, None)
        app._restore_together_config()
        self.assertEqual(app._together_runtime_api_key(), "secret-new-9876")
        self.assertEqual(app._together_env_base_url(), "https://provider.test/v1")
        self.assertEqual(app._together_env_model(), "vendor/model")
        self.assertTrue(app._together_public_config()["persisted"])

    def test_blank_key_can_persist_current_session_without_exposing_secret(self):
        os.environ["TOGETHER_API_KEY"] = "existing-runtime-1234"
        response = self._post(api_key="")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.record["api_key"], "existing-runtime-1234")
        self.assertTrue(response.json()["persisted"])

    def test_blank_key_without_current_key_does_not_claim_saved(self):
        self.assertEqual(self._post(api_key="").status_code, 400)
        self.save.assert_not_called()

    def test_runtime_only_override_does_not_claim_durable_and_keeps_stored_tuple(self):
        self._post(api_key="persisted-1234")
        response = self._post(api_key="temporary-4567", persist=False, base_url="https://other.test/v1")
        self.assertFalse(response.json()["persisted"])
        self.assertEqual(self.record["base_url"], "https://provider.test/v1")
        os.environ.pop("TOGETHER_API_KEY")
        app._restore_together_config()
        self.assertEqual(app._together_runtime_api_key(), "persisted-1234")
        self.assertEqual(app._together_env_base_url(), "https://provider.test/v1")

    def test_explicit_environment_override_not_mixed_with_saved_provider(self):
        self.record = {"api_key": "stored-1234", "base_url": "https://saved.test/v1", "model": "saved/model"}
        os.environ["TOGETHER_API_KEY"] = "environment-5678"
        os.environ["TOGETHER_API_BASE_URL"] = "https://env.test/v1"
        os.environ["TOGETHER_SEO_MODEL"] = "env/model"
        os.environ["ETSY_DASHBOARD_USE_ENV_TOGETHER"] = "1"
        app._restore_together_config()
        self.assertEqual(app._together_runtime_api_key(), "environment-5678")
        self.assertEqual(app._together_env_base_url(), "https://env.test/v1")
        self.assertFalse(app._together_public_config()["persisted"])

    def test_keychain_wins_over_stale_launchd_values_without_explicit_override(self):
        self.record = {"api_key": "stored-1234", "base_url": "https://saved.test/v1", "model": "saved/model"}
        os.environ["TOGETHER_API_KEY"] = "stale-5678"
        os.environ["TOGETHER_API_BASE_URL"] = "https://stale.test/v1"
        os.environ.pop("ETSY_DASHBOARD_USE_ENV_TOGETHER", None)
        app._restore_together_config()
        self.assertEqual(app._together_runtime_api_key(), "stored-1234")
        self.assertEqual(app._together_env_base_url(), "https://saved.test/v1")
        self.assertTrue(app._together_public_config()["persisted"])

    def test_committed_but_unverified_mutations_update_runtime_and_expose_uncertainty(self):
        for operation in ("save", "delete"):
            with self.subTest(operation=operation):
                os.environ["TOGETHER_API_KEY"] = "old-1234"
                backend = self.save if operation == "save" else self.delete
                old_effect = backend.side_effect
                backend.side_effect = app.ai_credentials.CredentialStoreError("Readback unverified", mutation_completed=True)
                response = self._post(api_key="new-9876") if operation == "save" else self._post(clear_key=True)
                self.assertEqual(response.status_code, 503)
                self.assertEqual(app._together_runtime_api_key(), "new-9876" if operation == "save" else "")
                self.assertFalse(app._together_public_config()["persisted"])
                self.assertEqual(app._together_public_config()["storage_error"], "Readback unverified")
                backend.side_effect = old_effect

    def test_clear_deletes_durable_key_even_for_legacy_client_and_does_not_resurrect(self):
        self._post(api_key="persisted-1234")
        response = self._post(clear_key=True, persist=False)
        self.assertEqual(response.status_code, 200)
        self.delete.assert_called_once()
        app._restore_together_config()
        self.assertFalse(app._together_public_config()["configured"])
        self.assertFalse(app._together_public_config()["persisted"])

    def test_failed_save_or_delete_preserves_runtime_and_returns_failure(self):
        for operation in ("save", "delete"):
            with self.subTest(operation=operation):
                os.environ["TOGETHER_API_KEY"] = "working-runtime-1234"
                backend = self.save if operation == "save" else self.delete
                old_effect = backend.side_effect
                backend.side_effect = app.ai_credentials.CredentialStoreError("Keychain unavailable")
                response = self._post(api_key="replacement-9876") if operation == "save" else self._post(clear_key=True)
                self.assertEqual(response.status_code, 503)
                self.assertEqual(app._together_runtime_api_key(), "working-runtime-1234")
                self.assertFalse(app._together_public_config()["persisted"])
                self.assertNotIn("replacement-9876", response.text)
                backend.side_effect = old_effect

    def test_keychain_failure_at_startup_is_nonfatal_visible_and_secret_safe(self):
        self.load.side_effect = app.ai_credentials.CredentialStoreError("private backend details")
        app._restore_together_config()
        state = app._together_public_config()
        self.assertFalse(state["configured"])
        self.assertTrue(state["storage_error"])
        self.assertNotIn("private backend details", str(state))

    def test_invalid_provider_from_keychain_is_not_loaded(self):
        self.record = {"api_key": "stored-1234", "base_url": "https://user:pass@provider.test", "model": "vendor/model"}
        app._restore_together_config()
        self.assertFalse(app._together_public_config()["configured"])
        self.assertTrue(app._together_public_config()["storage_error"])

    def test_validation_and_csrf_fail_before_keychain_access(self):
        self.assertEqual(self._post(api_key="new-1234", persist="yes").status_code, 400)
        self.assertEqual(self._post(api_key="x" * 8193).status_code, 400)
        self.assertEqual(self._post(api_key="new-1234", base_url="ftp://host").status_code, 400)
        response = self.client.post("/api/together/settings", json={"api_key": "new-1234", "persist": True})
        self.assertEqual(response.status_code, 403)
        self.save.assert_not_called()
        self.delete.assert_not_called()

    def test_short_key_hint_never_returns_entire_secret(self):
        self.assertEqual(app._together_key_hint("abc"), "")


if __name__ == "__main__":
    unittest.main()
