"""Recheck curated Kanto sales cases against separate scale and market documents."""
from lxml import html
from collectors.research_common import pdf_pages, read_original

KEY = 'kanto_verified_cases'
INDEX = 'https://www.idec.or.jp/support/supportcase_list.html?cat_id=6'
JETRO = 'https://www.jetro.go.jp/case_study/2025/'
# Market text, enterprise profile and staff field are independently verified.
CASES = [
    ('株式会社クロップオザキ', 'https://www.cropozaki.com/business/global/', 'https://www.cropozaki.com/company/',
     '上海を拠点に中国生地・付属・製品', '東京都千代田区東神田', '従業員 44名', '44',
     '上海法人经营服装面料和辅料，开展三国间贸易', '上海长宁团队最近的面辅料订单，从哪些供应商集货？样品和量产批次分别由谁安排运输？'),
    ('大川原化工機株式会社', 'https://www.ojn-sd.com/en/index/Lists/show/catid/7/id/78.html','https://matching.idec.or.jp/?p=1350',
     'Location:Shanghai New International Expo Center', '横浜市都筑区池辺町3847','従業員数 105名','105',
     '上海公司公布2026设备展计划；秋季制药机械展日期及展位待核', '上海制造、苏州销售的设备，下一批样机或备件由哪边发运？秋季参展计划有无落实？'),
    ('大島椿株式会社', JETRO+'oshimatsubaki.html','https://www.oshimatsubaki.co.jp/company/about/',
     '米国や中国、カナダ、EUをはじめ多くの国で販売認可', '東京都港区芝大門2-9-16','従業員数 26名（2026年8月現在）','26',
     '2025年案例记载已向包括中国的市场出口椿油产品', '现在中国经销商按订单从日本进口，还是在国内备货？下一批椿油产品的包装和到仓时间定了吗？'),
    ('株式会社わかさま陶芸', JETRO+'wakasama-mashiko.html','',
     '米国、オーストラリア、中国 ／ 陶器（益子焼）','栃木県益子町','社員数：40人','40',
     '2025年12月案例：已建跨境网店，中国列为陶器目标市场，首单待核','有没有收到中国买家的样品或小批量订单？易碎品目前如何打包、拼箱和处理破损？'),
    ('株式会社三笠','https://www.idec.or.jp/support/supportcase_info.html?id=47','https://www.tokyo-cci.or.jp/seisaku/topics/chizai/backstage/98_mikasa.pdf',
     '2006年に上海へ進出','神奈川県横浜市港南区日野南','従業員：79名','79',
     '2020年度横滨支援案例：在华展销与产地验货，先核现状','上海法人是否已恢复对中国连锁店供货？在华门店配送与发往日本的验货装柜分别由谁委托？'),
]
# These literals bind the Chinese descriptions to the specific operating action.
ACTION = {'株式会社クロップオザキ':'三国間貿易を含めた貿易業務',
          '大川原化工機株式会社':'The 69th (2026 Autumn) China National Pharmaceutical Machinery Exposition',
          '大島椿株式会社':'現在では10市場（米国、中国、香港、台湾、タイ、マレーシア、カナダ、オーストラリア、オランダ、ドイツ）に輸出を行っています',
          '株式会社わかさま陶芸':'越境ECの立ち上げを完了',
          '株式会社三笠':'中国各地の展示会にも参加'}


def material(url):
    result, doc = read_original(url)
    if doc['raw_path'].endswith('.pdf'):
        pages = pdf_pages(doc['raw_path'])['pages']
        body = ' '.join(p['text'] for p in pages)
    else:
        tree = html.fromstring(result['body'].decode('utf-8'))
        body = ' '.join(tree.xpath('//text()[not(ancestor::script or ancestor::style)]'))
    return {**doc, 'type':doc['raw_path'].rsplit('.',1)[1], 'text':' '.join(body.split())}


def evidence(doc, quote):
    if quote not in doc['text']:
        raise ValueError(f'原文已变更或尚未取得：{quote}')
    return {'quote':quote,'document_hash':doc['sha256'],'location':'企业简介、商会资料或经营案例正文'}


def collect_kanto(source):
    items, errors = [], []
    for name,url,profile,cn,hq,staff,num,stage,question in CASES:
        try:
            doc = material(url)
            company = material(profile) if profile else doc
            name_doc = company if name in company['text'] else doc
            # Japan company on the Yokohama directory; event calendar belongs to its local business.
            facts = [{'field':field,'value':value,**evidence(origin,quote)} for field,value,origin,quote in [
                ('market_stage',stage,doc,ACTION[name]),('employees',num,company,staff),('parent_address',hq,company,hq)]]
            if name=='大川原化工機株式会社':
                facts.append({'field':'parent_china_relation','value':'苏州销售、上海制造销售',**evidence(company,'蘇州に販売会社、上海に製作・販売会社')})
                evidence(doc,'Time:To be determined')
            if name=='大島椿株式会社':
                section = company['text'].partition('大島椿株式会社 所在地 本社')[2].split('大島椿株式会社の営業拠点')[0]
                if staff not in section:
                    raise ValueError('集团简介中的人数不能归入该销售法人')
            items.append({'external_id':url,'url':url,'title':name+' · '+stage,'document':doc,'text':doc['text'],
                'supporting_documents':[company] if profile else [],'research':{
                'event_type':'existing_presence' if name=='株式会社三笠' else 'market_research',
                'workstream':'planned_china' if name=='株式会社わかさま陶芸' else 'existing_in_china',
                'participants':[{'role':'japan_company','name':name,**evidence(name_doc,name)}],
                'china_scope':'mainland','china_evidence':evidence(doc,cn),'facts':facts,
                'size':{'value':'sme',**evidence(company,staff)},'next_step':question}})
        except Exception as exc:
            errors.append({'url':url,'profile':profile,'error':str(exc)})
    return {'items':items,'status':('partial' if items else 'blocked') if errors else 'success',
        'error':str(errors) if errors else '', 'stats':{'errors':errors,'error_count':len(errors),
        'discovery_method':'editorial_recheck','editorial_recheck':len(items),
        'coverage_complete':False,'scope':'静态材料复核'}}
