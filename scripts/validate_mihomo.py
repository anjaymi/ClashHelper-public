"""Validate the complete merged subscription with pinned Mihomo cores.

Only aggregate metadata is logged. Core output can contain node credentials and
must never be streamed to Actions logs. Run this before publishing out.yaml.
"""

from collections import Counter
import gzip
import json
from hashlib import sha256
from pathlib import Path
import re
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory

import requests
import yaml


# Official MetaCubeX release assets, SHA-256 supplied by GitHub release metadata.
CORE_RELEASES = (
    ('v1.19.33', '386508bfb1c72c43b6a0eda73708ecdce82546581b0ebcb16366c53757c622b6'),
    ('v1.19.25', '8d14bf2edbf2911db004abaed12754d63041eaf87e565af6f1e589883cd93ec8'),
)
MAX_ARCHIVE_SIZE = 64 * 1024 * 1024


class ValidationError(Exception):
    pass


def download_core(folder, version, expected_hash):
    archive = folder / 'mihomo.gz'
    url = ('https://github.com/MetaCubeX/mihomo/releases/download/'
           + version + '/mihomo-linux-amd64-compatible-' + version + '.gz')
    digest, size = sha256(), 0
    with requests.get(url, timeout=(10, 60), stream=True) as response:
        response.raise_for_status()
        with archive.open('wb') as output:
            for chunk in response.iter_content(128 * 1024):
                size += len(chunk)
                if size > MAX_ARCHIVE_SIZE:
                    raise ValidationError('内核下载大小异常。')
                digest.update(chunk)
                output.write(chunk)
    if digest.hexdigest() != expected_hash:
        raise ValidationError('内核 SHA-256 校验失败，已停止发布。')
    binary = folder / 'mihomo'
    with gzip.open(archive, 'rb') as source, binary.open('wb') as output:
        shutil.copyfileobj(source, output)
    binary.chmod(0o700)
    return binary


def run_core_check(binary, folder, config_path):
    result = subprocess.run(
        [str(binary), '-t', '-d', str(folder), '-f', str(config_path)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding='utf-8', errors='replace', timeout=120, check=False,
    )
    # Do not print raw core output, including when the parser rejects a node.
    if result.returncode != 0:
        raise ValidationError('内核拒绝完整配置；详细输出已抑制，避免泄露节点凭据。')
    if 'test is successful' not in result.stdout:
        raise ValidationError('内核未确认配置校验成功，已停止发布。')


def config_summary(config):
    proxies = config.get('proxies')
    if not isinstance(proxies, list) or not proxies:
        raise ValidationError('没有可校验的节点。')
    types = Counter(
        node['type'] for node in proxies
        if isinstance(node, dict) and isinstance(node.get('type'), str)
        and re.fullmatch(r'[a-z0-9-]+', node['type'])
    )
    return len(proxies), ', '.join(f'{key}={value}' for key, value in sorted(types.items()))


def record_validation_failure():
    status_path = Path('merge-status.json')
    try:
        report = json.loads(status_path.read_text(encoding='utf-8'))
        report.update(status='failed', message='Mihomo 内核校验未通过或未完成，已保留上次发布的订阅。')
        status_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    except (OSError, ValueError, TypeError, AttributeError):
        pass


def main():
    if len(sys.argv) != 2:
        print('Usage: python scripts/validate_mihomo.py <out.yaml>')
        return 1
    try:
        config_path = Path(sys.argv[1]).resolve()
        config = yaml.safe_load(config_path.read_text(encoding='utf-8'))
        if not isinstance(config, dict):
            raise ValidationError('配置不是有效的 YAML 对象。')
        count, types = config_summary(config)
        print(f'开始完整配置校验：{count} 个节点；协议统计：{types}', flush=True)
        for version, checksum in CORE_RELEASES:
            try:
                with TemporaryDirectory(prefix='mihomo-validation-') as temporary:
                    folder = Path(temporary)
                    binary = download_core(folder, version, checksum)
                    run_core_check(binary, folder, config_path)
            except (ValidationError, OSError, ValueError, requests.RequestException, subprocess.SubprocessError):
                if version == CORE_RELEASES[0][0]:
                    raise
                print(f'::warning::Mihomo {version} 参考校验未通过或未完成；当前版本已通过，旧版兼容性未确认。')
                continue
            print(f'Mihomo {version} 完整配置校验通过。', flush=True)
        return 0
    except ValidationError as exc:
        print(f'::error::{exc}')
    except (OSError, ValueError, requests.RequestException, yaml.YAMLError, subprocess.SubprocessError):
        print('::error::内核校验未完成，保留原订阅；未输出包含凭据的配置或诊断。')
    record_validation_failure()
    return 1


if __name__ == '__main__':
    sys.exit(main())
