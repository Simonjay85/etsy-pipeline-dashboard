#!/usr/bin/env python3
from __future__ import annotations

import logging
import unittest
from collections import deque
from typing import Deque
from unittest.mock import patch

from fastapi.testclient import TestClient

import dashboard_app


class _InMemoryLogHandler(logging.Handler):
    def __enter__(self):
        self.logger = logging.getLogger("etsy_dashboard")
        self.original_level = self.logger.level
        self.logger.setLevel(logging.INFO)
        self.logger.addHandler(self)
        return self

    def __init__(self):
        super().__init__()
        self.records: Deque[logging.LogRecord] = deque()

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def __exit__(self, exc_type, exc_value, tb):
        self.logger.removeHandler(self)
        self.logger.setLevel(self.original_level)


def _security_headers(host: str = "127.0.0.1:8090", origin: str | None = None, token: str | None = None):
    headers = {
        "Host": host,
    }
    if origin is not None:
        headers["Origin"] = origin
    if token is not None:
        headers[dashboard_app._DASHBOARD_MUTATION_TOKEN_HEADER] = token
    return headers


class DashboardSecurityBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(dashboard_app.app, base_url="http://127.0.0.1:8090")
        self.token = "A2-test-token"
        self.token_patch = patch.object(dashboard_app, "_DASHBOARD_MUTATION_TOKEN", self.token)
        self.token_patch.start()

    def tearDown(self):
        self.token_patch.stop()

    def test_loopback_host_is_accepted_for_readonly_request(self):
        response = self.client.get(
            "/api/services",
            headers=_security_headers(origin="http://127.0.0.1:8090"),
        )
        self.assertEqual(200, response.status_code)

    def test_hostile_host_is_rejected(self):
        response = self.client.get(
            "/api/services",
            headers=_security_headers(host="evil.local", origin="http://127.0.0.1:8090"),
        )
        self.assertEqual(403, response.status_code)

    def test_same_origin_read_request_is_accepted(self):
        with patch.object(dashboard_app, "products_from_excel", return_value=[]):
            response = self.client.get(
                "/api/products",
                headers=_security_headers(origin="http://127.0.0.1:8090"),
            )
        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertIn("products", payload)

    def test_hostile_origin_is_rejected_even_for_read_only_request(self):
        response = self.client.get(
            "/api/services",
            headers=_security_headers(origin="http://evil.example"),
        )
        self.assertEqual(403, response.status_code)

    def test_mutation_without_token_is_rejected(self):
        response = self.client.patch(
            "/api/products/7",
            headers=_security_headers(origin="http://127.0.0.1:8090"),
        )
        self.assertEqual(403, response.status_code)

    def test_mutation_with_invalid_token_is_rejected(self):
        response = self.client.post(
            "/api/products/7/reset-status",
            headers=_security_headers(origin="http://127.0.0.1:8090", token="wrong-token"),
        )
        self.assertEqual(403, response.status_code)

    def test_mutation_with_valid_token_reaches_mutation_handler(self):
        with patch.object(dashboard_app, "save_to_excel") as save_to_excel:
            response = self.client.post(
                "/api/products/7/reset-status",
                headers=_security_headers(origin="http://127.0.0.1:8090", token=self.token),
            )
            self.assertEqual(200, response.status_code)
            self.assertTrue(save_to_excel.called)

    def test_options_preflight_does_not_return_wildcard_cors(self):
        response = self.client.options(
            "/api/products/7/reset-status",
            headers={
                "Host": "127.0.0.1:8090",
                "Origin": "http://127.0.0.1:8090",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": dashboard_app._DASHBOARD_MUTATION_TOKEN_HEADER,
            },
        )
        self.assertIn(response.status_code, (200, 204, 405))
        self.assertNotEqual(
            "*",
            response.headers.get("access-control-allow-origin"),
        )

    def test_token_not_present_in_service_response_or_request_logs(self):
        with _InMemoryLogHandler() as capture:
            response = self.client.get(
                "/api/services",
                headers=_security_headers(origin="http://127.0.0.1:8090"),
            )
        self.assertEqual(200, response.status_code)
        payload = response.text
        self.assertNotIn(self.token, payload)
        self.assertNotIn(self.token, "\n".join(rec.getMessage() for rec in capture.records))
