"""Conservative organization roles and explicit reasons; no name-based ownership inference."""
import re

REASONS = {
    "source_redacted": "原站匿名或隐藏：追溯原始公告",
    "login_or_membership_required": "完整内容需要网站授权：取得授权材料后导入",
    "body_not_fetched": "详情或附件未取得：补充正文/附件",
    "parser_missed": "已取得内容尚未识别出明确采购人：重新解析并核对原文",
    "multiple_entities": "多个主体或角色待判断：逐项核对证据",
    "cross_source_conflict": "公开来源存在冲突：人工确认后采用",
    "not_applicable": "企业研究信息：关联建设单位不等于采购人",
    "buyer_role_unconfirmed": "已见企业名称，但采购角色仅来自标题或项目所在地，待证据确认",
}
ROLE_LABELS = {
    "buyer": r"采购人|招标人|采购方|发包人|招募人|征集人|寻源单位|采购单位|招标单位|需求单位|货主单位|业主单位",
    "agent": r"招标代理机构|采购代理机构|招标代理|采购代理|代理机构",
    "project_owner": r"建设单位|项目单位|项目业主",
    "group": r"所属集团|集团公司|品牌方",
    "china_entity": r"在华法人|在华子公司|中国子公司",
    "platform": r"平台运营方|平台运营单位",
    "environmental_consultant": r"环评单位|环评机构|环境影响评价单位|评价机构",
}
ORG = r"[\u4e00-\u9fffA-Za-z0-9][\u4e00-\u9fffA-Za-z0-9（）()·&\-]{1,85}?(?:股份有限公司|有限责任公司|有限公司|公司|研究院|大学|学院|医院|管理局|公安局|税务局|民政局|监狱|委员会)(?:[\u4e00-\u9fff]{2,20}(?:分公司|有限公司|公司|分局)(?:机关)?)?"

def organizations(title, text):
    source = title + "\n" + text
    found = []
    lines=source.splitlines()
    for i,line in enumerate(lines[:-1]):
        headers=[x.strip() for x in line.split('|')]
        labeled={column: role for column,header in enumerate(headers) for role,labels in ROLE_LABELS.items() if header in labels.split('|')}
        if not labeled:continue
        for row in lines[i+1:]:
            cells=[x.strip() for x in row.split('|')]
            if len(cells)!=len(headers):break
            for column,role in labeled.items():
                name=cells[column]
                if re.fullmatch(ORG,name) and not re.search(r'某企业|某公司|保密',name):
                    found.append({'name':name,'role':role,'quote':line+'\n'+row,'method':'explicit_table'})
    for role, labels in ROLE_LABELS.items():
        spaced = "|".join(r"\s*".join(re.escape(c) for c in label) for label in labels.split("|"))
        # Contact sections can use a numbered role heading followed by 名称 on the next line.
        pattern = rf"(?:^|[\n。；;|\s（(.、，,．])(?:\d+(?:\.\d+)*\s*)?(?:{spaced})(?:名称|信息)?\s*(?:[:：|为]\s*|\n\s*(?:名\s*称)\s*[:：]\s*)({ORG})"
        for m in re.finditer(pattern, source):
            name = m.group(1).strip()
            if re.search(r"某公司|某企业|不予|暂无|未公开", name):
                continue
            found.append({"name": name, "role": role, "quote": m.group(0).strip(), "method": "explicit_label"})
    for m in re.finditer(rf"(?:采购人|招标人|采购方)[（(]({ORG})[）)]", source):
        found.append({"name":m.group(1),"role":"buyer","quote":m.group(0),"method":"explicit_prose"})
    if not any(x["role"] == "buyer" for x in found):
        for m in re.finditer(rf"({ORG})[（(](?:以下简称[“\"]?)?(?:采购人|招标人|招募人)[”\"]?[）)]", source):
            name = re.split(r"[）)]受|^受|^关于", m.group(1))[-1]
            found.append({"name": name, "role": "buyer", "quote": m.group(0), "method": "explicit_prose"})
    if not any(x["role"] == "buyer" for x in found):
        headline = re.sub(r"^[【\[（(]?(?:关于)?", "", title)
        m = re.match(rf"({ORG})(?=(?:20\d{{2}}|关于|第|[\s·：:（）()\-]|[^\n]{{0,40}}(?:运输|仓储|配送|物流|承运商|装卸|入库|快递)))", headline)
        if m and re.search(r"有限(?:责任)?公司", m.group(1)) and not re.search(r"代理|咨询|关于|举办|采购|招标", m.group(1)):
            found.append({"name": m.group(1), "role": "buyer", "quote": title, "method": "title_candidate"})
    return list({(x["name"], x["role"]): x for x in found}.values())

