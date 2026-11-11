import copy
import unittest
from decimal import Decimal

from eth_account import Account
from experiments.start_demo_job import LOCAL_CLIENT_KEY, local_signed_body
from gpuchain_core.payout_terms import validate_terms


class LocalDemoSignedJob(unittest.TestCase):
    def test_signed_terms_match_demo_job_and_domain(self):
        info = {"testnet": False, "chain": {"chain_id": 31337, "escrow": "0x" + "1" * 40},
                "providers": {"a": "provider-a", "b": "provider-b"},
                "new_job_body": {"workload": {"template": "synthetic-linear/v1"}, "policy": {"metric": "accuracy"}, "deadline_seconds": 1800}}
        before = copy.deepcopy(info)
        body = local_signed_body(info, "5", LOCAL_CLIENT_KEY)
        self.assertEqual(info, before)
        validate_terms(job_id=body["job_id"], client=Account.from_key(LOCAL_CLIENT_KEY).address,
                       binding=body["job_binding"], spec=body["workload"], policy=body["policy"],
                       budget=Decimal("5"), chain_id=31337, contract=info["chain"]["escrow"])
        body["workload"]["provider_terms"][0]["price_per_step"] = "0.9"
        with self.assertRaises(ValueError):
            validate_terms(job_id=body["job_id"], client=body["client_address"], binding=body["job_binding"],
                           spec=body["workload"], policy=body["policy"], budget=Decimal("5"),
                           chain_id=31337, contract=info["chain"]["escrow"])

    def test_refuses_nonlocal_chain(self):
        with self.assertRaises(ValueError):
            local_signed_body({"testnet": True, "chain": {"chain_id": 84532}}, "5", LOCAL_CLIENT_KEY)


if __name__ == "__main__":
    unittest.main()
