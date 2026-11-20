import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from filelock import Timeout

from collectors.target_watch import load_targets, run


class Command(BaseCommand):
    help = '目标日企名单试点；默认列出入口，显式 --online 才读取原站。'
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument('--config', type=Path, default=settings.BASE_DIR/'config'/'target_companies.json')
        parser.add_argument('--company', action='append', help='企业编号，可重复指定')
        parser.add_argument('--output', type=Path, help='独立试点目录')
        parser.add_argument('--online', action='store_true')
        parser.add_argument('--include-references', action='store_true', help='同时核读配置中的历史公告')
        parser.add_argument('--max-requests', type=int, default=12, help='含 robots 与跳转，1–60')
        parser.add_argument('--details-per-entry', type=int, default=2, help='每个列表或订阅源读取 1–5 篇')
        parser.add_argument('--query', help='本试点没有搜索入口；提供此参数会报告不支持')

    def handle(self, **opts):
        try:
            companies = load_targets(opts['config'])
            selected = set(opts['company'] or [c['id'] for c in companies])
            if selected - {c['id'] for c in companies}:
                raise ValueError('名单中没有该企业编号')
            targets = [c for c in companies if c['id'] in selected]
            if opts['query'] is not None:
                raise ValueError('不支持 query 搜索；固定 RSS 与列表仅支持按入口读取')
            if not opts['online']:
                self.stdout.write(json.dumps(targets, ensure_ascii=False, indent=2))
                return
            output = opts['output']
            if not output or output.resolve().is_relative_to(settings.DATA_DIR.resolve()):
                raise ValueError('请用 --output 指定业务数据目录以外的独立目录')
            if not 1 <= opts['max_requests'] <= 60 or not 1 <= opts['details_per_entry'] <= 5:
                raise ValueError('请求预算须在 1–60，每个入口详情上限须在 1–5')
            report = run(targets, output, limit=opts['max_requests'], details=opts['details_per_entry'],
                         references=opts['include_references'], configured=len(companies))
        except (ValueError, OSError, KeyError, TypeError, Timeout) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(report['stats'], ensure_ascii=False))
        self.stdout.write(str(output.resolve()/'runs'/(report['id']+'.json')))
