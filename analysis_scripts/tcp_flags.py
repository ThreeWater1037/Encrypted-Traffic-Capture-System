"""Shared, evidence-based classification for TCP packets without payload."""


TCP_CONTROL_FIELDS = ("syn", "syn_ack", "fin", "fin_ack", "rst", "rst_ack", "tcp_other")
TCP_CATEGORIES = {0x10: "ack", 0x02: "syn", 0x12: "syn_ack",
                  0x01: "fin", 0x11: "fin_ack", 0x04: "rst", 0x14: "rst_ack"}


def parse_tcp_flags(flags):
    """Require the observed mask; missing flags must never become ACK."""
    try:
        value = str(flags).strip()
        bits = int(value, 16 if value.lower().startswith("0x") else 10)
        if not 0 <= bits <= 0xFFF:
            raise ValueError("flags out of range")
    except (TypeError, ValueError):
        raise ValueError(
            "Missing or invalid tcp_flags; rerun infer_packets.py/classify_packets.py "
            "from the original PCAP (do not reuse old TSVs)"
        ) from None
    return bits


def empty_tcp_category(flags):
    """Mutually exclusive categories for zero-payload packets."""
    bits = parse_tcp_flags(flags)
    return TCP_CATEGORIES.get(bits, "tcp_other" if bits else "unknown")


def tcp_flags_text(flags):
    """Keep every observed bit, including unusual combinations and reserved bits."""
    bits = parse_tcp_flags(flags)
    names = ((0x01, "FIN"), (0x02, "SYN"), (0x04, "RST"), (0x08, "PSH"),
             (0x10, "ACK"), (0x20, "URG"), (0x40, "ECE"), (0x80, "CWR"))
    labels = [name for bit, name in names if bits & bit]
    labels.extend(f"BIT_0x{bit:03X}" for bit in (0x100, 0x200, 0x400, 0x800) if bits & bit)
    return "|".join(labels) or "NONE"
