"""Presentation fields shared by the dashboard, sales cards and details."""
import re

from .china_places import region_weight
from .models import KeywordCandidate
from .sales_feedback import annotate
from .sales_pools import build_sales_pools, intake_grades

SOURCE_LABELS = {
    'jetro_biznews':'JETRO 商务短信',
    'smrj_100oku':'100 亿宣言','issuer_ir_pdf':'发行人 IR','b_recheck_primary':'官网复核',
    'atpress_rss':'企业新闻稿','tavily':'公开资料检索','japan_china_store_news':'企业公告',
    'chiginkyo_news':'日本地方银行协会新闻','shokochukin_news':'商工中金新闻与支持案例',
    'jbic_sme_china':'日本国际协力银行中小企业公告','jbic_sme':'日本国际协力银行中小企业公告','prd_media_discovery':'公开媒体线索','smrj_subsidy_global':'官方扶持项目',
}


def display_card(card):
    if not card:
        return card
    card['ui_title']=card.get('company_name') or card.get('title') or '企业待核实'
    source=card.get('provider_key') or card.get('source_kind','')
    card['ui_source']=SOURCE_LABELS.get(source) or (source if re.search('[一-龯]',source) and '_' not in source else '公开来源')
    card['ui_size']={'micro_sme':'中小微','sme':'中小微','micro':'中小微','mid_large':'中大型'}.get(card.get('size_tier'),'规模待核')
    city=card.get('china_location') or ''
    card['ui_region']={10:'east',6:'south'}.get(region_weight(city),'other')
    return card


def sales_data(request):
    if not hasattr(request,'sales_data'):
        pools=build_sales_pools()
        groups=annotate(intake_grades(pools))
        cards=[display_card(card) for rows in groups.values() for card in rows]
        candidate_ids=[int(m[1]) for c in cards if (m:=re.fullmatch(r'/sales-pools/candidate/(\d+)/',c.get('detail_url','')))]
        pages=dict(KeywordCandidate.objects.filter(pk__in=candidate_ids).values_list('pk','result__pdf_page'))
        for card in cards:
            m=re.fullmatch(r'/sales-pools/candidate/(\d+)/',card.get('detail_url',''))
            card['ui_pdf_page']=pages.get(int(m[1])) if m else None
        request.sales_data=pools,groups
    return request.sales_data
