"""Authorship provenance for text that arrives in a user-role transcript turn.

A user turn is not the same thing as the user's own words. People paste an
assistant's report back to another assistant, relay a prompt one agent wrote
for another, and paste terminal output, documents, code and test prompts.
Treating every sentence of such a turn as the user's standing instruction is
how pasted harness reports and research notes came to sit in
``companion/Kurallar.md`` as "Kullanıcının açık kalıcı direktifi".

This module splits a turn into blocks, labels where each block most plausibly
came from, and classifies the intent of the sentences the user actually wrote.
Every decision carries a short evidence string so an audit can see why a
sentence was or was not learned. It is deliberately conservative: when a block
looks pasted, relayed or test-shaped, it is excluded rather than guessed at.
"""
from __future__ import annotations

import dataclasses
import re

# --- block provenance -------------------------------------------------------
AUTHORED = "authored"
QUOTED_ASSISTANT = "quoted-assistant-output"
PASTED_TOOL_OUTPUT = "pasted-tool-output"
RELAY_PAYLOAD = "relay-payload"
STRUCTURED_DATA = "code-or-structured-data"
TEST_DATA = "test-data"

# --- sentence intent --------------------------------------------------------
DURABLE_DIRECTIVE = "durable-directive"
PREFERENCE_CANDIDATE = "preference-candidate"
TASK_INSTRUCTION = "task-instruction"
QUESTION = "question"
STATEMENT = "statement"

# Authored text longer than this, or carrying this many list/heading lines, is
# a task brief: its imperatives describe the job at hand, not standing rules.
TASK_PROMPT_CHARS = 1200
TASK_PROMPT_STRUCTURED_LINES = 4
QUOTE_SHINGLE_WORDS = 6
QUOTE_OVERLAP_THRESHOLD = 0.5

_TOOL_OUTPUT = re.compile(
    r"⏺|⎿|✻|✳|※\s*recap"
    r"|\bRan \d+ (?:shell )?commands?\b"
    r"|^\s*•\s*(?:Ran|Explored|Edited|Read|Searched|Search|Listed|List|Updated|Wrote)\b"
    r"|\bctrl \+ [a-z] to\b"
    r"|\bBackground command\b"
    r"|\bexit code \d+\b"
    r"|^\s*[└│├]\s"
    r"|^\s*❯\s"
    r"|Traceback \(most recent call last\)",
    re.M,
)
_STRUCTURED_LINE = re.compile(
    r"^\s*(?:```|~~~|diff --git |@@ .*@@|\+\+\+ |--- a/|def \w+\(|class \w+[(:]|import \w|from [\w.]+ import"
    r"|#!/|[{\[]\s*\"|\|.*\|.*\||</?[a-zA-Z][\w-]*[ >])"
)
_LIST_OR_HEADING = re.compile(r"^\s*(?:#{1,6}\s|[-*•]\s|\d+[.)]\s|[A-ZÇĞİÖŞÜ0-9][A-ZÇĞİÖŞÜ0-9 /&-]{3,}$)")
_FENCE = re.compile(r"```[\s\S]*?(?:```|$)|~~~[\s\S]*?(?:~~~|$)")
_RELAY_FRAMING = re.compile(
    r"(?i)(?:aşağıdaki\s+(?:metni|mesajı|promptu|prompt['’]?u|talimatı|raporu)"
    r"|\btek parça halinde\b"
    r"|\b(?:claude|opus|codex|hermes|astra|gpt)[\w'’]*\s+(?:ver|gönder|yapıştır|ilet)\w*"
    r"|\b(?:claude|opus|codex|hermes|astra)[\w'’]*\s+(?:çıktısı|cevabı|raporu|yanıtı|mesajı)\b"
    r"|\bşöyle diyor\b|\bböyle diyor\b|\bdiyor ki\b|\bbak bakalım\b"
    r"|\bhere is (?:the|its|their) (?:output|report|reply|answer)\b)"
)
# Fixture-shaped markers identify a whole turn as a test prompt: planted IDs and
# the phrasing of the acceptance scripts. A bare mention of "canary" does not --
# people discuss tests in real work, and reports pasted about tests are already
# excluded by their own provenance.
_TEST_FIXTURE_MARKER = re.compile(
    r"(?i)(?:PZ-HARNESS-TESTVALUE|\bPZ-(?:CH|HC|CW)-\d{8}|\bPZ-[A-Z]+-CANARY-[\w-]+"
    r"|\bSB2-[A-Z0-9]+(?:-[A-Z0-9]+)*-[0-9a-f]{6}\b|\btest notu\b|\btest tercihim\b"
    r"|hafızana kaydettiğini doğrula|tercihi kısa biçimde onayla)"
)
# A sentence that mentions a test artifact is about the test, never a rule.
_TEST_MENTION = re.compile(r"(?i)(?:\bcanary\b|continuity harness|PZ-M4-CANARY)")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+|(?<=[.!?])(?=[A-ZÇĞİÖŞÜ])|\n+")
_SENTENCE_END = re.compile(r"[.!?…:;\n]")

