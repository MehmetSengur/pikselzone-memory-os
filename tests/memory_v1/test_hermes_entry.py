import builtins
import sys
import types
import unittest
from unittest.mock import Mock, patch
from memory_v1 import hermes_entry

class EntrypointTests(unittest.TestCase):
    def test_native_profile_argument_is_not_reintroduced_after_import(self):
        native=types.ModuleType('hermes_cli'); native.main=types.ModuleType('hermes_cli.main')
        guarded=Mock(return_value=0)
        original_import=builtins.__import__
        def importing(name,*args,**kw):
            if name=='hermes_cli.main':
                sys.argv=['hermes',*sys.argv[3:]]  # real import consumes -p and its value
                return native
            return original_import(name,*args,**kw)
        with patch.object(hermes_entry,'install_native'), patch('builtins.__import__',side_effect=importing), patch('memory_v1.hermes_guards.main',guarded), patch.object(sys,'argv',['test']):
            hermes_entry.main(['-p','random-profile','gateway','--help'])
            guarded.assert_called_once_with(['gateway','--help'])
