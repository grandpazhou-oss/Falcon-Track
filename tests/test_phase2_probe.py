from hashlib import sha256
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from radar.models import AIConfig, KeywordCandidate, ModelCall

KEY='24858d1b-63df-4e02-a221-77bacf9909c6'


class ModelProbeTests(TestCase):
    def test_requires_explicit_model_permission(self):
        with patch('radar.management.commands.phase2_mimo_probe.extract') as extract:
            with self.assertRaises(CommandError):
                call_command('phase2_mimo_probe',candidate_id=1,max_calls=1,daily_limit=3,request_key=KEY,stdout=StringIO())
        extract.assert_not_called()

    def test_same_request_never_replays_and_preserves_manual_verification(self):
        with TemporaryDirectory() as tmp, override_settings(DATA_DIR=Path(tmp)):
            raw=Path(tmp,'raw','research','evidence.html');raw.parent.mkdir(parents=True)
            body=b'public evidence';raw.write_bytes(body)
            AIConfig.objects.create(base_url='https://api.xiaomimimo.com/v1',model='mimo-v2.6-flash',daily_limit=4)
            candidate=KeywordCandidate.objects.create(canonical_url='https://example.org/a',title='title',
                provider='test',query_id='q',text='正文',raw_path='raw/research/evidence.html',
                result={'source_sha256':sha256(body).hexdigest(),'verification':{'status':'verified_research_signal'}})
            def fake_extract(obj,cfg,limit):
                call=ModelCall.objects.create(status='succeeded',model=cfg.model,usage={'total_tokens':100})
                obj._keyword_call_id=call.pk
                return {'target_period':'2027'}
            with patch('radar.management.commands.phase2_mimo_probe.extract',side_effect=fake_extract) as mock:
                opts=dict(candidate_id=candidate.pk,allow_model=True,max_calls=2,daily_limit=4,request_key=KEY)
                call_command('phase2_mimo_probe',stdout=StringIO(),**opts)
                call_command('phase2_mimo_probe',stdout=StringIO(),**opts)
            candidate.refresh_from_db()
            self.assertEqual(mock.call_count,2)
            self.assertEqual(len(candidate.result['phase2_model_history']),2)
            self.assertEqual(candidate.result['verification']['status'],'verified_research_signal')
            self.assertEqual(ModelCall.objects.count(),2)

    def test_failed_request_does_not_replay_on_same_key(self):
        with TemporaryDirectory() as tmp, override_settings(DATA_DIR=Path(tmp)):
            raw=Path(tmp,'raw','research','evidence.html');raw.parent.mkdir(parents=True)
            body=b'public evidence';raw.write_bytes(body)
            AIConfig.objects.create(base_url='https://api.xiaomimimo.com/v1',model='mimo-v2.6-flash')
            candidate=KeywordCandidate.objects.create(canonical_url='https://example.org/a',title='title',
                provider='test',query_id='q',text='正文',raw_path='raw/research/evidence.html',
                result={'source_sha256':sha256(body).hexdigest()})
            opts=dict(candidate_id=candidate.pk,allow_model=True,max_calls=2,daily_limit=4,request_key=KEY)
            with patch('radar.management.commands.phase2_mimo_probe.extract',side_effect=RuntimeError('failure')) as mock:
                call_command('phase2_mimo_probe',stdout=StringIO(),**opts)
                call_command('phase2_mimo_probe',stdout=StringIO(),**opts)
            candidate.refresh_from_db()
            self.assertEqual(mock.call_count,1)
            self.assertEqual(candidate.result['phase2_model_history'][0]['status'],'failed')
