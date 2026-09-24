"""Static copy/markup contract for the authorized reader UI.

The settled wording, checked at the source level (the browser harness checks behaviour):

  * 精选 replaces every 通过 / 已筛 label; rejected = 未入选, pending = 待筛选
  * drawer actions: 收藏 / 取消收藏, 纳入精选, 排除, 原文
  * note field: label 备注（可选）, placeholder 补充筛选偏好
  * no conversational copy: 教筛选器 / 漏掉了 / 下一轮会读到 / 后台可能正在重启
  * the management control always reads 管理 (state lives in a separate indicator)
  * subtitle 浏览新帖，发现值得读的内容。 sits inside the masthead brand block,
    not as a far-left standfirst
  * no per-row 已读 text/badge is written into the DOM
  * the [hidden] reset that made the mobile scrim stop eating taps is still there

Run: python3 -m unittest tests.test_reader_copy -v
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "public"

FORBIDDEN_COPY = (
    "教筛选器",
    "漏掉了",
    "下一轮会读到",
    "后台可能正在重启",
    "想看 · ",
    "不感兴趣",
    "已通过",
    "已筛掉",
)


class ReaderCopy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (PUBLIC / "index.html").read_text(encoding="utf-8")
        cls.js = (PUBLIC / "app.js").read_text(encoding="utf-8")
        cls.css = (PUBLIC / "styles.css").read_text(encoding="utf-8")

    def test_no_conversational_copy_remains(self):
        for needle in FORBIDDEN_COPY:
            with self.subTest(copy=needle):
                self.assertNotIn(needle, self.html)
                self.assertNotIn(needle, self.js)

    def test_state_labels_are_the_settled_ones(self):
        self.assertIn("未入选", self.js)
        self.assertIn("待筛选", self.js)
        self.assertNotIn("待读", self.html)
        self.assertNotIn("待读", self.js)
        self.assertNotIn("已筛", self.html)

    def test_tabs_columns_and_counts_say_精选_and_收藏(self):
        for label in ("全部", "精选", "收藏"):
            self.assertIn(f">{label}<", self.html, f"{label} label must exist in the markup")
        self.assertIn('id="c-queue"', self.html)
        counts = self.html[self.html.index('id="countline"') : self.html.index("</p>", self.html.index('id="countline"'))]
        self.assertIn("收藏", counts)

    def test_drawer_actions_and_note_field(self):
        for element_id in ("d-queue", "d-keep", "d-skip", "d-link", "d-read", "d-note"):
            self.assertIn(f'id="{element_id}"', self.html)
        self.assertIn("纳入精选", self.html)
        self.assertIn("排除", self.html)
        self.assertIn(">原文<", self.html)
        self.assertIn("备注（可选）", self.html)
        self.assertIn('placeholder="补充筛选偏好"', self.html)
        self.assertIn("'收藏'", self.js)
        self.assertIn("'取消收藏'", self.js)

    def test_no_per_row_read_badge_text(self):
        # 已读 may exist only as the icon-only control's aria-label/title (settled contract);
        # no visible per-row 已读 text or badge may be written into the DOM
        visible = re.sub(r"<[^>]*>", " ", self.html)
        self.assertNotIn("已读", visible)
        self.assertNotRegex(self.js, r"textContent\s*=\s*[^;]*已读")
        self.assertIn("标记为已读", self.html)
        self.assertIn("标记为未读", self.js)
        self.assertIn("aria-label", self.js)

    def test_subtitle_lives_inside_the_brand_block(self):
        brand = re.search(r'<div class="masthead__brand">(.*?)</div>', self.html, re.S)
        self.assertIsNotNone(brand, "masthead__brand must still wrap the title")
        block = brand.group(1)
        self.assertIn("每日精选", block)
        self.assertIn('class="brand__sub"', block)
        self.assertIn("浏览新帖，发现值得读的内容。", block)
        self.assertNotIn("standfirst", self.html)

    def test_management_button_label_is_stable(self):
        self.assertIn('id="owner"', self.html)
        owner = self.html[self.html.index('id="owner"') - 200 : self.html.index('id="owner"') + 60]
        self.assertIn("管理", owner)
        self.assertNotIn("管理 · 已连接", self.js)
        self.assertNotIn("管理 · 已连接", self.html)

    def test_hidden_reset_and_read_style_are_present(self):
        self.assertIn("[hidden] { display: none !important; }", self.css)
        self.assertIn("is-read", self.css)
        self.assertIn("is-read", self.js)

    def test_read_state_is_browser_local_and_namespaced(self):
        self.assertIn("linuxdo-ai.read", self.js)
        self.assertIn("localStorage", self.js)
        self.assertRegex(self.js, r"API_BASE\s*\|\|\s*location\.origin")
        self.assertNotIn("sessionStorage.setItem(CFG.lsRead", self.js)

    def test_owner_token_stays_in_session_storage(self):
        self.assertIn("sessionStorage", self.js)
        self.assertNotIn("localStorage.setItem(CFG.ssOwner", self.js)

    def test_read_deadline_and_error_copy_are_truthful(self):
        self.assertIn("AbortController", self.js)
        self.assertIn("超时", self.js)
        self.assertIn("HTTP", self.js)

    def test_read_deadline_is_the_measured_30s_and_names_the_read_not_the_server(self):
        # 23383 B/s measured on the incident connection puts the ~347 KB list read at ~14.8 s,
        # so the shipped bound is 30 s; the copy must blame the unfinished read, not a silent
        # server, and the dev override stays for deterministic tests
        self.assertIn("readDeadlineMs: 30000", self.js)
        self.assertIn("秒内未能读完响应", self.js)
        self.assertNotIn("秒内没有响应", self.js)
        self.assertIn("?readtimeout=", (ROOT / "CONTRACT.md").read_text(encoding="utf-8"))

    def test_error_state_shows_collapsed_safe_technical_facts(self):
        # collapsed by default, and built only from safe values: category, the fixed trusted
        # endpoint path, the client clock, the last successful read
        self.assertIn("<details", self.html)
        self.assertIn('id="errortech"', self.html)
        self.assertIn('id="errorfacts"', self.html)
        self.assertIn("技术详情", self.html)
        self.assertIn(".errorstate__facts", self.css)
        body = self.js.split("function renderErrorFacts", 1)[1].split("\n  }", 1)[0]
        for label in ("分类", "端点", "客户端时间", "上次成功读取"):
            self.assertIn(label, body)
        self.assertIn("CFG.state", body)
        self.assertIn("toISOString", self.js)
        for forbidden in ("stateUrl", "authHeaders", "ownerToken", "fetch("):
            self.assertNotIn(forbidden, body, "the facts block must not carry URLs, headers or secret material")

    def test_docs_describe_the_bookmark_and_read_semantics(self):
        contract = (ROOT / "CONTRACT.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for text in (contract, readme):
            self.assertIn("收藏", text)
        self.assertIn("已读", contract)
        self.assertIn("bookmark", contract.lower())
        self.assertIn("bookmark", readme.lower())


if __name__ == "__main__":
    unittest.main()
