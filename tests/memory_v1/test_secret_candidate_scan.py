"""The vault secret scan must not fire on a credential's name.

After a backlog drain on 2026-09-23 the doctor reported
`secret_candidates: 2` as a FAIL. Both were systemd unit lines quoted in a
transcript:

    LoadCredential=supabase-service-role:/etc/sengur-seo/supabase-service-role

which names a credential and its path and carries no value. The real values in
those same artefacts were already `[REDACTED_SECRET]`, so nothing had leaked.

The cause is that `doctor.VALUE_SHAPED_SECRET` lacks the word boundary that
`core.SECRET_ASSIGNMENT` has, so `credential` matched inside `LoadCredential`.
A detector that fails on every transcript discussing systemd credential wiring
trains its reader to ignore it.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from memory_v1.core import SECRET_ASSIGNMENT
from memory_v1.doctor import VALUE_SHAPED_SECRET

_SYSTEMD_LINES = (
    "LoadCredential=supabase-service-role:/etc/sengur-seo/supabase-service-role",
    "LoadCredential=source-inspector-token:/etc/sengur-seo/source-inspector-token",
    "LoadCredential=shopify-client-id:/etc/sengur-seo/shopify-client-id",
    "Servis bunu `LoadCredential=supabase-service-role` ile okuyor.",
    "SetCredential=recovery-cutover:/etc/sengur-seo/recovery-cutover.json",
)

_REAL_SHAPES = (
    "api_key: sk-proj-1234567890abcdefghij",
    "access_token=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345",
    'client_secret: "aZ09._-+=aZ09._-+="',
    "password = hunter2hunter2hunter2",
    "credential: AKIAIOSFODNN7EXAMPLEKEY",
)


class VaultSecretScanTest(unittest.TestCase):
    def test_a_credential_name_is_not_a_secret(self) -> None:
        for line in _SYSTEMD_LINES:
            self.assertIsNone(VALUE_SHAPED_SECRET.search(line), line)

    def test_a_value_shaped_assignment_is_still_caught(self) -> None:
        for line in _REAL_SHAPES:
            self.assertIsNotNone(VALUE_SHAPED_SECRET.search(line), line)

    def test_the_two_patterns_agree_on_these_cases(self) -> None:
        """core redacts and doctor reports; they must not disagree."""
        for line in _SYSTEMD_LINES:
            self.assertIsNone(SECRET_ASSIGNMENT.search(line), line)
        for line in _REAL_SHAPES:
            self.assertIsNotNone(SECRET_ASSIGNMENT.search(line), line)


if __name__ == "__main__":
    unittest.main()
