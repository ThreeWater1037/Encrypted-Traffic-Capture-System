"""Extend extract_forward_seqs to both analysis modes and all three directions.

The original inferred sequence rule is deliberately retained. See
HEADER_SEQUENCES.md for the different meaning of decrypted lengths.
"""

import argparse
try:
    from .tcp_flags import empty_tcp_category
except ImportError:
    from tcp_flags import empty_tcp_category
import csv
import json
import logging
import re
from collections import Counter
from pathlib import Path

try:
    from .extract_forward_seqs import COUNT_FIELDS, parse_id_name, process_flow_file
    from .result_layout import page_name
except ImportError:
    from extract_forward_seqs import COUNT_FIELDS, parse_id_name, process_flow_file
    from result_layout import page_name

FIELDS = ["id", "wiki_name", "flow", "sni"] + COUNT_FIELDS + ["header_lens"]
DECRYPTED_FIELDS = FIELDS[:4] + ["protocol"] + FIELDS[4:]
DIRECTIONS = ("forward", "reverse", "bidirectional")
MODES = ("inferred", "decrypted")
log = logging.getLogger(__name__)


def read_flow(path):
    meta, lines = {}, []
    with path.open(encoding="utf-8-sig") as fh:
        for line in fh:
            if line.startswith("#"):
                key, sep, value = line[1:].partition(":")
                if sep:
                    meta[key.strip()] = value.strip()
            elif line.strip():
                lines.append(line)
    reader = csv.DictReader(lines, delimiter="\t")
    if not reader.fieldnames or "frame_no" not in reader.fieldnames:
        raise ValueError(f"Missing frame_no column: {path}")
    rows = list(reader)
    for row in rows:
        row["frame_no"] = int(row["frame_no"])
    rows.sort(key=lambda row: row["frame_no"])
    frames = [row["frame_no"] for row in rows]
    if len(set(frames)) != len(frames):
        raise ValueError(f"Duplicate frame numbers: {path}")
    return meta, rows, reader.fieldnames


def connection_key(meta):
    """Use endpoints, not just SNI or the legacy port-only filename."""
    keys = ("src_ip", "src_port", "dst_ip", "dst_port", "transport", "direction")
    if any(not meta.get(key) for key in keys):
        raise ValueError("Flow metadata requires endpoints, transport and direction")
    direction = meta["direction"]
    if direction not in DIRECTIONS[:2]:
        raise ValueError(f"Unknown direction: {direction}")
    src, dst = (meta["src_ip"], meta["src_port"]), (meta["dst_ip"], meta["dst_port"])
    if direction == "reverse":
        src, dst = dst, src
    return src, dst, meta["transport"], meta.get("tcp_stream", "")


def decrypted_info(row, protocol="http2"):
    """Return a mutually exclusive packet class and all observed HEADERS lengths.

    A packet may carry HEADERS together with DATA/control frames. Its HEADERS
    must not disappear just because the old classifier called the packet data.
    No HTTP/1, TLS or control-frame lengths are substituted for HEADERS.
    """
    if protocol == "http1":
        raw = row.get("http1_header_lens", "").strip()
        if raw not in ("", "-"):
            lengths = [int(value) for value in raw.split(",")]
            kinds = row.get("http1_message_types", "").split(",")
            if len(lengths) != len(kinds) or any(k not in ("request", "response") for k in kinds):
                raise ValueError("HTTP/1.1 type/length mismatch")
            if any(value <= 0 for value in lengths):
                raise ValueError("Nonpositive HTTP/1.1 header length")
            return "header", lengths
        row = {**row, "h2_frame_type": "-", "h2_frame_len": "-"}
    types = [v.strip() for v in row.get("h2_frame_type", "").split(",")]
    sizes = [v.strip() for v in row.get("h2_frame_len", "").split(",")]
    types = [] if types in ([""], ["-"]) else types
    sizes = [] if sizes in ([""], ["-"]) else sizes
    if len(types) != len(sizes):
        raise ValueError(f"HTTP/2 type/length mismatch at frame {row['frame_no']}")
    lengths = []
    for kind, size in zip(types, sizes):
        length = int(size)
        if length < 0:
            raise ValueError("Negative HTTP/2 length")
        if kind in ("HEADERS", "1"):
            lengths.append(length)
    if lengths:
        return "header", lengths
    if any(kind in ("DATA", "0") for kind in types):
        return "data", []
    if types:
        return "control", []
    ct = row.get("tls_content_type", "").split(",")[0].strip()
    if ct in ("Handshake", "ChangeCipherSpec", "22", "20"):
        return "handshake", []
    if ct in ("Alert", "21"):
        return "unknown", []
    if row.get("tcp_payload_len") == "0":
        return empty_tcp_category(row.get("tcp_flags")), []
    return "tls_segment", []


