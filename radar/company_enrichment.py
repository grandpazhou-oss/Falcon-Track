"""Classify disclosed business activities and extract explicitly listed company sites."""
import re
import unicodedata
from urllib.parse import urlsplit

from .search_provider import canonical_url

INDUSTRIES = (
    ('半导体与电子元件', r'半導体|半导体|電子部品|电子元件|基板材料|封止材'),
    ('汽车及零部件', r'自動車|汽车|車載|车载|汽車'),
    ('精密机械与工业设备', r'精密機械|精密机械|精密加工|機械加工|工作機械|射出成形機|産業機械|工业设备|金型|成形機'),
    ('化工与新材料', r'化工|化学|化學|触媒|触媒|新材料|特殊鋼|特殊钢|合金鋼|合金钢|油墨|塗料|樹脂|光学材料'),
    ('食品饮料与酒类', r'食品|菓子|飲料|饮料|酒造|焼酎|ドーナツ|酒類|酒类|精米|抹茶|醤油|鰹|削り節|冷凍弁当'),
    ('医疗健康与器械', r'医療|医疗|医薬|医药|歯科|牙科|製薬|介護機器'),
    ('消费品与零售', r'小売|零售|アパレル|衣料品|香水|化粧品|玩具|ジュエリー|アイウェア|キャラクター製作|陶器|陶芸|雑貨|店舗販売|スキンケア'),
    ('物流与贸易', r'物流業|物流企業|総合物流|倉庫業|貨物運送|倉庫管理事業|货运代理|商社|輸出入代行'),
    ('其他制造/服务', r'製造業|制造业|サービス業|服务业|情報通信業|情報処理|教育支援|ソフトウェア開発|建設業'),
)
FIELDS = ('company_industry','official_website','official_website_status')
PLATFORMS = ('prtimes.jp','atpress.ne.jp','jetro.go.jp','jpx.co.jp','biggo.jp','nikkei.com')
URL = re.compile(r'https?://[^\s<>「」【】）)；;]+',re.I)
BUSINESS = re.compile(r'事業内容|事業概要|主要製品|主な製品|取り扱い品目|業種|经营范围|主营业务|主要产品|産業分類')
NAME = re.compile(r'会社名|商号|会社概要|法人名|企業名|事業者|所在地|企业名称')


def identity(text):
    return re.sub(r'[^\w一-龯]+|株式会社|有限会社|合同会社|股份有限公司|有限公司','',
                  unicodedata.normalize('NFKC',str(text)).casefold()).replace('_','')


def same_name(name, text):
    term = identity(name)
    return bool(len(term) >= 3 and term in identity(text))


def company_site(value):
    url = canonical_url(value)
    host = urlsplit(url).hostname or ''
    if not host or '.' not in host or host.endswith(('.go.jp','.lg.jp','.gov.cn','.local')):
        return ''
    if any(host == name or host.endswith('.'+name) for name in PLATFORMS):
        return ''
    return url


def site_root(value):
    parts = urlsplit(company_site(value))
    return f'{parts.scheme}://{parts.netloc}/' if parts.netloc else ''


def classify(text):
    matches = [label for label,pattern in INDUSTRIES[:-1] if re.search(pattern,text,re.I)]
    return matches[0] if len(matches)==1 else (
        INDUSTRIES[-1][0] if not matches and re.search(INDUSTRIES[-1][1],text) else '')


def extract_profile(company, text, *, project=False):
    if not company or not same_name(company,text):
        return '', ''
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    activity=[]
    links=set()
    for i,line in enumerate(lines):
        neighborhood=' '.join(lines[max(0,i-3):i+3])
        if BUSINESS.search(line):
            activity.append(' '.join(lines[i:i+2]))
        if URL.search(line) and re.search(r'URL|公式|ホームページ|ウェブサイト|官网|企業サイト',neighborhood,re.I):
            nearby=' '.join(lines[max(0,i-12):i+4])
            if same_name(company,nearby) and NAME.search(nearby):
                links.update(root for value in URL.findall(line) if (root:=site_root(value.rstrip('.,。/'))))
    industry = classify(' '.join(activity)) if activity else ''
    if project and not industry:
        industry = classify(text)
    return industry, next(iter(links)) if len(links)==1 else ''


def fields(company=None):
    industry = getattr(company,'company_industry','') or ''
    status = getattr(company,'official_website_status','missing') or 'missing'
    website = company_site(getattr(company,'official_website','') or '') if status == 'verified' else ''
    return dict(zip(FIELDS,(industry,website,status if status != 'verified' or website else 'missing')))



def named_case(candidate):
    name = (candidate.result.get('extraction') or {}).get('company_name','')
    if name or not candidate.raw_path:
        return name
    if (urlsplit(candidate.canonical_url).hostname or '') != 'www.jetro.go.jp':
        return ''
    title=candidate.title.split(' · ',1)[0]
    first=candidate.text.splitlines()[0] if candidate.text else ''
    if 3<=len(title)<=120 and re.search(r'株式会社|有限会社|合同会社',title) and first.startswith(title):
        return title
    return ''
