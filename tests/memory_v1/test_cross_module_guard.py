import os
import threading
import unittest
from memory_v1.internal_calls import internal_call, is_internal

class CrossModuleGuardTests(unittest.TestCase):
    def test_an_unrelated_native_thread_is_not_suppressed(self):
        seen = []
        os.environ.pop('PZ_MEMORY_INTERNAL_CALL',None)
        with internal_call():
            self.assertTrue(is_internal())
            worker = threading.Thread(target=lambda: seen.append(is_internal()))
            worker.start(); worker.join(2)
        self.assertEqual(seen,[False])
        self.assertFalse(is_internal())
        self.assertNotIn('PZ_MEMORY_INTERNAL_CALL',os.environ)
    def test_nested_calls_restore_external_guard(self):
        os.environ['PZ_MEMORY_INTERNAL_CALL']='1'
        try:
            with internal_call():
                with internal_call(): self.assertTrue(is_internal())
            self.assertTrue(is_internal())
        finally: os.environ.pop('PZ_MEMORY_INTERNAL_CALL',None)
