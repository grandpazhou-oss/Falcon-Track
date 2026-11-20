"""TDnet discovery. Source access must pass robots before search/detail reads."""
import re
from urllib.parse import urlsplit

HOST = 'finance.biggo.jp'
TARGETS = ('https://finance.biggo.jp/news/jpx_tdnet_140120261007547068',
           'https://finance.biggo.jp/news/jpx_tdnet_140120260902530786')
QUERIES = (
    'site:finance.biggo.jp 適時開示 中国 子会社の設立',
    'site:finance.biggo.jp 適時開示 中国 新工場 OR 工場新設 OR 設備投資',
    'site:finance.biggo.jp 適時開示 上海 OR 蘇州 OR 張家港 OR 広州 OR 東莞 OR 深圳',
)
DAYS = 180


def disclosure_url(url):
    p = urlsplit(url)
    return p.hostname == HOST and bool(re.fullmatch(r'/news/jpx_tdnet_\d+',p.path))


def parse_disclosure(title, text, url):
    """Conservative handling for a successful, archived TDnet detail response."""
    from collectors.china_entry_evidence import CHINA, ACTION, region
    from collectors.keyword_radar import publication_from_text

    if not disclosure_url(url):
        return None
    code=re.search(r'(?:証券コード|証券ｺｰﾄﾞ|コード番号|銘柄コード)\s*[：:]?\s*([0-9A-Z]{4})(?![0-9A-Z])',text,re.I)
    company=re.search(r'(?:会社名|会\s*社\s*名)[：:\s]+([^\n]{2,80})',text)
    evidence=next((line.strip() for line in re.split(r'[。\n]',text)
                   if CHINA.search(line) and ACTION.search(line) and not re.search(r'中国地方|撤回|中止',line)), '')
    if not (code and company and evidence):
        return None
    target=re.search(r'(?:設立|稼働|操業|取得|着工|完了)[^。\n]{0,14}(20\d{2}年(?:\d{1,2}月)?)',text)
    publication=publication_from_text(text)
    return {'company':company.group(1).strip(),'security_code':code.group(1),
            'evidence_quote':evidence,'target_period':target.group(1) if target else '',
            'region':region(evidence),'published':publication.get('date',''),
            'source_scope':'tdnet_disclosure'}
