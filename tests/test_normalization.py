import unittest
from backend.config.config import parse_bool
from backend.functions.normalization import normalize_payload, normalize_hostname, normalize_port


class NormalizationTests(unittest.TestCase):
  def test_invalid_hostname_labels_and_ports(self):
    for name in ('bad-.example.com', 'good.-bad.com', 'bad..com', 'x;include /etc/passwd', '-bad.com'):
      with self.assertRaises(ValueError):
        normalize_hostname(name, 'invalid')
    for value in (True, False, '80', 80.5, 0, 65536):
      with self.assertRaises(ValueError):
        normalize_port(value, 'invalid')

  def test_partial_update_and_nested_domain_payload(self):
    current = dict(name='example.com', type='hostname', enabled=True, status='active',
             server_names=['example.com', 'www.example.com'],
             route=dict(upstream_host='127.0.0.1', upstream_port=3000, upstream_scheme='http'))
    payload = normalize_payload(dict(upstream_port=4000), existing=current)
    self.assertEqual(payload['route']['upstream_port'], 4000)
    self.assertEqual(payload['route']['upstream_host'], '127.0.0.1')
    payload = normalize_payload(dict(domain=dict(name='example.com'), upstream_host='::1', upstream_port=3000))
    self.assertEqual(payload['domain']['name'], 'example.com')
    self.assertEqual(payload['route']['upstream_host'], '::1')

  def test_boolean_settings(self):
    self.assertFalse(parse_bool('0'))
    self.assertFalse(parse_bool('false'))
    self.assertTrue(parse_bool('1'))
    with self.assertRaises(ValueError):
      parse_bool('maybe')
