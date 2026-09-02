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
        self.assertIn("on_message_size_allocate", AXONAI_SOURCE)
        self.assertIn("self.chat_is_near_bottom()", AXONAI_SOURCE)
        self.assertIn("self.hide()", AXONAI_SOURCE)
        self.assertIn("GLib.idle_add(hide_on_gtk_thread)", AXONAI_SOURCE)
        self.assertIn('capture_state["was_maximized"]', AXONAI_SOURCE)
        self.assertIn('capture_state["was_iconified"]', AXONAI_SOURCE)
        self.assertIn("window._screen_capture_active", AXONAI_SOURCE)
        self.assertIn("window._activation_pending = True", AXONAI_SOURCE)
        self.assertIn("self.unmaximize()", AXONAI_SOURCE)
        self.assertIn("css = css[css.index(theme_marker):]", AXONAI_SOURCE)


if __name__ == "__main__":
    unittest.main()
