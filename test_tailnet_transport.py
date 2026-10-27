import json
import subprocess
import unittest
from unittest.mock import patch
import http_api


class TailnetDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.deadline = patch.object(http_api, '_tailnet_deadline', 0.0)
        self.cache = patch.object(http_api, '_tailnet_cache', {'self': frozenset(), 'peers': frozenset()})
        self.deadline.start()
        self.cache.start()
        self.addCleanup(self.deadline.stop)
        self.addCleanup(self.cache.stop)

    def test_only_daemon_reported_tailnet_ips_are_cached(self):
        body = {'BackendState': 'Running', 'Self': {'TailscaleIPs': ['100.100.64.1', '8.8.8.8']},
                'Peer': {'peer': {'TailscaleIPs': ['100.99.44.67', 'fd7a:115c:a1e0::1234', '192.168.1.2']}}}
        response = subprocess.CompletedProcess([], 0, json.dumps(body), '')
        with patch.object(http_api.subprocess, 'run', return_value=response) as run:
            result = http_api.tailscale_network()
            self.assertEqual(result['self'], {'100.100.64.1'})
            self.assertEqual(result['peers'], {'100.99.44.67', 'fd7a:115c:a1e0::1234'})
            self.assertEqual(http_api.tailscale_network(), result)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.kwargs['env']['TAILSCALE_BE_CLI'], '1')

    def test_stopped_malformed_and_unavailable_daemons_fail_closed(self):
        cases = [subprocess.CompletedProcess([], 0, '{"BackendState":"Stopped"}', ''),
                 subprocess.CompletedProcess([], 0, 'not json', ''),
                 subprocess.CompletedProcess([], 0, '[]', '')]
        for response in cases:
            with self.subTest(output=response.stdout), patch.object(http_api, '_tailnet_deadline', 0.0), \
                    patch.object(http_api.subprocess, 'run', return_value=response):
                self.assertFalse(http_api.tailscale_network()['peers'])
        with patch.object(http_api, '_tailnet_deadline', 0.0), \
                patch.object(http_api.subprocess, 'run', side_effect=subprocess.TimeoutExpired('tailscale', 2)):
            self.assertFalse(http_api.tailscale_network()['peers'])
