import colorsys
import re
from pathlib import Path

import pymupdf
import pytest
from lxml import html


@pytest.mark.django_db
def test_brand_and_theme_are_shared_by_both_languages(client):
    for lang, title, toggle in (
        ('zh', '商机雷达', '切换深色模式'),
        ('ja', '商機レーダー', 'ダークモードに切替'),
    ):
        client.post('/language/', {'language': lang, 'next': '/'})
        body = client.get('/').content.decode()
        page = html.fromstring(body)
        assert f'{title} / FALCON TRACK' in page.xpath('string(//title)')
        assert page.xpath('string(//a[@class="brand"]/span/strong)') == title
        assert title in page.xpath('string(//footer)')
        assert page.xpath('//link[@rel="icon"]/@href')[0].endswith('/favicon.png')
        assert page.xpath('//img[@class="brand-logo"]/@src')[0].endswith('/falcon-logo.png')
        assert page.xpath('//button[@class="theme-toggle"]/@aria-label') == [toggle]
        assert body.index('localStorage.getItem("radar-theme")') < body.index('rel="stylesheet"')


def test_logo_transparency_and_visible_feathers():
    static = Path(__file__).resolve().parents[1] / 'static' / 'radar'
    for name, size in (('falcon-logo.png', 256), ('favicon.png', 64)):
        icon = pymupdf.Pixmap(str(static / name))
        assert (icon.width, icon.height, icon.alpha) == (size, size, 1)
        assert icon.pixel(0, 0)[3] == 0
        for x, y in ((110, 165), (62, 113)):
            *rgb, alpha = icon.pixel(x * size // 256, y * size // 256)
            assert min(rgb) >= 230 and alpha == 255


def test_css_contains_no_purple_or_indigo_colors():
    static = Path(__file__).resolve().parents[1] / 'static' / 'radar'
    for sheet in static.glob('*.css'):
        css = sheet.read_text()
        colors = []
        for hex_color in re.findall(r'#([0-9a-fA-F]{3,8})\b', css):
            if len(hex_color) in (3, 4):
                hex_color = ''.join(c * 2 for c in hex_color)
            if len(hex_color) in (6, 8):
                colors.append(bytes.fromhex(hex_color[:6]))
        colors.extend(tuple(map(int, channels))
                      for channels in re.findall(r'rgba?\(\s*(\d+),\s*(\d+),\s*(\d+)', css))
        for rgb in colors:
            hue, saturation, _ = colorsys.rgb_to_hsv(*(channel / 255 for channel in rgb))
            assert not (230 <= hue * 360 <= 315 and saturation > .1), (sheet.name, tuple(rgb))