def subject_reason(event, text, access="public"):
    if event.get("event_type") in {"early_signal", "irrelevant"}:
        return "not_applicable"
    if event.get("buyer_name"):
        return ""
    if access != "public" or re.search(r"会员|登录后|暂无下载权限", text):
        return "login_or_membership_required"
    if not text.strip():
        return "body_not_fetched"
    if re.search(r"\*{2,}|某企业|某公司|采购人.{0,5}(?:隐藏|保密|未公开)", text):
        return "source_redacted"
    if len({x['name'] for x in event.get('entities', []) if x['role']=='buyer'}) > 1:
        return "multiple_entities"
    if any(x.get('method')=='title_candidate' for x in event.get('entities',[])):
        return "buyer_role_unconfirmed"
    return "parser_missed"


def _affiliation_normalize(value):
    import unicodedata
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(value or '')).casefold())


def classify_japanese_link(name, discovery_evidence=None, ownership_status='', ownership_evidence='', dynamic=True):
    """Return an explainable candidate for the *identified* entity, never infer equity.

    Directory matches require an exact company-name mention in an authenticated source
    record. Brand and JV names remain weak search hints; same-name ambiguity needs review.
    """
    from .japanese_enterprise_seeds import CORE_BRANDS, JOINT_BRANDS, PEER_BRANDS, PEER_ACTIVITY, JAPANESE_LEGAL_SUFFIXES
    from .seed_dictionary import term_allowed, match_normalize, _load
    from django.conf import settings
    import unicodedata
    normalized = _affiliation_normalize(name)
    ascii_name = unicodedata.normalize('NFKC', str(name or '')).casefold()
    result = {'level':'none', 'matched':'', 'role':'unknown', 'evidence':[],
              'suffix_hint':any(s in str(name or '') for s in JAPANESE_LEGAL_SUFFIXES),
              'ownership_proven':False}
    if not normalized:
        return result
    records={}
    # Even static aliases must honour the current dictionary's ambiguity flag.
    path=settings.DATA_DIR/'japanese_enterprise_seeds.json'
    try:_,records=_load(str(path),path.stat().st_mtime_ns)
    except OSError:pass
    def has_brand(brand, force_context=False):
        needle=match_normalize(brand)
        record=records.get(needle,{})
        haystack=ascii_name if needle.isascii() else normalized
        return any(term_allowed(needle,haystack,m.start(),m.end(),ambiguous=force_context or record.get('ambiguous',False))
                   for m in re.finditer(re.escape(needle),haystack))
    valid=[]
    for item in discovery_evidence or []:
        if not isinstance(item,dict):continue
        key=str(item.get('source',''))
        cells=re.split(r'[|\n\t]',str(item.get('quote',''))) + [item.get('listed_name', '')]
        if (key in {'jetro_directory','shfia_directory'} or key.startswith('japan_directory_')) and item.get('status')=='japan_affiliation_unverified' and item.get('url') and any(normalized==_affiliation_normalize(cell) for cell in cells):
            valid.append({'source':key,'url':item['url'],'quote':item['quote']})
    if ownership_status=='japan_confirmed' and str(ownership_evidence or '').strip():
        result.update(level='confirmed',matched=name,evidence=[{'source':'manual_ownership','quote':ownership_evidence}],ownership_proven=True)
    elif valid:
        result.update(level='directory',matched=name,evidence=valid)
    else:
        for brand in JOINT_BRANDS:
            if has_brand(brand,force_context=True):
                result.update(level='joint_venture',matched=brand);break
        if result['level']=='none':
            for brand in sorted(CORE_BRANDS,key=len,reverse=True):
                if has_brand(brand):
                    result.update(level='seed',matched=brand);break
    if dynamic and result['level']=='none':
        from .seed_dictionary import matches
        hits = matches(name)
        if hits:
            result.update(level='seed', matched=hits[0]['term'], evidence=hits)
    peer_hit=any((bool(re.search(r'(?<![a-z])'+re.escape(x.casefold())+r'(?![a-z])',ascii_name)) if x.isascii() else _affiliation_normalize(x) in normalized) for x in PEER_BRANDS) or bool(re.search(r'(?<![a-z])nx(?![a-z])',ascii_name))
    activity=any(_affiliation_normalize(x) in normalized for x in PEER_ACTIVITY)
    equipment_maker=bool(re.search(r'(?:物流|仓储|倉庫).{0,8}(?:设备|設備|产品|産品|機械|机械|装备).{0,8}(?:制造|製造|生产|生産)',normalized))
    directory_logistics=result['level'] in {'directory','confirmed'} and activity and not equipment_maker
    if (result['level']!='none' and peer_hit and activity and not equipment_maker) or directory_logistics or any(x['source']=='japan_directory_mol_recruitment' for x in valid):
        result['role']='logistics_peer'
    return result


