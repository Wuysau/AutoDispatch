import unittest
from autodispatch.host import HostApi, process_requests

class WindowCommands(unittest.TestCase):
    def test_order_dedup_and_expiry(self):
        api=HostApi('unused'); calls=[]; api.apply=lambda req: calls.append(req['action'])
        data={'commands':[{'seq':2,'at':100,'action':'hide'}, {'seq':1,'at':100,'action':'size'}, {'seq':3,'at':0,'action':'show'}]}
        process_requests(api,data,101); process_requests(api,data,101)
        self.assertEqual(calls,['size','hide'])
        self.assertTrue(api.results['2']['ok']); self.assertFalse(api.results['3']['ok'])

    def test_failed_command_receipt_does_not_claim_success(self):
        api=HostApi('unused')
        process_requests(api,{'commands':[{'seq':1,'at':100,'target':'widget','action':'hide'}]},100)
        self.assertFalse(api.results['1']['ok'])

if __name__=='__main__': unittest.main()