# The intent patterns below run on ASCII-folded, lower-cased text so that a
# user typing without Turkish characters ("adlandir") is read the same way.
_TR_FOLD = str.maketrans("ıİşŞğĞçÇöÖüÜâÂîÎûÛ", "iIsSgGcCoOuUaAiIuU")

_QUESTION = re.compile(r"(?:\?\s*$|\b(?:mi|mu)(?:sin|sun|yiz|yuz|dir|dur)?\s*[.!…]*\s*$)")
_STRONG_PERSISTENCE = re.compile(
    r"\bbundan sonra\b|\bsundan sonra\b|\bfrom now on\b|\bkalici(?:\s+olarak|\s+tercih\w*|\s+kural\w*)"
    r"|\bher zaman\b|\bdaima\b|\bhicbir zaman\b|\basla\b|\bbir daha\b|\bdon'?t ever\b"
)
_EN_LEADING_ALWAYS = re.compile(r"^\s*(?:please\s+)?(?:always|never)\s+\w")
_TEMPORAL_ARTIK = re.compile(r"\bartik\b")
_DIRECTIVE_WORD = re.compile(
    r"\b(?:yap|yapma|yapin|yapmayin|kullan|kullanma|kullanin|uygula|yaz|yazma|calistir|calistirma"
    r"|et|etme|koy|koyma|goster|gosterme|ekle|ekleme|getir|tut|kil|dokunma|silme|baslat|baslatma|sor|sorma"
    r"|belirt|al|alma|ver|verme|olustur|birak|kapat|ac|gonder|gonderme|ozetle|raporla|adlandir|isimlendir"
    r"|formatla|commitle|pushla|dogrula|onayla|bekle|degistirme|kaydet|sakla|sil|iste|isteme|isteyin|istemeyin"
    r"|edelim|yapalim|kullanalim|olsun|olmasin|olmali|olmamali|zorunlu|zorunludur|gerekir|gerekmez|lazim)\b"
)
_FIRST_PERSON_PREF = re.compile(
    r"\b(?:tercihim|tercih ederim|tercih ediyorum|istiyorum|istemiyorum|isterim|istemem"
    r"|bence|i prefer|i want|i don'?t want)\b"
)
_REPORT_ENDING = re.compile(
    r"(?:ildi|uldu|lendi|landi|di|du|ti|tu|mis|mus|ecek|acak|ecektir|acaktir|iyor|uyor"
    r"|mektedir|maktadir|dir|dur|tir|tur)\s*[.!…]*\s*$"
)
# A sentence explaining or reasoning ("çünkü ...", "anladığım kadarıyla ...")
# carries the user's understanding, not an instruction to follow.
_REASONING_LEAD = re.compile(r"^(?:cunku|zira|because|anladigim kadariyla|sanirim|galiba|aslinda)\b")
_CORRECTION = re.compile(r"bunu\s+boyle\s+yapma|yanlis[,:\s]+(?:dogrusu|bunun yerine)|oyle degil[,:\s]")


@dataclasses.dataclass(frozen=True)
class Block:
    provenance: str
    text: str
    evidence: str = ""


