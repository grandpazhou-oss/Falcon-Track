"""Bounded overseas-report discovery from Aichi Prefecture, AIBSC and Kyoto."""
import re
from urllib.parse import urlsplit

from collectors.china_entry_evidence import CHINA, OVERSEAS, article, counters, failure, links, read

AICHI = 'https://www.pref.aichi.jp/site/overseas-expansion/'
LETTER = 'https://www.pref.aichi.jp/soshiki/ricchitsusho/gaikoku-report-letter.html'
AIBSC = 'https://www.aibsc.jp/sitemap/'
KYOTO = 'https://www.ki21.jp/informations/?genre=2'
TARGETS = (AICHI, LETTER, AIBSC, KYOTO)
AIBSC_CONTEXT = ('海外展開','上海産業情報センター','愛知県貿易関係企業名簿')
DETAIL = re.compile(r'/uploaded/attachment/\d+\.pdf$|/support/\d+/$|/informations/[^/?]+/?$|/press-release/[^?#]+\.html$')
CONTEXT = re.compile(r'セミナー|参加者募集|講座|市場レポート|産業の最新動向|産業情報|セッション|商談会|ブース|視察|コンテスト|インフラ|国家戦略')


def discovery(body, base):
    raw=list(links(body,base))
    selected = [(url,title) for url,title in raw if OVERSEAS.search(title)
                and DETAIL.search(urlsplit(url).path) and not any(word==title for word in AIBSC_CONTEXT)]
    selected.sort(key=lambda row: not bool(CHINA.search(row[1])))
    return selected, len(raw)


def collect(stats, max_details=6, targets=TARGETS):
    out=[]
    seen=set()
    for index in targets:
        retained=0
        try:
            response,_=read(index,stats)
            discovered,total=discovery(response['body'],response['url'])
            candidates=[(url,title) for url,title in discovered if url not in seen][:max_details]
            for url,title in candidates:
                seen.add(url)
                try:
                    r,doc=read(url,stats)
                    item=article(r,doc,title)
                    # Shanghai desk location by itself is not an operating company's location.
                    if not OVERSEAS.search(title + ' ' + item['text'][:2000]):continue
                    item.update(source_scope='regional_reference' if CONTEXT.search(title) else 'overseas_update')
                    if CONTEXT.search(title):
                        item['title']=title
                    out.append(item)
                    retained+=1
                except (ValueError,OSError) as exc: failure(stats,url,exc)
            counters(stats,index,total,retained)
        except (ValueError,OSError) as exc: failure(stats,index,exc)
    return out
