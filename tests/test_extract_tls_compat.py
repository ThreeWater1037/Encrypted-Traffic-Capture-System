import unittest
from collections import Counter
import xml.etree.ElementTree as ET
from analysis_scripts.extract_tls import number, consume_packet, ExtractionError


class PdmlBooleanCompatibilityTests(unittest.TestCase):
    def test_boolean_and_numeric_flag_representations(self):
        for flag in ('tcp.flags.syn', 'tcp.flags.ack'):
            for shown, expected in [('True', 1), ('False', 0), ('1', 1), ('0', 0), ('0x01', 1)]:
                with self.subTest(flag=flag, shown=shown):
                    root = ET.Element('proto')
                    ET.SubElement(root, 'field', name=flag, show=shown)
                    self.assertEqual(number(root, flag), expected)

    def test_missing_flag_retains_default(self):
        self.assertEqual(number(ET.Element('proto'), 'tcp.flags.syn', 0), 0)


class QuotedTcpTests(unittest.TestCase):
    def packet(self):
        return ET.fromstring('''<packet><proto name="frame">
          <field name="frame.number" show="3345"/><field name="frame.time_epoch" show="1.25"/>
          </proto><proto name="ip"><field name="ip.src" show="a"/><field name="ip.dst" show="b"/></proto>
          <proto name="tcp"><field name="tcp.stream" show="57"/><field name="tcp.srcport" show="1234"/>
          <field name="tcp.dstport" show="443"/><field name="tcp.len" show="10"/></proto></packet>''')

    def quote(self,p,kind):
        control=ET.SubElement(p,'proto',name=kind)
        inner=ET.SubElement(control,'proto',name='tcp')
        ET.SubElement(inner,'field',name='tcp.stream',show='57')
        ET.SubElement(inner,'field',name='tcp.len',show='999')
        return control

    def test_icmp_and_icmpv6_quotes_do_not_create_flows(self):
        for kind in ('icmp','icmpv6'):
            with self.subTest(kind=kind):
                p=self.packet();p.remove(p.find('proto[@name="tcp"]'));self.quote(p,kind)
                flows={};stats=Counter();consume_packet(p,flows,stats)
                self.assertEqual(flows,{})
                self.assertEqual(stats['ignored_icmp_quoted_tcp'],1)
                self.assertEqual(stats['tcp_payload_packets'],0)

    def test_quote_does_not_inflate_existing_flow(self):
        flows={};stats=Counter();p=self.packet();consume_packet(p,flows,stats)
        p.remove(p.find('proto[@name="tcp"]'));self.quote(p,'icmp');consume_packet(p,flows,stats)
        self.assertEqual(flows['57'].tcp,[(10,0,1250)])
        self.assertEqual(stats['tcp_payload_packets'],1)

    def test_direct_tcp_with_quote_retains_only_direct_payload(self):
        p=self.packet();self.quote(p,'icmp');flows={};stats=Counter()
        consume_packet(p,flows,stats)
        self.assertEqual(flows['57'].tcp,[(10,0,1250)])

    def test_unrelated_nested_tcp_remains_explicit_error(self):
        p=self.packet();tcp=p.find('proto[@name="tcp"]');p.remove(tcp)
        ET.SubElement(p,'proto',name='unknown_tunnel').append(tcp)
        with self.assertRaisesRegex(ExtractionError,'嵌套 TCP'):
            consume_packet(p,{},Counter())

    def test_deep_quote_cannot_change_roles_or_tls_features(self):
        for kind in ('icmp', 'icmpv6'):
            with self.subTest(kind=kind):
                flows = {}; stats = Counter()
                consume_packet(self.packet(), flows, stats)
                packet = self.packet()
                packet.remove(packet.find('proto[@name="tcp"]'))
                control = ET.SubElement(packet, 'proto', name=kind)
                quoted = ET.SubElement(control, 'field', name='quoted_packet')
                tcp = ET.SubElement(quoted, 'proto', name='tcp')
                for name, value in [('tcp.stream', '57'), ('tcp.len', '999'),
                                    ('tcp.flags.syn', 'True')]:
                    ET.SubElement(tcp, 'field', name=name, show=value)
                tls = ET.SubElement(quoted, 'proto', name='tls')
                hello = ET.SubElement(tls, 'field', name='tls.handshake')
                for name, value in [('tls.handshake.type', '1'),
                                    ('tls.handshake.version', '0x0303'),
                                    ('tls.handshake.extensions_server_name', 'quoted.invalid')]:
                    ET.SubElement(hello, 'field', name=name, show=value)
                consume_packet(packet, flows, stats)
                self.assertEqual(flows['57'].tcp, [(10, 0, 1250)])
                self.assertEqual(flows['57'].syn_clients, set())
                self.assertEqual(flows['57'].hello_clients, set())
                self.assertEqual(flows['57'].names, set())
                self.assertEqual(flows['57'].records, [])
                self.assertEqual(stats['ignored_icmp_quoted_tcp'], 1)


if __name__ == '__main__':
    unittest.main()
