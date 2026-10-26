"""Run the actual suite and expose test failures as GitHub annotations (no skipped checks)."""
import unittest,sys,os,pathlib
if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8',errors='backslashreplace')
if hasattr(sys.stderr,'reconfigure'):sys.stderr.reconfigure(encoding='utf-8',errors='backslashreplace')
root=pathlib.Path(__file__).resolve().parents[1];sys.path.insert(0,str(root));suite=unittest.defaultTestLoader.discover(str(root/'tests'));result=unittest.TextTestRunner(verbosity=2).run(suite)
def escape(s):return s.replace('%','%25').replace('\r','%0D').replace('\n','%0A')
for test,trace in result.failures+result.errors:print('::error title='+escape(str(test))+'::'+escape(trace[-14000:]),flush=True)
summary=os.environ.get('GITHUB_STEP_SUMMARY')
if summary:
 with open(summary,'a',encoding='utf-8') as f:
  f.write(f'## Tests: {result.testsRun}\nFailures: {len(result.failures)}, errors: {len(result.errors)}\n')
  for test,trace in result.failures+result.errors:f.write('\n### '+str(test)+'\n```text\n'+trace+'\n```\n')
sys.exit(0 if result.wasSuccessful() else 1)
