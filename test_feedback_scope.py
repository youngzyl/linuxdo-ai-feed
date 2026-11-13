"""Explicitly demonstrate existing feedback journal durability limitation."""
import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from store import Store
class FeedbackScopeTests(unittest.TestCase):
 def test_journal_failure_is_swallowed_and_memory_changes(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp);s=Store(p/'state.json',p/'failure.jsonl');entry={'id':1,'vote':'keep','at':'2026-01-01T00:00:00Z'}
   original=Path.open
   def open_or_fail(path,*args,**kwargs):
    if path==s.feedback_log:raise OSError('synthetic journal failure')
    return original(path,*args,**kwargs)
   with patch('pathlib.Path.open',open_or_fail):result=s.add_feedback(entry)
   self.assertEqual(result,[entry]);self.assertEqual(s.feedback_rows(),[entry]);self.assertFalse(s.feedback_log.exists())
if __name__=='__main__':unittest.main()
