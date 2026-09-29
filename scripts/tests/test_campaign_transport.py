"""Changing the TCP destination must preserve SSH trust and completed evidence."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from release_campaign import Remote, validate_transport_resume


class CampaignTransportTests(unittest.TestCase):
    def test_changed_address_requires_previous_platform_qualification_and_reason(self):
        phases = ('workflows', 'download', 'assemble', 'prepare-vm', 'qualify-windows')
        state = {'phases': {name: [{'status': 'PASS'}] for name in phases}}
        validate_transport_resume({}, None, None)
        validate_transport_resume(state, 'new-address.invalid', 'VM address changed')
        with self.assertRaises(ValueError):
            validate_transport_resume(state, 'new-address.invalid', None)
        for name in phases:
            bad = {'phases': dict(state['phases'], **{name: [{'status': 'FAIL'}]})}
            with self.subTest(phase=name), self.assertRaises(ValueError):
                validate_transport_resume(bad, 'new-address.invalid', 'VM address changed')

    def test_socket_routes_to_new_address_but_ssh_checks_original_identity(self):
        client, transport = Mock(), Mock()
        paramiko = SimpleNamespace(SSHClient=Mock(return_value=client))
        config = {'host': 'trusted-host.invalid', 'user': 'fixture', 'ssh_key': 'fixture-key'}
        with patch.dict(sys.modules, paramiko=paramiko), \
                patch('release_campaign.socket.create_connection', return_value=transport) as connect:
            Remote(config, connect_host='new-address.invalid')
            connect.assert_called_once_with(('new-address.invalid', 22), timeout=15)
            self.assertEqual(client.connect.call_args.args, ('trusted-host.invalid',))
            self.assertIs(client.connect.call_args.kwargs['sock'], transport)
            client.set_missing_host_key_policy.assert_not_called()
            client.connect.side_effect = ValueError('host key mismatch')
            with self.assertRaises(ValueError):
                Remote(config, connect_host='new-address.invalid')
            transport.close.assert_called_once()