@dataclasses.dataclass(frozen=True)
class TurnAnalysis:
    blocks: tuple[Block, ...]
    is_task_prompt: bool
    test_marker: str = ""
    relay_marker: str = ""

    @property
    def authored_text(self) -> str:
        return "\n\n".join(b.text for b in self.blocks if b.provenance == AUTHORED)


def split_rendered_transcript(rendered: str) -> list[tuple[str, str]]:
    """Split a ``USER: ...`` / ``ASSISTANT: ...`` rendering into whole turns.

    Continuation lines stay with their turn. Reading only lines that start with
    a role prefix kept the first line of a pasted block and silently dropped
    the rest, which is the opposite of what provenance checks need.
    """
    turns: list[tuple[str, str]] = []
    role: str | None = None
    buf: list[str] = []

    def flush() -> None:
        if role is not None:
            turns.append((role, "\n".join(buf)))

    for line in rendered.splitlines():
        if line.startswith("USER: "):
            flush()
            role, buf = "user", [line[6:]]
        elif line.startswith("ASSISTANT: "):
            flush()
            role, buf = "assistant", [line[11:]]
        elif role is not None:
            buf.append(line)
    flush()
    return turns


def split_sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE_SPLIT.split(text) if part and part.strip()]


def _shingles(text: str, size: int = QUOTE_SHINGLE_WORDS) -> set[str]:
    words = re.findall(r"\w+", text.lower())
    return {" ".join(words[i:i + size]) for i in range(max(0, len(words) - size + 1))}


def _chunks(text: str):
    """Yield (chunk, is_fenced) in order: fenced blocks whole, prose by paragraph."""
    pos = 0
    for match in _FENCE.finditer(text):
        for para in re.split(r"\n\s*\n", text[pos:match.start()]):
            if para.strip():
                yield para.strip("\n"), False
        yield match.group(0), True
        pos = match.end()
    for para in re.split(r"\n\s*\n", text[pos:]):
        if para.strip():
            yield para.strip("\n"), False


