"""Disambiguate port-only filenames and reject merged TCP connections."""

from collections import Counter

# An ICMP error quotes the offending TCP header; it is not a TCP packet.
TCP_DISPLAY_FILTER = "tcp.port == 443 && !(icmp || icmpv6)"
IDENTITY_FIELDS = ["tls.handshake.extensions_server_name", "tls.handshake.version",
                   "tls.record.version", "tls.handshake.extensions.supported_version"]


def packet_key(pkt):
    return (pkt.get('ip.src') or pkt.get('ipv6.src'),
            pkt.get('ip.dst') or pkt.get('ipv6.dst'),
            pkt.get('tcp.srcport', '').split(',')[0].strip(),
            pkt.get('tcp.dstport', '').split(',')[0].strip(),
            pkt.get('tcp.stream', '').strip())


def split_connections(flow_meta, packets):
    """Expand a legacy endpoint index into actual directional TCP streams.

    TSV columns remain unchanged. Stream identity lives in comment metadata
    and in filenames only when their original port-only names collide.
    """
    result, evidence = {}, {}
    versions = {'0x0301': 'TLSv1.0', '0x0302': 'TLSv1.1',
                '0x0303': 'TLSv1.2', '0x0304': 'TLSv1.3'}
    for pkt in packets:
        key = packet_key(pkt)
        if key[:4] not in flow_meta:
            continue
        if not key[4].isdigit():
            raise ValueError('TCP stream identity unavailable')
        if any(',' in str(v) for v in key):
            raise ValueError('Ambiguous encapsulated TCP endpoints')
        result.setdefault(key, {**flow_meta[key[:4]], 'tcp_stream': key[4]})
        # Include endpoints as well as stream ID to avoid joining distinct tuples.
        conn = (tuple(sorted(((key[0], key[2]), (key[1], key[3])))), key[4])
        ev = evidence.setdefault(conn, {'sni': set(), 'version': set(), 'clients': set()})
        if key[2] == key[3] == '443':
            flags = int(pkt.get('tcp.flags') or '0', 16)
            if flags & 2:
                ev['clients'].add((key[1], key[3]) if flags & 16 else (key[0], key[2]))
        ev['sni'].update(v for v in pkt.get(IDENTITY_FIELDS[0], '').split(',') if v)
        raw = (pkt.get(IDENTITY_FIELDS[3]) or pkt.get(IDENTITY_FIELDS[1])
               or pkt.get(IDENTITY_FIELDS[2]) or '')
        ev['version'].update(v for v in raw.split(',') if v in versions)
    for key, meta in result.items():
        conn = (tuple(sorted(((key[0], key[2]), (key[1], key[3])))), key[4])
        ev = evidence[conn]
        if key[2] == key[3] == '443':
            if len(ev['clients']) != 1:
                raise ValueError(f'Cannot identify client/server for TCP stream {key[4]} with both ports 443')
            meta['direction'] = 'forward' if (key[0], key[2]) in ev['clients'] else 'reverse'
        if len(ev['sni']) > 1:
            raise ValueError(f'Conflicting SNI within TCP stream {key[4]}')
        meta['sni'] = next(iter(ev['sni']), '')
        meta['tls_version'] = versions[max(ev['version'])] if ev['version'] else '-1'
        # Legacy endpoint-wide protocol hints may belong to another connection.
        meta['app_protocol'] = 'other'
    names = {k: f"{m['direction']}_{m['src_port']}_{m['dst_port']}"
             for k, m in result.items()}
    counts = Counter(names.values())
    collided = {k[4] for k, name in names.items() if counts[name] > 1}
    names = {k: name + (f'_tcp_{k[4]}' if k[4] in collided else '') + '.tsv'
             for k, name in names.items()}
    if len(set(names.values())) != len(names):
        raise ValueError('Unresolved flow filename collision')
    return result, names


def validate_connections(flow_meta, packets):
    names = {key: f"{meta['direction']}_{meta['src_port']}_{meta['dst_port']}"
             for key, meta in flow_meta.items()}
    counts = Counter(names.values())
    streams = {}
    for pkt in packets:
        key = (pkt.get('ip.src') or pkt.get('ipv6.src'),
               pkt.get('ip.dst') or pkt.get('ipv6.dst'),
               pkt.get('tcp.srcport', '').split(',')[0].strip(),
               pkt.get('tcp.dstport', '').split(',')[0].strip())
        stream = pkt.get('tcp.stream', '')
        if key not in flow_meta or not stream:
            continue
        previous = streams.setdefault(key, stream)
        if previous != stream:
            raise ValueError(f"Reused TCP endpoints in streams {previous}, {stream}; "
                             "legacy flow format cannot distinguish these connections")
    collided_streams = set()
    for key, name in names.items():
        if counts[name] > 1:
            if key not in streams:
                raise ValueError(f"Flow filename collision: {name}; TCP stream identity unavailable")
            collided_streams.add(streams[key])
    # Both directions of a colliding connection receive the same stream suffix.
    result = {key: name + (f"_tcp_{streams[key]}" if streams.get(key) in collided_streams else "") + '.tsv'
              for key, name in names.items()}
    if len(set(result.values())) != len(result):
        raise ValueError("Unresolved flow filename collision")
    return result
