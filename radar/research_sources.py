"""Source registry separates live collectors from leads for future investigation."""
from collectors.japan_channels import CHANNELS, TOKYO, JETRO, PRESS
from collectors.kanto_cases import KEY as KANTO, INDEX as KANTO_INDEX
from collectors.idec_cases import KEY as IDEC

from collectors.press_intake import SITES as PRESS_INTAKE
from collectors.china_entry_sources import DEFINITIONS as CHINA_ENTRY

SOURCES = {
 **{key:(name,next(iter(urls),''),'HTML/PDF','原站探活后以 collect_china_entry 手动采集','robots、限速、原件哈希','testing') for key,(name,urls) in CHINA_ENTRY.items() if key!='jbic_sme'},
 **{key:(name,url,'HTML','公开详情宽进，按地域和行业文本初筛','robots、限速和访问限制','testing') for key,(name,url,_) in PRESS_INTAKE.items()},
 IDEC: ('横滨 IDEC · 海外经营案例', KANTO_INDEX, 'HTML', '滚动读取栏目首页最多20篇详情，历史案例归研究', '公开原文与披露年度', 'testing'),
 KANTO: ('关东 · 商会与政府经营案例复核',KANTO_INDEX,'HTML/PDF','editorial_recheck · 静态复核既有经营事实、所在地及规模','公开材料，员工数按各法人披露口径','testing'),
 TOKYO: ('东京都中小企业振兴公社 · 海外支援案例','https://www.tokyo-kosha.or.jp/TTC/support/success_case/index.html','HTML','滚动栏目，历史案例逐篇核实大陆经营方向','公开原文及年度','testing'),
 JETRO: ('JETRO · 日本企业对华经营案例','https://www.jetro.go.jp/case_study/','HTML','中国标签中筛选本年度及上一年度具名销售案例','全国来源，企业所在地另核','testing'),
 PRESS: ('PR TIMES · 日本企业在华经营公告','https://prtimes.jp/topics/keywords/%E4%B8%AD%E5%9B%BD%E9%80%B2%E5%87%BA','HTML','中国进出与上海标签，核对署名、地点及日期，覆盖门店、工厂、法人和物流据点','企业自述的计划需回访','testing'),
 'sh_market_disclosures': ('上海市场开拓 · 企业原发复核', 'https://vsw.co.jp/shanghai-shop/', 'HTML', 'editorial_recheck · 静态复核既有企业原发链接', '逐站 robots 与证据校验', 'testing'),
 'fbc_ptc_manufacturers': ('工场网 · 上海PTC在华制造企业介绍', 'https://fna.cn/front/action/detail/526?type=industry', 'HTML', '当前链接对应的 PTC 展商专题，只收录有日本总部或独资原文的大陆企业', '公开专题；新一期需另行确认', 'testing'),
 'fhc_shanghai_japan': ('FHC 上海环球食品展 · 日本展商', 'https://www.fhcchina.com/exhibitor_list/country_%E6%97%A5%E6%9C%AC_lastadd_country', 'HTML', '日本地区展位和品类，地区标签不证明股权', '仅采集公开主办方名单', 'testing'),
 'osaka_ciie_roster': ('大阪政府上海事务所 · 进博会具名展商', 'https://osaka-sh.com.cn/news/', 'HTML', '最新公布的大阪展位企业及产品', '仅采集具名名单，排除招募', 'testing'),
 'jbic_sme_china': ('日本国际协力银行 · 中小企业对华融资', 'https://www.jbic.go.jp/ja/information/press/smes.html', 'HTML', '官方融资公告，回溯窗口最多24个月', '商业复制需事先咨询；仅允许用户提供有权使用的材料', 'policy_blocked'),
 'osaka_sme_china_sites': ('大阪府 · 中小制造业海外据点中国篇', 'https://www.pref.osaka.lg.jp/o110070/keizaikoryu/mono_kaigaikyoten/index.html', 'HTML/XLSX', '大阪制造业自报据点，非新设立名单', '检查 robots、限速及公开页面使用条款', 'testing'),
 'maff_export_plans': ('日本农水省 · 出口事业计划', 'https://www.maff.go.jp/j/shokusan/export/gfp/yusyutsu_keikaku_kohyo.html', 'HTML/PDF', '按计划年度筛选对中国大陆有明确原文的具名企业', '遵守来源引用；第三方图片权利单独核实', 'testing'),
 'china_zones_japan_projects': ('常州高新区 · 对日项目进展', 'https://www.cznd.gov.cn/class/EFBJILJD', 'HTML', '一个已选栏目，非全部园区；不限企业白名单', '遵守 robots、限速和官网声明', 'testing'),
 'osaka_china_business_events': ('大阪政府上海事务所 · 商谈活动', 'https://osaka-sh.com.cn/news/', 'HTML', '活动招募与具名参加分开核实', '活动无具名企业不生成客户记录', 'candidate'),
 'aichi_china_sites': ('爱知产业振兴机构 · 海外据点', 'https://www.aibsc.jp/support/961/', 'HTML/PDF', '2024 调查，不能当作2026新设', '结构化表格和权限待验证', 'candidate'),
 'fna_fbc_exhibitors': ('FNA/FBC · 商谈会', 'https://fbc.fna.cn/fbcsh/26PTC/index', 'HTML/PDF', '展商身份及历届资料待核', '列表或详情不可读时不绕过', 'candidate'),
 'parent_official_disclosures': ('企业官网 · 动态发现', 'https://www.waida.co.jp/', 'HTML/PDF', '从已发现企业逐一核实官网，不构建固定品牌池', '官网关联和访问条件须逐站确认', 'candidate'),
 'cifer_japan_registrations': ('海关 · 境外食品生产企业注册', 'https://ciferquery.singlewindow.cn/', '动态查询', '注册不是设立中国法人或已出货', '网站注明未经协议不得转载；先核权限', 'policy_blocked'),
 'nmpa_japan_product_records': ('国家药监局 · 进口产品', 'https://www.nmpa.gov.cn/zwfwqjd/index.html', '动态查询', '查询壳不计为具名数据', '412/验证码/网络失败不绕过', 'candidate'),
 'jetro_china_pavilion': ('JETRO · 中国相关展会', 'https://www.jetro.go.jp/services/export_guide/tradefair.html', 'HTML/PDF', '招募、入选和参展分开记录', '须取得具名参展原始材料', 'candidate'),
 'jgoodtech_profiles': ('J-GoodTech · 中小企业档案', 'https://jgoodtech.smrj.go.jp/pub/ja/', 'HTML', '中国供应商不等于中国客户或子公司', '详情读取及批量使用条件待核', 'candidate'),
 'prefecture_export_awards': ('日本地方政府 · 海外拓展', 'https://www.pref.hiroshima.lg.jp/soshiki/76/animal-spirits-r7.html', 'HTML/PDF', '没有明确中国证据时只作一般海外意向', '日本中国地方不能认作中国大陆', 'candidate'),
 'jcea_monthly_reference': ('日中经济协会 · 月度资料', 'https://www.jc-web.or.jp/', 'HTML/PDF', '转载汇编，仅作追溯原发线索', '未经许可不批量复制或再分发', 'policy_blocked'),
}
EXECUTABLE = set(CHANNELS) | set(PRESS_INTAKE) | {IDEC,KANTO,'sh_market_disclosures','fbc_ptc_manufacturers','fhc_shanghai_japan','osaka_ciie_roster','jbic_sme_china','osaka_sme_china_sites','maff_export_plans','china_zones_japan_projects'}

def definitions():
    return [{'key': key, 'name': row[0], 'url': row[1], 'kind': 'early_signal',
             'note': row[3]} for key,row in SOURCES.items()]
