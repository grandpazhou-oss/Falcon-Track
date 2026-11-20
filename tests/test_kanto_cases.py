import pytest
from collectors.kanto_cases import evidence

def test_size_evidence_must_match_scope():
    doc = {'sha256':'d'*64,'text':'従業員数 26名（2026年8月現在）'}
    assert evidence(doc,'従業員数 26名（2026年8月現在）')['document_hash']==doc['sha256']
    with pytest.raises(ValueError):
        evidence(doc,'従業員数 36名（2026年8月現在）')
