import json
from datetime import datetime, timezone
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from subscription_manager import COOKIE, GitHub, LocalStore, Manager, ManagerServer, UserError, load_settings


class MemoryStore:
    def __init__(self):
        self.data = {'urls': [], 'saved_at': None}

    def read(self):
        return self.data

    def encode(self, urls):
        return json.dumps({'urls': urls, 'saved_at': datetime.now(timezone.utc).isoformat()}).encode()

    def write(self, data):
        self.data = json.loads(data)


class FakeGitHub:
    def __init__(self):
        self.urls = []
        self.triggers = 0

    def secret_names(self):
        return {'SUBSCRIPTIONS'} if self.urls else set()

    def save(self, urls):
        self.urls = urls

    def trigger(self):
        self.triggers += 1

    def snapshot(self):
        return dict(account='example-user', repo='example-user/ClashHelper',
                    configured=bool(self.urls), advanced=False,
                    subscription_url='https://gist.githubusercontent.com/example/test/raw/clash.yaml',
                    report=dict(updated_at=datetime.now(timezone.utc).isoformat(), status='ok', nodes=len(self.urls),
                                sources=[dict(index=i, status='ok', nodes=1, traffic=dict(remaining=1073741824, expire=1800000000)) for i in range(1,len(self.urls)+1)]) if self.urls else None,
                    run=dict(databaseId=self.triggers, createdAt='2026-10-08T12:00:00Z',
                             status='completed', conclusion='success',
                             url='https://github.com/example-user/ClashHelper/actions'))


class ManagerTests(unittest.TestCase):
    def test_load_settings_requires_the_users_repository_and_gist(self):
        with tempfile.TemporaryDirectory() as folder, patch('subscription_manager.ROOT', Path(folder)):
            path = Path(folder) / '.clashhelper.json'
            with self.assertRaises(UserError):
                load_settings()
            path.write_text(json.dumps({'repository': 'example-user/ClashHelper',
                                        'gist_id': '0123456789abcdef01234567'}), encoding='utf-8')
            self.assertEqual(load_settings(), ('example-user/ClashHelper', '0123456789abcdef01234567'))
            path.write_text(json.dumps({'repository': 'https://example.com/not-a-repository',
                                        'gist_id': 'not-a-gist-id'}), encoding='utf-8')
            with self.assertRaises(UserError):
                load_settings()

    def test_save_deduplicates_and_triggers_and_remembers(self):
        github, store = FakeGitHub(), MemoryStore()
        manager = Manager(github, store)
        result = manager.save([' https://example.com/a?token=fake ', 'https://example.com/a?token=fake', ''])
        self.assertTrue(result['started'])
        self.assertEqual(result['count'], 1)
        self.assertEqual(github.triggers, 1)
        self.assertEqual(manager.state()['urls'], github.urls)

    def test_empty_and_invalid_urls_never_save(self):
        github = FakeGitHub()
        manager = Manager(github, MemoryStore())
        for urls in ([], [''], ['vmess://fake'], [3], None):
            with self.assertRaises(UserError):
                manager.save(urls)
        self.assertEqual(github.urls, [])
        self.assertEqual(github.triggers, 0)

    def test_dispatch_failure_reports_saved_without_claiming_update(self):
        github, store = FakeGitHub(), MemoryStore()
        github.trigger = Mock(side_effect=UserError('offline'))
        result = Manager(github, store).save(['https://example.com/sub'])
        self.assertTrue(result['saved'])
        self.assertFalse(result['started'])
        self.assertEqual(store.read()['urls'], ['https://example.com/sub'])

    def test_local_encryption_failure_does_not_change_remote(self):
        github, store = FakeGitHub(), MemoryStore()
        store.encode = Mock(side_effect=UserError('cannot encrypt'))
        with self.assertRaises(UserError):
            Manager(github, store).save(['https://example.com/sub'])
        self.assertEqual(github.urls, [])

    def test_cli_secret_is_stdin_and_advanced_config_is_preserved(self):
        github = GitHub('example-user/ClashHelper', '0123456789abcdef01234567')
        with patch.object(github, 'run', side_effect=['[]', '']) as command:
            github.save(['https://example.com/sub?token=fake'])
        args, kwargs = command.call_args
        self.assertNotIn('fake', ' '.join(args[0]))
        self.assertEqual(kwargs['value'], 'https://example.com/sub?token=fake\n')
        with patch.object(github, 'run', return_value='[{"name":"SITES_YAML"}]') as command:
            with self.assertRaises(UserError):
                github.save(['https://example.com/sub'])
            self.assertEqual(command.call_count, 1)

    def test_configured_repository_and_gist_are_used_for_state_and_save(self):
        repo = 'example-user/ClashHelper'
        gist_id = '0123456789abcdef01234567'
        github = GitHub(repo, gist_id)
        commands = []

        def run(args, **kwargs):
            commands.append((args, kwargs))
            if args[:2] == ['api', 'user']:
                return 'example-user'
            if args[:2] == ['secret', 'list']:
                return '[{"name":"SUBSCRIPTIONS"}]'
            if args[:2] == ['run', 'list']:
                return '[]'
            if args[:2] == ['api', f'gists/{gist_id}']:
                return ''
            if args[:2] == ['secret', 'set']:
                return ''
            self.fail('Unexpected GitHub command')

        with patch.object(github, 'run', side_effect=run):
            state = github.snapshot()
            github.save(['https://example.com/sub?token=fake'])
        self.assertEqual(state['repo'], repo)
        self.assertEqual(state['subscription_url'],
                         f'https://gist.githubusercontent.com/example-user/{gist_id}/raw/clash.yaml')
        for args, _ in commands:
            if '--repo' in args:
                self.assertEqual(args[args.index('--repo') + 1], repo)

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI')
    def test_dpapi_file_is_not_plaintext_and_round_trips(self):
        with tempfile.TemporaryDirectory() as folder:
            store = LocalStore(Path(folder) / 'addresses.enc')
            urls = ['https://example.com/sub?token=fake-private-token']
            store.write(store.encode(urls))
            self.assertNotIn(b'fake-private-token', store.path.read_bytes())
            self.assertNotIn(b'example.com', store.path.read_bytes())
            self.assertEqual(store.read()['urls'], urls)

    def test_http_requires_session_host_and_same_origin(self):
        github = FakeGitHub()
        server = ManagerServer(('127.0.0.1', 0), Manager(github, MemoryStore()))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        cookie = f'{COOKIE}={server.session}'
        try:
            with self.assertRaises(HTTPError) as error:
                urlopen(server.origin + '/api/state')
            self.assertEqual(error.exception.code, 403)
            request = Request(server.origin + '/api/state', headers={'Cookie': cookie, 'Host': 'evil.example'})
            with self.assertRaises(HTTPError) as error:
                urlopen(request)
            self.assertEqual(error.exception.code, 403)
            request = Request(server.origin + '/api/save', data=json.dumps({'urls': ['https://example.com/sub']}).encode(),
                              headers={'Cookie': cookie, 'Origin': 'https://evil.example', 'Content-Type': 'application/json'})
            with self.assertRaises(HTTPError) as error:
                urlopen(request)
            self.assertEqual(error.exception.code, 403)
            self.assertFalse(github.urls)
            request.headers['Origin'] = server.origin
            with urlopen(request) as response:
                self.assertTrue(json.load(response)['started'])
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
            self.assertEqual(github.urls, ['https://example.com/sub'])
        finally:
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    unittest.main()
