"""The verdict axioms a trustworthy benchmark must satisfy, and the finding
taxonomy built on them. Every finding names the axiom it falsifies."""
from dataclasses import dataclass, field

PROPERTIES = {
    "P1_SOUND_ACCEPT":  "The reference solution's outputs receive PASS.",
    "P2_REJECT_GARBAGE": "Outputs that differ from the reference receive FAIL "
                         "(checked per-case, with coincidence exclusion).",
    "P3_DETERMINISM":   "Identical (cases, outputs, environment) always yield "
                        "the identical verdict.",
    "P4_ENV_INVARIANCE": "The verdict does not depend on ambient environment "
                         "(hash seed, timezone, locale).",
    "P5_CASE_ISOLATION": "A case's verdict is a function of that case alone — "
                         "not of which other cases ran before it.",
    "P6_NON_GAMEABLE":  "No solution ignorant of the task semantics (leak "
                        "readers, constants, echo) can achieve PASS.",
    "P7_DECLARED_EQUIVALENCE": "Outputs equivalent under the benchmark's own "
                               "declared semantics receive equal verdicts.",
    "P8_DISCRIMINATION": "Single-edit mutants of the reference that change "
                         "behavior on reachable inputs should FAIL.",
}

SEVERITY_ORDER = ["CRITICAL", "HIGH", "MEDIUM", "INFO"]

# finding class -> (property, severity)
CLASSES = {
    "ANSWER_LEAKAGE":            ("P6_NON_GAMEABLE", "CRITICAL"),
    "ACCEPTS_TRIVIAL":           ("P6_NON_GAMEABLE", "CRITICAL"),
    "UNSOUND_ACCEPT":            ("P2_REJECT_GARBAGE", "CRITICAL"),
    "FALSE_FAIL":                ("P7_DECLARED_EQUIVALENCE", "HIGH"),
    "REFERENCE_REJECTED":        ("P1_SOUND_ACCEPT", "HIGH"),
    "NONDETERMINISTIC_VERIFIER": ("P3_DETERMINISM", "HIGH"),
    "STATEFUL_VERIFIER":         ("P5_CASE_ISOLATION", "HIGH"),
    "ENV_SENSITIVE_VERDICT":     ("P4_ENV_INVARIANCE", "MEDIUM"),
    "NONDETERMINISTIC_EXECUTION": ("P3_DETERMINISM", "MEDIUM"),
    "VERIFIER_CRASH":            ("P3_DETERMINISM", "MEDIUM"),
    "SHALLOW_TEST_SET":          ("P8_DISCRIMINATION", "MEDIUM"),
    "OUTPUT_FORM_SENSITIVITY":   ("P7_DECLARED_EQUIVALENCE", "INFO"),
    "EQUIVALENT_MUTANTS":        ("P8_DISCRIMINATION", "INFO"),
}


@dataclass
class Finding:
    task_id: str
    klass: str
    title: str
    claim: str                       # one-sentence falsification statement
    witnesses: list = field(default_factory=list)   # minimized concrete evidence
    evidence: list = field(default_factory=list)    # trial refs / raw observations
    stats: dict = field(default_factory=dict)
    replay: list = field(default_factory=list)      # trial specs that reproduce it

    @property
    def property_id(self) -> str:
        return CLASSES[self.klass][0]

    @property
    def severity(self) -> str:
        return CLASSES[self.klass][1]

    def to_dict(self) -> dict:
        return {"task_id": self.task_id, "class": self.klass,
                "property": self.property_id,
                "property_text": PROPERTIES[self.property_id],
                "severity": self.severity, "title": self.title, "claim": self.claim,
                "witnesses": self.witnesses, "evidence": self.evidence,
                "stats": self.stats, "replay": self.replay}
