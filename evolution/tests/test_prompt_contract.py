import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from openevolve.prompt.sampler import PromptSampler
from controller_runner import make_service_config, SYSTEM_CONSTRAINTS, unique_mutation_count
from responses_adapter import AdapterService
from receipts import ReceiptStore


class PromptContractTests(unittest.TestCase):
    def test_framework_prompt_reaches_worker_and_provider_payload(self):
        with tempfile.TemporaryDirectory() as root:
            with ReceiptStore(root).singleton() as store:
                _, run = store.admit()
                class Client:
                    max_retries = 0
                    def __init__(self): self.responses=self;self.calls=[]
                    async def create(self, **kwargs):
                        self.calls.append(kwargs)
                        return SimpleNamespace(id='resp_test',model='gpt-6-luna',service_tier='flex',status='completed',output_text='code',usage=None)
                client=Client()
                with AdapterService(store,run['run_id'],client=client,deadline_seconds=20) as service:
                    config=make_service_config(service)
                    prompt=PromptSampler(config.prompt).build_prompt(current_program=Path('seed.py').read_text(),
                                                                      program_metrics={'combined_score':0.8},
                                                                      language='python',diff_based_evolution=False,island_id=0)
                    self.assertEqual(prompt['system'],SYSTEM_CONSTRAINTS)
                    worker=config.llm.models[0].init_client(None)
                    self.assertEqual(asyncio.run(worker.generate_with_context(prompt['system'],[{'role':'user','content':prompt['user']}])), 'code')
                system=client.calls[0]['input'][0]['content']
                self.assertTrue(system.startswith(SYSTEM_CONSTRAINTS))
                for required in ('<=8 rows','image_bytes<=4194304','ALWAYS include its smallest-age member','no I/O'):
                    self.assertIn(required,system)
                self.assertEqual(len(client.calls),1)

    def test_unique_mutations_counts_code_not_iterations(self):
        programs={'seed':SimpleNamespace(code='seed',parent_id=None),
                  'a':SimpleNamespace(code='mutated',parent_id='seed'),
                  'b':SimpleNamespace(code='mutated',parent_id='seed')}
        self.assertEqual(unique_mutation_count(programs),1)
        programs['c']=SimpleNamespace(code='other',parent_id='seed')
        self.assertEqual(unique_mutation_count(programs),2)

if __name__=='__main__':unittest.main()
