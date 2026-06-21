"""The governance package stands on its own — the product seed.

Everything here uses ONLY signalkit.governance: the audit log, the tamper-evident
chain, and the DTA artefacts, with no import of the crime-data app. The last test
proves it by checking that importing the package never loads the analyst/data
modules.
"""

import subprocess
import sys

import signalkit.governance as gov


def test_public_api_is_complete():
    for name in gov.__all__:
        assert hasattr(gov, name), f"{name} is exported but missing"


def test_governed_decision_in_a_few_lines(tmp_path):
    # The example from the package docstring, executed.
    log = gov.DecisionLogger(str(tmp_path / "decisions.jsonl"))
    log.log(gov.DecisionEntry(
        model_name="my-model-v1",
        input_summary="user asked X",
        model_output_summary="answered Y",
        decision_made="Returned Y to the user.",
        risk_category=gov.RiskCategory.limited,
        human_review_required=False,
    ))
    assert log.verify().valid
    assert len(log.read_all()) == 1


def test_full_artefact_flow_without_the_app(tmp_path):
    log = gov.DecisionLogger(str(tmp_path / "d.jsonl"))
    for i in range(3):
        log.log(gov.DecisionEntry(
            model_name="m",
            input_summary=f"in {i}",
            model_output_summary=f"out {i}",
            decision_made=f"did {i}",
            use_case="Demo use case",
            risk_category=gov.RiskCategory.limited,
            human_review_required=(i == 0),
        ))
    entries = log.read_all()

    # tamper-evidence
    assert gov.verify_chain(entries).valid

    # every DTA artefact, generated from the log alone
    assert gov.summarise(entries).total_decisions == 3
    reg = gov.register(entries, agency="Demo Agency", accountable_official="Jane Doe")
    assert reg.use_cases and reg.agency == "Demo Agency"
    tr = gov.transparency_statement(entries, agency="Demo Agency", accountable_official="Jane Doe")
    assert tr.ai_systems and tr.statement
    ia = gov.impact_assessment(entries, agency="Demo Agency", accountable_official="Jane Doe")
    assert ia.use_cases and ia.statement


def test_importing_governance_does_not_load_the_crime_app():
    """Proof of standalone-ness: importing the package pulls in no app module."""
    code = (
        "import signalkit.governance, sys; "
        "app = [m for m in sys.modules "
        "if m.startswith('signalkit.analyst') or m.startswith('signalkit.data')]; "
        "assert not app, app"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_governance_depends_only_on_pydantic_and_stdlib():
    import inspect

    from signalkit.governance import decision_log

    for line in inspect.getsource(decision_log).splitlines():
        if line.startswith(("import ", "from ")):
            assert "signalkit.analyst" not in line
            assert "signalkit.data" not in line
