"""Local evidence-linked name dictionary, independent from models and web fetching."""
import hashlib
import json
import os
import re
import unicodedata
from functools import lru_cache
from django.conf import settings
from django.utils import timezone
from .roster_import import normalize_name

from .china_places import MAINLAND

GEOGRAPHY = MAINLAND.pattern + r'|江苏|浙江|山东|辽宁'
LEGAL = r'(?:股份有限公司|有限责任公司|有限公司|有限会社|株式会社|股份公司|公司)$'
GENERIC = {'电子','贸易','科技','物流','工业','机械','电气','制造','国际','投资','供应链','商贸','日本','中国','集团','包装','材料','精密','技术','化工','设备','进出口','汽车','电机','仪器','信息','工业园区'}

# These names are particularly common in ordinary prose.  Even if a future
# roster happens to contain only one company for a brand, treat it as broad.
BROAD_BRANDS = {'三菱','三井','住友','日立','丰田','豐田','本田','松下','佳能'}
ORG_INDUSTRY = r'(?:股份|有限|集团|集団|公司|会社|工厂|工場|汽车|汽車|电机|電機|电子|電子|电气|電気|化工|化学|半导体|半導体|光学|医疗|医療|制药|製薬|食品|材料|机械|機械|精密|制造|製造|工业|工業|物流|运输|運輸|货运|貨運|仓储|倉庫|供应链|供應鏈|贸易|貿易|商事|商社|银行|銀行|保险|保険|电梯|電梯|重工|咨询|諮詢|通信|技术|技術|科技|数码|數碼|轮胎|輪胎|发动机|發動機|logistics|freight|transport)'
_INTERNAL_ORG = re.compile(ORG_INDUSTRY, re.I)


def match_normalize(text):
    text=unicodedata.normalize('NFKC',text or '').casefold()
    return re.sub(r'(?<=[^\x00-\x7f])[ \t]+|[ \t]+(?=[^\x00-\x7f])','',text)


def term_allowed(term, text, start, end, *, ambiguous=False):
    """A weak ambiguous name needs adjacent organization/industry context.

    An exact longer term containing the organization word already carries
    that context.  Never bridge a paragraph boundary to manufacture it.
    """
    if term == '东丽' and text[end:end+1] in {'区','县','镇'}:return False
    if term.isascii() and ((start and text[start-1].isascii() and text[start-1].isalnum()) or (end<len(text) and text[end].isascii() and text[end].isalnum())):return False
    if not ambiguous and term not in BROAD_BRANDS:return True
    if _INTERNAL_ORG.search(term):return True
    tail=text[end:].lstrip(' \t')
    return bool(re.match(r'(?:\([^()\n]{1,20}\))?'+ORG_INDUSTRY, tail, re.I))


def name_terms(name):
    from .japanese_enterprise_seeds import CORE_BRANDS
    name = normalize_name(name)
    terms = {name: 'full_name'}
    for brand in CORE_BRANDS:
        if brand in name and not (brand == '东丽' and re.search(r'东丽[区县镇]',name)):
            terms[brand] = 'known_brand'
    # Do not turn a branch suffix such as 苏州分公司 into a fake 苏州分.
    if re.search(r'分公司|代表处|办事处|事务所', name):return terms
    core = re.sub(LEGAL, '', name)
    core = re.sub(r'\((?:'+GEOGRAPHY+r')\)', '', core)
    core = re.sub(r'^(?:'+GEOGRAPHY+r')(?:市)?', '', core)
    if 2 <= len(core) <= 35 and core not in GENERIC and not core.endswith(('分','代表','办事')):
        terms[core] = 'derived_name'
    return terms


def export_dictionary():
    from filelock import FileLock
    with FileLock(str(settings.DATA_DIR / "seed-dictionary.lock"), timeout=30):
        return _export_dictionary()


def _export_dictionary():
    from .models import Company
    from .entities import classify_japanese_link
    terms = {}
    companies = 0
    for c in Company.objects.filter(subscribed=True).iterator():
        link = classify_japanese_link(c.name, c.discovery_evidence, c.ownership_status, c.ownership_evidence, dynamic=False)
        if link['level'] not in {'directory','confirmed'}:
            continue
        if any(e.get('entity_type') in {'unresolved','other_organization'} for e in c.discovery_evidence if isinstance(e,dict)):
            continue
        companies += 1
        for term, basis in name_terms(c.name).items():
            if term.endswith(('分','代表','办事')):continue
            from .entities import business_role_for_company
            record = terms.setdefault(term, {'term':term,'basis':basis,'company_ids':[],'evidence_urls':[],'business_roles':[]})
            record['company_ids'].append(c.pk)
            role=business_role_for_company(c.name, c.profile, link)
            if role not in record['business_roles']:record['business_roles'].append(role)
            record['evidence_urls'] = sorted(set(record['evidence_urls'] + [e['url'] for e in link['evidence'] if e.get('url')]))
    result = {'schema_version':1, 'generated_at':timezone.now().isoformat(), 'company_count':companies,
              'terms':[{**v,'ambiguous':len(v['company_ids'])>1} for k,v in sorted(terms.items())]}
    path = settings.DATA_DIR / 'japanese_enterprise_seeds.json'
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');os.replace(tmp,path)
    _load.cache_clear()
    return result


@lru_cache(maxsize=4)
def _load(path, modified):
    try:
        data = json.loads(open(path,encoding='utf-8').read())
    except (OSError,ValueError):
        return None, {}
    records={match_normalize(x['term']):x for x in data.get('terms',[]) if len(x.get('term',''))>=2}
    if not records:return None,{}
    pattern = re.compile('|'.join(re.escape(x) for x in sorted(records,key=len,reverse=True)))
    return pattern, records


def matches(text, limit=20):
    path=settings.DATA_DIR/'japanese_enterprise_seeds.json'
    try:modified=path.stat().st_mtime_ns
    except OSError:return []
    pattern,records=_load(str(path),modified)
    if not pattern:return []
    normalized=match_normalize(text)
    found=[];seen=set()
    pos=0
    while pos < len(normalized):
        match=pattern.search(normalized,pos)
        if not match:break
        key=match.group()
        if not term_allowed(key,normalized,match.start(),match.end(),ambiguous=records[key].get('ambiguous',False)):
            pos=match.start()+1
            continue
        if key not in seen:found.append(records[key]);seen.add(key)
        if len(found)>=limit:break
        pos=match.end()
    return found
