"""Two real collection rounds without touching the business database.
Example: python scripts/validate_sources.py --sources chinapost airchina --output /new/evidence
"""
import argparse,json,subprocess,sys
from pathlib import Path
from datetime import datetime,timezone
ROOT=Path(__file__).resolve().parent.parent

def main():
    p=argparse.ArgumentParser();p.add_argument('--sources',nargs='+',required=True);p.add_argument('--output',required=True);p.add_argument('--pages',type=int,default=10);p.add_argument('--items',type=int,default=12);a=p.parse_args()
    out=Path(a.output).resolve()
    if out.exists():raise SystemExit('请使用新目录，保留前次验证。')
    if not 1<=a.pages<=30 or not 1<=a.items<=30:raise SystemExit('页数和详情上限须在 1–30。')
    out.mkdir(parents=True);(out/'plan.json').write_text(json.dumps({'frozen_at':datetime.now(timezone.utc).isoformat(),'sources':a.sources,'pages':a.pages,'items':a.items,'rounds':2,'selection':'列表顺序与固定关键词；保留匿名、过期、低相关和失败'},ensure_ascii=False,indent=2),encoding='utf-8')
    for key in a.sources:
        seen=[]
        for round in [1,2]:
            seen_path=out/f'{key}-seen.json';seen_path.write_text(json.dumps(seen),encoding='utf-8')
            target=out/f'{key}-{round}.json';args=[sys.executable,'-m','collectors.cli','--source',key,'--output',str(target),'--max-pages',str(a.pages),'--max-items',str(a.items)]
            if round==2:args+=['--seen-file',str(seen_path),'--refresh-items','2']
            with (out/f'{key}-{round}.log').open('w',encoding='utf-8') as log:
                try:subprocess.run(args,cwd=ROOT,stdout=log,stderr=log,timeout=270,check=False)
                except subprocess.TimeoutExpired:(out/f'{key}-{round}-failure.json').write_text(json.dumps({'status':'timeout','items':[],'error':'270-second validation limit'}),encoding='utf-8')
            if target.exists():
                result=json.loads(target.read_text(encoding='utf-8'));seen=list(dict.fromkeys(seen+[x['external_id'] for x in result['items']]))
                print(key,round,result['status'],len(result['items']),flush=True)
            else:print(key,round,'no_result',flush=True)
if __name__=='__main__':main()
