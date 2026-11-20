"""Bounded navigation using same-column next links."""
import re
from urllib.parse import urljoin, urlsplit


def max_pages(source):
    return getattr(source, '_max_pages', None) or 1


def next_page(tree, current):
    base = urlsplit(current)
    root = re.sub(r'/page/\d+/?$', '', base.path.rstrip('/'))
    for link in tree.xpath('//a[@href] | //link[@rel="next"]'):
        title = ' '.join(link.text_content().split()) if link.tag == 'a' else ''
        if link.get('rel') != 'next' and not re.search(r'^(?:次へ|次の\s*\d*\s*件|下一页|Next\b)', title, re.I):
            continue
        target = urlsplit(urljoin(current, link.get('href')))
        if ((target.scheme, target.netloc) == (base.scheme, base.netloc)
                and (target.path.rstrip('/') == root or target.path.startswith(root + '/'))
                and target.geturl() != current):
            return target.geturl()
    return ''
