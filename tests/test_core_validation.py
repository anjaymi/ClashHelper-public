"""Check that publication is gated on real core validation without log leaks."""

from contextlib import redirect_stdout
import io
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

import yaml

from scripts.validate_mihomo import ValidationError, config_summary, download_core, run_core_check, main


class CoreValidationTests(unittest.TestCase):
    def test_core_check_uses_complete_config_and_does_not_log_credentials(self):
        output = io.StringIO()
        with patch('scripts.validate_mihomo.subprocess.run',
                   return_value=SimpleNamespace(returncode=1, stdout='private-node-credential')) as run:
            with redirect_stdout(output), self.assertRaises(ValidationError) as caught:
                run_core_check(Path('/core/mihomo'), Path('/temporary'), Path('/private/out.yaml'))
        self.assertNotIn('private-node-credential', output.getvalue() + str(caught.exception))
        self.assertEqual(run.call_args.args[0],
                         [str(Path('/core/mihomo')), '-t', '-d', str(Path('/temporary')), '-f', str(Path('/private/out.yaml'))])
        self.assertTrue(run.call_args.kwargs['stdout'])

    def test_exit_zero_without_success_marker_is_not_accepted(self):
        with patch('scripts.validate_mihomo.subprocess.run',
                   return_value=SimpleNamespace(returncode=0, stdout='')):
            with self.assertRaises(ValidationError):
                run_core_check(Path('core'), Path('work'), Path('out.yaml'))

    def test_core_success_marker_and_exit_code_are_both_required(self):
        with patch('scripts.validate_mihomo.subprocess.run',
                   return_value=SimpleNamespace(returncode=0, stdout='configuration file out.yaml test is successful')):
            run_core_check(Path('core'), Path('work'), Path('out.yaml'))

    def test_bad_download_checksum_cannot_be_executed(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.iter_content.return_value = [b'not-a-trusted-core']
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            with patch('scripts.validate_mihomo.requests.get', return_value=response):
                with self.assertRaises(ValidationError):
                    download_core(folder, 'v1.19.33', '0' * 64)
            self.assertFalse((folder / 'mihomo').exists())

    def test_protocol_summary_never_includes_node_values(self):
        count, summary = config_summary({'proxies': [
            {'name': 'private-name', 'server': 'private-server', 'type': 'vless', 'uuid': 'private-uuid'},
            {'type': 'ss', 'password': 'private-password'},
        ]})
        self.assertEqual(count, 2)
        self.assertEqual(summary, 'ss=1, vless=1')
        self.assertNotIn('private', summary)
        with self.assertRaises(ValidationError):
            config_summary({'proxies': []})

    def test_current_core_rejection_stops_publication_check(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'out.yaml'
            source.write_text('proxies: [{name: test, type: ss}]', encoding='utf-8')
            with patch('sys.argv', ['validate', str(source)]), \
                    patch('scripts.validate_mihomo.download_core', return_value=Path('core')), \
                    patch('scripts.validate_mihomo.run_core_check', side_effect=ValidationError('rejected')) as check, \
                    patch('scripts.validate_mihomo.record_validation_failure') as failed, \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(main(), 1)
                self.assertEqual(check.call_count, 1)
                failed.assert_called_once()

    def test_reference_core_failure_does_not_reject_a_valid_current_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'out.yaml'
            source.write_text('proxies: [{name: test, type: ss}]', encoding='utf-8')
            with patch('sys.argv', ['validate', str(source)]), \
                    patch('scripts.validate_mihomo.download_core', return_value=Path('core')), \
                    patch('scripts.validate_mihomo.run_core_check', side_effect=[None, ValidationError('old')]), \
                    redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(), 0)
                self.assertIn('::warning::', output.getvalue())

    def test_publication_requires_core_validation_success(self):
        workflow = yaml.safe_load(Path('.github/workflows/merge.yml').read_text(encoding='utf-8'))
        steps = workflow['jobs']['merge']['steps']
        validate_index = next(i for i, step in enumerate(steps) if step.get('id') == 'core_validation')
        publish_index = next(i for i, step in enumerate(steps) if step.get('name') == '更新固定订阅地址')
        self.assertLess(validate_index, publish_index)
        self.assertIn('steps.core_validation.outcome', steps[publish_index]['if'])
        self.assertIn('success', steps[publish_index]['if'])


if __name__ == '__main__':
    unittest.main()
