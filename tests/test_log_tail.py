import subprocess
import unittest
from unittest.mock import patch

from panel import panel


class ReadRemoteTailTests(unittest.TestCase):
    def read_remote(self, content, cap, tail=True):
        def fake_pull(_serial, _remote, local):
            with open(local, "wb") as output:
                output.write(content)
            return subprocess.CompletedProcess([], 0)

        with patch.object(panel, "adb_pull", side_effect=fake_pull):
            return panel.read_remote("offline-test", "/unused", cap=cap, tail=tail)

    def test_small_file_returns_all_bytes(self):
        content = b"one\ntwo\n"
        self.assertEqual(self.read_remote(content, cap=64), content)

    def test_oversized_file_returns_distinct_tail_lines(self):
        content = b"discarded\nlast-one\nlast-two\n"
        self.assertEqual(self.read_remote(content, cap=20), b"last-one\nlast-two\n")

    def test_oversized_single_line_keeps_final_bytes(self):
        content = b"prefix-" + b"x" * 40 + b"-tail"
        self.assertEqual(self.read_remote(content, cap=12), content[-12:])

    def test_default_read_still_returns_prefix_up_to_cap_plus_one(self):
        content = b"0123456789abcdef"
        self.assertEqual(self.read_remote(content, cap=5, tail=False), content[:6])


if __name__ == "__main__":
    unittest.main()
