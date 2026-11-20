"""Fetch, filter and merge Clash subscriptions into a local template.

Based on https://github.com/nonPointer/ClashHelper.
Subscription URLs and node credentials must never appear in Action logs.
"""

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import socket
import sys

import requests
import yaml


SUBSCRIPTION_USER_AGENT = 'clash.meta'


class MergeError(Exception):
    pass


def read_nodes(content, diagnostic=None):
    diagnostic = diagnostic if diagnostic is not None else {}
    diagnostic.update(received_nodes=0, invalid_nodes=0, invalid_reasons={},
                      filtered_nodes=0, node_entries=[], listing_read=False,
                      connectivity='not_tested', compatibility_notice_count=0,
                      compatibility_warning=None)
    data = yaml.safe_load(content)
    if not isinstance(data, dict) or not isinstance(data.get('proxies'), list):
        raise MergeError('订阅必须返回 Clash YAML（包含 proxies 列表），不支持 Base64 或节点 URI 列表。')
    nodes = data['proxies']
    diagnostic['received_nodes'] = len(nodes)
    diagnostic['listing_read'] = True
    if not nodes:
        raise MergeError('订阅没有节点。')
    valid = []
    for index, raw_node in enumerate(nodes, 1):
        entry = dict(index=index, name=f'第 {index} 项', status='included')
        if isinstance(raw_node, dict):
            if isinstance(raw_node.get('name'), str) and raw_node['name'].strip():
                entry['name'] = raw_node['name']
            if isinstance(raw_node.get('type'), str):
                entry['type'] = raw_node['type']
            name = entry['name'].lower()
            if 'clash' in name and any(phrase in name for phrase in (
                    '不受支持', '不支持', '请更换', '请使用', '请更新',
                    'unsupported', 'update your client', 'upgrade your client')):
                entry['provider_notice'] = True
                diagnostic['compatibility_notice_count'] += 1
                diagnostic['compatibility_warning'] = '服务商返回了客户端兼容性提示，这份内容可能不是完整节点列表。'
        diagnostic['node_entries'].append(entry)
        try:
            valid.append(normalize_node(raw_node))
        except MergeError as exc:
            reason = str(exc)
            entry.update(status='configuration_error', reason=reason)
            diagnostic['invalid_nodes'] += 1
            reasons = diagnostic['invalid_reasons']
            reasons[reason] = reasons.get(reason, 0) + 1
    if not valid:
        raise MergeError(f'订阅含有 {len(nodes)} 个节点，但没有格式有效的节点。')
    return valid


def normalize_node(raw_node):
    if not isinstance(raw_node, dict):
        raise MergeError('节点不是有效的配置对象')
    node = deepcopy(raw_node)
    for key in ('name', 'server', 'type'):
        if not isinstance(node.get(key), str) or not node[key].strip():
            raise MergeError('节点缺少有效的 name、server 或 type')
    port = node.get('port')
    if isinstance(port, str) and port.isascii() and port.isdigit() and len(port) <= 5:
        port = int(port)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise MergeError('节点端口无效')
    node['port'] = port
    for key in ('password', 'uuid', 'cipher', 'dialer-proxy'):
        if key in node and not isinstance(node[key], str):
            raise MergeError('节点凭据或上游引用的字段类型无效')
    if 'dialer-proxy' in node and not node['dialer-proxy'].strip():
        raise MergeError('节点上游引用为空')
    return node


def remove_invalid_dependencies(nodes, allowed):
    # A name can identify several source nodes. Inspect each occurrence, then
    # resolve references to the first surviving occurrence, as merge_config does.
    remaining = list(range(len(nodes)))
    while remaining:
        by_name = {}
        for index in remaining:
            by_name.setdefault(nodes[index]['name'], index)
        invalid = {
            index for index in remaining
            if (reference := nodes[index].get('dialer-proxy')) is not None
            and reference not in by_name and reference not in allowed
        }
        if not invalid:
            finished = set()
            for start in remaining:
                current, path, positions = start, [], {}
                while current is not None and current not in finished:
                    if current in positions:
                        # Remove the actual cycle first. A dependent may still
                        # be usable through another occurrence of the same name.
                        invalid.update(path[positions[current]:])
                        break
                    positions[current] = len(path)
                    path.append(current)
                    current = by_name.get(nodes[current].get('dialer-proxy'))
                finished.update(path)
        if not invalid:
            break
        remaining = [index for index in remaining if index not in invalid]
    valid = [nodes[index] for index in remaining]
    return valid, len(nodes) - len(valid)


