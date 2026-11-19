from __future__ import annotations

from afx_ops.api_client import Reply
from afx_ops.web.headers import SECURITY_HEADERS
from afx_ops.web.render import MAX_RESPONSE_BYTES
from afx_ops.web.request import Request, handle_request


class HostileClient:
    def status(self):
        return Reply(200, {"mode": '<script>alert("x")</script>', "equity": 1,
                           "token": "secret-token", "nested": {"key": "secret"}}, None)


def test_html_escapes_allowed_values_and_omits_unknown_values():
    response = handle_request(Request("GET", "/", {}), HostileClient())
    html = response.body.decode()

    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in html
    assert "<script>" not in html
    assert "secret-token" not in html
    assert "secret" not in html


def test_every_response_has_security_headers():
    response = handle_request(Request("GET", "/missing", {}), HostileClient())

    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value
    assert response.headers["Content-Type"] == "text/html; charset=utf-8"
    assert response.headers["Cache-Control"] == "no-store"


def test_renderers_project_the_read_dto_shapes_without_unknown_fields():
    class Client:
        def log(self, *, n):
            return Reply(200, {"lines": ["log line"], "truncated": True, "token": "no"}, None)

        def activity(self, *, n, category):
            return Reply(200, {"lines": ["activity line"], "truncated": True, "token": "no"}, None)

        def approvals(self, *, limit):
            return Reply(200, {"approvals": [{"id": 4, "kind": "plugin", "name": "p",
                                                "content_hash": "0123456789abcdef" * 4,
                                                "created_at": "now"}],
                               "open_journals": [{"approval_id": 4, "name": "p", "phase": "prepared"}]}, None)

    client = Client()
    assert "log line" in handle_request(Request("GET", "/logs", {}), client).body.decode()
    assert "activity line" in handle_request(Request("GET", "/activity", {}), client).body.decode()
    page = handle_request(Request("GET", "/approvals", {}), client).body.decode()
    assert "0123456789ab" in page and "prepared" in page and "token" not in page


def test_response_body_has_a_fixed_upper_bound():
    class Client:
        def log(self, *, n):
            return Reply(200, {"lines": ["x" * 1000] * 500}, None)

    response = handle_request(Request("GET", "/logs", {"n": "500"}), Client())

    assert len(response.body) <= MAX_RESPONSE_BYTES


# --- 見た目の最小構成: 固定 CSS、ナビ、自動更新、詳細への link ----------------------

import base64  # noqa: E402
import hashlib  # noqa: E402
import re  # noqa: E402

from afx_ops.web import headers, render  # noqa: E402
from afx_ops.web.routes import ROUTE_TABLE  # noqa: E402

# CSS を変えたらこの値も変える (CSP の hash と埋め込む CSS の取り違えを防ぐ)
PINNED_STYLE_HASH = "sha256-qiZacxC6roHXmI7P8rm8egku/grOsHbS3rfGS/tgUC4="


class ReadClient:
    def __init__(self, status=None, approvals=None):
        self._status = status or {"mode": "paper"}
        self._approvals = approvals or {"approvals": []}

    def status(self):
        return Reply(200, self._status, None)

    def approvals(self, *, limit):
        return Reply(200, self._approvals, None)

    def approval(self, approval_id):
        return Reply(200, {"id": approval_id, "status": "pending"}, None)

    def log(self, *, n):
        return Reply(200, {"lines": ["l"]}, None)

    def activity(self, *, n, category):
        return Reply(200, {"lines": ["a"]}, None)

    def plugins(self):
        return Reply(200, {"plugins": [], "sources": {}}, None)

    def plugin(self, name):
        return Reply(200, {"plugin": {"name": name}}, None)

    def improve_list(self, *, limit):
        return Reply(200, {"runs": [], "has_more": False}, None)

    def improve_detail(self, mission_id):
        return Reply(200, {"id": mission_id, "snapshot": None, "summary": None}, None)

    def performance(self, *, range):
        return Reply(200, {"points": [], "hwm_steps": [], "marks": [], "strategies": [],
                           "stats": {}}, None)


PAGES = ["/", "/approvals", "/approvals/7", "/logs", "/activity", "/plugins", "/plugins/rsi",
         "/improve", "/improve/3", "/performance", "/missing"]


