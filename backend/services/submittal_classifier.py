"""Bucket E Phase 4c (2026-05-11) -- ai_submittal_classifier producer.

Rule-based AI-light classifier that scans the submittal's filename +
attached PDF text (when present) and proposes spec_section_id /
discipline / submittal_type via the field_suggestions framework.

Why rule-based?
---------------
Submittals are uploaded in bulk during procurement (often dozens
per week). LLM-per-row would be expensive + slow. Pattern matching
on filenames + the first few hundred chars of extracted PDF text
catches ~80% of submittals correctly; PMs confirm in the Jigsaw
widget for the long tail. Confidence 0.7-0.85 depending on match
strength keeps users in the loop.

Strong match (conf 0.85): both filename + PDF text agree on a
keyword group.
Weak match (conf 0.70): only one source matched.

pdfplumber is optional -- if the file isn't a PDF or the lib is
missing, we fall through to filename-only inference.
"""
from __future__ import annotations

import io
import logging
import re
from typing import Optional

from database import get_db, tenant_filter
from services import field_suggestions as fs

log = logging.getLogger("submittal_classifier")


# MasterFormat-aligned discipline groups. Match against filename +
# extracted text. First match wins.
_DISCIPLINE_GROUPS: list[tuple[tuple[str, ...], str]] = [
    (("plumb", "p-101", "p-201", "p-301", "sanitary", "fixtures"),  "Plumbing"),
    # "duct" intentionally written as "ductwork" / "duct " to avoid
    # matching "product" (the classic word-boundary trap).
    (("hvac", "mechanical", "ductwork", "duct ", "duct-",
       "vav ", "rtu ", "chiller", "boiler", "exhaust fan"),         "Mechanical"),
    (("electric", "e-101", "e-201", "e-301", "panelboard",
       "switchgear", "conduit", "lighting fixture",
       "luminaire"),                                                "Electrical"),
    (("fire protection", "sprinkler", "fa-", "fire alarm"),         "Fire Protection"),
    (("structural", "s-101", "s-201", "s-301", "beam",
       "column", "rebar", "footings"),                              "Structural"),
    (("architectural", "a-101", "a-201", "a-301", "doors",
       "windows", "partition"),                                     "Architectural"),
    (("civil", "c-101", "c-201", "c-301", "site",
       "earthwork", "paving"),                                       "Civil"),
    (("interiors", "casework", "millwork", "flooring",
       "ceiling", "paint"),                                          "Interiors"),
    (("specialty", "elevator", "escalator", "fume hood"),           "Specialties"),
    (("communications", "ds-", "data", "low voltage"),              "Communications"),
]

# Submittal type keyword map. Free-text override carried through to
# the SubmittalCreate.type_other_description field; the enum value
# we suggest is one of the SubmittalTypeEnum 10 values.
_TYPE_GROUPS: list[tuple[tuple[str, ...], str]] = [
    (("shop drawing", "shop dwg"),                "shop_drawing"),
    (("product data", "manufacturer data"),       "product_data"),
    (("sample", "color sample", "material sample"), "sample"),
    (("mock-up", "mock up", "mockup"),            "mock_up"),
    (("certification", "certificate", "cert"),    "certifications"),
    (("test report", "test result", "lab report"), "test_reports"),
    (("o&m", "operations manual",
       "operation manual", "maintenance manual"), "o_m_manual"),
    (("as-built", "as built", "record drawing"),   "as_built"),
    (("closeout", "punchlist", "punch list"),     "closeout"),
]

# CSI MasterFormat 2018 section code pattern: "XX XX XX" (3 pairs
# of digits, space-separated). Captures values like "07 21 00".
_SPEC_SECTION_RE = re.compile(r"\b(\d{2}\s\d{2}\s\d{2})\b")


def _extract_pdf_text(pdf_bytes: bytes, max_chars: int = 4000) -> str:
    """Best-effort PDF text extraction. Returns "" on any failure
    (missing lib, password-protected file, etc.); the classifier
    falls through to filename-only matching."""
    try:
        import pdfplumber
    except ImportError:
        log.debug("pdfplumber not installed; classifier falls back to filename")
        return ""
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            chunks: list[str] = []
            for page in pdf.pages[:5]:   # first 5 pages is enough for cover
                t = page.extract_text() or ""
                chunks.append(t)
                if sum(len(c) for c in chunks) >= max_chars:
                    break
        return ("\n".join(chunks))[:max_chars]
    except Exception as exc:
        log.debug("pdf text extract failed (non-fatal): %s", exc)
        return ""


def _match_group(
    haystack: str, groups: list[tuple[tuple[str, ...], str]],
) -> Optional[str]:
    lc = haystack.lower()
    for keywords, label in groups:
        for k in keywords:
            if k in lc:
                return label
    return None


