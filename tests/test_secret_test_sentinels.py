import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from autodispatch.model import Blocked
from autodispatch.safety import check_scope


class SecretTestSentinelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.task = SimpleNamespace(scope_write=('tests/**', 'src/**'))

    def tearDown(self):
        self.temp.cleanup()

    def scan(self, source, name='tests/contracts/test_wire_semantics.py'):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(source.encode('utf-8'))
        check_scope(self.task, [name], self.root)
        self.assertEqual(source.encode('utf-8'), path.read_bytes())

    def test_exact_negative_authorization_fixture_is_not_a_credential(self):
        source = (
            'def test_nested_permission_fields_rejected():\n'
            '    payload = {}\n'
            "    payload['headers'] = {'Authorization': 'fictional'}\n"
            '    with pytest.raises(ValidationError):\n'
            '        ContractCodec().decode("intent", payload)\n'
        )
        try:
            self.scan(source)
        except Blocked as exc:
            self.fail('Public negative-test sentinel was mistaken for a credential: ' + str(exc))

    def test_assignment_of_public_sentinel_inside_test_is_allowed(self):
        self.scan("def test_reject_key():\n    api_key = 'fictional'\n")

    def test_utf8_offsets_and_crlf_do_not_mask_another_field(self):
        self.scan("def test_reject_header():\r\n    payload = {'说明': '无效字段', 'Authorization': 'fictional'}\r\n")

    def test_real_looking_provider_token_stays_blocked_even_in_a_test(self):
        token = 'sk-' + 'a' * 28
        source = f"def test_reject_header():\n    payload = {{'Authorization': {token!r}}}\n"
        with self.assertRaises(Blocked):
            self.scan(source)

    def test_other_credential_on_same_line_is_not_exempted(self):
        source = "def test_reject_header():\n    payload = {'Authorization': 'fictional', 'password': 'a-plausible-secret-value'}\n"
        with self.assertRaises(Blocked):
            self.scan(source)

    def test_production_placeholder_is_still_blocked(self):
        with self.assertRaises(Blocked):
            self.scan("def test_name_is_not_authority():\n    payload = {'Authorization': 'fictional'}\n", 'src/client.py')

    def test_module_level_test_credential_is_still_blocked(self):
        with self.assertRaises(Blocked):
            self.scan("Authorization = 'fictional'\n")

    def test_non_test_helper_is_not_exempted(self):
        with self.assertRaises(Blocked):
            self.scan("def load_client():\n    payload = {'Authorization': 'fictional'}\n")

    def test_unknown_placeholder_does_not_get_a_blanket_test_exemption(self):
        with self.assertRaises(Blocked):
            self.scan("def test_client():\n    payload = {'Authorization': 'unreviewed-value'}\n")

    def test_parse_failure_falls_back_to_strict_scan(self):
        with self.assertRaises(Blocked):
            self.scan("def test_broken(:\n    payload = {'Authorization': 'fictional'}\n")


if __name__ == '__main__':
    unittest.main()