def test_the_csp_style_hash_is_the_hash_of_the_embedded_css():
    digest = base64.b64encode(hashlib.sha256(render.STYLE.encode("utf-8")).digest()).decode()
    assert f"sha256-{digest}" == PINNED_STYLE_HASH == headers.STYLE_HASH
    csp = headers.SECURITY_HEADERS["Content-Security-Policy"]
    assert csp == ("default-src 'none'; style-src '" + PINNED_STYLE_HASH + "'; base-uri 'none'; "
                   "form-action 'none'; frame-ancestors 'none'")
    for path in PAGES:
        page = handle_request(Request("GET", path, {}), ReadClient()).body.decode()
        # browser が hash を取るのは <style> の中身そのもの
        assert re.findall(r"<style>(.*?)</style>", page, re.S) == [render.STYLE], path
        assert "<script" not in page and " style=" not in page, path


def test_every_page_has_the_route_nav_and_a_read_only_refresh_except_the_costly_performance_read():
    static = [route.path for route in ROUTE_TABLE if "{" not in route.path]
    # /approvals は nav に出さず、/plugins の画面から辿る
    in_nav = ["/", "/plugins", "/improve", "/performance", "/logs", "/activity"]
    assert sorted(in_nav + ["/approvals"]) == sorted(static)
    for path in PAGES:
        page = handle_request(Request("GET", path, {}), ReadClient()).body.decode()
        nav = re.search(r"<nav>(.*?)</nav>", page, re.S).group(1)
        assert re.findall(r'<a href="([^"]*)"(?: aria-current="page")?>', nav) == in_nav, path
        assert 'Approvals</a>' not in nav, path
        expected = 0 if path == "/performance" else 1
        assert page.count('<meta http-equiv="refresh" content="30">') == expected, path


def test_each_pending_approval_links_to_its_detail_route():
    approvals = {"approvals": [
        {"id": 7, "name": "<b>p</b>"},
        {"id": "7\"><script>x</script>", "name": "bad id"},
        {"id": True, "name": "bool id"},
        {"id": 0, "name": "zero id"},
    ]}
    page = handle_request(Request("GET", "/approvals", {}), ReadClient(approvals=approvals)).body.decode()
    content = re.sub(r'<nav class="tabs">.*?</nav>', "", page).split("</nav>", 1)[1]
    assert re.findall(r'<a href="([^"]*)">', content) == ["/approvals/7"]
    assert "&lt;b&gt;p&lt;/b&gt;" in page and "<b>" not in page
    assert "<script>" not in page


def test_kill_switch_and_autopilot_are_marked_by_class_only_when_on():
    on = handle_request(Request("GET", "/", {}), ReadClient(status={
        "kill_switch": {"latched": True, "generation": 2}, "autopilot": True})).body.decode()
    assert '<dt class="danger">kill_switch</dt><dd class="danger">' in on
    assert "autopilot</dt>" not in on and '<span class="st warn">△ ON</span>' in on
    off = handle_request(Request("GET", "/", {}), ReadClient(status={
        "kill_switch": {"latched": False}, "autopilot": False})).body.decode()
    # 表示の dl の中には、印の class を入れない (印はカードにだけ出る)
    assert not [dl for dl in re.findall(r"<dl>.*?</dl>", off.split("</nav>")[-1]) if "class=" in dl]


def test_log_and_activity_show_the_newest_lines_of_a_long_window():
    lines = [f"entry-{i:03d}" for i in range(100)]

    class Client:
        def log(self, *, n):
            return Reply(200, {"lines": lines}, None)

        def activity(self, *, n, category):
            return Reply(200, {"lines": lines}, None)

    for path in ("/logs", "/activity"):
        html = handle_request(Request("GET", path, {}), Client()).body.decode()
        assert "entry-099" in html and "entry-050" in html, path
        assert "entry-049" not in html and "entry-000" not in html, path


def test_the_plugins_list_band_links_to_the_approvals_and_no_page_repeats_the_link_paragraph():
    page = handle_request(Request("GET", "/plugins", {}), ReadClient()).body.decode()
    assert '<a href="/approvals">承認待ち</a>' in page
    for path in ("/plugins", "/plugins/rsi"):
        page = handle_request(Request("GET", path, {}), ReadClient()).body.decode()
        assert "Pending approvals" not in page.split("</nav>", 1)[1], path


