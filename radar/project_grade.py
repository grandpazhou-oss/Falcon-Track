"""Evidence gate for elevating an existing reviewable project to A."""
import hashlib
import re
import unicodedata

from django.core.management.base import CommandError

from .management.commands.backup_data import raw_source_file
from .project_analysis import normalize_excerpt

from .china_places import project_city
CHANGE = re.compile(r'(?:工[場厂]|産線|产线|生产线|生産|产能|拠点|建屋|設備).{0,70}(?:新設|新建|建設|扩产|扩建|拡張|増産|増強|増設|移転|搬迁|移管|稼働|投产|投入生産|投入生产|量産|量产|操業)|(?:新設|新建|増設|新工場|新工厂|扩产|扩建|新增|拡張|移転|搬迁|移管).{0,65}(?:工[場厂]|産線|产线|生产线|生産|产能|拠点|建屋|設備)|(?:子会社|子公司|現地法人|当地法人|新会社).{0,40}(?:設立|设立|稼働開始|开始运营)|生産.{0,35}移管|生产.{0,35}移管',re.S)
DISQUALIFIED = re.compile(r'撤回|(?:計画|项目|項目).{0,10}(?:中止|取消)|稼働率')


def original_matches(path, expected):
    if not path or not re.fullmatch('[0-9a-f]{64}',expected or ''):
        return False
    try:
        with raw_source_file(path).open('rb') as f:
            return hashlib.file_digest(f,'sha256').hexdigest()==expected
    except (OSError,CommandError):
        return False


def timeline_matches(period, quote):
    match=re.fullmatch(r'(202[6-9])(?:-(0[1-9]|1[0-2])(?:-(\d{2}))?|年([春夏秋冬])?)?',period or '')
    if not match:
        return False
    year,month,day,season=match.groups()
    raw=unicodedata.normalize('NFKC',normalize_excerpt(quote))
    if season:
        return bool(re.search(year+'年'+season,raw))
    if month:
        month=str(int(month))
        d=r'[-/.月]0?'+str(int(day))+r'(?:日|\b)' if day else ''
        return bool(re.search(r'(?<!\d)(?:'+year+'|'+year[-2:]+r')[-/.年]0?'+month+(d or r'(?!\d|月期)'),raw))
    return bool(re.search(r'(?<!\d)'+year+r'年(?!度|\d{1,2}月期)',raw))


def qualify(location, period, action, date_quote, *, source_verified, location_verified=False):
    action_city=project_city(action)
    city=project_city(location) or action_city
    if not city or (not action_city and not location_verified):
        return None
    if not CHANGE.search(normalize_excerpt(action or '')) or DISQUALIFIED.search(action or ''):
        return None
    if not timeline_matches(period,date_quote) or not source_verified:
        return None
    return {'city':city,'target_period':period,'action_quote':action,'time_quote':date_quote,
            'rule':'大陆城市＋2026–2029 项目进度＋一手原件 SHA-256 校验'}


def promote(card, evidence):
    if not evidence or card['grade']!='B':
        return card
    return {**card,'grade':'A','score':max(65,card['score']),'label':'优先联系',
            'target_period':evidence['target_period'],'promotion_basis':evidence}
