"""Check that core OpenAI routes cannot be shadowed by earlier rules."""

from pathlib import Path
import unittest

import yaml


def first_host_route(rules, host):
    """Return the first explicit match, or an earlier rule that may preempt it.

    Providers and IP-based or other unknown rules cannot be evaluated from a
    hostname alone, so treat them as potential blockers rather than skipping
    them.  This is a conservative order check, not a Mihomo rules simulator.
    """
    for position, rule in enumerate(rules, 1):
        parts = rule.split(',')
        kind = parts[0]
        if kind not in ('DOMAIN', 'DOMAIN-SUFFIX', 'DOMAIN-KEYWORD'):
            return ('potential_preemption', position, kind)
        if len(parts) < 3:
            return ('invalid_rule', position, kind)
        value, target = parts[1:3]
        matches = (
            (kind == 'DOMAIN' and host == value)
            or (kind == 'DOMAIN-SUFFIX'
                and (host == value or host.endswith('.' + value)))
            or (kind == 'DOMAIN-KEYWORD' and value in host)
        )
        if matches:
            return ('matched', position, target)
    return ('missing', None, None)


class OpenAIRulePriorityTests(unittest.TestCase):
    def test_core_domains_match_before_dynamic_and_catch_all_rules(self):
        template_path = Path(__file__).resolve().parents[1] / 'template.yaml'
        template = yaml.safe_load(template_path.read_text(encoding='utf-8'))
        for host in (
            'chatgpt.com', 'ws.chatgpt.com', 'codex-cloud-backend.chatgpt.com',
            'api.openai.com', 'auth.openai.com', 'cdn.oaistatic.com',
            'files.oaiusercontent.com',
        ):
            with self.subTest(host=host):
                kind, position, target = first_host_route(template['rules'], host)
                self.assertEqual((kind, target), ('matched', 'OpenAI-Sub4'),
                                 f'{host}: earlier or incorrect rule at {position}: {target}')

    def test_guard_detects_catch_all_provider_and_keyword_shadowing(self):
        intended = 'DOMAIN-SUFFIX,chatgpt.com,OpenAI-Sub4'
        for earlier, expected in (
            ('MATCH,DIRECT', ('potential_preemption', 1, 'MATCH')),
            ('RULE-SET,reject,REJECT', ('potential_preemption', 1, 'RULE-SET')),
            ('DOMAIN-KEYWORD,chatgpt,PROXY', ('matched', 1, 'PROXY')),
        ):
            with self.subTest(earlier=earlier):
                self.assertEqual(first_host_route([earlier, intended], 'chatgpt.com'),
                                 expected)


if __name__ == '__main__':
    unittest.main()
