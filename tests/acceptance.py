"""Однокомандная приёмка с отчётом и ненулевым кодом при ошибке."""
import argparse
import json
from pathlib import Path
import sys
import unittest
from datetime import datetime, timezone
from . import test_acceptance as scenarios


def main():
    p=argparse.ArgumentParser();p.add_argument('--rounds',type=int,default=5);p.add_argument('--buyers',type=int,default=20);p.add_argument('--stock',type=int,default=1)
    p.add_argument('--reserve-seconds',type=int,default=120)
    p.add_argument('--report',default='reports/acceptance.json');args=p.parse_args()
    if args.reserve_seconds<1 or args.rounds<5 or not 1<=args.buyers<=200 or not 1<=args.stock<=10000:p.error('rounds >= 5; buyers 1..200; stock 1..10000')
    scenarios.RESERVE_SECONDS=args.reserve_seconds;scenarios.ROUNDS=args.rounds;scenarios.CUSTOM_BUYERS=args.buyers;scenarios.CUSTOM_STOCK=args.stock
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(scenarios.Acceptance)
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    report=Path(args.report);report.parent.mkdir(parents=True,exist_ok=True)
    data=dict(generated_at=datetime.now(timezone.utc).isoformat(),command='python -m tests.acceptance '+' '.join(sys.argv[1:]),
              tests=result.testsRun,passed=result.testsRun-len(result.errors)-len(result.failures),errors=[(str(t),e) for t,e in result.errors],
              failures=[(str(t),e) for t,e in result.failures],scenarios=scenarios.EVIDENCE)
    report.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    lines=['# Фактическая приёмка',f'\nUTC: {data["generated_at"]}',f'\nКоманда: `{data["command"]}`',f'\nПроверок: {data["tests"]}; успешно: {data["passed"]}.',
           '\nИзолированные временные SQLite, два настоящих HTTP-сервера, отдельные соединения, подписанные учебные личности. Сброс — удаление только созданных временных каталогов.']
    for r in scenarios.EVIDENCE:lines+=['\n## '+r['scenario'],'\nОжидалось: '+json.dumps(r['expected'],ensure_ascii=False),'\nПолучено: '+json.dumps(r['actual'],ensure_ascii=False)]
    for title,entries in [('Ошибки',data['errors']),('Провалы',data['failures'])]:
        for name,error in entries:lines+=['\n## '+title+': '+name,'\n```text\n'+error+'\n```']
    report.with_suffix('.md').write_text('\n'.join(lines),encoding='utf-8')
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__=='__main__':main()
