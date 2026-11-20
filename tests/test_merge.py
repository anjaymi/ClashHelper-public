from contextlib import redirect_stdout
from copy import deepcopy
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
import yaml

from helper import (MergeError, fetch_nodes, filter_nodes, merge_config, read_nodes,
                    remove_invalid_dependencies, traffic_info)
from scripts.prepare_sites import parse_sites


def node(name='香港', server='example.com', port=443):
    return dict(name=name, server=server, port=port, type='ss',
                cipher='aes-128-gcm', password='fake-test-password')


class MergeTests(unittest.TestCase):
    def test_subscription_requests_meta_format_instead_of_legacy_placeholder(self):
        site = parse_sites('https://example.com/sub?token=private-test-token', '')[0]
        legacy = Mock(text=yaml.safe_dump({'proxies': [node('老版本Clash不受支持'), node('请更换基于ClashMeta的软件')]}), headers={})
        meta = Mock(text=yaml.safe_dump({'proxies': [node('HK'), node('Japan'), node('暂时离线', 'offline.invalid')]}), headers={})
        meta.raise_for_status.return_value = None
        legacy.raise_for_status.return_value = None
        template = yaml.safe_load(Path('template.yaml').read_text(encoding='utf-8'))
        report = {}
        def serve(url, **kwargs):
            return meta if kwargs.get('headers', {}).get('User-Agent') == 'clash.meta' else legacy
        with tempfile.TemporaryDirectory() as folder, patch('helper.requests.get', side_effect=serve) as request, redirect_stdout(io.StringIO()):
            result = merge_config(template, [site], folder, report)
            cache = next(Path(folder).glob('*.yaml'))
            self.assertEqual(cache.read_text(encoding='utf-8'), meta.text)
        self.assertEqual([item['name'] for item in result['proxies']],
                         ['[订阅1] HK', '[订阅1] Japan', '[订阅1] 暂时离线'])
        self.assertEqual(request.call_count, 1)
        self.assertEqual(report['sources'][0]['request_user_agent'], 'clash.meta')
        self.assertEqual(report['sources'][0]['received_nodes'], 3)
        self.assertIsNone(report['sources'][0]['compatibility_warning'])
        self.assertNotIn('private-test-token', str(report))

    def test_provider_compatibility_notices_are_labelled_and_still_listed(self):
        notices = [node('老版本Clash不受支持'), node('请更换基于ClashMeta的软件')]
        report = {}
        result = read_nodes(yaml.safe_dump({'proxies': notices}), report)
        self.assertEqual(len(result), 2)
        self.assertEqual(report['received_nodes'], 2)
        self.assertEqual(report['compatibility_notice_count'], 2)
        self.assertTrue(all(entry['provider_notice'] for entry in report['node_entries']))
        self.assertEqual(report['invalid_nodes'], 0)
        self.assertIn('可能不是完整节点列表', report['compatibility_warning'])

    def test_normal_node_names_with_meta_label_are_not_compatibility_notices(self):
        report = {}
        result = read_nodes(yaml.safe_dump({'proxies': [node('香港 ClashMeta'), node('暂时失效')]}), report)
        self.assertEqual(len(result), 2)
        self.assertEqual(report['compatibility_notice_count'], 0)
        self.assertIsNone(report['compatibility_warning'])

    def test_default_merge_keeps_all_nodes_without_probing_node_servers(self):
        raw = [node('暂时离线', 'offline.invalid'), node('官网'),
               node('剩余流量 1 GB'), node('到期时间 2020-01-01'),
               node('same'), node('same')]
        sites = parse_sites('https://example.com/sub', '')
        self.assertEqual(sites[0]['exclusion'], [])
        response = Mock(text=yaml.safe_dump({'proxies': raw}),
                        headers={'subscription-userinfo': 'expire=1; total=1; download=2'})
        response.raise_for_status.return_value = None
        template = yaml.safe_load(Path('template.yaml').read_text(encoding='utf-8'))
        report = {}
        with tempfile.TemporaryDirectory() as folder, patch('helper.requests.get', return_value=response) as request, patch('helper.socket.getaddrinfo', side_effect=AssertionError('must not resolve node servers')) as dns, redirect_stdout(io.StringIO()):
            result = merge_config(template, sites, folder, report)
        self.assertEqual(len(result['proxies']), len(raw))
        self.assertEqual([item['server'] for item in result['proxies']], [item['server'] for item in raw])
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args, (sites[0]['url'],))
        dns.assert_not_called()
        source = report['sources'][0]
        self.assertEqual(source['received_nodes'], len(raw))
        self.assertEqual([entry['name'] for entry in source['node_entries']], [item['name'] for item in raw])
        self.assertEqual([entry['merged_name'] for entry in source['node_entries']], [item['name'] for item in result['proxies']])
        self.assertEqual(source['connectivity'], 'not_tested')
        self.assertEqual(source['invalid_nodes'], 0)
        self.assertEqual(source['filtered_nodes'], 0)
        self.assertNotIn('fake-test-password', str(report))

    def test_full_listing_includes_configuration_errors_without_credentials(self):
        raw = [node('offline', 'offline.invalid'), node('bad-port', port=-1),
               {'name': 'missing-fields', 'password': 'private-test-password'},
               'https://example.com/private?token=private-test-token', None]
        report = {}
        valid = read_nodes(yaml.safe_dump({'proxies': raw}), report)
        self.assertEqual([item['name'] for item in valid], ['offline'])
        self.assertEqual(report['received_nodes'], len(raw))
        self.assertEqual(len(report['node_entries']), len(raw))
        self.assertEqual([entry['name'] for entry in report['node_entries']],
                         ['offline', 'bad-port', 'missing-fields', '第 4 项', '第 5 项'])
        self.assertEqual([entry['status'] for entry in report['node_entries']],
                         ['included'] + ['configuration_error'] * 4)
        self.assertNotIn('private-test-password', str(report))
        self.assertNotIn('private-test-token', str(report))
        self.assertNotIn('offline.invalid', str(report))

    def test_explicit_advanced_filters_keep_filtered_entries_in_listing(self):
        sites = parse_sites('https://example.com/sub', '')
        sites[0].update(inclusion=['HK'], exclusion=['官网'])
        response = Mock(text=yaml.safe_dump({'proxies': [node('HK'), node('HK 官网'), node('Japan')]}), headers={})
        response.raise_for_status.return_value = None
        template = yaml.safe_load(Path('template.yaml').read_text(encoding='utf-8'))
        report = {}
        with tempfile.TemporaryDirectory() as folder, patch('helper.requests.get', return_value=response), redirect_stdout(io.StringIO()):
            result = merge_config(template, sites, folder, report)
        self.assertEqual(len(result['proxies']), 1)
        source = report['sources'][0]
        self.assertEqual(source['received_nodes'], 3)
        self.assertEqual(source['filtered_nodes'], 2)
        self.assertEqual(source['invalid_nodes'], 0)
        self.assertEqual([entry['status'] for entry in source['node_entries']], ['included', 'filtered', 'filtered'])

    def test_failed_cache_parse_does_not_replace_current_subscription_listing(self):
        site = parse_sites('https://example.com/sub', '')[0]
        response = Mock(text=yaml.safe_dump({'proxies': [node('current', port=-1)]}), headers={})
        response.raise_for_status.return_value = None
        template = yaml.safe_load(Path('template.yaml').read_text(encoding='utf-8'))
        with tempfile.TemporaryDirectory() as folder, redirect_stdout(io.StringIO()):
            from hashlib import sha256
            cache = Path(folder) / (sha256(site['url'].encode()).hexdigest() + '.yaml')
            cache.write_text(yaml.safe_dump({'proxies': [node('cached', port=-1)]}), encoding='utf-8')
            report = {}
            with patch('helper.requests.get', return_value=response):
                with self.assertRaises(MergeError):
                    merge_config(template, [site], folder, report)
            self.assertEqual(report['sources'][0]['node_entries'][0]['name'], 'current')
            self.assertEqual(report['sources'][0]['received_nodes'], 1)

    def test_duplicate_names_validate_each_occurrence_and_keep_usable_upstream(self):
        healthy, broken, relay = node('same'), node('same'), node('relay')
        broken['dialer-proxy'] = 'missing'
        relay['dialer-proxy'] = 'same'
        template = yaml.safe_load(Path('template.yaml').read_text(encoding='utf-8'))
        sites = parse_sites('https://example.com/sub', '')
        for raw in ([healthy, broken, relay], [broken, healthy, relay]):
            with self.subTest(broken_first=raw[0] is broken):
                response = Mock(text=yaml.safe_dump({'proxies': raw}), headers={})
                response.raise_for_status.return_value = None
                report = {}
                with tempfile.TemporaryDirectory() as folder, patch('helper.requests.get', return_value=response), redirect_stdout(io.StringIO()):
                    result = merge_config(template, sites, folder, report)
                self.assertEqual([item['name'] for item in result['proxies']],
                                 ['[订阅1] same', '[订阅1] relay'])
                self.assertEqual(result['proxies'][1]['dialer-proxy'], '[订阅1] same')
                self.assertEqual(report['sources'][0]['invalid_nodes'], 1)
                self.assertEqual(report['status'], 'partial')
        self.assertEqual(broken['dialer-proxy'], 'missing')
        self.assertEqual(relay['dialer-proxy'], 'same')

    def test_cyclic_duplicate_does_not_discard_healthy_duplicate_or_dependents(self):
        cycle_a, cycle_b, healthy, relay = node('same'), node('cycle'), node('same'), node('relay')
        cycle_a['dialer-proxy'], cycle_b['dialer-proxy'] = 'cycle', 'same'
        relay['dialer-proxy'] = 'same'
        for raw in ([cycle_a, cycle_b, healthy, relay], [relay, cycle_b, cycle_a, healthy]):
            with self.subTest(relay_first=raw[0] is relay):
                valid, skipped = remove_invalid_dependencies(raw, {'DIRECT'})
                self.assertEqual(valid, [item for item in raw if item is healthy or item is relay])
                self.assertEqual(skipped, 2)

    def test_valid_duplicate_names_are_preserved_and_references_match_first_survivor(self):
        first, second, relay = node('same'), node('same', port=8443), node('relay')
        second['dialer-proxy'] = 'DIRECT'
        relay['dialer-proxy'] = 'same'
        response = Mock(text=yaml.safe_dump({'proxies': [first, second, relay]}), headers={})
        response.raise_for_status.return_value = None
        sites = parse_sites('https://example.com/sub', '')
        template = yaml.safe_load(Path('template.yaml').read_text(encoding='utf-8'))
        report = {}
        with tempfile.TemporaryDirectory() as folder, patch('helper.requests.get', return_value=response), redirect_stdout(io.StringIO()):
            result = merge_config(template, sites, folder, report)
        self.assertEqual([item['name'] for item in result['proxies']],
                         ['[订阅1] same', '[订阅1] same (2)', '[订阅1] relay'])
        self.assertEqual(result['proxies'][1]['dialer-proxy'], 'DIRECT')
        self.assertEqual(result['proxies'][2]['dialer-proxy'], '[订阅1] same')
        self.assertEqual(report['sources'][0]['invalid_nodes'], 0)

    def test_duplicate_dependency_checks_also_apply_when_using_cache(self):
        broken, healthy, relay = node('same'), node('same'), node('relay')
        broken['dialer-proxy'] = 'missing'
        relay['dialer-proxy'] = 'same'
        response = Mock(text=yaml.safe_dump({'proxies': [broken, healthy, relay]}), headers={})
        response.raise_for_status.return_value = None
        sites = parse_sites('https://example.com/sub', '')
        template = yaml.safe_load(Path('template.yaml').read_text(encoding='utf-8'))
        with tempfile.TemporaryDirectory() as folder, redirect_stdout(io.StringIO()):
            with patch('helper.requests.get', return_value=response):
                expected = merge_config(template, sites, folder)
            cache = next(Path(folder).glob('*.yaml'))
            saved = cache.read_bytes()
            report = {}
            with patch('helper.requests.get', side_effect=requests.ConnectionError()):
                result = merge_config(template, sites, folder, report)
            self.assertEqual(result, expected)
            self.assertEqual(len(result['proxies']), 2)
            self.assertEqual(result['proxies'][1]['dialer-proxy'], result['proxies'][0]['name'])
            self.assertEqual(report['sources'][0]['status'], 'cached')
            self.assertEqual(report['sources'][0]['invalid_nodes'], 1)
            self.assertEqual(cache.read_bytes(), saved)

    def test_mixed_invalid_nodes_keep_valid_nodes_and_normalize_numeric_ports(self):
        missing = node('missing-server')
        del missing['server']
        bad_reference = node('bad-reference')
        bad_reference['dialer-proxy'] = ['invalid-reference']
        bad_password = node('bad-password')
        bad_password['password'] = ['invalid-password']
        raw = [node('healthy'), 'https://example.com/invalid?token=private-test-token',
               None, node('bad-port', port=0), missing, bad_reference, bad_password,
               node('numeric-port', port='8443')]
        report = {}
        result = read_nodes(yaml.safe_dump({'proxies': raw}), report)
        self.assertEqual([item['name'] for item in result], ['healthy', 'numeric-port'])
        self.assertEqual(result[1]['port'], 8443)
        self.assertEqual(raw[-1]['port'], '8443')
        self.assertEqual(report['received_nodes'], 8)
        self.assertEqual(report['invalid_nodes'], 6)
        self.assertNotIn('private-test-token', str(report))

    def test_mixed_subscription_fetches_once_and_keeps_metrics_in_cache(self):
        site = parse_sites('https://example.com/sub', '')[0]
        response = Mock(text=yaml.safe_dump({'proxies': [node('healthy'), node('bad', port=True)]}), headers={})
        response.raise_for_status.return_value = None
        with tempfile.TemporaryDirectory() as folder:
            report = {}
            with patch('helper.requests.get', return_value=response) as request:
                result = fetch_nodes(site, folder, report)
                self.assertEqual(request.call_count, 1)
            self.assertEqual(len(result), 1)
            self.assertEqual(report['invalid_nodes'], 1)
            self.assertEqual(report['status'], 'ok')
            cached_report = {}
            with patch('helper.requests.get', side_effect=requests.ConnectionError()), redirect_stdout(io.StringIO()):
                cached = fetch_nodes(site, folder, cached_report)
            self.assertEqual(cached, result)
            self.assertEqual(cached_report['invalid_nodes'], 1)
            self.assertEqual(cached_report['status'], 'cached')

    def test_all_invalid_response_preserves_last_valid_cache(self):
        site = parse_sites('https://example.com/sub', '')[0]
        good = Mock(text=yaml.safe_dump({'proxies': [node()]}), headers={})
        good.raise_for_status.return_value = None
        bad = Mock(text=yaml.safe_dump({'proxies': [node(port=70000)]}), headers={})
        bad.raise_for_status.return_value = None
        with tempfile.TemporaryDirectory() as folder:
            with patch('helper.requests.get', return_value=good):
                expected = fetch_nodes(site, folder)
            cache = next(Path(folder).glob('*.yaml'))
            content = cache.read_bytes()
            report = {}
            with patch('helper.requests.get', return_value=bad), redirect_stdout(io.StringIO()):
                result = fetch_nodes(site, folder, report)
            self.assertEqual(result, expected)
            self.assertEqual(report['status'], 'cached')
            self.assertEqual(cache.read_bytes(), content)
            self.assertEqual(report['invalid_nodes'], 0)
            self.assertEqual(report['node_entries'][0]['name'], '香港')
            self.assertEqual(report['latest_listing']['received_nodes'], 1)
            self.assertEqual(report['latest_listing']['node_entries'][0]['status'], 'configuration_error')

    def test_unusable_dependencies_preserve_cache_and_other_sources(self):
        sites = parse_sites('https://example.com/1\nhttps://example.com/2', '')
        template = yaml.safe_load(Path('template.yaml').read_text(encoding='utf-8'))
        good = Mock(text=yaml.safe_dump({'proxies': [node('cached-healthy')]}), headers={})
        good.raise_for_status.return_value = None
        missing = node('missing-upstream')
        missing['dialer-proxy'] = 'discarded-node'
        cycle_a, cycle_b = node('cycle-a'), node('cycle-b')
        cycle_a['dialer-proxy'], cycle_b['dialer-proxy'] = 'cycle-b', 'cycle-a'
        bad = Mock(text=yaml.safe_dump({'proxies': [missing, cycle_a, cycle_b]}), headers={})
        bad.raise_for_status.return_value = None
        other = Mock(text=yaml.safe_dump({'proxies': [node('other-healthy')]}), headers={})
        other.raise_for_status.return_value = None
        with tempfile.TemporaryDirectory() as folder, redirect_stdout(io.StringIO()):
            with patch('helper.requests.get', return_value=good):
                merge_config(template, sites[:1], folder)
            cache = next(Path(folder).glob('*.yaml'))
            saved = cache.read_bytes()
            report = {}
            with patch('helper.requests.get', side_effect=lambda url, **kwargs: bad if url == sites[0]['url'] else other):
                config = merge_config(template, sites, folder, report)
            self.assertEqual([item['name'] for item in config['proxies']],
                             ['[订阅1] cached-healthy', '[订阅2] other-healthy'])
            self.assertEqual(cache.read_bytes(), saved)
            self.assertEqual(report['sources'][0]['status'], 'cached')
            self.assertIn('节点依赖无效', report['sources'][0]['message'])
            self.assertEqual(report['sources'][1]['status'], 'ok')
            self.assertEqual(report['status'], 'partial')

    def test_filtered_empty_response_uses_cache_that_matches_current_filter(self):
        site = parse_sites('https://example.com/sub', '')[0]
        site['inclusion'] = ['HK']
        template = yaml.safe_load(Path('template.yaml').read_text(encoding='utf-8'))
        good = Mock(text=yaml.safe_dump({'proxies': [node('HK healthy')]}), headers={})
        good.raise_for_status.return_value = None
        bad = Mock(text=yaml.safe_dump({'proxies': [node('Japan')]}), headers={})
        bad.raise_for_status.return_value = None
        with tempfile.TemporaryDirectory() as folder, redirect_stdout(io.StringIO()):
            with patch('helper.requests.get', return_value=good):
                expected = merge_config(template, [site], folder)
            cache = next(Path(folder).glob('*.yaml'))
            saved = cache.read_bytes()
            report = {}
            with patch('helper.requests.get', return_value=bad):
                result = merge_config(template, [site], folder, report)
            self.assertEqual(result, expected)
            self.assertEqual(cache.read_bytes(), saved)
            self.assertEqual(report['sources'][0]['status'], 'cached')
            # A cached subscription must still satisfy newly changed filters.
            site['inclusion'] = ['Singapore']
            with patch('helper.requests.get', side_effect=requests.ConnectionError()):
                with self.assertRaises(MergeError):
                    merge_config(template, [site], folder)
            self.assertEqual(cache.read_bytes(), saved)

    def test_bad_nodes_and_their_dependents_do_not_block_independent_nodes(self):
        good = node('healthy')
        bad = node('bad')
        del bad['port']
        first, second = node('first'), node('second')
        first['dialer-proxy'], second['dialer-proxy'] = 'bad', 'first'
        cycle_a, cycle_b = node('cycle-a'), node('cycle-b')
        cycle_a['dialer-proxy'], cycle_b['dialer-proxy'] = 'cycle-b', 'cycle-a'
        relay = node('valid-relay')
        relay['dialer-proxy'] = 'healthy'
        response = Mock(text=yaml.safe_dump({'proxies': [good, bad, first, second, cycle_a, cycle_b, relay]}), headers={})
        response.raise_for_status.return_value = None
        sites = parse_sites('https://example.com/sub', '')
        template = yaml.safe_load(Path('template.yaml').read_text(encoding='utf-8'))
        report = {}
        with tempfile.TemporaryDirectory() as folder, patch('helper.requests.get', return_value=response), redirect_stdout(io.StringIO()):
            result = merge_config(template, sites, folder, report)
        self.assertEqual([item['name'] for item in result['proxies']], ['[订阅1] healthy', '[订阅1] valid-relay'])
        self.assertEqual(result['proxies'][1]['dialer-proxy'], '[订阅1] healthy')
        self.assertEqual(report['sources'][0]['invalid_nodes'], 5)
        self.assertEqual(report['sources'][0]['nodes'], 2)
        self.assertEqual(report['sources'][0]['received_nodes'], 7)
        entries = report['sources'][0]['node_entries']
        self.assertEqual(len(entries), 7)
        self.assertEqual(sum(entry['status'] == 'configuration_error' for entry in entries), 5)
        self.assertEqual(report['status'], 'partial')

    def test_all_invalid_nodes_are_reported(self):
        report = {}
        with self.assertRaises(MergeError):
            read_nodes(yaml.safe_dump({'proxies': [None, node(port=-1)]}), report)
        self.assertEqual(report['invalid_nodes'], 2)
        self.assertEqual(report['received_nodes'], 2)

    def test_traffic_header_handles_missing_and_unknown_values(self):
        self.assertIsNone(traffic_info(''))
        traffic = traffic_info('upload=100; download=200; total=1000; expire=1800000000; token=private')
        self.assertEqual(traffic['remaining'], 700)
        self.assertEqual(traffic['expire'], 1800000000)
        self.assertNotIn('token', traffic)

    def test_plain_urls_and_duplicate_subscription(self):
        sites = parse_sites('# comment\nhttps://example.com/sub?token=secret\n'
                            'https://example.org/sub\nhttps://example.com/sub?token=secret', '')
        self.assertEqual(len(sites), 2)
        self.assertEqual(sites[0]['group'], 'PROXY')
        self.assertFalse(sites[0]['dedup'])

    def test_invalid_config_and_non_clash_response(self):
        for text in ('vmess://invalid', 'javascript:alert(1)'):
            with self.assertRaises(ValueError):
                parse_sites(text, '')
        for text in ('c3M6Ly9leGFtcGxl', 'proxies: []', 'proxies: [{name: bad}]'):
            with self.assertRaises(MergeError):
                read_nodes(text)

    def test_same_names_preserved_and_template_not_mutated(self):
        sites = parse_sites('https://example.com/1\nhttps://example.com/2', '')
        template = yaml.safe_load(Path('template.yaml').read_text(encoding='utf-8'))
        original = deepcopy(template)
        with patch('helper.fetch_nodes', return_value=[node()]), redirect_stdout(io.StringIO()):
            config = merge_config(template, sites)
        names = [n['name'] for n in config['proxies']]
        self.assertEqual(len(set(names)), 2)
        self.assertEqual(template, original)
        groups = {g['name']: g for g in config['proxy-groups']}
        self.assertTrue(set(names).issubset(groups['PROXY']['proxies']))
        self.assertEqual(set(names), set(groups['AUTO']['proxies']))

    def test_failed_request_uses_cache_but_changed_url_does_not(self):
        sites = parse_sites('https://example.com/1\nhttps://example.com/2', '')
        response = Mock(text=yaml.safe_dump({'proxies': [node()]}), headers={})
        response.raise_for_status.return_value = None
        with tempfile.TemporaryDirectory() as folder:
            with patch('helper.requests.get', return_value=response):
                fetched = fetch_nodes(sites[0], folder)
            with patch('helper.requests.get', side_effect=requests.ConnectionError('secret-url')):
                with redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(fetch_nodes(sites[0], folder), fetched)
                self.assertNotIn('secret-url', output.getvalue())
                with self.assertRaises(MergeError):
                    fetch_nodes(sites[1], folder)

    def test_case_insensitive_filter_and_dns_failure_keeps_node(self):
        site = dict(inclusion=['HK'], exclusion=['官网'], dedup=True)
        nodes = [node('HK 1'), node('HK 官网'), node('Japan')]
        with patch('helper.socket.getaddrinfo', side_effect=OSError()):
            self.assertEqual([n['name'] for n in filter_nodes(nodes, site)], ['HK 1'])

    def test_unavailable_source_is_reported_and_valid_source_is_kept(self):
        with self.assertRaises(MergeError):
            filter_nodes([node()], dict(inclusion=['does-not-match']))
        template = dict(proxies=[], **{'proxy-groups': [dict(name='PROXY', proxies=[])]})
        sites = parse_sites('https://example.com/1\nhttps://example.com/2', '')
        report = {}
        with patch('helper.fetch_nodes', side_effect=[[node()], MergeError('failed')]), redirect_stdout(io.StringIO()):
            config = merge_config(template, sites, report=report)
        self.assertEqual(len(config['proxies']), 1)
        self.assertEqual(report['status'], 'partial')
        self.assertEqual(report['sources'][1]['status'], 'failed')
        self.assertEqual(template['proxies'], [])

    def test_all_sources_failed_prevents_publication(self):
        template = dict(proxies=[], **{'proxy-groups': [dict(name='PROXY', proxies=[])]})
        sites = parse_sites('https://example.com/1', '')
        with patch('helper.fetch_nodes', side_effect=MergeError('failed')), redirect_stdout(io.StringIO()):
            with self.assertRaises(MergeError):
                merge_config(template, sites)


    def test_service_groups_follow_merged_ai_and_us_nodes(self):
        raw = [
            node('🇺🇸 AI专用节点（已禁流媒体）'),
            node('🇺🇸【US】 丨  DMIT EB - ExampleOne'),
            node('🇺🇸【US】 丨  美西三网优化（无AI分流）'),
            node('香港普通节点'),
        ]
        template = yaml.safe_load(Path('template.yaml').read_text(encoding='utf-8'))
        sites = parse_sites('\n'.join(f'https://example.com/{i}' for i in range(4)), '')
        responses = [Mock(text=yaml.safe_dump({'proxies': items}), headers={})
                     for items in ([node('香港')], [node('香港')], [node('香港')], raw)]
        with tempfile.TemporaryDirectory() as folder, patch('helper.requests.get', side_effect=responses), redirect_stdout(io.StringIO()):
            result = merge_config(template, sites, folder, {})
        groups = {group['name']: group for group in result['proxy-groups']}
        self.assertEqual(groups['ChatGPT']['proxies'][:2], [
            '[订阅4] 🇺🇸 AI专用节点（已禁流媒体）',
            '[订阅4] 🇺🇸【US】 丨  DMIT EB - ExampleOne',
        ])
        self.assertEqual(set(groups['ChatGPT']['proxies']),
                         {item['name'] for item in result['proxies']})
        self.assertEqual(groups['US-Sub4']['proxies'], [
            '[订阅4] 🇺🇸【US】 丨  DMIT EB - ExampleOne',
            '[订阅4] 🇺🇸【US】 丨  美西三网优化（无AI分流）',
        ])

    def test_common_service_groups_receive_all_merged_nodes(self):
        raw = [node('香港节点'), node('美国节点')]
        template = yaml.safe_load(Path('template.yaml').read_text(encoding='utf-8'))
        sites = parse_sites('https://example.com/sub', '')
        response = Mock(text=yaml.safe_dump({'proxies': raw}), headers={})
        response.raise_for_status.return_value = None
        with tempfile.TemporaryDirectory() as folder, patch('helper.requests.get', return_value=response), redirect_stdout(io.StringIO()):
            result = merge_config(template, sites, folder, {})
        groups = {group['name']: group for group in result['proxy-groups']}
        merged = [item['name'] for item in result['proxies']]
        for name in ('Google-All', 'GitHub', 'Docker', 'Microsoft', 'OneDrive',
                     'Telegram', 'Discord', 'Social', 'Media', 'Games', 'Arena', 'Mirasim'):
            self.assertEqual(groups[name]['proxies'], merged)
        for name in ('Gemini', 'Google'):
            self.assertEqual(groups[name]['proxies'], ['Google-All'] + merged)

