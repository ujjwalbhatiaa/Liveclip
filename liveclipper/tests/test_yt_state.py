import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import liveclipper as L

def page(status, reason="", extra=""):
    r = f',"reason":"{reason}"' if reason else ""
    return f'<html>{extra}"playabilityStatus":{{"status":"{status}"{r}}}</html>'

class YtState(unittest.TestCase):
    def test_bot_check_is_blocked(self):
        p = page("LOGIN_REQUIRED", "Sign in to confirm you’re not a bot")
        self.assertEqual(L.yt_classify(p), "blocked")
        with self.assertRaises(L.AccessBlocked):
            L.yt_initial('<link rel="canonical" href="undefined">' + p)
    def test_offline_stream(self):
        p = page("LIVE_STREAM_OFFLINE", "This live event will begin in 2 hours.")
        self.assertEqual(L.yt_classify(p), "offline")
        with self.assertRaises(LookupError) as c:
            L.yt_initial('<link rel="canonical" href="undefined">' + p)
        self.assertNotIsInstance(c.exception, L.AccessBlocked)
    def test_unrecognised_page_is_unknown(self):
        self.assertEqual(L.yt_classify("<html>weird</html>"), "unknown")
        with self.assertRaises(L.StateUnknown):
            L.yt_initial("<html>weird</html>")
    def test_blocked_not_subclass_of_lookup(self):
        self.assertFalse(issubclass(L.AccessBlocked, LookupError))
        self.assertFalse(issubclass(L.StateUnknown, LookupError))
    def test_live_page_without_chat_is_not_blocked(self):
        p = page("OK", "", '"isLive":true ')
        self.assertEqual(L.yt_classify(p), "unknown")

if __name__ == "__main__":
    unittest.main()
