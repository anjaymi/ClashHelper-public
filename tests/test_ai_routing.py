"""Regression checks for AI routing, especially shared-cloud exceptions."""

from contextlib import redirect_stdout
import io
from pathlib import Path
import unittest
from unittest.mock import patch

import yaml

from helper import merge_config, refresh_routing_groups
from scripts.prepare_sites import parse_sites


class AIRoutingTests(unittest.TestCase):
    def setUp(self):
        self.template = yaml.safe_load(Path('template.yaml').read_text(encoding='utf-8'))

    def first_route(self, host):
        # All AI rules are domain rules ahead of the generic provider rules.
        for rule in self.template['rules']:
            fields = rule.split(',')
            if fields[0] == 'DOMAIN' and host == fields[1]:
                return fields[2]
            if fields[0] == 'DOMAIN-SUFFIX' and (host == fields[1] or host.endswith('.' + fields[1])):
                return fields[2]
            if fields[0] == 'RULE-SET':
                return None
        return None

    def test_kiro_covers_runtime_auth_updates_and_extensions(self):
        hosts = (
            'kiro.dev', 'app.kiro.dev', 'assets.app.kiro.dev',
            'prod.us-east-1.auth.desktop.kiro.dev', 'prod.download.desktop.kiro.dev',
            'runtime.us-east-1.kiro.dev', 'management.eu-central-1.kiro.dev',
            'telemetry.kiro.aws.dev', 'q.us-east-1.amazonaws.com',
            'q.eu-central-1.amazonaws.com', 'codewhisperer.us-east-1.amazonaws.com',
            'cognito-identity.us-east-1.amazonaws.com', 'oidc.us-east-1.amazonaws.com',
            'view.awsapps.com', 'profile.aws.amazon.com', 'opfcaptcha-prod.s3.amazonaws.com',
            'example.us-east-1.sdk.awswaf.com', 'example.edge.captcha-sdk.awswaf.com',
            'oidc.eu-central-1.amazonaws.com', 'portal.sso.us-east-1.amazonaws.com',
            'assets.sso-portal.eu-central-1.amazonaws.com', 'us-east-1.signin.aws',
            'eu-central-1.signin.aws.amazon.com', 'example.captcha.awswaf.com',
            'example.token.awswaf.com', 'prod.tools.shortbread.aws.dev',
            'open-vsx.org', 'openvsx.eclipsecontent.org',
        )
        for host in hosts:
            with self.subTest(host=host):
                self.assertEqual(self.first_route(host), 'Kiro')

    def test_service_rules_win_before_google_github_and_direct_providers(self):
        expected = {
            'api2.cursor.sh': 'AI-Coding', 'api.cursor.com': 'AI-Coding',
            'server.codeium.com': 'AI-Coding', 'windsurf.com': 'AI-Coding',
            'api.individual.githubcopilot.com': 'AI-Coding',
            'copilot-proxy.githubusercontent.com': 'AI-Coding',
            'origin-tracker.githubusercontent.com': 'AI-Coding',
            'copilot-workspace.githubnext.com': 'AI-Coding',
            'grazie.aws.intellij.net': 'AI-Coding', 'api.augmentcode.com': 'AI-Coding',
            'ampcode.com': 'AI-Coding', 'trae-api-sg.mchost.guru': 'AI-Coding',
            'trae.ai': 'AI-Coding', 'api.openrouter.ai': 'AI-Services',
            'perplexity.ai': 'AI-Services', 'ppl-ai-file-upload.s3.amazonaws.com': 'AI-Services',
            'pplx-res.cloudinary.com': 'AI-Services', 'huggingface.co': 'AI-Services',
            'claudeusercontent.com': 'ChatGPT', 'claude.com': 'ChatGPT',
            'notebooklm.google.com': 'Gemini', 'notebooklm-pa.googleapis.com': 'Gemini',
            'antigravity-pa.googleapis.com': 'Gemini', 'antigravity.google': 'Gemini',
            'jules.google.com': 'Gemini', 'gemini.gstatic.com': 'Gemini',
            'api.openai.com': 'OpenAI-Sub4', 'chatgpt.com': 'OpenAI-Sub4',
            'api.github.com': 'GitHub', 'raw.githubusercontent.com': 'GitHub',
            'www.google.com': 'Google', 'maps.googleapis.com': 'Google',
            'copilot.microsoft.com': 'ChatGPT', 'outlook.live.com': 'Microsoft',
        }
        for host, target in expected.items():
            with self.subTest(host=host):
                self.assertEqual(self.first_route(host), target)

    def test_shared_cloud_domains_and_lookalikes_are_not_captured(self):
        for host in (
            'example.amazonaws.com', 'unrelated.s3.amazonaws.com', 'd123.cloudfront.net',
            'tenant.auth0.com', 'example.awsapps.com', 'api.stripe.com', 'sentry.io',
            'random-openai.example.net', 'kiro.dev.example.com', 'evilkiro.dev',
            'other.cognito-identity.us-east-1.amazonaws.com', 'cursor.com.example.net',
            'trae.cn', 'deepseek.com', 'kimi.com',
        ):
            with self.subTest(host=host):
                self.assertIsNone(self.first_route(host))
        # Shared Google storage retains the general Google route, not an AI exit.
        self.assertEqual(self.first_route('storage.googleapis.com'), 'Google')

    def test_new_selectors_keep_every_subscription_and_stable_default(self):
        sites = parse_sites('\n'.join('https://example.com/' + str(i) for i in range(4)), '')
        sources = [[dict(name='node-' + str(i), server='example.com', port=443, type='ss',
                         cipher='aes-128-gcm', password='unit-test-only')] for i in range(4)]
        with patch('helper.fetch_nodes', side_effect=sources), redirect_stdout(io.StringIO()):
            result = merge_config(self.template, sites)
        groups = {group['name']: group for group in result['proxy-groups']}
        names = {node['name'] for node in result['proxies']}
        for service in ('Kiro', 'AI-Coding', 'AI-Services'):
            with self.subTest(service=service):
                group = groups[service]
                self.assertEqual(group['type'], 'select')
                self.assertEqual(group['proxies'][0], 'OpenAI-Sub4')
                self.assertEqual(set(group['proxies']), names | {'OpenAI-Sub4'})
                self.assertNotIn('AUTO', group['proxies'])
                self.assertNotIn(service, groups['OpenAI-Sub4']['proxies'])

        # The shared selector is optional in customized templates.
        result['proxy-groups'] = [group for group in result['proxy-groups']
                                  if group['name'] != 'OpenAI-Sub4']
        refresh_routing_groups(result)
        groups = {group['name']: group for group in result['proxy-groups']}
        for service in ('Kiro', 'AI-Coding', 'AI-Services'):
            self.assertEqual(set(groups[service]['proxies']), names)

    def test_domain_rules_are_valid_unique_and_target_existing_groups(self):
        groups = {group['name'] for group in self.template['proxy-groups']}
        seen = set()
        for rule in self.template['rules']:
            fields = rule.split(',')
            if fields[0] not in ('DOMAIN', 'DOMAIN-SUFFIX'):
                continue
            self.assertEqual(len(fields), 3)
            self.assertRegex(fields[1], r'^[a-z0-9]+(?:[a-z0-9.-]*[a-z0-9])?$')
            self.assertNotIn('..', fields[1])
            self.assertIn(fields[2], groups | {'DIRECT', 'REJECT'})
            self.assertNotIn(tuple(fields[:2]), seen)
            seen.add(tuple(fields[:2]))
        for rule in self.template['rules']:
            self.assertFalse(rule.startswith(('PROCESS-NAME,', 'DOMAIN-KEYWORD,', 'IP-ASN,')))
        self.assertEqual(self.template['dns']['enhanced-mode'], 'redir-host')
        self.assertFalse(any(key.startswith('fake-ip') for key in self.template['dns']))


if __name__ == '__main__':
    unittest.main()
