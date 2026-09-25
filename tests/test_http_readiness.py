"""Native readiness preserves body/authentication policy without project code."""

import base64
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import services
import configuration


class HTTPReadinessTests(unittest.TestCase):
    def test_body_and_auth_resolution(self):
        spec = {
            "port": 9393,
            "body": "OK",
            "trim_body": True,
            "basic_auth": {
                "username_env": "USER",
                "password_env": "PASSWORD",
                "optional": True,
                "trim": True,
            },
        }
        configuration.fields(
            {"http_get": spec}, configuration.TABLES["readiness"], "readiness"
        )
        public = services.http_readiness(spec)
        self.assertNotIn("headers", public)
        self.assertNotIn("headers", services.resolve_http_readiness(spec, {}))
        resolved = services.resolve_http_readiness(
            spec, {"USER": " user ", "PASSWORD": " password "}
        )
        self.assertEqual(
            resolved["headers"],
            {"Authorization": "Basic " + base64.b64encode(b"user:password").decode()},
        )
        self.assertEqual(resolved["body"], "OK")
        with self.assertRaises(ValueError):
            services.resolve_http_readiness(spec, {"USER": "user"})

    def test_header_errors_do_not_echo_secrets(self):
        spec = {"port": 8080, "headers_from_environment": {"Authorization": "AUTH"}}
        for env in ({}, {"AUTH": "private\r\nvalue"}, {"AUTH": "private" * 2000}):
            with self.assertRaises(ValueError) as error:
                services.resolve_http_readiness(spec, env)
            self.assertNotIn("private", str(error.exception))
        self.assertEqual(
            services.resolve_http_readiness(spec, {"AUTH": "Bearer token"})["headers"],
            {"Authorization": "Bearer token"},
        )

    def test_invalid_declarations(self):
        for fields in (
            {"body": "x" * 4097},
            {"trim_body": True},
            {"body": "OK", "trim_body": "yes"},
            {"headers_from_environment": {"Host": "HOST"}},
            {"basic_auth": {"username_env": "USER"}},
            {
                "basic_auth": {
                    "username_env": "USER",
                    "password_env": "PASS",
                    "optional": "yes",
                }
            },
            {
                "headers_from_environment": {
                    "Authorization": "AUTH",
                    "authorization": "OTHER",
                }
            },
        ):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                services.http_readiness({"port": 80, **fields})
