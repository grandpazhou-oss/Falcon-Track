"""Conservative, evidence-bearing extraction for public procurement notices.

No external calls are made here. A successful parse is not a verified sales lead.
Dates without a year or time retain their uncertainty instead of being invented.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

CHINA_TZ = timezone(timedelta(hours=8))
SERVICE_PATTERNS = {
    "transport": r"运输|派送|快递|邮寄|承运商|承运服务|整车|零担|干线|transport(?:ation)?|haulage|trucking",
    "warehousing": r"仓储|仓库运营|仓库管理服务|仓配|warehous(?:e|ing)",
    "distribution": r"配送|仓配|distribution|last[ -]mile",
    "freight_forwarding": r"货代|货运代理|国际货运|海运|空运|freight forward|ocean freight|air freight",
    "customs": r"报关|清关|customs clearance",
    "cold_chain": r"冷链|冷藏|冷冻|温控|cold chain|refrigerat|temperature.controlled",
    "inplant": r"厂内物流|线边配送|厂内配送|生产物流|in.plant logistics|line.side",
    "handling": r"装卸|搬运|loading|unloading|material handling",
}
SERVICE_LABELS = {
    "transport": "运输", "warehousing": "仓储", "distribution": "配送",
    "freight_forwarding": "国际货运／货代", "customs": "报关", "cold_chain": "冷链",
    "inplant": "厂内物流", "handling": "装卸搬运",
}
EVENT_PATTERNS = {
    "award": r"中标候选人|成交候选人|中标(?:[（(][^）)]{1,12}[）)])?结果|中选结果|成交结果|采购结果|中标公告|成交公告|终止公告|取消公告|废标公告|流标公告|中标供应商\s*[:：]|成交供应商\s*[:：]|拟定供应商\s*[:：]|单一来源采购公示|直接采购公示|award notice|contract awarded|cancellation notice",
    "change": r"延期公告|更正公告|澄清公告|补充公告|变更公告|延长.{0,10}截止|amendment|corrigendum",
    "supplier_recruitment": r"供应商.{0,6}(?:招募|征集|入库|准入)|(?:承运商|物流服务商|运输服务商).{0,6}(?:招募|征集|入库|准入)|征集.{0,50}供应商|供应商募集|供应商注册|supplier registration|supplier recruitment",
    "rfi": r"\bRFI\b|信息征询|信息征集|request for information",
    "rfq": r"\bRFQ\b|询价|询比价|询比采购|询比公告|谈判采购|比选|遴选|报价邀请|request for quotation",
    "tender": r"招标|投标邀请|竞争性磋商|竞争性谈判|谈判公告|采购公告|入札|\bRFP\b|\btender\b|request for proposal",
}
REGIONS = [
    "北京", "天津", "上海", "重庆", "河北", "山西", "辽宁", "吉林", "黑龙江",
    "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南",
    "广东", "海南", "四川", "贵州", "云南", "陕西", "甘肃", "青海", "台湾",
    "内蒙古", "广西", "西藏", "宁夏", "新疆", "香港", "澳门", "苏州", "无锡",
    "昆山", "张家港", "常熟", "南京", "杭州", "宁波", "嘉兴", "深圳", "广州",
    "东莞", "佛山", "武汉", "成都", "西安", "青岛", "大连", "沈阳", "南昌",
    "宜宾", "合肥", "长沙", "郑州", "日本", "东京", "大阪", "名古屋", "Singapore",
]

_DATE = re.compile(
    r"(?<!\d)(?P<year>20\d{2})\s*(?:年|[-/.])\s*(?P<month>\d{1,2})\s*(?:月|[-/.])\s*"
    r"(?P<day>\d{1,2})\s*日?(?:[ T\t]*(?P<hour>\d{1,2})\s*(?:[:：时])\s*(?P<minute>\d{1,2})?\s*分?(?::(?P<second>\d{1,2}))?)?"
)
_DEADLINE_LABEL = re.compile(
    r"报名(?:截止(?:时间|日期)?|结束(?:时间|日期)?|时间)|响应文件递交截止(?:时间|日期)?|响应(?:截止(?:时间|日期)?)|"
    r"报价(?:截止(?:时间|日期)?)|投标(?:文件)?(?:截止(?:时间|日期)?)|递交(?:截止(?:时间|日期)?)|"
    r"(?:报名|报价|投标|响应)材料提交截止(?:时间|日期)?|"
    r"(?:招标|采购)文件获取时间|购买(?:招标|采购)文件时间|获取时间|发售截止时间|截止日期|截止时间|截止至|截止到|截至|registration deadline|bid deadline|submission deadline|quotation deadline",
    re.I,
)


def parse_explicit_dates(value: str) -> list[tuple[str, bool, str]]:
    """Return ISO value, actual time precision, and original date fragment."""
    result: list[tuple[str, bool, str]] = []
    for match in _DATE.finditer(value or ""):
        try:
            y, m, d = (int(match.group(x)) for x in ("year", "month", "day"))
            if match.group("hour") is not None:
                parsed = datetime(y, m, d, int(match.group("hour")), int(match.group("minute") or 0),
                                  int(match.group("second") or 0), tzinfo=CHINA_TZ)
                result.append((parsed.isoformat(), True, match.group(0).strip()))
            else:
                result.append((date(y, m, d).isoformat(), False, match.group(0).strip()))
        except (ValueError, TypeError):
            continue
    return result


def _short_quote(source: str, start: int, end: int, maximum: int = 240) -> str:
    line_start = source.rfind("\n", 0, start) + 1
    line_end = source.find("\n", end)
    if line_end < 0:
        line_end = len(source)
    if line_end - line_start <= maximum:
        return source[line_start:line_end].strip()
    return source[max(line_start, start - 28): min(line_end, max(end, start) + 160)].strip()


def _find_buyer(source: str) -> tuple[str | None, str | None]:
    from radar.entities import organizations
    title, _, body = source.partition("\n")
    buyers = [x for x in organizations(title, body) if x["role"] == "buyer" and x["method"] != "title_candidate"]
    if len({x["name"] for x in buyers}) == 1:
        return buyers[0]["name"], buyers[0]["quote"]
    return None, None


def _non_service_reason(title: str, source: str) -> str | None:
    unconditional = re.search(r"旅客地面运输|客车通勤|班车|校车|案例复盘|门店电话|专线广告|企业年金|结算审计|安全技术评价|资产处置|软硬件采购|系统维护|运输车采购|运输船采购|配送车辆租赁|低空物流.{0,5}装修|计量测试公共服务平台",title)
    if unconditional:return unconditional.group(0)
    goods = re.search(r"食材|蔬菜|饮食原材料|农副产品|盒饭|成品餐|中药饮片|原材料物资|办公设施设备耗材",title)
    if goods and not re.search(r"运输服务|运输外包|冷链运输",title):return goods.group(0)+'供应与配送，尚未证实独立物流外包'
    explicit_services = re.search(
        r"运输服务|物流服务|仓储服务|配送服务|货运代理服务|装卸服务|承运商|仓库运营|仓储运营|仓配一体|厂内物流|线边配送|运输项目|运输招标|运输询价", title, re.I
    )
    if not explicit_services:
        exclusions = (
            r"(?:仓库|物流园|仓储|配送中心|物流中心).{0,14}(?:施工|建设|建造|改造工程)",
            r"(?:叉车|货车|车辆|托盘|货架|输送设备|物流设备|仓储设备|办公用品|食材|饮用水|原材料)\s*(?:采购|购置|招标)",
            r"(?:物流|仓储|运输|配送).{0,8}(?:软件|信息系统|管理系统|信息化系统|系统开发)",
            r"(?:车辆尾板|雨布|计量测试平台|研训系统|监控平台|立体库系统|办公室装修|交通运输服务).{0,20}(?:购置|采购|维护|保养|招标)",
            r"旅客地面运输|班车|校车|案例复盘|门店电话|专线广告",
            r"(?:物流基地|仓储基地).{0,20}(?:装修|施工)",
            r"(?:WMS|TMS|ERP)\s*(?:系统|平台|软件|采购|招标)",
        )
        for pattern in exclusions:
            hit = re.search(pattern, title, re.I)
            if hit:
                return hit.group(0)
        purchase = re.search(r"采购(?:内容|标的|项目)\s*[:：]\s*([^\n]{1,100})", source)
        if purchase and re.search(r"(?:叉车|托盘|货架|设备|食材|饮用水|办公用品).{0,18}(?:含|包含|负责|免费).{0,12}(?:配送|运输|送货)", purchase.group(1)):
            return purchase.group(0)
    return None


def _deadline_candidates(source: str) -> list[dict[str, Any]]:
    candidates = []
    for match in _DEADLINE_LABEL.finditer(source):
        # Limit the window and stop at another labelled field to avoid taking a publication date.
        end = min(len(source), match.end() + 100)
        window = source[match.end():end]
        window = re.sub(r'[（(]以下简称[“\"]截止时间[”\"][）)]', '', window)
        if re.match(r"\s*(?:前|后|之前|之后|届满)",window):continue
        # A deadline mention must not borrow the next paragraph's publication/start date.
        window = re.split(r"\n(?=\s*(?:\(二\)|（二）)?\s*(?:采购公告发布时间|发布日期|公告发布))",window)[0]
        next_deadline_label = _DEADLINE_LABEL.search(window)
        if next_deadline_label:
            window = window[:next_deadline_label.start()]
        stop = re.search(r"\n\s*(?:发布日期|发布时间|公告日期|服务期限|服务期|开标时间|项目名称|采购人|招标人)\s*[:：]", window)
        if stop:
            window = window[:stop.start()]
        dates = parse_explicit_dates(window)
        if not dates:
            continue
        label = match.group(0)
        # A registration window contains its start and end; only the end is a deadline.
        is_window = label == "报名时间" or "获取时间" in label or label.startswith("购买")
        pick = dates[-1] if is_window and len(dates) >= 2 else dates[0]
        if is_window and len(dates) < 2:
            continue
        kind = "registration" if re.search(r"报名|获取时间|购买|发售|registration", label, re.I) else (
            "bid" if re.search(r"报价|投标|响应|递交|bid|submission|quotation", label, re.I) else "unknown"
        )
        fragment_end = source.find(pick[2], match.end(), end)
        quote_end = fragment_end + len(pick[2]) if fragment_end >= 0 else match.end()
        candidates.append({"value": pick[0], "has_time": pick[1], "kind": kind,
                           "quote": source[match.start():quote_end].strip()})
    for m in re.finditer(r"供应商在(20\d{2}[^\n]{1,100}至20\d{2}[^\n]{1,100})",source):
        if not re.search(r'下载(?:询比)?采购文件|获取采购文件',m.group(1)):continue
        dates=parse_explicit_dates(m.group(1))
        if len(dates)>=2:
            value=dates[-1];candidates.append({'value':value[0],'has_time':value[1],'kind':'registration','quote':m.group(0)})
    for m in re.finditer(r"采购公告发布时间为([^\n]{1,100})",source):
        dates=parse_explicit_dates(m.group(1))
        nearby=source[m.end():m.end()+350]
        if len(dates)>=2 and '报名' in nearby and '截止' in nearby:
            value=dates[-1]
            candidates.append({'value':value[0],'has_time':value[1],'kind':'registration','quote':m.group(0)})
    for m in re.finditer(r"投标人应在([^\n]{8,70}?)截止时间",source):
        dates=parse_explicit_dates(m.group(1))
        if dates:
            value=dates[-1];candidates.append({'value':value[0],'has_time':value[1],'kind':'bid','quote':m.group(0)})
    for m in re.finditer(r"投标人应在截止时间\s*([^\n。]{1,100})",source):
        dates=parse_explicit_dates(m.group(1))
        if dates:
            value=dates[0];end=m.group(0).find(value[2])+len(value[2])
            candidates.append({'value':value[0],'has_time':value[1],'kind':'bid','quote':m.group(0)[:end]})
    # Some announcements put the file-sale heading and its end date on adjacent lines.
    for m in re.finditer(r"(?:招标|采购|比选|询比|谈判|磋商)文件(?:的)?(?:发售|获取|购买)(?:时间|期限)[^\n]{0,30}\n[ \t]*(?:\d+[.、])?\s*时间\s*[:：]\s*([^\n]{1,180})",source):
        window=m.group(1)
        if not re.search(r"(?:至|截至|截止|到)\s*20\d{2}",window):continue
        dates=parse_explicit_dates(window)
        if dates:
            value=dates[-1];candidates.append({'value':value[0],'has_time':value[1],'kind':'registration','quote':m.group(0)})
    # File acquisition windows can also be written as prose in the same paragraph.
    for m in re.finditer(r"(?:请|须|可)于([^\n。]{1,180}?)(?:获取|购买)(?:招标|采购)文件",source):
        window=m.group(1)
        dates=parse_explicit_dates(window)
        if len(dates)>=2 and re.search(r"(?:至|到)\s*20\d{2}",window):
            value=dates[-1];candidates.append({'value':value[0],'has_time':value[1],'kind':'registration','quote':m.group(0)})
    return candidates


def usable_participation(value: str | None) -> bool:
    """A generic 'see attachment/login later' is not a usable participation route."""
    if not value or re.search(r"详见附件|会员|登录后可见|待通知|另行通知|未公开|指定平台|相关平台", value):
        return False
    if re.search(r"https?://[^\s]+|www\.[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", value):
        return True
    if re.search(r"现场|递交至|提交至|邮寄", value) and re.search(r"(?:市|区|县).{1,40}(?:路|街|号|楼)", value):
        return True
    platform = re.search(r"([\u4e00-\u9fffA-Za-z0-9]{2,40})(?:平台|系统)", value)
    if platform:
        name = re.sub(r"^(?:请|登录|登陆|通过|在|进入|使用)+", "", platform.group(1))
        name = re.sub(r"电子|采购|招标|交易|供应商|公共资源|报名|投标", "", name)
        return len(name) >= 2 and name not in ("统一", "指定", "相关", "上述")
    return False


def _participation(source: str) -> tuple[str | None, str | None]:
    labelled = re.compile(r"(?:报名方式|投标方式|参与方式|递交方式|提交方式|报价方式|应标方式|报名渠道|获取方式)\s*[:：]\s*([^\n\r]{3,240})")
    for match in labelled.finditer(source):
        value = match.group(1).strip()
        if usable_participation(value):
            return value, match.group(0).strip()
    for line in source.splitlines():
        line = line.strip()
        if 4 <= len(line) <= 1200 and re.search(r"投标人|供应商|登录|登陆|报名|获取|购标", line) and re.search(r"登录|登陆|发送|提交至|递交至|报名方式|现场(?:报名|购买|领取|获取|递交|提交)|获取方法", line) and not re.search(r"信用中国|失信被执行|经营异常",line) and usable_participation(line):
            return line[:1000], line[:1000]
    return None, None


def _parse_iso(value: str | None) -> date | datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if "T" in value or " " in value else date.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def qualify_event(event: dict[str, Any], as_of: date | None = None) -> dict[str, Any]:
    """Recompute business status from validated facts, never from model status claims."""
    current = datetime.now(CHINA_TZ)
    today = as_of or current.date()
    if event.get("event_type") in {"early_signal", "irrelevant"}:
        event.update(processing_status=event["event_type"], deadline=None, deadline_kind="unknown",
                     deadline_has_time=False, participation_path=None, qualifications=[],
                     missing_fields=(["related_company"] if not event.get("buyer_name") else []) +
                                    (["project_location"] if not event.get("regions") else []),
                     ownership_status="unknown")
        event["summary"] = ("企业研究：" if event["event_type"] == "early_signal" else "低相关归档：") + (event.get("buyer_name") or "关联企业待核实") + "；" + event.get("relevance_reason", "尚未证实物流外包需求。")
        return event
    missing = []
    open_ended = event.get("event_type") == "supplier_recruitment" and event.get("deadline_semantics") == "open_ended"
    if not event.get("buyer_name"):
        missing.append("buyer_name")
    if not event.get("service_types"):
        missing.append("service_types")
    parsed = _parse_iso(event.get("deadline"))
    if parsed is None and not open_ended:
        missing.append("deadline")
    if event.get("deadline_kind") not in ("registration", "bid") and not open_ended:
        missing.append("deadline_kind")
    if not usable_participation(event.get("participation_path")):
        missing.append("participation_path")
    if not event.get("qualifications"):
        missing.append("qualifications")
    # Unknown foreign ownership does not disqualify a valid logistics demand.
    missing.append("ownership_evidence")

    event_type = event.get("event_type", "unknown")
    deadline_date = parsed.date() if isinstance(parsed, datetime) else parsed
    expired = bool(deadline_date and deadline_date < today)
    same_day_unconfirmed = bool(deadline_date == today)
    if isinstance(parsed, datetime) and as_of is None:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=CHINA_TZ)
        expired = parsed < current
        same_day_unconfirmed = False
    if same_day_unconfirmed:
        missing.append("deadline_time_verification")

    if event_type == "early_signal":
        status = "early_signal"
    elif event_type == "irrelevant":
        status = "irrelevant"
    elif event_type == "award":
        status = "closed"
    elif expired:
        bid = _parse_iso(event.get("bid_deadline"))
        bid_date = bid.date() if isinstance(bid,datetime) else bid
        bid_open = bool(bid_date and bid_date>=today)
        if isinstance(bid,datetime) and as_of is None:
            bid_open = (bid if bid.tzinfo else bid.replace(tzinfo=CHINA_TZ)) >= current
        if event.get("deadline_kind")=="registration" and bid_open:
            status = "needs_review"
            missing.append("registration_closed")
        else:
            status = "closed"
    elif (
        event_type in ("tender", "rfq", "supplier_recruitment")
        and event.get("buyer_name") and event.get("service_types")
        and usable_participation(event.get("participation_path")) and (parsed is not None or open_ended)
        and (event.get("deadline_kind") in ("registration", "bid") or open_ended)
        and not same_day_unconfirmed and event.get("buyer_role") != "agent"
    ):
        # A bid date alone does not prove that earlier registration is still open.
        path = event.get("participation_path", "") + "\n" + "\n".join(
            x.get("quote", "") for x in event.get("evidence", []) if x.get("field") == "participation_path"
        )
        if event.get("deadline_kind") == "bid" and re.search(r"报名|准入|入库|注册", path) and not re.search(r"无需|不需|免于", path):
            status = "needs_review"
            missing.append("registration_deadline")
        else:
            status = "actionable"
    else:
        status = "needs_review"
    event["processing_status"] = status
    event["missing_fields"] = sorted(set(missing))
    event["ownership_status"] = "unknown"
    method = "模型提取" if event.get("extraction_method") == "model" else "规则初筛"
    service_label = "、".join(SERVICE_LABELS.get(s, s) for s in event.get("service_types", [])) or "服务范围待核实"
    buyer = event.get("buyer_name") or "采购主体待核实"
    suffix = {
        "actionable": "具备公开参与线索，资格与承接能力仍需营业复核",
        "needs_review": "需补齐或核对参与条件",
        "early_signal": "企业变化信号，尚未证实物流外包",
        "closed": "历史、结果或已过期限信息",
        "irrelevant": "当前内容不属于明确的物流服务采购",
    }[status]
    event["summary"] = f"{method}：{buyer}；{service_label}；{suffix}。"
    return event


def extract_event(
    title: str, text: str, source_kind: str = "procurement", as_of: date | None = None,
    listed_deadline: str | None = None,
) -> dict[str, Any]:
    """Extract only explicit facts; callers must handle source access and provenance."""
    title, text = str(title or "").strip(), str(text or "").strip()
    source = f"{title}\n{text}"
    evidence: list[dict[str, str]] = []
    event: dict[str, Any] = {
        "buyer_name": None, "buyer_role": "unknown", "service_types": [], "regions": [],
        "event_type": "unknown", "deadline": None, "deadline_kind": "unknown",
        "deadline_has_time": False, "service_period": None, "qualifications": [],
        "participation_path": None, "evidence": evidence, "missing_fields": [],
        "processing_status": "needs_review", "ownership_status": "unknown", "summary": "",
        "extraction_method": "rules", "model_warning": None,
    }
    for service, pattern in SERVICE_PATTERNS.items():
        match = re.search(pattern, source, re.I)
        if match:
            event["service_types"].append(service)
            evidence.append({"field": "service_types", "quote": _short_quote(source, match.start(), match.end())})
    location_text = "\n".join(line for line in source.splitlines() if not re.search(r"条例|管理办法|法律|法规|环评单位|评价机构|版权所有|备案号", line)
                              and re.search(r"项目地点|项目地址|建设地点|位于|坐落|服务区域|运输线路|地区[：:]|配送范围|(?:至|到).{0,20}(?:运输|线路)|运输.{0,30}(?:至|到)", line))
    for region in REGIONS:
        match = re.search(re.escape(region), location_text, re.I)
        if match:
            event["regions"].append(region)
            evidence.append({"field": "regions", "quote": _short_quote(location_text, match.start(), match.end())})
    event["regions"] = event["regions"][:12]

    from radar.entities import organizations
    event["entities"] = organizations(title, text)
    buyer, buyer_quote = _find_buyer(source)
    if source_kind == "early_signal":
        owners = [x for x in event["entities"] if x["role"] == "project_owner"]
        buyer, buyer_quote = (owners[0]["name"], owners[0]["quote"]) if len({x["name"] for x in owners}) == 1 else (None, None)
        event["entities"] = [x for x in event["entities"] if x["method"] != "title_candidate"]
    if buyer:
        event["buyer_name"] = buyer
        evidence.append({"field": "buyer_name", "quote": buyer_quote or buyer})
        if re.search(r"物流|货运代理|运输有限公司|运输有限责任公司|logistics|freight", buyer, re.I):
            event["buyer_role"] = "lsp"
            evidence.append({"field": "buyer_role", "quote": buyer_quote or buyer})
        elif re.search(r"货主(?:单位)?\s*[:：]\s*" + re.escape(buyer), source):
            event["buyer_role"] = "shipper"
    # Only the identified buyer or project owner is matched; unrelated agents,
    # cited customers and anonymous subjects cannot confer an affiliation.
    from radar.entities import classify_japanese_link
    event['japanese_link'] = classify_japanese_link(buyer)
    event['is_japanese_linked'] = event['japanese_link']['level']

    irrelevant = _non_service_reason(title, source)
    if source_kind == "early_signal":
        relevant = re.search(r"扩产|扩建|搬迁|产业化|产线|生产|制造|年产|吨/年|仓库|配送中心|仓储|工厂|运输|物流|配送", title)
        if not relevant and re.search(r"新建|技改|技术改造",title):
            relevant = re.search(r"[^\n]{0,60}(?:年新增|年产|生产厂房|生产线)[^\n]{0,100}",source)
        low = re.search(r"宿舍|住宅|学校|教学|办公楼|医院", title) and not re.search(r"运输|仓储|配送|物流", title)
        event["event_type"] = "early_signal" if relevant and not low else "irrelevant"
        event["relevance_reason"] = "生产/仓网变化可能影响物流，属于推断，尚无外包证据。" if event["event_type"] == "early_signal" else "未取得与物流需求直接相关的事件证据。"
        evidence.append({"field": "event_type", "quote": title[:240]})
        if relevant and relevant.string != title:
            evidence.append({"field": "event_type", "quote": relevant.group(0)})
    elif irrelevant:
        event["event_type"] = "irrelevant"
        evidence.append({"field": "event_type", "quote": irrelevant})
    else:
        for event_type, pattern in EVENT_PATTERNS.items():
            # Most closure/change signals must be in the headline; explicit supplier labels may be in the body.
            headline_has_event = any(re.search(p,title,re.I) for p in EVENT_PATTERNS.values())
            haystack = title if headline_has_event or event_type in ("award", "change", "supplier_recruitment") else source
            match = re.search(pattern, haystack, re.I)
            if not match and event_type == "award":
                match = re.search(r"(?:^|\n)\s*(?:中标供应商|成交供应商|拟定供应商)\s*[:：]\s*[^\n]{2,90}", source)
                haystack = source
            if match:
                event["event_type"] = event_type
                evidence.append({"field": "event_type", "quote": _short_quote(haystack, match.start(), match.end())})
                break

    deadlines = _deadline_candidates(source)
    if deadlines:
        registration = [x for x in deadlines if x["kind"] == "registration"]
        bids = [x for x in deadlines if x["kind"] == "bid"]
        if bids:
            event["bid_deadline"] = min(bids, key=lambda x: x["value"])["value"]
        chosen = min(registration or bids or deadlines, key=lambda x: x["value"])
        event.update(deadline=chosen["value"], deadline_kind=chosen["kind"], deadline_has_time=chosen["has_time"])
        evidence.extend({"field": "deadline", "quote": x["quote"]} for x in deadlines[:6])
    elif listed_deadline:
        values = parse_explicit_dates(str(listed_deadline))
        if values:
            event.update(deadline=values[0][0], deadline_has_time=values[0][1], deadline_kind="unknown")
            # Structured collector metadata is not an excerpt from the article.
            evidence.append({"field": "listed_deadline", "quote": str(listed_deadline)[:120]})

    path, path_quote = _participation(source)
    if path:
        event["participation_path"] = path
        evidence.append({"field": "participation_path", "quote": path_quote or path})
    period = re.search(r"(?:服务期(?:限)?|合同期(?:限)?)\s*[:：]\s*([^\n\r]{2,160})", source)
    if period:
        event["service_period"] = period.group(1).strip()
        evidence.append({"field": "service_period", "quote": period.group(0).strip()})
    for match in re.finditer(r"(?:资质要求|资格要求|业绩要求|准入条件|投标人资格)\s*[:：]\s*([^\n\r]{3,240})", source):
        value = match.group(1).strip()
        event["qualifications"].append(value)
        evidence.append({"field": "qualifications", "quote": match.group(0).strip()})
        if len(event["qualifications"]) >= 6:
            break
    event["deadline_semantics"] = "dated" if event.get("deadline") else "unknown"
    if event["event_type"] == "supplier_recruitment":
        opened = re.search(r"长期(?:开放|有效|接受报名|招募)|常年(?:接受|招募|开放)|随时(?:报名|注册)", source)
        if opened and not event.get("deadline"):
            event["deadline_semantics"] = "open_ended"
            evidence.append({"field": "deadline_semantics", "quote": opened.group(0)})
    registrations={x['value'] for x in deadlines if x['kind']=='registration'}
    if len(registrations)>1:event['deadline_conflict']=True
    event["evidence"] = list({(x["field"], x["quote"]): x for x in evidence if x["quote"]}.values())[:40]
    event=qualify_event(event, as_of)
    if event.get("deadline_conflict") and event["processing_status"]=="actionable":
        event["processing_status"]="needs_review";event["missing_fields"].append("deadline_conflict")
    return event


def recruitment_signal_event(event, published=False):
    """A named recruitment posting is an employer signal, never a procurement award."""
    event.update(event_type='early_signal', processing_status='early_signal', buyer_name=None,
                 buyer_role='unknown', deadline=None, deadline_kind='unknown', deadline_has_time=False,
                 deadline_semantics='unknown', entities=[],
                 summary=('招聘岗位信号：发布时间见原文，是否仍招聘未知；招聘不代表已确认物流外包采购。' if published else '招聘岗位信号：发布时间和有效性未知；招聘不代表已确认物流外包采购。'),
                 missing_fields=[], qualifications=[], participation_path='')
    event['evidence'] = [e for e in event.get('evidence', [])
                         if e.get('field') not in {'buyer_name', 'buyer_role', 'deadline'}]
    return event