# --- CSS の token・配色・大きさ ------------------------------------------------------

STYLE_MAX_BYTES = 4096


def _style_tokens() -> tuple[dict[str, str], dict[str, str]]:
    css = render.STYLE
    light_block = re.match(r"\s*:root \{([^}]*)\}", css).group(1)
    dark_block = re.search(r"@media \(prefers-color-scheme: dark\) \{ :root \{([^}]*)\}", css).group(1)

    def parse(block):
        return dict(re.findall(r"(--[a-z-]+):\s*(#[0-9a-fA-F]{3,6})", block))
    return parse(light_block), parse(dark_block)


def _luminance(color: str) -> float:
    color = color.lstrip("#")
    if len(color) == 3:
        color = "".join(c * 2 for c in color)
    channels = []
    for i in (0, 2, 4):
        c = int(color[i:i + 2], 16) / 255
        channels.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def _contrast(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def test_colors_are_written_only_in_the_two_root_blocks():
    css = render.STYLE
    outside = re.sub(r"\s*:root \{[^}]*\}", "", css)
    outside = re.sub(r"@media \(prefers-color-scheme: dark\) \{ :root \{[^}]*\} \}", "", outside)
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", outside), re.findall(r"#[0-9a-fA-F]{3,8}\b", outside)
    light, dark = _style_tokens()
    assert set(light) == set(dark) and "--bg" in light
    assert "color-scheme: light dark" in css


_TEXT_ON_BG = [("--fg", "--bg"), ("--fg", "--soft"), ("--fg", "--ok-bg"), ("--fg", "--warn-bg"),
               ("--fg", "--bad-bg"), ("--mut", "--bg"), ("--mut", "--soft"), ("--link", "--bg"),
               ("--link", "--soft"), ("--ok", "--bg"), ("--ok", "--soft"), ("--ok", "--ok-bg"),
               ("--warn", "--bg"), ("--warn", "--soft"), ("--warn", "--warn-bg"),
               ("--bad", "--bg"), ("--bad", "--soft"), ("--bad", "--bad-bg")]


def test_every_text_and_background_pair_has_contrast_of_at_least_4_5_in_light_and_dark():
    for name, tokens in zip(("light", "dark"), _style_tokens()):
        for fg, bg in _TEXT_ON_BG:
            assert _contrast(tokens[fg], tokens[bg]) >= 4.5, (name, fg, bg)


def test_the_style_stays_within_its_byte_budget():
    assert len(render.STYLE.encode("utf-8")) <= STYLE_MAX_BYTES


def test_every_page_declares_japanese_and_a_device_width_viewport():
    for path in PAGES:
        page = handle_request(Request("GET", path, {}), ReadClient()).body.decode()
        assert '<html lang="ja">' in page, path
        assert '<meta name="viewport" content="width=device-width,initial-scale=1">' in page, path


def _rule(selector: str) -> str:
    match = re.search(r"(?:^|\n)" + re.escape(selector) + r" \{([^}]*)\}", render.STYLE)
    assert match, selector
    return match.group(1)


def test_tables_are_dense_striped_and_digits_are_tabular():
    assert "tabular-nums" in _rule("body")
    assert "font-size: 0.85rem" in _rule("table")
    assert "padding: 0.15rem 0.45rem" in _rule("th, td")
    assert "var(--soft)" in _rule("tbody tr:nth-child(even)")
    assert "var(--soft)" in _rule("th")


def test_a_section_is_framed_and_scrolls_a_wide_table_inside_itself():
    section = _rule("section")
    assert "border: 1px solid var(--line)" in section and "overflow-x: auto" in section


def test_stacked_forms_are_spaced_and_buttons_carry_no_state_color():
    assert "margin-top: 1.5rem" in _rule("form + form")
    controls = _rule("button, input")
    assert "font: inherit" in controls
    assert "color" not in controls and "background" not in controls


def test_the_css_has_the_mark_and_card_rules_and_the_full_style_fits_the_budget():
    for selector in (".st", ".ok", ".kpi", ".kpi div", ".kpi strong"):
        assert re.search(r"(^|\n|, )" + re.escape(selector) + r"[ ,{]", render.STYLE), selector
    assert "dd.danger, .st.danger" in render.STYLE and "dd.warn, .st.warn" in render.STYLE
