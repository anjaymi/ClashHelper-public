"""Publish without exposing node credentials or the GitHub token in logs."""

import os
import json
from pathlib import Path
import re
import sys

import requests
import yaml


def main():
    gist_id = os.environ.get('GIST_ID', '')
    token = os.environ.get('GIST_TOKEN', '')
    if not re.fullmatch(r'[0-9a-f]{20,40}', gist_id) or not token:
        print('::error::缺少有效的 GIST_ID 变量或 GIST_TOKEN Secret。')
        return 1
    url = f'https://api.github.com/gists/{gist_id}'
    headers = {'Authorization': f'Bearer {token}',
               'Accept': 'application/vnd.github+json',
               'X-GitHub-Api-Version': '2022-11-28'}
    try:
        current = requests.get(url, headers=headers, timeout=30)
        current.raise_for_status()
        gist = current.json()
        if gist.get('public') is not False or 'clash.yaml' not in gist.get('files', {}):
            print('::error::目标必须是包含 clash.yaml 的 Secret Gist。')
            return 1
        if sys.argv[1:] == ['--check']:
            payload = {'description': gist.get('description', '')}
        elif len(sys.argv) == 3 and sys.argv[1] == '--status':
            status_content = Path(sys.argv[2]).read_text(encoding='utf-8')
            json.loads(status_content)
            payload = {'files': {'status.json': {'content': status_content}}}
        elif len(sys.argv) == 2:
            content = Path(sys.argv[1]).read_text(encoding='utf-8')
            config = yaml.safe_load(content)
            if not isinstance(config, dict) or not config.get('proxies'):
                print('::error::没有可发布的节点，保留上次订阅。')
                return 1
            payload = {'files': {'clash.yaml': {'content': content}}}
            status_path = Path('merge-status.json')
            if status_path.exists():
                payload['files']['status.json'] = {'content': status_path.read_text(encoding='utf-8')}
        else:
            print('Usage: publish_gist.py <config.yaml> | --check | --status <status.json>')
            return 1
        result = requests.patch(url, headers=headers, json=payload, timeout=30)
        result.raise_for_status()
        if sys.argv[1:] == ['--check']:
            print('Gist 读取和写入权限验证成功。')
        elif sys.argv[1] == '--status':
            print('订阅诊断结果已保存，原订阅配置保持不变。')
        else:
            print('合并订阅已发布，固定地址保持不变。')
        return 0
    except (requests.RequestException, ValueError, OSError, yaml.YAMLError):
        print('::error::Gist 发布失败；为保护凭据，不输出响应或配置内容。请检查 GIST_TOKEN 是否有效。')
        return 1


if __name__ == '__main__':
    sys.exit(main())