def _classify_chunk(chunk: str, prior_shingles: set[str]) -> Block:
    tool = _TOOL_OUTPUT.search(chunk)
    if tool:
        return Block(PASTED_TOOL_OUTPUT, chunk, f"tool-output-marker:{tool.group(0).strip()[:40]}")
    lines = [line for line in chunk.splitlines() if line.strip()]
    if lines:
        structured = sum(1 for line in lines if _STRUCTURED_LINE.match(line))
        if structured >= max(2, len(lines) // 2):
            return Block(STRUCTURED_DATA, chunk, f"structured-lines:{structured}/{len(lines)}")
    if prior_shingles:
        shingles = _shingles(chunk)
        if len(shingles) >= 3:
            overlap = len(shingles & prior_shingles) / len(shingles)
            if overlap >= QUOTE_OVERLAP_THRESHOLD:
                return Block(QUOTED_ASSISTANT, chunk, f"assistant-overlap:{overlap:.2f}")
    return Block(AUTHORED, chunk)


def analyze_user_turn(text: str, *, prior_assistant_text: str = "") -> TurnAnalysis:
    """Label each block of a user turn with where it most plausibly came from."""
    if not text or not text.strip():
        return TurnAnalysis((), False)

    test = _TEST_FIXTURE_MARKER.search(text)
    if test:
        # A test prompt is a test end to end; its "tercihim" is the fixture.
        return TurnAnalysis(
            (Block(TEST_DATA, text, f"test-marker:{test.group(0)}"),), False,
            test_marker=test.group(0),
        )

    prior_shingles = _shingles(prior_assistant_text) if prior_assistant_text else set()
    blocks: list[Block] = []
    relay_marker = ""
    for chunk, fenced in _chunks(text):
        if relay_marker:
            blocks.append(Block(RELAY_PAYLOAD, chunk, f"after-relay-framing:{relay_marker}"))
            continue
        if fenced:
            blocks.append(Block(STRUCTURED_DATA, chunk, "fenced-block"))
            continue
        framing = _RELAY_FRAMING.search(chunk)
        if framing:
            relay_marker = framing.group(0)
            end_match = _SENTENCE_END.search(chunk, framing.end())
            cut = end_match.end() if end_match else len(chunk)
            head, tail = chunk[:cut], chunk[cut:]
            if head.strip():
                blocks.append(_classify_chunk(head, prior_shingles))
            if tail.strip():
                blocks.append(Block(RELAY_PAYLOAD, tail.strip(), f"after-relay-framing:{relay_marker}"))
            continue
        blocks.append(_classify_chunk(chunk, prior_shingles))

    authored = "\n\n".join(b.text for b in blocks if b.provenance == AUTHORED)
    authored_lines = [line for line in authored.splitlines() if line.strip()]
    structured_lines = sum(1 for line in authored_lines if _LIST_OR_HEADING.match(line))
    is_task_prompt = (
        len(authored) > TASK_PROMPT_CHARS or structured_lines >= TASK_PROMPT_STRUCTURED_LINES
    )
    return TurnAnalysis(tuple(blocks), is_task_prompt, relay_marker=relay_marker)


def find_test_marker(text: str) -> str:
    """The first test/canary marker in ``text``, or "" when there is none."""
    match = _TEST_FIXTURE_MARKER.search(text or "")
    return match.group(0) if match else ""


def fold_text(text: str) -> str:
    """Whitespace-normalized, ASCII-folded, lower-cased text for comparisons."""
    return re.sub(r"\s+", " ", text).strip().translate(_TR_FOLD).lower()


def classify_sentence(sentence: str, *, in_task_prompt: bool = False) -> tuple[str, str]:
    """Classify one sentence the user wrote. Returns (intent, evidence).

    A standing rule needs an explicit persistence marker ("bundan sonra",
    "her zaman", "asla", "kalıcı tercihim", a leading "always"/"never") on a
    sentence that recognizably instructs. "artık" alone is a temporal adverb --
    "X artık doğrulanıyor" is a status report -- so on its own it can at most
    suggest a preference worth watching. A marker without a recognizable
    instruction (typos, unusual verbs) is also only a candidate: acting on it
    needs a second, independent sighting.
    """
    s = sentence.strip().lstrip("-*•> ").strip()
    if not s:
        return STATEMENT, "empty"
    folded = s.translate(_TR_FOLD).lower()
    if _QUESTION.search(folded):
        return QUESTION, "question-form"
    mention = _TEST_MENTION.search(s)
    if mention:
        return STATEMENT, f"test-artifact-mention:{mention.group(0)}"

    strong = _STRONG_PERSISTENCE.search(folded) or _EN_LEADING_ALWAYS.search(folded)
    directive = _DIRECTIVE_WORD.search(folded)
    preference = _FIRST_PERSON_PREF.search(folded)
    reasoning = _REASONING_LEAD.search(folded)

    if strong and not reasoning:
        marker = strong.group(0).strip()
        instructs = directive or preference or _EN_LEADING_ALWAYS.search(folded)
        if instructs:
            if in_task_prompt:
                return PREFERENCE_CANDIDATE, f"persistence:{marker};inside-task-brief"
            return DURABLE_DIRECTIVE, f"persistence:{marker};directive:{instructs.group(0).strip()}"
        if _REPORT_ENDING.search(folded):
            return STATEMENT, f"persistence-marker-in-report:{marker}"
        if in_task_prompt:
            return TASK_INSTRUCTION, f"persistence-without-instruction:{marker};inside-task-brief"
        return PREFERENCE_CANDIDATE, f"persistence-without-recognized-instruction:{marker}"

    if in_task_prompt:
        if directive or preference:
            return TASK_INSTRUCTION, "imperative-inside-task-brief"
        return STATEMENT, "descriptive"
    if preference:
        return PREFERENCE_CANDIDATE, f"first-person-preference:{preference.group(0)}"
    if reasoning:
        return STATEMENT, f"reasoning-connector:{reasoning.group(0)}"
    if _TEMPORAL_ARTIK.search(folded) and directive:
        return PREFERENCE_CANDIDATE, f"temporal-shift-with-directive:{directive.group(0)}"
    if _CORRECTION.search(folded):
        return PREFERENCE_CANDIDATE, "correction"
    if directive:
        return TASK_INSTRUCTION, f"imperative-without-persistence:{directive.group(0)}"
    return STATEMENT, "descriptive"
