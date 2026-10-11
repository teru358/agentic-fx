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
                                                "content_hash": "abc", "created_at": "now"}],
                               "open_journals": [{"approval_id": 4, "name": "p", "phase": "prepared"}]}, None)

    client = Client()
    assert "log line" in handle_request(Request("GET", "/logs", {}), client).body.decode()
    assert "activity line" in handle_request(Request("GET", "/activity", {}), client).body.decode()
    page = handle_request(Request("GET", "/approvals", {}), client).body.decode()
    assert "abc" in page and "prepared" in page and "token" not in page


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
PINNED_STYLE_HASH = "sha256-dIAw3Rfkv/SAUhYuTRQUAz5UAqeCbdU2MPWMfhMTwaA="


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


PAGES = ["/", "/approvals", "/approvals/7", "/logs", "/activity", "/missing"]


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


def test_every_page_has_the_route_nav_and_a_read_only_refresh():
    static = [route.path for route in ROUTE_TABLE if "{" not in route.path]
    for path in PAGES:
        page = handle_request(Request("GET", path, {}), ReadClient()).body.decode()
        nav = re.search(r"<nav>(.*?)</nav>", page, re.S).group(1)
        assert re.findall(r'<a href="([^"]*)">', nav) == static, path
        assert page.count('<meta http-equiv="refresh" content="30">') == 1, path


def test_each_pending_approval_links_to_its_detail_route():
    approvals = {"approvals": [
        {"id": 7, "name": "<b>p</b>"},
        {"id": "7\"><script>x</script>", "name": "bad id"},
        {"id": True, "name": "bool id"},
        {"id": 0, "name": "zero id"},
    ]}
    page = handle_request(Request("GET", "/approvals", {}), ReadClient(approvals=approvals)).body.decode()
    links = re.findall(r'<a href="([^"]*)">', page.split("</nav>", 1)[1])
    assert links == ["/approvals/7"]
    assert "&lt;b&gt;p&lt;/b&gt;" in page and "<b>" not in page
    assert "<script>" not in page


def test_kill_switch_and_autopilot_are_marked_by_class_only_when_on():
    on = handle_request(Request("GET", "/", {}), ReadClient(status={
        "kill_switch": {"latched": True, "generation": 2}, "autopilot": True})).body.decode()
    assert '<dt class="danger">kill_switch</dt><dd class="danger">' in on
    assert '<dt class="warn">autopilot</dt><dd class="warn">' in on
    off = handle_request(Request("GET", "/", {}), ReadClient(status={
        "kill_switch": {"latched": False}, "autopilot": False})).body.decode()
    assert "class=" not in off.split("</nav>", 1)[1]


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
