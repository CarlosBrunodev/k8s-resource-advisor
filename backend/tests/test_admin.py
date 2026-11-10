import base64
import json
import os
import tempfile
import unittest
from dataclasses import replace

from app import admin
from app.cluster import KubeClient
from tests.fakes import FakeKubeApi, make_settings


class AdminCredentialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.api = FakeKubeApi()

    def tearDown(self):
        self.api.close()
        self.tmp.cleanup()

    def kube(self):
        return KubeClient(base_url=self.api.url, token_file="/nonexistent", namespace="resource-advisor")

    def test_secret_is_created_on_first_boot_and_reused_afterwards(self):
        settings = make_settings()
        first = admin.load_or_create(settings, kube=self.kube())
        self.assertEqual(first.admin_user, "passuser")
        self.assertTrue(first.admin_password)

        created = next(c for c in self.api.calls if c["method"] == "POST")
        self.assertEqual(created["path"], "/api/v1/namespaces/resource-advisor/secrets")
        self.assertEqual(created["body"]["stringData"]["username"], "passuser")
        self.assertEqual(created["body"]["stringData"]["password"], first.admin_password)

        # segundo boot: o Secret já existe e a senha é a mesma (não gira a cada reinício)
        second = admin.load_or_create(settings, kube=self.kube())
        self.assertEqual(second.admin_password, first.admin_password)
        self.assertEqual(sum(1 for c in self.api.calls if c["method"] == "POST"), 1)

    def test_existing_secret_with_other_user_is_honoured(self):
        self.api.secrets["resource-advisor-admin"] = {"data": {
            "username": base64.b64encode(b"ops").decode(),
            "password": base64.b64encode(b"segredo-antigo").decode(),
        }}
        loaded = admin.load_or_create(make_settings(), kube=self.kube())
        self.assertEqual((loaded.admin_user, loaded.admin_password), ("ops", "segredo-antigo"))

    def test_secret_without_keys_disables_editing(self):
        self.api.secrets["resource-advisor-admin"] = {"data": {}}
        loaded = admin.load_or_create(make_settings(), kube=self.kube())
        self.assertIsNone(loaded.admin_password)

    def test_api_unreachable_disables_editing_without_crashing(self):
        unreachable = KubeClient(base_url="http://127.0.0.1:1", token_file="/nonexistent", namespace="ns")
        loaded = admin.load_or_create(make_settings(), kube=unreachable)
        self.assertIsNone(loaded.admin_password)

    def test_file_is_created_once_outside_the_cluster(self):
        path = os.path.join(self.tmp.name, "admin.json")
        settings = replace(make_settings(), admin_file=path)
        first = admin.load_or_create(settings)
        self.assertTrue(os.path.exists(path))
        with open(path, encoding="utf-8") as fh:
            stored = json.load(fh)
        self.assertEqual((stored["username"], stored["password"]), ("passuser", first.admin_password))
        second = admin.load_or_create(settings)
        self.assertEqual(second.admin_password, first.admin_password)

    def test_without_secret_or_file_editing_is_disabled(self):
        self.assertIsNone(admin.load_or_create(make_settings()).admin_password)


if __name__ == "__main__":
    unittest.main()
