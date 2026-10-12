"""Stage registry. Real stages replace the fakes phase by phase (see BUILD_AND_ROLLOUT.md)."""

from __future__ import annotations

from sdlc.stages.fake import FAKE_STAGES
from sdlc.stages.pipeline_a import Grill, Intake, Propose, Validate
from sdlc.stages.pipeline_b import Fetch, Implement, Prep, Verify
from sdlc.stages.pipeline_b_review import Archive, Close, Fix, Guard, RaisePr, Review
from sdlc.stages.publish import Publish, SpecPr

REAL_STAGES: dict[str, type] = {
    "a0_intake": Intake, "a1_grill": Grill, "a2_propose": Propose, "a3_validate": Validate,
    "a3b_spec_pr": SpecPr, "a4_publish": Publish,
    "b0_fetch": Fetch, "b1_prep": Prep, "b3_implement": Implement, "b4_verify": Verify,
    "b5_review": Review, "b6_guard": Guard, "b7_pr": RaisePr, "b8_fix": Fix, "b9_archive": Archive,
    "b10_close": Close,
}

# Which build phase delivers the real version of each stage.
ARRIVES_IN = {
    "a0_intake": "P3", "a1_grill": "P3", "a2_propose": "P3", "a3_validate": "P3", "a3b_spec_pr": "P4", "a4_publish": "P4",
    "b0_fetch": "P5", "b1_prep": "P5", "b3_implement": "P5", "b4_verify": "P5",
    "b5_review": "P6", "b6_guard": "P6", "b7_pr": "P6", "b8_fix": "P6", "b9_archive": "P6", "b10_close": "P6",
}


def get_stage(name: str, fake: bool = False):
    registry = FAKE_STAGES if fake else REAL_STAGES
    if name not in registry:
        if fake:
            raise KeyError(f"no fake stage {name!r}")
        raise NotImplementedError(
            f"the real {name} stage arrives in {ARRIVES_IN.get(name, 'a later phase')}; run with --fake for now")
    return registry[name](name)