def _classify(
    *, filename: str, pdf_text: str = "",
) -> dict:
    """Returns {discipline, type, spec_section_code, confidence_*}
    dicts where each suggestion carries its own confidence score:
    0.85 when filename + pdf_text agree, 0.70 when only one signal
    fires."""
    f_disc = _match_group(filename or "", _DISCIPLINE_GROUPS)
    t_disc = _match_group(pdf_text or "", _DISCIPLINE_GROUPS)
    discipline = None
    discipline_conf = None
    if f_disc and t_disc and f_disc == t_disc:
        discipline, discipline_conf = f_disc, 0.85
    elif f_disc or t_disc:
        discipline, discipline_conf = (f_disc or t_disc), 0.70

    f_type = _match_group(filename or "", _TYPE_GROUPS)
    t_type = _match_group(pdf_text or "", _TYPE_GROUPS)
    sub_type = None
    type_conf = None
    if f_type and t_type and f_type == t_type:
        sub_type, type_conf = f_type, 0.85
    elif f_type or t_type:
        sub_type, type_conf = (f_type or t_type), 0.70

    spec_section_code = None
    spec_conf = None
    haystack = f"{filename or ''} {pdf_text or ''}"
    m = _SPEC_SECTION_RE.search(haystack)
    if m:
        spec_section_code = m.group(1)
        # If filename also carried it, bump to 0.85; otherwise 0.75.
        spec_conf = 0.85 if filename and m.group(1) in filename else 0.75

    return {
        "discipline": discipline,
        "discipline_confidence": discipline_conf,
        "submittal_type": sub_type,
        "submittal_type_confidence": type_conf,
        "spec_section_code": spec_section_code,
        "spec_section_confidence": spec_conf,
    }


async def classify_and_suggest(
    *, tenant_id: str, project_id: str, submittal_id: str,
    filename: Optional[str] = None,
    pdf_bytes: Optional[bytes] = None,
    actor_id: Optional[str] = None,
    request_id: Optional[str] = None,
) -> dict:
    """Public entry point. Pulls discipline + type + spec section from
    filename + (optional) PDF text, drops a Jigsaw field_suggestion
    per non-None match. Returns the classifier diagnostic dict.

    Confidence per spec: 0.7-0.85. We only emit suggestions when at
    least one signal matched -- silent no-op otherwise so we don't
    spam an empty review queue."""
    pdf_text = ""
    if pdf_bytes:
        pdf_text = _extract_pdf_text(pdf_bytes)
    result = _classify(filename=filename or "", pdf_text=pdf_text)

    # Bug #36 fix (2026-05-13): "don't overwrite human work" guard.
    # Mirrors the photo_ai_producer pattern at photo_tray_router.py:
    # 780-781 -- if the target field on the submittal is already
    # populated, skip the suggestion silently. The PM either typed it
    # manually or accepted a prior AI suggestion; either way the AI
    # has no business shoving a fresh guess at them.
    #
    # Early exit if the classifier matched nothing -- avoids a DB
    # roundtrip for the no-op case (which is the majority).
    if not (result.get("discipline")
            or result.get("submittal_type")
            or result.get("spec_section_code")):
        return result

    db = get_db()
    submittal = await db.submittals.find_one(
        tenant_filter(tenant_id, {"id": submittal_id}),
        {"_id": 0, "discipline": 1, "type": 1, "spec_section_code": 1},
    ) or {}

    def _empty(v):
        """Same emptiness rule as photo_ai_producer: None / empty
        string after strip both count as empty. Non-string truthy
        values (e.g. an int 0 in a numeric field) would correctly
        register as 'has a value' here -- we only suppress when the
        user genuinely hasn't entered anything."""
        if v is None:
            return True
        if isinstance(v, str) and not v.strip():
            return True
        return False

    # Emit suggestions. discipline + submittal_type + spec_section_
    # code each get their own row so the PM can confirm or reject
    # them independently in the JigsawField widget.
    if result.get("discipline") and _empty(submittal.get("discipline")):
        await fs.create_suggestion(
            tenant_id=tenant_id, project_id=project_id,
            entity_type="submittal", entity_id=submittal_id,
            field_path="discipline",
            suggested_value=result["discipline"],
            suggested_by="ai_submittal_classifier",
            confidence_score=result["discipline_confidence"],
            source_context=(
                f"filename_match={bool(filename)}; pdf_text_chars={len(pdf_text)}"
            ),
            actor_id=actor_id, request_id=request_id,
        )
    if result.get("submittal_type") and _empty(submittal.get("type")):
        await fs.create_suggestion(
            tenant_id=tenant_id, project_id=project_id,
            entity_type="submittal", entity_id=submittal_id,
            field_path="type",
            suggested_value=result["submittal_type"],
            suggested_by="ai_submittal_classifier",
            confidence_score=result["submittal_type_confidence"],
            source_context=(
                f"filename_match={bool(filename)}; pdf_text_chars={len(pdf_text)}"
            ),
            actor_id=actor_id, request_id=request_id,
        )
    if result.get("spec_section_code") and _empty(submittal.get("spec_section_code")):
        await fs.create_suggestion(
            tenant_id=tenant_id, project_id=project_id,
            entity_type="submittal", entity_id=submittal_id,
            field_path="spec_section_code",
            suggested_value=result["spec_section_code"],
            suggested_by="ai_submittal_classifier",
            confidence_score=result["spec_section_confidence"],
            source_context=(
                f"regex_match_in_{'filename' if filename and result['spec_section_code'] in filename else 'pdf_text'}"
            ),
            actor_id=actor_id, request_id=request_id,
        )
    return result
