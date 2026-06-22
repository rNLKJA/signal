"""A governed support bot in ~15 lines — the Signal SDK on a non-crime app.

This is a minimal FastAPI app that has nothing to do with crime data. It shows how
any team adds governance with the SDK: each answer is logged to a tamper-evident
audit trail on the request path, and the DTA artefacts are exposed for free.

Run it::

    pip install -e ".[dev]"
    uvicorn examples.governed_support_bot:app --reload

Then::

    curl -X POST localhost:8000/ask -H 'content-type: application/json' \
         -d '{"question": "how do I reset my password?"}'
    curl localhost:8000/governance/verify       # the audit chain, intact
    curl localhost:8000/governance/register      # the DTA use-case register, live
"""

from fastapi import FastAPI
from pydantic import BaseModel

from signalkit.governance import Governor

app = FastAPI(title="Governed support bot")
gov = Governor("support_decisions.jsonl", agency="Acme Corp", accountable_official="Jane Doe")
gov.mount(app)  # exposes /governance/verify, /summary, /register, /transparency, ...


class Ask(BaseModel):
    question: str


def answer_question(question: str) -> str:
    # Pretend this calls your LLM. The governance is identical whatever the model.
    return f"To resolve '{question}', try our help centre and these steps: 1) ... 2) ..."


@app.post("/ask")
def ask(req: Ask) -> dict:
    # The answer cannot be returned without being logged: the `with` block writes
    # a governed, tamper-evident audit entry as it completes.
    with gov.record(use_case="support-bot", model_name="gpt-4o", input_summary=req.question) as rec:
        text = answer_question(req.question)
        rec.output(text)
    return {"answer": text, "decision_id": rec.decision_id}
