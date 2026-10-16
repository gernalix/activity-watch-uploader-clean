import copy, os, re, unittest
from pathlib import Path
WORKFLOW=Path(os.environ.get('WORKFLOW_PATH',str(Path(__file__).resolve().parents[1]/'.github/workflows/ci.yml'))).read_text()
def expression(key):
    return next(line.split('${{',1)[1].rsplit('}}',1)[0].strip() for line in WORKFLOW.splitlines() if line.strip().startswith(key+':'))
def evaluate(s, context):
    def value(match):
        v=context
        for k in match.group().split('.'):v=v.get(k,'') if isinstance(v,dict) else ''
        return repr(v)
    s=re.sub(r'\b(?:github|vars|inputs|runner)\.[A-Za-z0-9_.]+',value,s)
    s=re.sub(r'!(?!=)',' not ',s).replace('&&',' and ').replace('||',' or ')
    return eval(s,{'__builtins__':{},'startsWith':lambda s,p:s.startswith(p)},{})
class DispatchRoutingTests(unittest.TestCase):
    def setUp(self):
        repo=re.search(r"github.repository == '([^']+)'",WORKFLOW)[1]
        prefix=re.search(r"startsWith\(vars.CI_JIT_LABEL, '([^']+)'",WORKFLOW)[1]
        self.c={'github':{'repository':repo,'event_name':'workflow_dispatch','sha':'reviewed','event':{'repository':{'private':True}}},'inputs':{'mode':'private','expected_sha':'reviewed'},'vars':{'CI_JIT_HEAD_SHA':'reviewed','CI_JIT_LABEL':prefix+'synthetic'}}
    def test_private_exact_dispatch_uses_reserved_jit(self):
        self.assertTrue(evaluate(expression('if'),self.c));self.assertEqual(evaluate(expression('runs-on'),self.c),self.c['vars']['CI_JIT_LABEL'])
    def test_wrong_expected_sha_is_skipped(self):
        self.c['inputs']['expected_sha']='other';self.assertFalse(evaluate(expression('if'),self.c))
    def test_each_missing_private_override_skips_instead_of_hosted_spend(self):
        for k in self.c['vars']:
            c=copy.deepcopy(self.c);c['vars'][k]='';self.assertFalse(evaluate(expression('if'),c))
    def test_public_mode_in_private_repo_is_skipped(self):
        self.c['inputs']['mode']='public';self.assertFalse(evaluate(expression('if'),self.c))
    def test_public_dispatch_routes_hosted_without_jit(self):
        self.c['github']['event']['repository']['private']=False;self.c['inputs']['mode']='public';self.c['vars']={}
        self.assertTrue(evaluate(expression('if'),self.c));self.assertEqual(evaluate(expression('runs-on'),self.c),'ubuntu-latest')
    def test_wrong_private_repo_is_skipped(self):
        self.c['github']['repository']='foreign/repository';self.assertFalse(evaluate(expression('if'),self.c))
    def test_checkout_always_pins_actual_workflow_sha(self):
        self.assertEqual(evaluate(expression('ref'),self.c),'reviewed')
    def test_private_push_without_overrides_is_skipped(self):
        self.c['github']['event_name']='push';self.c['vars']={};self.assertFalse(evaluate(expression('if'),self.c))
if __name__=='__main__':unittest.main()