def load_flow(path, mode, protocol="http2"):
    meta, rows, columns = read_flow(path)
    required = ({"category", "est_h2_payload_range"} if mode == "inferred" else
                {"h2_frame_type", "h2_frame_len", "tls_content_type", "tcp_payload_len"})
    if not required.issubset(columns):
        raise ValueError(f"Wrong {mode} input schema: {path}")
    if mode == "decrypted" and protocol == "http1" and not {"http1_header_lens", "http1_message_types"}.issubset(columns):
        raise ValueError(f"Missing HTTP/1.1 columns; rerun classify_packets.py: {path}")
    events = []
    if mode == "inferred":
        # Counts and range handling are exactly those of the original script.
        legacy = process_flow_file(path)
        counts = {key: legacy[key] for key in COUNT_FIELDS}
        for row in rows:
            if row["category"].strip() == "header":
                try:
                    value = int(row["est_h2_payload_range"].strip().split("-")[0])
                except ValueError:
                    continue
                events.append((row["frame_no"], value, meta["direction"]))
    else:
        counts = dict.fromkeys(COUNT_FIELDS, 0)
        for row in rows:
            category, lengths = decrypted_info(row, protocol)
            counts[category] += 1
            events.extend((row["frame_no"], value, meta["direction"]) for value in lengths)
    return {"path": path, "meta": meta, "rows": rows, "counts": counts, "events": events}


def summarize_directory(folder, mode, directions=DIRECTIONS, allow_unpaired=False, audit=None,
                        protocol="http2", only_observed=False):
    """One result row per connection and direction; missing pairs fail explicitly."""
    groups = {}
    for path in sorted(folder.glob("*.tsv")):
        if not path.name.startswith(("forward_", "reverse_")):
            continue
        flow = load_flow(path, mode, protocol)
        key = connection_key(flow["meta"])
        group = groups.setdefault(key, {})
        direction = flow["meta"]["direction"]
        if direction in group:
            raise ValueError(f"Duplicate connection/direction in {folder}: {key}")
        group[direction] = flow
    output = {direction: [] for direction in directions}
    entry_id, name = parse_id_name(page_name(folder))
    for key, group in groups.items():
        if mode == "decrypted" and (protocol == "http1" or only_observed):
            observed = set()
            for flow in group.values():
                observed.update(flow["meta"].get("observed_app_protocol", "").split(","))
                if flow["events"]:
                    observed.add(protocol)
                if any(row.get("h2_frame_type", "") not in ("", "-") for row in flow["rows"]):
                    observed.add("http2")
            if protocol not in observed:
                continue
        paired = set(group) == {"forward", "reverse"}
        if audit is not None:
            audit.append({"source": str(folder), "mode": mode,
                          "protocol": protocol if mode == "decrypted" else "legacy_heuristic",
                          "client": key[0], "server": key[1],
                          "directions": sorted(group), "paired": paired,
                          "header_events": sum(len(f["events"]) for f in group.values())})
        snis = {flow["meta"].get("sni", "") for flow in group.values()} - {"", "-"}
        if len(snis) > 1:
            raise ValueError(f"Conflicting SNI for connection: {key}")
        sni = next(iter(snis), "")
        for direction in directions:
            if direction == "bidirectional":
                if not paired and not allow_unpaired:
                    raise ValueError(f"Missing direction for bidirectional flow: {folder}, {key}")
                if not paired:
                    log.warning("Unpaired connection: %s %s; merging observed packets only", folder, key)
                selected = [group[d] for d in DIRECTIONS[:2] if d in group]
                frames = [row["frame_no"] for flow in selected for row in flow["rows"]]
                if len(frames) != len(set(frames)):
                    raise ValueError(f"Duplicate frame across directions: {folder}, {key}")
                if "forward" in group:
                    tail = group["forward"]["path"].stem.removeprefix("forward_")
                else:
                    tail = f"{key[0][1]}_{key[1][1]}"
                    suffix = group["reverse"]["path"].stem.split("_")[3:]
                    if suffix:
                        tail += "_" + "_".join(suffix)
                flow_name = "bidirectional_" + tail
            elif direction in group:
                selected = [group[direction]]
                flow_name = selected[0]["path"].stem
            else:
                continue
            counts = Counter()
            events = []
            for flow in selected:
                counts.update(flow["counts"])
                events.extend(flow["events"])
            # Stable sorting preserves frame-internal HEADERS order.
            events.sort(key=lambda event: event[0])
            lengths = [f"{'-' if direction == 'bidirectional' and side == 'reverse' else ''}{value}"
                       for _, value, side in events]
            output[direction].append({
                "id": entry_id, "wiki_name": name, "flow": flow_name, "sni": sni,
                **{field: counts[field] for field in COUNT_FIELDS},
                "header_lens": ",".join(lengths),
            })
    return output


