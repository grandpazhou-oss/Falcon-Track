from types import SimpleNamespace

import pytest
from django.template.loader import render_to_string
from pydantic import ValidationError

from radar import pro_brief as brief
from radar.analysis import generation_parameters
from radar.mimo_tiers import PRO


def sample_fact():
    return {'field':'扩产计划','summary_zh':'上海 计划在2027年春启用新厂房。','quote':'上海臨港\n既存工場を拡張（2027年春より稼働開始予定）',
            'block_id':'k462:p19:s1','page':19,'event_status':'planned','date_kind':'event','date_text':'2027年春',
            'date_block_id':'k462:p19:s1','date_quote':'2027年春'}


def sample_brief():
    return {
        'executive_brief':{'headline':'公司拟在上海扩产','window_judgment':'尽快核对产线窗口','opportunity_summary':'优先核对仓配需求'},
        'logistics_opportunities':[
            {'line_name':'国内仓配','timing_tag':'优先接洽','detail':'核对工厂仓配货流','basis_label':brief.INFERENCE},
            {'line_name':'设备运输','timing_tag':'配套增量','detail':'核对搬迁运输清单','basis_label':brief.INFERENCE}],
        'entry_strategy':{'target_roles':['物流负责人'],'pitch_angle':'了解交付计划','pitfall_warning':'尊重既有承运商'},
        'discovery_questions':['如何安排设备转运？','是否另有仓储需求？','哪些节点需要应急支持？'],
        'outreach_kit':{'email_subject_ja':'物流の情報交換のお願い','email_body_ja':'貴社ご担当者様。上海の工場に関するご発表を拝見しました。物流体制につきまして、十五分ほど情報交換のお時間をいただければ幸いです。どうぞよろしくお願いいたします。',
                        'call_script_zh':'您好，我想请教这次工厂扩产过程中，物流团队是否会调整收货和仓配窗口？',
                        'objection_reply_zh':'我们可以先从备选方案谈起。'}}


def test_two_part_schema_and_layout_normalization_only():
    fact = sample_fact()
    block = {'text':fact['quote'].replace('既存','\uf06e 既 存'),'document_id':None,'hash':'hash','page':19,'url':'https://example.org/a.pdf'}
    wrong = {**fact,'summary_zh':'广州在2026年第二季度完成扩产。','quote':'广州工厂已开工。'}
    data = brief.FactBatch.model_validate({'facts':[fact,wrong]}).model_dump()
    facts, issues = brief.validate_facts(data,{fact['block_id']:block},[],'2026-11-20')
    assert facts[0]['summary_zh'].startswith('上海计划') and facts[0]['page']==19
    assert not facts[0]['needs_manual_check'] and facts[1]['needs_manual_check'] and len(issues)==1
    payload = brief.SalesBrief.model_validate(sample_brief()).model_dump()
    result, issues = brief.validate_brief(payload,[facts[0]])
    assert len(result['discovery_questions'])==3 and not issues
    payload['discovery_questions'][0] = '上海的设备能否配合2026年Q2投产？'
    result, issues = brief.validate_brief(payload,[facts[0]])
    assert len(issues)==1 and len(result['discovery_questions'])==2
    with pytest.raises(ValidationError):
        brief.SalesBrief.model_validate({**payload,'discovery_questions':['只有一条']})


def test_fact_date_cannot_import_the_foreign_project():
    fact = {**sample_fact(),'quote':'上海臨港：拡張予定。ベトナム生産：2027年4月開業予定',
            'summary_zh':'上海拟在2027年4月投产。','date_text':'2027年4月','date_quote':'2027年4月'}
    block = {'text':fact['quote'],'document_id':None,'hash':'hash','page':19,'url':'https://example.org/a.pdf'}
    facts,_=brief.validate_facts({'facts':[fact]},{fact['block_id']:block},[],'2026-11-20')
    assert facts[0]['needs_manual_check']


def test_render_new_and_legacy_without_exposing_refs_or_empty_headings():
    fact = {**sample_fact(),'url':'https://example.org/a.pdf','needs_manual_check':True}
    def render(result):
        return render_to_string('radar/analysis_result.html',{'ui_lang':'zh','run':SimpleNamespace(
            result={'analysis':result},capability_snapshot={'services':'仓配'},input_snapshot={})})
    page=render({'brief':sample_brief(),'facts':[fact]})
    assert page.count('data-copy-trigger')==3 and '需人工复核' in page
    assert '查看原始取证依据与校验详情' in page and '#page=19' in page
    assert 'k462:p19:s1' not in page and '待确认问题' not in page
    old=render({'facts':[{'field':'公开主体','value':'例公司','quote':'原始日文引文','block_id':'k461:s44'}]})
    assert '公开主体：例公司' in old and '原始日文引文' in old and 'k461:s44' not in old
    assert '待确认问题' not in old and '下一步建议' not in old


def test_pro_reasoning_serialization():
    cfg=SimpleNamespace(model=PRO,max_tokens=10000,base_url='https://api.xiaomimimo.com/v1',pro_thinking=True)
    assert generation_parameters(cfg,'analysis')=={'max_completion_tokens':10000,'thinking':{'type':'enabled'}}


def test_pdf_scope_uses_verified_subsection_instead_of_neighboring_quarter():
    relevant = '中国（上海臨港）\n生産増強\n近隣に新建屋を賃借、工場を拡張（2027年春より稼働開始予定）'
    foreign = 'ベトナム\n新工場の稼働を2027年Q4に前倒し'
    data = sample_fact()
    loan = {**data,'summary_zh':'上海新厂房计划于2027年春启用。','quote':relevant.splitlines()[-1]}
    mixed = {**data,'summary_zh':'上海新厂于2027年第四季度启用。','quote':foreign.splitlines()[-1],
             'date_text':'2027年Q4','date_quote':'2027年Q4'}
    block = {'text':foreign+'\n'+relevant,'hash':'hash','document_id':None,'page':19,'url':'https://example.org/ir.pdf'}
    priorities=[{'kind':'china_action','quote':relevant}]
    results,_=brief.validate_facts({'facts':[loan,mixed]},{data['block_id']:block},priorities,'2026-11-20')
    assert results[0]['scope_quote']==relevant and not results[0]['needs_manual_check']
    assert results[1]['needs_manual_check'] and results[1]['hide_summary']
    scope=brief.facts_for_brief(results,priorities)
    proposal = sample_brief()
    proposal['executive_brief']['window_judgment'] = '建议先核对上海新厂进度。2027年Q4投产。再问设备搬入时点。'
    result, issues=brief.validate_brief(proposal,scope)
    assert result['executive_brief']['window_judgment']=='建议先核对上海新厂进度。再问设备搬入时点。'
    assert len(issues)==1
