import unittest

from panel import panel


class ParseLsTextTests(unittest.TestCase):
    def test_link_count_layout(self):
        text = (
            "total 24\n"
            "-rw-rw---- 1 root sdcard_r 8998 2026-09-10 13:24 ui.xml\n"
            "drwxrwx--- 2 root sdcard_r 4096 2026-09-09 19:46 svc\n")
        self.assertEqual(panel.parse_ls_text(text), [
            {"name": "svc", "type": "dir", "size": 4096,
             "mtime": "2026-09-09 19:46"},
            {"name": "ui.xml", "type": "file", "size": 8998,
             "mtime": "2026-09-10 13:24"},
        ])

    def test_no_link_count_layout(self):
        text = (
            "-rwxrwx--- root     sdcard_r       20 2026-09-09 19:47 sup.pid\n"
            "drwxrwx--- root     sdcard_r          2026-09-09 19:46 svc\n")
        self.assertEqual(panel.parse_ls_text(text), [
            {"name": "svc", "type": "dir", "size": 0,
             "mtime": "2026-09-09 19:46"},
            {"name": "sup.pid", "type": "file", "size": 20,
             "mtime": "2026-09-09 19:47"},
        ])

    def test_crlf_and_bare_filename_fallback(self):
        text = "hello.txt\r\n-rw-r--r-- 1 root root 5 2026-10-28 17:05 real.txt\r\n"
        self.assertEqual(panel.parse_ls_text(text), [
            {"name": "hello.txt", "type": "file", "size": 0, "mtime": ""},
            {"name": "real.txt", "type": "file", "size": 5,
             "mtime": "2026-10-28 17:05"},
        ])

    def test_symlink_target_is_stripped(self):
        text = "lrwxrwxrwx 1 root root 11 2026-09-10 13:24 link -> /sdcard/x\n"
        self.assertEqual(panel.parse_ls_text(text), [
            {"name": "link", "type": "link", "size": 11,
             "mtime": "2026-09-10 13:24"},
        ])

    def test_dot_entries_and_total_are_skipped(self):
        text = (
            "total 0\n"
            "drwxrwx--- 1 root sdcard_r 4096 2026-09-09 19:46 .\n"
            "drwxrwx--- 1 root sdcard_r 4096 2026-09-09 19:46 ..\n")
        self.assertEqual(panel.parse_ls_text(text), [])


if __name__ == "__main__":
    unittest.main()
