import unittest
import xml.etree.ElementTree as ET
from analysis_scripts.extract_tls import number


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


if __name__ == '__main__':
    unittest.main()
