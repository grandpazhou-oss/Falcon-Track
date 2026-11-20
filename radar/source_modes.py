"""One capability decision for the cards, POST endpoint and scheduled dispatcher."""
from collectors.china_entry_sources import DEFINITIONS
from collectors.japanese_public import REGISTRY
from collectors.sources import CATALOG
from collectors.source_cards import LISTS, CORPORATE
from .discovery import DIRECTORIES
from .research_sources import EXECUTABLE

ON_DEMAND = frozenset(LISTS) | CORPORATE
FREE_GROUPS = frozenset(key for key,(_, urls) in DEFINITIONS.items() if urls and key!='jbic_sme') | ON_DEMAND
RUNNABLE = frozenset(CATALOG) | frozenset(DIRECTORIES) | frozenset(REGISTRY) | FREE_GROUPS | frozenset(EXECUTABLE) | {'ysczw','suzhou_epia'}

MANUAL = {
    'jbic_sme_china':'原站要求先咨询商业复制权限，仅支持有权使用的材料导入。',
    'tdnet_biggo':'BigGo 的访问规则未能取得。可从发行人官网采集公告。',
    'fna_fbc_exhibitors':'展会报名站的访问规则无法读取。可从工场网主办方专题采集已公开的展商。',
    'jgoodtech_profiles':'J-GoodTech 条款限制商业使用。请先取得许可，或采集独立的公开企业宣言。',
}
ARCHIVE = {
    'archived-evidence-review':'该来源只标记历史原件复核，没有可采集的网站。',
    'nmpa_japan_product_records':'现有入口仅提供动态查询界面，没有已核验的公开数据接口。',
}


def source_mode(key):
    if key in ARCHIVE:
        return 'C',ARCHIVE[key]
    if key.startswith('manual'):
        return 'B','此来源由人工导入。'
    if key in MANUAL:
        return 'B',MANUAL[key]
    if key in RUNNABLE:
        return 'A',''
    return 'B','此来源尚未接通采集器，可导入经核对的原文。'


def schedulable(key):
    return key not in ON_DEMAND and source_mode(key)[0] == "A"