def extract(root, browser, modes, directions, output_dir, min_header=0, split_sni=False,
            allow_unpaired=False, decrypted_protocol="both"):
    if not root.is_dir():
        raise ValueError(f"Input root does not exist: {root}")
    results = {}
    audit = []
    # Validate all input before opening any result files.
    for mode in modes:
        suffix = "inferred" if mode == "inferred" else "flows"
        folders = sorted(root.rglob(f"capture_{browser}_{suffix}"))
        folders = [folder for folder in folders if folder.is_dir()]
        if not folders:
            raise ValueError(f"No capture_{browser}_{suffix} directories under {root}")
        protocols = (("http1", "http2") if decrypted_protocol == "both" else (decrypted_protocol,)) if mode == "decrypted" else ("http2",)
        for protocol in protocols:
            merged = {direction: [] for direction in directions}
            for folder in folders:
                summaries = summarize_directory(folder, mode, directions, allow_unpaired, audit,
                                                protocol, mode == "decrypted")
                for direction, rows in summaries.items():
                    if mode == "decrypted":
                        rows = [{**row, "protocol": "HTTP/1.1" if protocol == "http1" else "HTTP/2"}
                                for row in rows]
                    merged[direction].extend(row for row in rows if row["header"] >= min_header)
            combined = results.setdefault(mode, {direction: [] for direction in directions})
            for direction, rows in merged.items():
                combined[direction].extend(rows)
    written = {}
    for mode, summaries in results.items():
        target = output_dir / mode
        target.mkdir(parents=True, exist_ok=True)
        for direction, rows in summaries.items():
            partitions = {direction: rows}
            if split_sni:
                partitions = {}
                for row in rows:
                    # Percent-encode unsafe bytes (including %) without collisions.
                    sni = row['sni'] or 'unknown'
                    safe = ''.join(c if re.fullmatch(r'[a-zA-Z0-9._-]', c)
                                   else ''.join(f'%{b:02X}' for b in c.encode()) for c in sni)
                    partitions.setdefault(f"{direction}_{safe}", []).append(row)
            for stem, subset in partitions.items():
                path = target / f"{stem}.tsv"
                with path.open("w", encoding="utf-8", newline="") as fh:
                    fields = DECRYPTED_FIELDS if mode == "decrypted" else FIELDS
                    writer = csv.DictWriter(fh, fieldnames=fields, delimiter="\t")
                    writer.writeheader()
                    writer.writerows(subset)
                written[str(path)] = len(subset)
                log.info("%s: %d flows, %d nonempty header sequences", path, len(subset),
                         sum(bool(row["header_lens"]) for row in subset))
    report = {"browser": browser, "allow_unpaired": allow_unpaired,
              "min_header": min_header, "files": written, "connections": audit,
              "inferred_semantics": "legacy TLS-length heuristic, not verified HTTP/2",
              "decrypted_protocol": decrypted_protocol,
              "ordering": "TShark dissection/reassembly completion frame; stable message order within frame",
              "decrypted_semantics": {
                  "http1": "HTTP/1.1 start line + fields + final CRLF, raw bytes, excluding body",
                  "http2": "observed HTTP/2 HEADERS frame payload lengths",
                  "empty": "does not prove decryption failure"}}
    (output_dir / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return written


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="Recursively scan capture directories")
    parser.add_argument("--browser", default="chrome", choices=("chrome", "edge", "firefox"))
    parser.add_argument("--mode", choices=(*MODES, "both"), default="both")
    parser.add_argument("--direction", choices=(*DIRECTIONS, "all"), default="all")
    parser.add_argument("--protocol", choices=("http1", "http2", "both"), default="both",
                        help="Decrypted protocols (default both); merged by direction with a protocol column")
    parser.add_argument("--output-dir", type=Path, help="Default: <root>/header_sequences/<browser>")
    parser.add_argument("--min-header", type=int, default=0)
    parser.add_argument("--split-sni", action="store_true")
    parser.add_argument("--allow-unpaired", action="store_true",
                        help="Explicitly allow one-sided captures; record them in summary.json")
    args = parser.parse_args()
    if args.min_header < 0:
        parser.error("--min-header must be nonnegative")
    try:
        extract(args.root, args.browser, MODES if args.mode == "both" else (args.mode,),
                DIRECTIONS if args.direction == "all" else (args.direction,),
                args.output_dir or args.root / "header_sequences" / args.browser,
                args.min_header, args.split_sni, args.allow_unpaired, args.protocol)
    except (ValueError, OSError) as exc:
        parser.exit(1, f"Extraction failed: {exc}\n")


if __name__ == "__main__":
    main()
