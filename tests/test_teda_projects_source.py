from collectors.sources import parse_detail, parse_listing


def test_teda_head_refresh_preserves_project_signal_and_excludes_external_links():
    listing = '''<html><body>
    <a title="关于某制造有限公司新建仓库项目的公布" href="/kfq/contents/11018/123.html">项目</a>
    <a title="关于某制造有限公司新建仓库项目的公布" href="https://other.example/kfq/contents/11018/456.html">项目</a>
    <a title="法规通知" href="/kfq/contents/11018/789.html">法规通知</a>
    </body></html>'''
    page = parse_listing('teda_projects', listing)
    assert len(page.items) == 1
    assert page.next_page is None
    assert page.items[0].url == 'https://www.tjbh.gov.cn/kfq/contents/11018/123.html'
    detail = '''<html><h1>关于某制造有限公司新建仓库项目的公布<br>发布日期：2026-10-16</h1>
    <div id="zoomcon"><p>某制造有限公司新建仓库项目规划方案总平面图已审批。</p>
    <p>公布期限为信息发布之日起至建设项目规划验收合格为止。</p>
    <p>联系电话：022-00000000 天津经济技术开发区规划和自然资源局</p>
    <a href="/kfq/upload/plan.jpg">规划图</a>
    <a href="https://other.example/plan.jpg">外站图</a></div></html>'''
    item = parse_detail('teda_projects', detail, page.items[0])
    assert item['kind'] == 'early_signal'
    assert item['metadata']['signal_type'] == 'construction_project'
    assert '政府联系人不是采购联系人' in item['metadata']['signal_note']
    assert '规划方案' in item['text'] and item['access_level'] == 'public'
    assert item['published_date'] == '2026-10-16'
    assert item['attachments'] == [{'url': 'https://www.tjbh.gov.cn/kfq/upload/plan.jpg', 'label': '规划图'}]