def business_role_for_company(name, profile=None, link=None):
    """Display categories are candidates based on name/profile, not verified roles."""
    link=link or classify_japanese_link(name)
    if link.get('role')=='logistics_peer':return 'peer_logistics'
    description=str(name or '')+' '+str((profile or {}).get('industry',''))
    if re.search(r'(?:物流|仓储|倉庫).{0,8}(?:设备|設備|产品|産品|机械|機械|装备).{0,8}(?:制造|製造|生产|生産)',description):return 'shipper'
    if re.search(r'物流|运输|運輸|货运|貨運|仓储|倉庫|供应链|供應鏈|货代|咨询|諮詢|律师|会计|協会|协会|服务|服务业|服务中心|logistics|freight|transport',description,re.I):
        return 'unknown_service'
    if re.search(r'制造|製造|工厂|工場|生产|生産|汽车|汽車|电机|電機|电子|電子|机械|機械|化工|化学|材料|半导体|半導体|食品|制药|製薬|器械',description,re.I):
        return 'shipper'
    return 'unknown_service'


def japanese_link_for_lead(lead):
    """Classify only the linked company, not other organizations in the text."""
    company=lead.company if lead.company_id and lead.buyer_role!='agent' else None
    if not company:
        meta = lead.metadata or {}
        result = classify_japanese_link('')
        claim = meta.get('japanese_employer_claim', '')
        if lead.source.kind == 'recruitment' and claim and claim in lead.text:
            result.update(level='platform_claim', matched=claim, evidence=[{'url':lead.url,'quote':claim}])
            if re.search(r'物流|货代|货运|运输|倉庫',claim):result['role']='logistics_peer'
        elif lead.source.kind == 'recruitment' and meta.get('language_hint'):
            result.update(level='language_hint',matched=meta['language_hint'],evidence=[{'url':lead.url,'quote':meta['language_hint']}])
        elif lead.source.kind == 'early_signal':
            from .seed_dictionary import matches
            # Old stored hints can predate the ambiguity rules; re-evaluate
            # the text rather than trusting a stale collector match.
            hits = matches(lead.title)
            if not hits:
                fallback=classify_japanese_link(lead.title,dynamic=False)
                if fallback['level']!='none':hits=[{'term':fallback['matched'],'basis':'known_brand'}]
            if not hits:
                from collectors.japanese_public import expansion_hits
                hits=[x for x in expansion_hits(lead.title,lead.text) if x.get('basis') not in {'explicit_japan_ownership','logistics_unknown'}]
            if hits:result.update(level='seed',matched=hits[0]['term'],evidence=hits)
            else:
                from collectors.japanese_public import expansion_hits
                claims=[x for x in expansion_hits(lead.title,lead.text) if x.get('basis')=='explicit_japan_ownership']
                if claims:result.update(level='explicit_claim',matched=claims[0]['term'],evidence=claims)
        return result
    result=classify_japanese_link(company.name,company.discovery_evidence,company.ownership_status,company.ownership_evidence)
    if result['level']=='none' and lead.source.kind=='early_signal':
        from collectors.japanese_public import expansion_hits
        claims=[x for x in expansion_hits(lead.title,lead.text) if x.get('basis')=='explicit_japan_ownership' and company.name in x.get('quote','')]
        if claims:result.update(level='explicit_claim',matched=claims[0]['term'],evidence=claims)
    return result
