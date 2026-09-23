"""Authorship provenance and intent classification for user-role turns.

Every excluded shape here reached companion/Kurallar.md as an "explicit
standing directive" before provenance was checked: pasted terminal output,
relayed agent prompts, test prompts, questions and status reports.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from memory_v1 import provenance as pv
from memory_v1.companion import CompanionManager
from memory_v1.rule_learner import RuleLearner


class TurnProvenanceTests(unittest.TestCase):
    def test_pasted_terminal_output_is_not_authored(self):
        turn = (
            "⏺ Background command \"Rerun harness\" failed with exit code 144\n"
            "Adım artık kanıtlanabilir olanı doğruluyor: publisher bundle'ı yeniden üretti."
        )
        analysis = pv.analyze_user_turn(turn)
        self.assertEqual([pv.PASTED_TOOL_OUTPUT], [b.provenance for b in analysis.blocks])
        self.assertEqual("", analysis.authored_text)

    def test_relay_framing_keeps_the_frame_and_excludes_the_payload(self):
        turn = (
            "Aşağıdaki metni tek parça halinde Claude'a verebilirsin:\n\n"
            "Bundan sonra her commit mesajını İngilizce yaz."
        )
        analysis = pv.analyze_user_turn(turn)
        kinds = [b.provenance for b in analysis.blocks]
        self.assertIn(pv.RELAY_PAYLOAD, kinds)
        self.assertNotIn("İngilizce", analysis.authored_text)

    def test_inline_relay_after_a_question_is_excluded(self):
        turn = "claude çıktısı böyle,bak bakalım sıkıntı var mı?Anlaşıldı. Bundan sonra her şeyi logla."
        analysis = pv.analyze_user_turn(turn)
        self.assertNotIn("logla", analysis.authored_text)

    def test_fenced_block_is_structured_data(self):
        analysis = pv.analyze_user_turn("Şuna bak:\n\n```python\nprint('x')\n```")
        self.assertIn(pv.STRUCTURED_DATA, [b.provenance for b in analysis.blocks])

    def test_test_marker_makes_the_whole_turn_test_data(self):
        turn = "Yeni kalıcı tercihim: Git commit mesajlarında tipi küçük harfle yaz. SB2-CODEX-FRESH-RULE-7c39b1"
        analysis = pv.analyze_user_turn(turn)
        self.assertEqual([pv.TEST_DATA], [b.provenance for b in analysis.blocks])

    def test_quoted_assistant_text_is_detected(self):
        assistant = "Harness artık servis kullanıcısı olarak çalışıyor ve root sahipli dosyalar onarıldı tamamen"
        turn = "Harness artık servis kullanıcısı olarak çalışıyor ve root sahipli dosyalar onarıldı tamamen"
        analysis = pv.analyze_user_turn(turn, prior_assistant_text=assistant)
        self.assertEqual([pv.QUOTED_ASSISTANT], [b.provenance for b in analysis.blocks])

    def test_long_structured_brief_is_a_task_prompt(self):
        brief = "AMAÇ\n" + "\n".join(f"- madde {i} için şunu yap" for i in range(6))
        self.assertTrue(pv.analyze_user_turn(brief).is_task_prompt)

    def test_rendered_transcript_keeps_continuation_lines(self):
        rendered = "USER: ilk satır\nikinci satır\nASSISTANT: cevap\nUSER: son"
        self.assertEqual(
            [("user", "ilk satır\nikinci satır"), ("assistant", "cevap"), ("user", "son")],
            pv.split_rendered_transcript(rendered),
        )


class TestMentionTests(unittest.TestCase):
    def test_mentioning_a_canary_does_not_discard_the_users_other_sentences(self):
        turn = "Canary testini sonra konuşuruz. Bundan sonra commit mesajlarını Türkçe yaz."
        analysis = pv.analyze_user_turn(turn)
        self.assertEqual([pv.AUTHORED], [b.provenance for b in analysis.blocks])
        intents = [pv.classify_sentence(s)[0] for s in pv.split_sentences(analysis.authored_text)]
        self.assertEqual([pv.STATEMENT, pv.DURABLE_DIRECTIVE], intents)

    def test_fixture_marker_still_covers_the_whole_turn(self):
        self.assertEqual("PZ-CODEX-CANARY-20260830-f3a9", pv.find_test_marker(
            "Kalıcı tercihim PZ-CODEX-CANARY-20260830-f3a9: release notlarında her zaman ilk sıraya koy."
        ))
        self.assertEqual("", pv.find_test_marker("Canary koşusunu yarın tekrarlayalım."))

    def test_acceptance_script_phrasings_are_fixtures(self):
        """Recovered from a VPS-only hotfix; without these both turns became rules."""
        self.assertTrue(pv.find_test_marker(
            "test (memory os hafiza yasam dongusu) icin bu degeri sakla."
        ))
        self.assertTrue(pv.find_test_marker(
            "Bu kurulum testinin kontrol degeri: 4821."
        ))
        # A real sentence that merely mentions an installation is not a fixture.
        self.assertEqual("", pv.find_test_marker("Kurulum adımlarını yarın gözden geçirelim."))


class SentenceIntentTests(unittest.TestCase):
    def assertIntent(self, sentence, intent, **kwargs):
        got, evidence = pv.classify_sentence(sentence, **kwargs)
        self.assertEqual(intent, got, f"{sentence!r} -> {got} ({evidence})")

    def test_explicit_standing_directive(self):
        self.assertIntent("Bundan sonra tüm bash scriptlerinde set -euo pipefail kullan.", pv.DURABLE_DIRECTIVE)
        self.assertIntent("Bundan sonra migration dosyalarini daima UTC ile adlandir.", pv.DURABLE_DIRECTIVE)
        self.assertIntent(
            "Kalıcı tercihim: Memory işlemlerinde manual receipt write kullanma.", pv.DURABLE_DIRECTIVE,
        )

    def test_artik_alone_is_a_status_report_not_a_rule(self):
        self.assertIntent("Adım artık kanıtlanabilir olanı doğruluyor.", pv.STATEMENT)
        self.assertIntent("timer artık ENABLED/ACTIVE — saatlik, en fazla iki olay.", pv.STATEMENT)

    def test_questions_are_never_rules(self):
        self.assertIntent("Doctor'da codex_startup_recall artık PASS mı", pv.QUESTION)
        self.assertIntent("events_seen içinde session_end artık var mı", pv.QUESTION)

    def test_reasoning_connector_is_not_an_instruction(self):
        # The live example: a typo'd explanation that read as a directive.
        self.assertIntent("Çünkü anladığım kdarıyla bundan sonra doğal olarak commitleincekl.", pv.STATEMENT)

    def test_first_person_preference_is_a_candidate(self):
        self.assertIntent("Çünkü artık terminal değilde uygulamalara dönmek istiyorum.", pv.PREFERENCE_CANDIDATE)
        self.assertIntent(
            "artık bu projeyi ortak hafızaya aldık ya doğal akışıymış gibi commit edelim.",
            pv.PREFERENCE_CANDIDATE,
        )

    def test_marker_without_recognized_instruction_is_only_a_candidate(self):
        self.assertIntent("Bundan sonra her şey düzenli bir şekilde ilerlesin böylece.", pv.PREFERENCE_CANDIDATE)

    def test_inside_a_task_brief_standing_language_is_downgraded(self):
        self.assertIntent(
            "Bundan sonra olağan işler için onay isteme.", pv.PREFERENCE_CANDIDATE, in_task_prompt=True,
        )
        self.assertIntent("Testleri çalıştır ve raporla.", pv.TASK_INSTRUCTION, in_task_prompt=True)

    def test_artik_in_a_status_clause_does_not_turn_a_request_into_a_preference(self):
        # A Desktop recall test prompt reached the candidates this way.
        self.assertNotIn(
            pv.classify_sentence("Dosya artık yerinde değil; hafızandan cevap ver.")[0],
            {pv.PREFERENCE_CANDIDATE, pv.DURABLE_DIRECTIVE},
        )
        self.assertIntent("Artık n8n kullanma, ajanlarla ilerle.", pv.PREFERENCE_CANDIDATE)

    def test_plain_imperative_is_a_task_instruction(self):
        self.assertIntent("Şu dosyayı düzelt ve testleri çalıştır.", pv.TASK_INSTRUCTION)

    def test_english_modal_mid_sentence_is_not_a_rule(self):
        self.assertIntent("Discovery must never call it", pv.STATEMENT)


class LearnerUsesProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.companion = CompanionManager(Path(self._tmp.name).resolve())
        self.companion.ensure_companion_files()
        self.learner = RuleLearner(self.companion)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_pasted_report_teaches_nothing(self):
        pasted = (
            "Codex şöyle diyor:\n\n"
            "⏺ Ran 3 shell commands\n"
            "Artık yalnız son agent_message değerlendiriliyor. Bundan sonra tüm raporları İngilizce yaz."
        )
        before = self.companion.read_rules()
        learned = self.learner.learn_from_transcript([("user", pasted)], source_session="codex-x")
        self.assertEqual(0, learned)
        self.assertEqual(before, self.companion.read_rules())
        self.assertEqual([], self.companion.read_rule_candidates())

    def test_candidate_is_recorded_not_activated(self):
        self.learner.learn_from_transcript(
            [("user", "Çünkü artık terminal değilde uygulamalara dönmek istiyorum.")], source_session="claude-a",
        )
        self.assertFalse(any("uygulamalara" in r.text for r in self.companion.read_rules()))
        self.assertEqual(["claude-a"], self.companion.read_rule_candidates()[0].sources)

    def test_candidate_promotes_only_after_a_second_session(self):
        text = "Çünkü artık terminal değilde uygulamalara dönmek istiyorum."
        self.learner.learn_from_transcript([("user", text)], source_session="claude-a")
        self.learner.learn_from_transcript([("user", text)], source_session="claude-a")
        self.assertFalse(any("uygulamalara" in r.text for r in self.companion.read_rules()))
        self.learner.learn_from_transcript([("user", text)], source_session="codex-b")
        self.assertTrue(any("uygulamalara" in r.text for r in self.companion.read_rules()))
        self.assertEqual([], self.companion.read_rule_candidates())

    def test_reconcile_keeps_archive_history(self):
        # The old line filter removed every line containing the replaced rule's
        # text, archive entries included, so real history disappeared.
        self.learner.learn_from_transcript(
            [("user", "Bundan sonra testleri unittest framework'ü ile yaz.")], source_session="s1",
        )
        self.learner.learn_from_transcript(
            [("user", "Bundan sonra testleri unittest yerine pytest ile yaz.")], source_session="s2",
        )
        self.learner.learn_from_transcript(
            [("user", "Bundan sonra testleri pytest yerine nose ile yaz.")], source_session="s3",
        )
        content = (self.companion.companion_dir / "Kurallar.md").read_text(encoding="utf-8")
        self.assertIn("**eski_kural:** Bundan sonra testleri unittest framework'ü ile yaz.", content)
        self.assertIn("**eski_kural:** Bundan sonra testleri unittest yerine pytest ile yaz.", content)


if __name__ == "__main__":
    unittest.main()
