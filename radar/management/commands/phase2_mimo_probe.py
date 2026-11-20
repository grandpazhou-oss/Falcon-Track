"""Explicit, idempotent paid-model probe against an already saved candidate."""
import hashlib
import json
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from filelock import FileLock, Timeout

from radar.ai_config import config
from radar.keyword_extraction import KEYWORD_PROMPT_VERSION, extract
from radar.management.commands.backup_data import raw_source_file
from radar.models import KeywordCandidate, ModelCall


class Command(BaseCommand):
    help = 'Run a bounded MiMo probe on saved evidence only; never automatic.'

    def add_arguments(self, parser):
        parser.add_argument('--candidate-id', type=int, required=True)
        parser.add_argument('--allow-model', action='store_true')
        parser.add_argument('--max-calls', type=int, default=0)
        parser.add_argument('--daily-limit', type=int, default=0)
        parser.add_argument('--request-key', required=True)

    def handle(self, *args, **o):
        if not o['allow_model'] or not 1 <= o['max_calls'] <= 3 or not 1 <= o['daily_limit'] <= 10:
            raise CommandError('需显式允许模型，并设置本次 1–3 次及每日 1–10 次上限。')
        try:
            uuid.UUID(o['request_key'])
        except (TypeError, ValueError):
            raise CommandError('请求键必须为 UUID。')
        cfg = config()
        if cfg.model != 'mimo-v2.6-flash' or urlsplit(cfg.base_url).hostname not in {'api.xiaomimimo.com','token-plan-cn.xiaomimimo.com'}:
            raise CommandError('未配置官方 MiMo V2.6 Flash 端点。')
        try:
            with FileLock(str(settings.DATA_DIR / 'phase2-model-probe.lock'), timeout=0):
                candidate = KeywordCandidate.objects.get(pk=o['candidate_id'])
                if not candidate.text or not candidate.raw_path:
                    raise CommandError('候选缺少已保存的原始正文。')
                raw = raw_source_file(candidate.raw_path).read_bytes()
                source_sha = hashlib.sha256(raw).hexdigest()
                if source_sha != candidate.result.get('source_sha256'):
                    raise CommandError('原文哈希不一致，未调用模型。')
                result = dict(candidate.result)
                history = list(result.get('phase2_model_history', []))
                if any(item.get('request_key') == o['request_key'] for item in history):
                    self.stdout.write(json.dumps({'status':'already_recorded','request_key':o['request_key']},ensure_ascii=False))
                    return
                for sequence in range(1, o['max_calls'] + 1):
                    item = {'request_key':o['request_key'], 'sequence':sequence,
                            'prompt_version':KEYWORD_PROMPT_VERSION, 'source_sha256':source_sha,
                            'started_at':timezone.now().isoformat(), 'status':'started'}
                    history.append(item)
                    result['phase2_model_history'] = history
                    candidate.result = result
                    candidate.save(update_fields=['result'])
                    if hasattr(candidate, '_keyword_call_id'):
                        del candidate._keyword_call_id
                    try:
                        value = extract(candidate, cfg, o['daily_limit'])
                        call = ModelCall.objects.get(pk=candidate._keyword_call_id)
                        item.update({'status':'succeeded', 'model_call_id':call.pk, 'model':call.model,
                                     'usage':call.usage, 'cost':str(call.cost) if call.cost is not None else None,
                                     'output':value})
                    except Exception as exc:
                        call_id = getattr(candidate, '_keyword_call_id', None)
                        call = ModelCall.objects.filter(pk=call_id).first() if call_id else None
                        item.update({'status':'failed','error':type(exc).__name__,
                                     'model_call_id':call_id,'usage':call.usage if call else None,
                                     'cost':str(call.cost) if call and call.cost is not None else None})
                    item['finished_at'] = timezone.now().isoformat()
                    candidate.result = result
                    candidate.save(update_fields=['result'])
                    if item['status'] != 'succeeded':
                        break
                self.stdout.write(json.dumps({'request_key':o['request_key'],'candidate_id':candidate.pk,
                    'calls':[x for x in history if x.get('request_key')==o['request_key']]},ensure_ascii=False))
        except KeywordCandidate.DoesNotExist as exc:
            raise CommandError('候选不存在。') from exc
        except Timeout as exc:
            raise CommandError('另一个模型测试正在运行。') from exc
