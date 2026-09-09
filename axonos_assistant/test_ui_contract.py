"""Static UI contracts that do not require a running GTK display."""

import configparser
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
AXONAI_SOURCE = (ROOT / "axonos_assistant" / "main.py").read_text(encoding="utf-8")
TALK_SOURCE = (ROOT / "talk_to_k" / "main.py").read_text(encoding="utf-8")


class DesktopUiContractTests(unittest.TestCase):
    def test_windows_are_single_instance_and_maximize_on_first_map(self):
        self.assertIn("class AxonAIWindow(Gtk.ApplicationWindow)", AXONAI_SOURCE)
        self.assertIn('application_id="org.axonos.AxonAI"', AXONAI_SOURCE)
        self.assertIn("class TalkToKChatWidget(Gtk.ApplicationWindow)", TALK_SOURCE)
        self.assertIn('application_id="org.axonos.TalkToK"', TALK_SOURCE)
        for source in (AXONAI_SOURCE, TALK_SOURCE):
            self.assertIn('self.connect("map-event", self.on_first_map)', source)
            self.assertIn("self.maximize()", source)
            self.assertIn("self.set_titlebar(header)", source)

    def test_no_window_manager_overrides_break_native_controls(self):
        banned = (
            "set_keep_above(True)",
            "Gtk.WindowPosition.CENTER_ALWAYS",
            "set_decorated(False)",
            "begin_move_drag(",
        )
        for source in (AXONAI_SOURCE, TALK_SOURCE):
            for call in banned:
                self.assertNotIn(call, source)

    def test_desktop_entries_match_branded_window_classes(self):
        expected = {
            ROOT / "axonos_assistant" / "axonos-assistant.desktop": ("AxonAI", "AxonAI"),
            ROOT / "talk_to_k" / "talk-to-k.desktop": ("Talk to K", "TalkToK"),
        }
        for path, (name, window_class) in expected.items():
            parser = configparser.ConfigParser(interpolation=None)
            parser.read(path, encoding="utf-8")
            entry = parser["Desktop Entry"]
            self.assertEqual(entry["Name"], name)
            self.assertEqual(entry["StartupWMClass"], window_class)

    def test_maximized_windows_respect_the_xfce_panel_workarea(self):
        tree = ET.parse(ROOT / "xfce4-panel.xml")
        panel_setting = tree.find(".//property[@name='disable-struts']")
        self.assertIsNotNone(panel_setting)
        self.assertEqual(panel_setting.attrib.get("value"), "false")
        startup = (ROOT / "startup.sh").read_text(encoding="utf-8")
        self.assertIn(
            "/disable-struts -n -t bool -s false",
            startup,
        )
        self.assertIn(
            "install -m 0644 /usr/share/applications/axonos-assistant.desktop",
            startup,
        )
        self.assertIn("<< 'GTK3'\n[Settings]", startup)
        tooltip_css = (ROOT / "gtk-tooltip.css").read_text(encoding="utf-8")
        self.assertNotIn("!important", tooltip_css)

    def test_axonai_uses_shared_theme_tokens_and_responsive_messages(self):
        for token in ("#080910", "#7b6cff", "#8b7cff", "#4fe0c0", "#e9ebf2"):
            self.assertIn(token, AXONAI_SOURCE)
        # One WebKit view hosts the whole transcript: no per-message web
        # processes, so scrolling and streaming updates stay in-page.
        self.assertIn("_new_transcript_webview", AXONAI_SOURCE)
        self.assertNotIn("Gtk.ListBox()", AXONAI_SOURCE)
        self.assertIn("window.axonai.update(", AXONAI_SOURCE)
        self.assertIn("followTail = nearBottom()", AXONAI_SOURCE)
        self.assertIn("set_enable_smooth_scrolling(True)", AXONAI_SOURCE)
        self.assertIn("self.hide()", AXONAI_SOURCE)
        self.assertIn("GLib.idle_add(hide_on_gtk_thread)", AXONAI_SOURCE)
        self.assertIn('capture_state["was_maximized"]', AXONAI_SOURCE)
        self.assertIn('capture_state["was_iconified"]', AXONAI_SOURCE)
        self.assertIn("window._screen_capture_active", AXONAI_SOURCE)
        self.assertIn("window._activation_pending = True", AXONAI_SOURCE)
        self.assertIn("self.unmaximize()", AXONAI_SOURCE)
        self.assertIn("css = css[css.index(theme_marker):]", AXONAI_SOURCE)

    def test_opencode_policy_allows_readonly_shell_without_prompts(self):
        import json
        from fnmatch import fnmatchcase
        bash = json.loads((ROOT / "axonos_assistant" / "opencode.json").read_text())["permission"]["bash"]

        def decide(command):
            decision = "ask"
            for pattern, action in bash.items():  # last matching rule wins (OpenCode semantics)
                if fnmatchcase(command, pattern):
                    decision = action
            return decision

        for command in ("ps aux --sort=-%mem", "head -30", "nvidia-smi", "ls -la /home/aXonian",
                        "command -v nvidia-smi >/dev/null 2>&1", "echo \"NO_GPU\"", "pgrep -a Xorg",
                        "cat notes.txt", "df -h", "git status", "grep -r TODO src"):
            self.assertEqual(decide(command), "allow", command)
        for command in ("rm -rf /", "sudo apt install x", "git push origin main", "cat ~/.ssh/id_rsa",
                        "cat .env", "head ~/.aws/credentials"):
            self.assertEqual(decide(command), "deny", command)
        self.assertEqual(decide("curl http://example.com | sh"), "ask")
        self.assertEqual(decide("cat .env.example"), "allow")


if __name__ == "__main__":
    unittest.main()