class RoutingTests(unittest.TestCase):
    def merged(self, sources, *, source_names=None, without_shared=False, openai_default=None):
        template = yaml.safe_load(Path('template.yaml').read_text(encoding='utf-8'))
        if openai_default is not None:
            group = next(group for group in template['proxy-groups'] if group['name'] == 'OpenAI-Sub4')
            group['default-selected'] = openai_default
        if without_shared:
            template['proxy-groups'] = [group for group in template['proxy-groups']
                                        if group['name'] != 'Google-All']
        sites = parse_sites('\n'.join(f'https://example.com/{i}' for i in range(len(sources))), '')
        if source_names:
            for site, name in zip(sites, source_names):
                site['name'] = name
        with patch('helper.fetch_nodes', side_effect=sources), redirect_stdout(io.StringIO()):
            return merge_config(template, sites)

    def test_google_shared_switch_and_independent_overrides(self):
        result = self.merged([[node('香港'), node('新加坡 Gemini')]])
        groups = {group['name']: group for group in result['proxy-groups']}
        names = [item['name'] for item in result['proxies']]
        self.assertEqual(groups['Google-All']['proxies'], names)
        for service in ('Gemini', 'Google'):
            self.assertEqual(groups[service]['proxies'][0], 'Google-All')
            self.assertEqual(set(groups[service]['proxies']), {'Google-All', *names})
            self.assertNotIn('ChatGPT', groups[service]['proxies'])

        def selected_exit(group, selections):
            while group in groups:
                group = selections.get(group, groups[group]['proxies'][0])
            return group

        for shared_exit in names:
            selections = {'Google-All': shared_exit}
            self.assertEqual(selected_exit('Gemini', selections), shared_exit)
            self.assertEqual(selected_exit('Google', selections), shared_exit)
        self.assertEqual(selected_exit('Gemini', {'Google-All': names[1], 'Gemini': names[0]}), names[0])
        self.assertEqual(selected_exit('Google', {'Google-All': names[1], 'Gemini': names[0]}), names[1])
        self.assertEqual(selected_exit('Gemini', {'Google-All': names[1], 'Google': names[0]}), names[1])

    def test_github_prioritizes_subscription_three_and_keeps_all_nodes(self):
        result = self.merged([
            [node('香港')], [node('日本')], [node('IEPL 香港')], [node('美国')],
        ])
        groups = {group['name']: group for group in result['proxy-groups']}
        names = [item['name'] for item in result['proxies']]
        self.assertEqual(groups['GitHub']['proxies'][0], '[订阅3] IEPL 香港')
        self.assertEqual(set(groups['GitHub']['proxies']), set(names))

    def test_gemini_candidates_keep_order_without_losing_other_nodes(self):
        result = self.merged([[node('香港'), node('新加坡 Gemini'), node('DMIT EB'), node('日本')]])
        groups = {group['name']: group for group in result['proxy-groups']}
        self.assertEqual(groups['Gemini']['proxies'], [
            'Google-All', '[订阅1] 新加坡 Gemini', '[订阅1] DMIT EB', '[订阅1] 香港', '[订阅1] 日本',
        ])

    def test_ai_candidates_do_not_prioritize_negative_labels(self):
        for label in ('美国 不支持GPT', '美国 不支持 GPT DMIT EB', 'GPT unavailable',
                      'no GPT', 'not supported Codex', 'Gemini不可用 GPT',
                      '美国 无AI分流 DMIT EB'):
            with self.subTest(label=label):
                result = self.merged([[node(label), node('DMIT EB'), node('新加坡 Gemini'), node('香港')]])
                groups = {group['name']: group for group in result['proxy-groups']}
                names = {item['name'] for item in result['proxies']}
                self.assertEqual(groups['ChatGPT']['proxies'][0], '[订阅1] DMIT EB')
                self.assertEqual(groups['Gemini']['proxies'][:3],
                                 ['Google-All', '[订阅1] 新加坡 Gemini', '[订阅1] DMIT EB'])
                self.assertEqual(set(groups['ChatGPT']['proxies']), names)
                self.assertEqual(set(groups['Gemini']['proxies']), names | {'Google-All'})

    def test_chatgpt_without_ai_labels_stays_manual_and_keeps_all_nodes(self):
        result = self.merged([[node('香港'), node('日本')]])
        groups = {group['name']: group for group in result['proxy-groups']}
        self.assertEqual(groups['ChatGPT']['type'], 'select')
        self.assertEqual(groups['ChatGPT']['proxies'], ['[订阅1] 香港', '[订阅1] 日本'])
        self.assertNotIn('AUTO', groups['ChatGPT']['proxies'])

    def test_source_name_is_not_an_ai_node_capability_hint(self):
        result = self.merged([[node('香港'), node('GPT 美国'), node('Gemini 新加坡')]],
                             source_names=['GPT Google机场'])
        groups = {group['name']: group for group in result['proxy-groups']}
        self.assertEqual(groups['ChatGPT']['proxies'][0], '[GPT Google机场] GPT 美国')
        self.assertEqual(groups['Gemini']['proxies'][:3],
                         ['Google-All', '[GPT Google机场] GPT 美国', '[GPT Google机场] Gemini 新加坡'])

    def test_openai_sub4_ranks_candidates_and_keeps_every_source4_node(self):
        raw = [node('德国 普通'), node('US 普通'), node('US DMIT EB'),
               node('美国 AI 专用（已禁流媒体）'), node('US DMIT EB'),
               node('美国 无AI分流 DMIT EB'), node('香港'), node('美国 到期时间'),
               node('Codex 专线')]
        result = self.merged([
            [node('[订阅4] 美国 AI专用 伪装来源')], [node('美国')], [node('美国')], raw,
            [node('美国 AI专用')],
        ], source_names=['订阅4', '二', '三', '香港自定义机场', '五'])
        groups = {group['name']: group for group in result['proxy-groups']}
        group = groups['OpenAI-Sub4']
        self.assertEqual(group['proxies'], [
            '[香港自定义机场] 美国 AI 专用（已禁流媒体）',
            '[香港自定义机场] US DMIT EB', '[香港自定义机场] US DMIT EB (2)',
            '[香港自定义机场] Codex 专线', '[香港自定义机场] US 普通',
            '[香港自定义机场] 德国 普通',
            '[香港自定义机场] 美国 无AI分流 DMIT EB',
            '[香港自定义机场] 香港', '[香港自定义机场] 美国 到期时间',
            '[订阅4] [订阅4] 美国 AI专用 伪装来源', '[五] 美国 AI专用',
            '[二] 美国', '[三] 美国',
        ])
        self.assertEqual(group['type'], 'select')
        self.assertEqual(group['interval'], 0)
        self.assertEqual(group['default-selected'], group['proxies'][0])
        all_names = {item['name'] for item in result['proxies']}
        self.assertEqual(len(all_names), len(raw) + 4)
        self.assertEqual(set(group['proxies']), all_names)
        self.assertEqual(set(groups['ChatGPT']['proxies']), all_names)
        self.assertEqual(set(groups['AUTO']['proxies']), all_names)
        self.assertIn('[香港自定义机场] 美国 无AI分流 DMIT EB', all_names)
        self.assertIn('[香港自定义机场] 美国 到期时间', all_names)
        self.assertTrue(all_names.issubset(groups['PROXY']['proxies']))

    def test_openai_group_keeps_other_subscriptions_when_source4_is_absent_or_failed(self):
        cases = (
            ([[node('美国 AI专用')]], ['[订阅1] 美国 AI专用']),
            ([[node('美国 AI专用')], [node('香港')], [node('德国')], MergeError('failed')],
             ['[订阅1] 美国 AI专用', '[订阅2] 香港', '[订阅3] 德国']),
        )
        for sources, expected in cases:
            with self.subTest(sources=len(sources)):
                result = self.merged(sources)
                group = next(group for group in result['proxy-groups'] if group['name'] == 'OpenAI-Sub4')
                self.assertEqual(group['proxies'], expected)
                self.assertEqual(group['default-selected'], expected[0])

    def test_openai_default_is_kept_when_it_belongs_to_any_merged_node(self):
        raw = [node('🇺🇸 AI专用节点（已禁流媒体）'),
               node('🇺🇸【US】 丨  DMIT EB - ExampleOne'), node('US DMIT EB - ExampleTwo')]
        sources = [[node('US DMIT EB - ExampleOne')], [node('香港')], [node('美国')], raw]
        result = self.merged(sources,
                             openai_default='[订阅4] 🇺🇸【US】 丨  DMIT EB - ExampleOne')
        group = next(group for group in result['proxy-groups'] if group['name'] == 'OpenAI-Sub4')
        self.assertEqual(group['default-selected'], '[订阅4] 🇺🇸【US】 丨  DMIT EB - ExampleOne')
        self.assertEqual(group['default-selected'], group['proxies'][0])

        result = self.merged(sources, source_names=['一', '二', '三', '自定义四'],
                             openai_default='[自定义四] US DMIT EB - ExampleTwo')
        group = next(group for group in result['proxy-groups'] if group['name'] == 'OpenAI-Sub4')
        self.assertEqual(group['default-selected'], '[自定义四] US DMIT EB - ExampleTwo')
        self.assertEqual(group['proxies'][0], group['default-selected'])

        result = self.merged(sources, openai_default='[订阅1] US DMIT EB - ExampleOne')
        group = next(group for group in result['proxy-groups'] if group['name'] == 'OpenAI-Sub4')
        self.assertEqual(group['default-selected'], '[订阅1] US DMIT EB - ExampleOne')

    def test_openai_default_matches_provider_suffix(self):
        raw = [node('🇺🇸 AI专用节点'), node('🇺🇸【US】 丨  DMIT EB - ExampleOne（动态ATT IPV6）')]
        result = self.merged([[node('香港')], [node('日本')], [node('德国')], raw],
                             openai_default='[订阅4] 🇺🇸【US】 丨  DMIT EB - ExampleOne')
        group = next(group for group in result['proxy-groups'] if group['name'] == 'OpenAI-Sub4')
        self.assertEqual(group['default-selected'],
                         '[订阅4] 🇺🇸【US】 丨  DMIT EB - ExampleOne（动态ATT IPV6）')
        self.assertEqual(group['proxies'][0], group['default-selected'])

    def test_openai_default_uses_ranked_nodes_without_personal_preference(self):
        result = self.merged([[node('香港')], [node('示例线路B')], [node('德国')], [node('美国 AI专用')]])
        group = next(group for group in result['proxy-groups'] if group['name'] == 'OpenAI-Sub4')
        self.assertEqual(group['default-selected'], '[订阅4] 美国 AI专用')
        self.assertEqual(group['proxies'][0], group['default-selected'])

    def test_openai_sub4_keeps_negative_ai_hints_available_for_manual_testing(self):
        for label in ('US AI 专用 不支持 GPT', '美国 GPT unavailable',
                      'US DMIT EB no GPT', '美国 无AI分流 DMIT EB'):
            with self.subTest(label=label):
                result = self.merged([[node('美国 AI专用')], [node('香港')], [node('美国')],
                                      [node(label), node('美国 DMIT EB')]])
                groups = {group['name']: group for group in result['proxy-groups']}
                self.assertEqual(groups['OpenAI-Sub4']['proxies'][0], '[订阅4] 美国 DMIT EB')
                self.assertEqual(set(groups['OpenAI-Sub4']['proxies']),
                                 {item['name'] for item in result['proxies']})
                self.assertIn('[订阅4] ' + label, groups['ChatGPT']['proxies'])

    def test_google_services_work_without_optional_shared_group(self):
        result = self.merged([[node('香港'), node('Gemini')]], without_shared=True)
        groups = {group['name']: group for group in result['proxy-groups']}
        self.assertEqual(groups['Gemini']['proxies'], ['[订阅1] Gemini', '[订阅1] 香港'])
        self.assertEqual(groups['Google']['proxies'], ['[订阅1] 香港', '[订阅1] Gemini'])

    def test_all_generated_group_references_exist_and_have_no_cycles(self):
        result = self.merged([[node('Google-All'), node('美国 AI专用'), node('新加坡 Gemini')]])
        groups = {group['name']: group for group in result['proxy-groups']}
        terminals = {item['name'] for item in result['proxies']} | {'DIRECT', 'REJECT', 'PASS', 'COMPATIBLE'}

        def visit(name, path):
            self.assertNotIn(name, path)
            self.assertTrue(groups[name]['proxies'])
            for target in groups[name]['proxies']:
                self.assertIn(target, set(groups) | terminals)
                if target in groups:
                    visit(target, path | {name})

        for name in groups:
            visit(name, set())

    def test_us_sub4_uses_actual_source_and_original_node_labels(self):
        raw = [node('【US】 普通'), node('美国 AI 专用'), node('US AI-only'),
               node('美国 已禁流媒体'), node('香港'), node('US 普通'), node('US 普通')]
        result = self.merged([
            [node('[订阅4] 美国 伪装来源')], [node('美国')], [node('美国')], raw,
        ], source_names=['订阅4', '二', '三', '美国自定义机场'])
        groups = {group['name']: group for group in result['proxy-groups']}
        self.assertEqual(groups['US-Sub4']['proxies'], [
            '[美国自定义机场] 【US】 普通', '[美国自定义机场] US 普通', '[美国自定义机场] US 普通 (2)',
        ])
        all_names = [item['name'] for item in result['proxies']]
        self.assertEqual(len(all_names), 10)
        self.assertEqual(set(groups['AUTO']['proxies']), set(all_names))
        self.assertTrue(set(all_names).issubset(groups['PROXY']['proxies']))
        self.assertIn('[美国自定义机场] 美国 AI 专用', all_names)

    def test_us_sub4_rejects_when_source_is_absent_ineligible_or_failed(self):
        cases = (
            [[node('美国')]],
            [[node('美国')], [node('香港')], [node('日本')], [node('美国 AI专用')]],
            [[node('美国')], [node('香港')], [node('日本')], MergeError('failed')],
        )
        for sources in cases:
            with self.subTest(sources=len(sources)):
                result = self.merged(sources)
                groups = {group['name']: group for group in result['proxy-groups']}
                self.assertEqual(groups['US-Sub4']['proxies'], ['REJECT'])

    def test_failed_earlier_source_does_not_renumber_sub4(self):
        result = self.merged([MergeError('failed'), [node('美国')], [node('美国')], [node('美国')]])
        groups = {group['name']: group for group in result['proxy-groups']}
        self.assertEqual(groups['US-Sub4']['proxies'], ['[订阅4] 美国'])
        self.assertEqual(groups['OpenAI-Sub4']['proxies'], [
            '[订阅4] 美国', '[订阅2] 美国', '[订阅3] 美国',
        ])
        self.assertEqual(groups['OpenAI-Sub4']['default-selected'], '[订阅4] 美国')

    def test_specific_service_rules_precede_broader_domains(self):
        template = yaml.safe_load(Path('template.yaml').read_text(encoding='utf-8'))

        def first_domain_target(host):
            for rule in template['rules']:
                if not rule.startswith(('DOMAIN,', 'DOMAIN-SUFFIX,')):
                    continue
                kind, value, target, *_ = rule.split(',')
                if ((kind == 'DOMAIN' and host == value)
                        or (kind == 'DOMAIN-SUFFIX' and (host == value or host.endswith('.' + value)))):
                    return target
            return None

        expectations = {
            'chatgpt.com': 'OpenAI-Sub4', 'ws.chatgpt.com': 'OpenAI-Sub4',
            'api.openai.com': 'OpenAI-Sub4', 'auth.openai.com': 'OpenAI-Sub4',
            'cdn.oaistatic.com': 'OpenAI-Sub4', 'files.oaiusercontent.com': 'OpenAI-Sub4',
            'events.oaistatsig.com': 'OpenAI-Sub4', 'cdn.openaimerge.com': 'OpenAI-Sub4',
            'sora.com': 'OpenAI-Sub4', 'chatgpt.livekit.cloud': 'OpenAI-Sub4',
            'challenges.cloudflare.com': 'OpenAI-Sub4', 'cdn.workos.com': 'OpenAI-Sub4',
            'forwarder.workos.com': 'OpenAI-Sub4', 'frontend-apps-multi-region.workos.com': 'OpenAI-Sub4',
            'images.workoscdn.com': 'OpenAI-Sub4', 'setup.workos.com': 'OpenAI-Sub4',
            'workos.imgix.net': 'OpenAI-Sub4',
            'gemini.google.com': 'Gemini', 'generativelanguage.googleapis.com': 'Gemini',
            'aistudio.google.com': 'Gemini', 'www.google.com': 'Google', 'maps.googleapis.com': 'Google',
            'onedrive.live.com': 'OneDrive', 'outlook.live.com': 'Microsoft',
            'copilot.microsoft.com': 'ChatGPT', 'claude.ai': 'ChatGPT', 'grok.com': 'ChatGPT',
            'unrelated.livekit.cloud': None, 'api.workos.com': None, 'www.cloudflare.com': None,
            'chatgpt.com.example.net': None, 'extra.cdn.workos.com': None,
        }
        for host, target in expectations.items():
            with self.subTest(host=host):
                self.assertEqual(first_domain_target(host), target)

    def test_openai_and_gemini_dns_policies_follow_their_own_selectors(self):
        template = yaml.safe_load(Path('template.yaml').read_text(encoding='utf-8'))
        self.assertEqual(template['dns']['enhanced-mode'], 'redir-host')
        self.assertNotIn('fake-ip-range', template['dns'])
        self.assertNotIn('fake-ip-filter', template['dns'])
        policy = template['dns']['nameserver-policy']
        for host in ('gemini.google.com', 'generativelanguage.googleapis.com', 'aistudio.google.com'):
            self.assertTrue(policy['+.' + host].endswith('#Gemini'))
        for host in ('chatgpt.com', 'openai.com', 'oaistatic.com', 'oaiusercontent.com',
                     'oaistatsig.com', 'openaimerge.com', 'sora.com', 'chatgpt.livekit.cloud'):
            self.assertEqual(len(policy['+.' + host]), 2)
            self.assertTrue(all(server.endswith('#OpenAI-Sub4') for server in policy['+.' + host]))
        for host in ('challenges.cloudflare.com', 'cdn.workos.com', 'forwarder.workos.com',
                     'frontend-apps-multi-region.workos.com', 'images.workoscdn.com',
                     'setup.workos.com', 'workos.imgix.net'):
            self.assertEqual(len(policy[host]), 2)
            self.assertTrue(all(server.endswith('#OpenAI-Sub4') for server in policy[host]))
        for host in ('cloudflare.com', 'workos.com', 'workoscdn.com', 'imgix.net', 'livekit.cloud'):
            self.assertNotIn('+.' + host, policy)
        for host in ('anthropic.com', 'claude.ai'):
            self.assertTrue(policy['+.' + host].endswith('#ChatGPT'))
        self.assertTrue(template['dns']['proxy-server-nameserver'])

if __name__ == '__main__':
    unittest.main()
