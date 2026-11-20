"""Public association lists: foreign membership is not Japanese ownership."""
import re
from lxml import html
from collectors.sources import node_text
from .public_fetch import fetch
from .roster_import import import_roster

SHFIA_ALL = 'https://www.shfia.cn/cms/xinwen/1590.html'


def parse_shfia_members(body):
    tree=html.fromstring(body);rows=[]
    for tr in tree.xpath('//table//tr'):
        cells=[node_text(x).strip() for x in tr.xpath('./td')]
        if len(cells)>=2 and cells[0].isdigit() and cells[1]:
            rows.append({'name':cells[1], 'quote':cells[1]})
    if not rows:raise ValueError('association_roster_layout_missing')
    return rows


def discover_association(source,limit=50):
    if source.url != SHFIA_ALL:raise ValueError('association_entry_mismatch')
    r=fetch(source.url,timeout=30)
    return import_roster(source,parse_shfia_members(r['body']),r,basis='association_list',japanese=False,limit=limit)
