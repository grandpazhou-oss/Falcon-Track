"""Mainland project cities. Chinese canonical names and source spelling aliases."""
import re
import unicodedata

EAST = {
    '上海': (), '苏州': ('蘇州',), '昆山': (), '太仓': ('太倉',),
    '无锡': ('無錫',), '常州': (), '南通': (), '张家港': ('張家港',),
    '南京': (), '杭州': (), '宁波': ('寧波',), '嘉兴': ('嘉興',),
    '绍兴': ('紹興',), '合肥': (), '宣城': (), '广德': ('広徳', '廣德', '広德'),
}
SOUTH = {
    '广州': ('広州', '廣州'), '深圳': ('深セン',), '东莞': ('東莞',),
    '佛山': ('仏山',), '珠海': (), '中山': (), '惠州': ('恵州',), '江门': ('江門',), '肇庆': ('肇慶',),
}
OTHER = {
    '天津': ('天津経済技術開発区',), '北京': (), '大连': ('大連',), '青岛': ('青島',),
    '成都': (), '重庆': ('重慶',), '武汉': ('武漢',), '厦门': ('廈門', '厦門', 'アモイ'),
    '西安': (), '常熟': (), '镇江': ('鎮江',), '盐城': ('鹽城',), '扬州': ('揚州',),
    '徐州': (), '泰州': (), '淮安': (), '连云港': ('連雲港',), '宿迁': ('宿遷',),
    '温州': ('溫州',), '烟台': ('煙台',), '威海': (), '哈尔滨': ('哈爾濱',),
    '济南': ('済南', '濟南'), '沈阳': ('瀋陽',), '长春': ('長春',), '福州': (), '郑州': ('鄭州',),
    '长沙': ('長沙',), '南昌': (), '芜湖': ('蕪湖',),
    '揭阳': ('掲陽', '揭陽'), '滁州': (), '福清': (), '秦皇岛': ('秦皇島',), '海口': (), '三亚': ('三亞',),
}
DISTRICTS = {'苏州工业园区': '苏州', '苏州高新区': '苏州', '无锡高新区': '无锡', '广州开发区': '广州'}
ALIASES = {alias: city for city, names in (OTHER | EAST | SOUTH).items() for alias in (city, *names)}
ALIASES.update(DISTRICTS)
CITY = re.compile('|'.join(map(re.escape, sorted(ALIASES, key=lambda s: (-len(s), s)))))
CITIES = set(ALIASES) | {name+'市' for name in ALIASES if not name.endswith(('市','区','區'))}
SEARCH_CITIES = tuple(city for pair in zip(tuple(EAST)[:9], SOUTH) for city in pair) + tuple(EAST)[9:]
WEIGHTS = {city: weight for group, weight in ((EAST, 10), (SOUTH, 6)) for city in group}
MAINLAND = re.compile(CITY.pattern + r'|中国(?!地方|経済産業局)|中華人民共和国|华东|華東|广东|広東|廣東|安徽')


def project_text(text):
    visible = unicodedata.normalize('NFKC', text or '')
    return re.sub(r'(?:本社所在地|本社|总部|總部)\s*[:：(（][^\n。)）]{0,120}[)）]?', '', visible)


def project_city_spelling(text, *, municipal_suffix=False):
    text = project_text(text)
    match = CITY.search(text)
    if not match:
        return ''
    spelling = match[0]
    if municipal_suffix and spelling+'市' in CITIES and text[match.end():match.end()+1]=='市':
        return spelling+'市'
    return spelling


def project_city(text):
    return ALIASES.get(project_city_spelling(text),'')


def region_weight(text):
    return max((WEIGHTS.get(ALIASES[hit], 0) for hit in CITY.findall(project_text(text))), default=0)
