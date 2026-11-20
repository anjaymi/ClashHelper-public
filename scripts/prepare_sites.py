"""Read encrypted GitHub configuration without printing subscription URLs."""

import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

import yaml


def parse_sites(subscriptions, advanced):
    if advanced.strip():
        sites = yaml.safe_load(advanced)
        if not isinstance(sites, list) or not sites:
            raise ValueError("SITES_YAML 必须是非空 YAML 列表")
    else:
        urls = list(dict.fromkeys(
            line.strip() for line in subscriptions.splitlines()
            if line.strip() and not line.strip().startswith('#')
        ))
        sites = [
            dict(name=f"订阅{i}", group="PROXY", url=url,
                 inclusion=[], exclusion=[],
                 dedup=False)
            for i, url in enumerate(urls, 1)
        ]
    names = set()
    for site in sites:
        if not isinstance(site, dict):
            raise ValueError("每个订阅必须是 YAML 对象")
        for key in ("name", "group", "url"):
            if not isinstance(site.get(key), str) or not site[key].strip():
                raise ValueError(f"订阅缺少有效的 {key} 字段")
        if site['name'] in names:
            raise ValueError("订阅 name 不能重复")
        names.add(site['name'])
        parsed = urlsplit(site['url'])
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            raise ValueError("订阅地址必须是 HTTP 或 HTTPS 链接")
        for key in ('inclusion', 'exclusion'):
            values = site.setdefault(key, [])
            if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                raise ValueError(f"{key} 必须是关键词列表")
        if not isinstance(site.setdefault('dedup', False), bool):
            raise ValueError("dedup 必须是 true 或 false")
    return sites


def main():
    try:
        sites = parse_sites(os.environ.get('SUBSCRIPTIONS', ''),
                            os.environ.get('SITES_YAML', ''))
    except (ValueError, yaml.YAMLError):
        print('::error::订阅配置格式错误。请按 README 检查 Secret；为保护订阅凭据，不输出配置内容。')
        return 1
    configured = bool(sites)
    if configured:
        Path('sites.yaml').write_text(yaml.safe_dump(sites, allow_unicode=True), encoding='utf-8')
        print(f'已读取 {len(sites)} 个订阅配置。')
    else:
        print('尚未填写订阅；等待添加 SUBSCRIPTIONS Secret。')
    output = os.environ.get('GITHUB_OUTPUT')
    if output:
        with open(output, 'a', encoding='utf-8') as f:
            f.write(f"configured={str(configured).lower()}\n")
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a', encoding='utf-8') as f:
            if configured:
                f.write(f'已读取 {len(sites)} 个订阅。抓取和发布结果见后续步骤。\n')
            else:
                repo = os.environ['GITHUB_REPOSITORY']
                f.write('自动化已就绪，尚未填写订阅。\n\n'
                        f'[添加 SUBSCRIPTIONS Secret](https://github.com/{repo}/settings/secrets/actions/new)：'
                        '每行一个 Clash YAML 订阅链接，然后重新运行本工作流。\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