def traffic_info(header):
    values = {}
    for field in header.split(';'):
        key, separator, value = field.strip().partition('=')
        if separator and key in ('upload', 'download', 'total', 'expire') and value.isdigit() and len(value) < 21:
            values[key] = int(value)
    if 'total' in values:
        values['remaining'] = max(0, values['total'] - values.get('upload', 0) - values.get('download', 0))
    return values or None


def fetch_nodes(site, cache_dir, diagnostic=None, *, allowed_names=None):
    diagnostic = diagnostic if diagnostic is not None else {}
    diagnostic['request_user_agent'] = SUBSCRIPTION_USER_AGENT

    def read_usable_nodes(content):
        nodes = read_nodes(content, diagnostic)
        if allowed_names is None:
            return nodes
        nodes = filter_nodes(nodes, site, diagnostic)
        entries = [entry for entry in diagnostic['node_entries'] if entry['status'] == 'included']
        usable, invalid_dependencies = remove_invalid_dependencies(nodes, allowed_names)
        if invalid_dependencies:
            diagnostic['invalid_nodes'] += invalid_dependencies
            reason = '上游节点缺失或存在循环依赖'
            reasons = diagnostic['invalid_reasons']
            reasons[reason] = reasons.get(reason, 0) + invalid_dependencies
            kept = {id(node) for node in usable}
            for node, entry in zip(nodes, entries):
                if id(node) not in kept:
                    entry.update(status='configuration_error', reason=reason)
        if not usable:
            raise MergeError('节点依赖无效，没有可合并的节点。')
        return usable

    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    # Cache identity follows the URL, so changing subscriptions cannot reuse old nodes.
    cache_file = cache_dir / (sha256(site['url'].encode()).hexdigest() + '.yaml')
    metadata_file = cache_file.with_suffix('.meta.json')
    reason = '无法连接订阅服务商。'
    latest_listing = None
    for _ in range(3):
        try:
            response = requests.get(site['url'], timeout=(10, 30),
                                    headers={'User-Agent': SUBSCRIPTION_USER_AGENT})
            response.raise_for_status()
            diagnostic['traffic'] = traffic_info(response.headers.get('subscription-userinfo', ''))
            # Only replace the cache after filtering and dependency checks pass.
            nodes = read_usable_nodes(response.text)
        except requests.HTTPError as exc:
            reason = f'服务商返回 HTTP {exc.response.status_code}。'
            continue
        except requests.Timeout:
            reason = '连接订阅服务商超时。'
            continue
        except requests.exceptions.SSLError:
            reason = 'HTTPS 连接失败。'
            continue
        except requests.RequestException:
            reason = '无法连接订阅服务商。'
            continue
        except MergeError as exc:
            reason = str(exc)
            if diagnostic.get('listing_read'):
                latest_listing = deepcopy({key: diagnostic[key] for key in ('received_nodes', 'node_entries')})
            continue
        except (yaml.YAMLError, ValueError):
            reason = '订阅返回的配置格式无法解析。'
            continue
        cache_file.write_text(response.text, encoding='utf-8')
        traffic = traffic_info(response.headers.get('subscription-userinfo', ''))
        metadata_file.write_text(json.dumps(traffic), encoding='utf-8')
        diagnostic.update(status='ok', message='读取成功', traffic=traffic, fetched_nodes=len(nodes))
        return nodes
    if cache_file.exists():
        fresh_diagnostic = deepcopy(diagnostic)
        try:
            # The cached response must also satisfy the current source settings.
            nodes = read_usable_nodes(cache_file.read_text(encoding='utf-8'))
        except (OSError, MergeError, yaml.YAMLError):
            diagnostic.clear()
            diagnostic.update(fresh_diagnostic)
        else:
            print('订阅抓取失败，使用上次成功缓存。')
            traffic = None
            try:
                traffic = json.loads(metadata_file.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                pass
            diagnostic.update(status='cached', message=reason + '已使用上次成功缓存。',
                              traffic=traffic, fetched_nodes=len(nodes))
            if latest_listing is not None:
                diagnostic['latest_listing'] = latest_listing
            return nodes
    diagnostic.update(status='failed', message=reason, nodes=0)
    raise MergeError(reason + '没有可用缓存。')


def filter_nodes(nodes, site, diagnostic=None):
    inclusion = [word.lower() for word in site.get('inclusion', [])]
    exclusion = [word.lower() for word in site.get('exclusion', [])]
    result, endpoints = [], set()
    entries = [entry for entry in diagnostic['node_entries'] if entry['status'] == 'included'] if diagnostic is not None else []
    for index, node in enumerate(nodes):
        reason = None
        fields = (node['name'].lower(), node['server'].lower())
        if any(word in field for word in exclusion for field in fields):
            reason = '已按高级配置的 exclusion 筛选'
        elif inclusion and not any(word in field for word in inclusion for field in fields):
            reason = '未匹配高级配置的 inclusion'
        elif site.get('dedup', False):
            try:
                host = socket.getaddrinfo(node['server'], None)[0][4][0]
            except OSError:
                host = node['server'].lower()
            endpoint = (host, node['port'])
            if endpoint in endpoints:
                reason = '已按高级配置去重'
            endpoints.add(endpoint)
        if reason is not None:
            if diagnostic is not None:
                entries[index].update(status='filtered', reason=reason)
                diagnostic['filtered_nodes'] += 1
            continue
        result.append(deepcopy(node))
    if not result:
        raise MergeError('筛选后没有节点；请检查 inclusion 和 exclusion，保留上次订阅。')
    return result



def refresh_routing_groups(config, *, sub3_nodes=(), sub4_nodes=(), node_labels=None):
    """Populate service groups from merged node names without probing endpoints."""
    groups = {group['name']: group for group in config.get('proxy-groups', [])}
    names = [node.get('name', '') for node in config.get('proxies', [])]
    node_labels = node_labels if node_labels is not None else {}
    lowered = {name: node_labels.get(name, name).lower() for name in names}
    services = r'(?:chatgpt|gpt|openai|codex|gemini|google|ai)'
    negative_ai_label = re.compile(
        rf'(?:不支持|不解锁|未解锁|无法使用|禁用|禁止|无)\s*{services}'
        rf'|{services}\s*(?:不可用|不支持|未解锁|不解锁)'
        rf'|(?:no|not[\s_-]+supported|unsupported|blocked|without)[\s_-]+{services}'
        rf'|{services}[\s_-]+(?:unsupported|blocked|unavailable|not[\s_-]+supported)'
    )

    def matching(terms):
        return [name for name in names
                if any(term in lowered[name] for term in terms)
                and not negative_ai_label.search(lowered[name])]

    ai_nodes = matching(('ai专用', 'dmit eb', 'gpt', 'openai', 'codex'))
    gemini_nodes = matching(('gemini', 'google', 'gpt', 'ai专用', '新加坡', '狮城', '台湾'))
    # Source membership comes from merge_config, never from a provider's label.
    # Labels only nominate candidates; they do not prove location or access.
    us_nodes = []
    openai_candidates = [[], [], [], []]
    for name, original_name in sub4_nodes:
        label = original_name.lower()
        compact_label = ''.join(label.split())
        is_us = (any(term in label for term in ('美国', '美西', '美东', '🇺🇸'))
                 or re.search(r'(?<![a-z0-9])(?:us|usa|united states)(?![a-z0-9])', label))
        restricted = any(term in compact_label for term in (
            'ai专用', '仅限ai', '仅ai', 'ai-only', 'ai_only', 'aionly',
            '禁流媒体', '禁止流媒体',
        ))
        if is_us and not restricted:
            us_nodes.append(name)
        if (negative_ai_label.search(label)
                or any(term in label for term in ('官网', '剩余流量', '到期', '有效期', '请更换'))):
            continue
        ai_only = any(term in compact_label for term in (
            'ai专用', '仅限ai', '仅ai', 'ai-only', 'ai_only', 'aionly',
        ))
        openai_hint = any(term in compact_label for term in ('dmiteb', 'chatgpt', 'gpt', 'openai', 'codex'))
        is_de = (any(term in label for term in ('德国', '🇩🇪'))
                 or re.search(r'(?<![a-z0-9])(?:de|germany|deutschland)(?![a-z0-9])', label))
        if ai_only:
            openai_candidates[0].append(name)
        elif openai_hint:
            openai_candidates[1].append(name)
        elif is_us:
            openai_candidates[2].append(name)
        elif is_de:
            openai_candidates[3].append(name)
    if 'OpenAI-Sub4' in groups:
        # Keep OpenAI on a stable, manually selected exit. Source-4 candidates
        # stay first because that subscription is intended for AI, but every
        # merged node remains available for testing other providers too.
        group = groups['OpenAI-Sub4']
        ranked = [name for bucket in openai_candidates for name in bucket]
        all_sub4_names = [name for name, _ in sub4_nodes]
        other_ai_names = [name for name in ai_nodes if name not in all_sub4_names]
        group['proxies'] = list(dict.fromkeys(
            ranked + all_sub4_names + other_ai_names + names
        )) or ['REJECT']
        default_name = group.get('default-selected')
        if default_name not in group['proxies']:
            # Providers often append traffic/IP annotations to a stable node
            # name. Keep the configured default when its name is the prefix.
            matching_default = next(
                (name for name in group['proxies']
                 if isinstance(name, str) and isinstance(default_name, str)
                 and name.startswith(default_name)),
                None,
            )
            group['default-selected'] = matching_default or group['proxies'][0]
        # Older Mihomo versions ignore default-selected and take the first item.
        group['proxies'].remove(group['default-selected'])
        group['proxies'].insert(0, group['default-selected'])
    if 'ChatGPT' in groups:
        # Prioritize name hints, but retain every node for explicit manual choice.
        # Do not introduce AUTO when no hint matches: it can change the AI exit.
        groups['ChatGPT']['proxies'] = list(dict.fromkeys(ai_nodes + names)) or ['REJECT']
    if 'AgentRouter' in groups:
        groups['AgentRouter']['proxies'] = ['DIRECT', *names]
    # Follow the stable OpenAI selector initially, with every source still
    # available for a manual per-service override. Never fall back to AUTO.
    for service in ('Kiro', 'AI-Coding', 'AI-Services'):
        if service in groups:
            shared_exit = ['OpenAI-Sub4'] if 'OpenAI-Sub4' in groups else []
            groups[service]['proxies'] = shared_exit + list(dict.fromkeys(ai_nodes + names)) or ['REJECT']
    if 'US-Sub4' in groups:
        # Do not silently fall back to other subscriptions or AI-only nodes.
        groups['US-Sub4']['proxies'] = us_nodes or ['REJECT']

    if 'GitHub' in groups:
        # Subscription 3 is the IEPL/专线 source, so keep its nodes first for
        # manual fallback. The url-test group itself probes github.com and can
        # skip these entries when the provider's current GitHub route is down.
        github_priority = [name for name, _ in sub3_nodes]
        groups['GitHub']['proxies'] = list(dict.fromkeys(
            github_priority + names
        )) or ['AUTO']

    # A shared selector must be downstream of the two service selectors.
    # Reversing these edges makes the shared switch ineffective (or cyclic).
    shared_google = ['Google-All'] if 'Google-All' in groups else []
    if 'Google-All' in groups:
        groups['Google-All']['proxies'] = names or ['AUTO']
    if 'Gemini' in groups:
        groups['Gemini']['proxies'] = list(dict.fromkeys(
            shared_google + gemini_nodes + ai_nodes + names
        )) or ['AUTO']
    if 'Google' in groups:
        groups['Google']['proxies'] = shared_google + names or ['AUTO']

    # Service groups are selectors, not connectivity claims. Keep all merged
    # nodes available so users can choose a working exit per service.
    for group_name in (
        'Docker', 'Microsoft', 'OneDrive',
        'Telegram', 'Discord', 'Social', 'Media', 'Games', 'Arena', 'Mirasim',
    ):
        if group_name in groups:
            groups[group_name]['proxies'] = names or ['AUTO']


def merge_config(template, sites, cache_dir='.cache', report=None):
    report = report if report is not None else {}
    report['sources'] = []
    config = deepcopy(template)
    if not isinstance(config, dict):
        raise MergeError('模板必须是 YAML 对象。')
    groups = {group['name']: group for group in config.get('proxy-groups', [])}
    proxies = config.get('proxies') or []
    config['proxies'] = proxies
    sub4_nodes = []
    sub3_nodes = []
    node_labels = {node['name']: node['name'] for node in proxies}
    names = {node['name'] for node in proxies} | set(groups) | {'DIRECT', 'REJECT', 'PASS', 'COMPATIBLE'}
    if not sites:
        raise MergeError('请先配置至少一个订阅。')
    for index, site in enumerate(sites, 1):
        diagnostic = dict(index=index, name=site['name'], nodes=0)
        report['sources'].append(diagnostic)
        if site.get('group') not in groups:
            raise MergeError('订阅的 group 在模板中不存在。')
        try:
            nodes = deepcopy(fetch_nodes(site, cache_dir, diagnostic, allowed_names=names))
        except MergeError as exc:
            diagnostic.update(status='failed', message=str(exc), nodes=0)
            print(f'订阅 {index}：{exc} 已跳过。')
            continue
        diagnostic['nodes'] = len(nodes)
        diagnostic.setdefault('status', 'ok')
        diagnostic.setdefault('message', '读取成功')
        # Every source gets a stable prefix. Different subscriptions can use identical names.
        renamed = {}
        entries = iter(entry for entry in diagnostic.get('node_entries', []) if entry['status'] == 'included')
        for node in nodes:
            old_name = node['name']
            base = f"[{site['name']}] {old_name}"
            name, suffix = base, 2
            while name in names:
                name = f'{base} ({suffix})'
                suffix += 1
            names.add(name)
            renamed.setdefault(old_name, name)
            node['name'] = name
            node_labels[name] = old_name
            if index == 4:
                sub4_nodes.append((name, old_name))
            if index == 3:
                sub3_nodes.append((name, old_name))
            entry = next(entries, None)
            if entry is not None:
                entry['merged_name'] = name
        # Preserve references between nodes inside the same subscription.
        for node in nodes:
            reference = node.get('dialer-proxy')
            if reference in renamed:
                node['dialer-proxy'] = renamed[reference]
        proxies.extend(nodes)
        group = groups[site['group']]
        group['proxies'] = list(dict.fromkeys(
            (group.get('proxies') or []) + [node['name'] for node in nodes]
        ))
        skipped = diagnostic.get('invalid_nodes', 0)
        print(f'订阅 {index}：已合并 {len(nodes)} 个节点，{skipped} 项配置无法合并；未检测节点连通性。')
    refresh_routing_groups(config, sub3_nodes=sub3_nodes, sub4_nodes=sub4_nodes, node_labels=node_labels)
    if not proxies:
        report.update(status='failed', nodes=0, message='本次未取得可合并的节点配置，保留上次发布的配置。')
        raise MergeError('本次没有可合并的节点配置，保留上次订阅。')
    if 'AUTO' in groups and not groups['AUTO'].get('proxies'):
        groups['AUTO']['proxies'] = [node['name'] for node in proxies]
    print(f'合并完成，共 {len(proxies)} 个节点。')
    report.update(status='partial' if any(source['status'] in ('failed', 'cached') or source.get('invalid_nodes', 0) for source in report['sources']) else 'ok',
                  nodes=len(proxies), message='合并完成')
    return config


def main():
    if len(sys.argv) not in (2, 3):
        print('Usage: python helper.py <template.yaml> [out.yaml]')
        return 1
    report = dict(updated_at=datetime.now(timezone.utc).isoformat(), status='failed', nodes=0, sources=[])
    try:
        from scripts.prepare_sites import parse_sites

        sites = parse_sites('', Path('sites.yaml').read_text(encoding='utf-8'))
        template = yaml.safe_load(Path(sys.argv[1]).read_text(encoding='utf-8'))
        config = merge_config(template, sites, report=report)
        output = Path(sys.argv[2] if len(sys.argv) == 3 else 'out.yaml')
        temporary = output.with_name(output.name + '.tmp')
        temporary.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding='utf-8')
        temporary.replace(output)
        return 0
    except MergeError as exc:
        report['message'] = str(exc)
        print(f'::error::{exc}')
        return 1
    except (OSError, ValueError, TypeError, KeyError, yaml.YAMLError):
        report['status'] = 'failed'
        report['message'] = '配置格式错误或文件无法读写。'
        print('::error::配置格式错误或文件无法读写；请检查 README，不输出含凭据的配置。')
        return 1
    finally:
        Path('merge-status.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    sys.exit(main())
