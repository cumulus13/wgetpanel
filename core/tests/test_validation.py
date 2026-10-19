import os
import stat

from django.test import SimpleTestCase, TestCase

from core import wget
from core.models import Download, SpeedRule
from core.tests.helpers import PanelTestCase


class UrlTests(PanelTestCase):
    def test_schemes(self):
        for bad in ("file:///etc/passwd", "gopher://x/", "javascript:alert(1)", "http://", "ftp:/x"):
            with self.assertRaises(wget.ValidationError, msg=bad):
                wget.validate_url(bad, block_private=False)
        self.assertEqual(wget.validate_url("https://example.com/a.iso", block_private=False), "https://example.com/a.iso")

    def test_no_credentials_or_whitespace_in_url(self):
        for bad in ("http://u:p@example.com/x", "http://example.com/a b", "http://example.com/a\nb"):
            with self.assertRaises(wget.ValidationError):
                wget.validate_url(bad, block_private=False)

    def test_private_addresses_blocked(self):
        for bad in ("http://127.0.0.1/x", "http://localhost/x", "http://10.1.2.3/x", "http://169.254.169.254/latest/meta-data",
                    "http://[::1]/x", "http://192.168.0.1/x"):
            with self.assertRaises(wget.ValidationError, msg=bad):
                wget.validate_url(bad, block_private=True)
        wget.validate_url("http://93.184.216.34/x", block_private=True)  # public literal, no DNS needed


class PathTests(PanelTestCase):
    def test_filename(self):
        self.assertEqual(wget.filename_from_url("http://h/a/b/My%20File.iso?x=1"), "My File.iso")
        self.assertEqual(wget.filename_from_url("http://h/"), "download")
        self.assertEqual(wget.safe_filename("../../etc/passwd"), "passwd")
        self.assertEqual(wget.safe_filename(".."), "")

    def test_directory_confinement(self):
        self.assertEqual(wget.validate_directory(""), os.path.realpath(self.dl_dir))
        sub = wget.validate_directory("movies")
        self.assertEqual(sub, os.path.join(os.path.realpath(self.dl_dir), "movies"))
        for bad in ("../outside", "/etc", "/", "movies/../../x"):
            with self.assertRaises(wget.ValidationError, msg=bad):
                wget.validate_directory(bad)

    def test_symlink_escape(self):
        os.symlink("/etc", os.path.join(self.dl_dir, "evil"))
        with self.assertRaises(wget.ValidationError):
            wget.validate_directory("evil")


class OptionTests(SimpleTestCase):
    def test_rate(self):
        self.assertEqual(wget.parse_rate("500k"), 512000)
        self.assertEqual(wget.parse_rate("2M"), 2 * 1024 * 1024)
        self.assertEqual(wget.parse_rate("1000"), 1000)
        with self.assertRaises(wget.ValidationError):
            wget.parse_rate("fast")

    def test_whitelist_drops_unknown(self):
        out = wget.validate_options({"limit_rate": "1m", "tries": "3", "evil": "x", "execute": "rm -rf /", "check_certificate": False})
        self.assertEqual(out, {"limit_rate": 1048576, "tries": 3, "check_certificate": False})

    def test_header_injection(self):
        for bad in ("X-A: b\r\nHost: evil", "Host: evil", "no colon", "X: " + "a" * 2000):
            with self.assertRaises(wget.ValidationError, msg=bad):
                wget.validate_options({"headers": [bad]})
        self.assertEqual(wget.validate_options({"headers": "X-Token: abc"})["headers"], ["X-Token: abc"])

    def test_bounds(self):
        with self.assertRaises(wget.ValidationError):
            wget.validate_options({"tries": 1000})


class WgetrcTests(PanelTestCase):
    def test_credentials_only_in_private_file(self):
        d = self.make_download("http://h/f.bin", http_user="bob")
        d.http_password = "s3cret"
        d.save()
        rc = wget.write_wgetrc(d, rate=0)
        try:
            self.assertEqual(stat.S_IMODE(os.stat(rc).st_mode), 0o600)
            self.assertIn("http_password = s3cret", open(rc).read())
            cmd = " ".join(wget.build_command(d, rc))
            self.assertNotIn("s3cret", cmd)
            self.assertNotIn("bob", cmd)
            self.assertIn("-O", wget.build_command(d, rc))
            self.assertEqual(wget.build_command(d, rc)[-2:], ["--", "http://h/f.bin"])
            self.assertNotIn("s3cret", d.http_password_enc)  # encrypted at rest
        finally:
            os.unlink(rc)

    def test_newline_in_password_rejected(self):
        d = self.make_download("http://h/f.bin", http_user="bob")
        d.http_password = "a\nheader = X: y"
        with self.assertRaises(wget.ValidationError):
            wget.write_wgetrc(d, 0)


class RuleTests(PanelTestCase):
    def test_window_and_midnight(self):
        import datetime as dt
        from django.utils import timezone
        tz = timezone.get_current_timezone()
        at = lambda y, m, d, hh, mm: dt.datetime(y, m, d, hh, mm, tzinfo=tz)  # 2026-10-19 is a Monday
        day = SpeedRule(name="day", start=dt.time(9), end=dt.time(17), limit_kbps=100, days="0,1,2,3,4")
        self.assertTrue(day.is_active(at(2026, 10, 19, 12, 0)))
        self.assertFalse(day.is_active(at(2026, 10, 19, 18, 0)))
        self.assertFalse(day.is_active(at(2026, 10, 24, 12, 0)))  # Saturday
        night = SpeedRule(name="night", start=dt.time(22), end=dt.time(6), limit_kbps=50, days="4")  # Friday night
        self.assertTrue(night.is_active(at(2026, 10, 23, 23, 0)))   # Fri 23:00
        self.assertTrue(night.is_active(at(2026, 10, 24, 5, 0)))    # Sat 05:00 still Friday's window
        self.assertFalse(night.is_active(at(2026, 10, 24, 23, 0)))  # Sat 23:00
        self.assertFalse(SpeedRule(name="off", enabled=False, limit_kbps=1).is_active(at(2026, 10, 19, 12, 0)))

    def test_effective_rate_takes_minimum(self):
        import datetime as dt
        SpeedRule.objects.create(name="all", start=dt.time(0, 0), end=dt.time(23, 59), limit_kbps=200)
        d = self.make_download("http://h/f", options={"limit_rate": 100 * 1024})
        self.assertEqual(wget.effective_rate(d), 100 * 1024)
        d.options = {}
        self.assertEqual(wget.effective_rate(d), 200 * 1024)
