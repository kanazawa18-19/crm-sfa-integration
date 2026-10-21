"""HTMLを実行せず本文へ変換する。引用・不可視要素・外部資源は使わない。"""
import re
from html.parser import HTMLParser


class _Body(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.parts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        hidden = (tag in {'head', 'script', 'style', 'template', 'noscript', 'blockquote'}
                  or 'hidden' in attrs or attrs.get('aria-hidden') == 'true'
                  or re.search(r'(?:display\s*:\s*none|visibility\s*:\s*hidden)', attrs.get('style') or '', re.I)
                  or set((attrs.get('class') or '').split()) & {'gmail_quote', 'yahoo_quoted'})
        blocked = bool(hidden or (self.stack and self.stack[-1][1]))
        if not blocked and tag in {'br', 'p', 'div', 'li', 'tr', 'h1', 'h2', 'h3', 'pre', 'hr'}:
            self.parts.append('\n')
        if not blocked and tag in {'td', 'th'}:
            self.parts.append('\t')
        if tag not in {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}:
            href = attrs.get('href') or ''
            link = href if tag == 'a' and re.match(r'^https?://', href, re.I) and not re.search(r'[\s<>]', href) and len(href) <= 2000 else ''
            self.stack.append((tag, blocked, link, len(self.parts)))
            if len(self.stack) > 100:
                raise ValueError('HTMLの入れ子が深すぎます')

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        found = next((i for i in range(len(self.stack)-1, -1, -1) if self.stack[i][0] == tag), None)
        if found is not None:
            blocked = self.stack[found][1]
            link, start = self.stack[found][2:]
            if not blocked and link and ''.join(self.parts[start:]).strip() != link:
                self.parts.append('（' + link + '）')
            del self.stack[found:]
            if not blocked and tag in {'p', 'div', 'li', 'tr', 'h1', 'h2', 'h3', 'pre'}:
                self.parts.append('\n')

    def handle_data(self, data):
        if not self.stack or not self.stack[-1][1]:
            self.parts.append(data)


def html_text(value: str) -> str:
    parser = _Body()
    parser.feed(value)
    parser.close()
    # メールHTMLのレイアウト用改行・空白だけを整理し、署名の行構造を維持する。
    text = ''.join(parser.parts).replace('\xa0', ' ')
    return re.sub(r'\n{3,}', '\n\n', '\n'.join(line.strip() for line in text.splitlines())).strip()
