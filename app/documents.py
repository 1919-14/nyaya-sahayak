"""
Turns session context into a ready-to-use drafted document. RTI applications
work universally across departments, which is why they're the primary
document type — the same template applies whether the underlying question
was about a welfare scheme, a labour dispute, or a consumer complaint (e.g.
"file an RTI to find out why my PM-JAY claim was rejected").

A second template covers consumer/labour complaint letters for when the
user already knows who's at fault and just needs a formal written complaint.
"""
from datetime import date

from app import llm_client
from app.config import SUPPORTED_LANGUAGES, DEFAULT_LANGUAGE

RTI_TEMPLATE = """To,
The Public Information Officer,
{department}

Subject: Request for information under the Right to Information Act, 2005

Sir/Madam,

Under Section 6(1) of the Right to Information Act, 2005, I would like to request the
following information:

{questions}

I am enclosing the prescribed application fee of Rs. 10 (or requesting fee exemption
under BPL category, if applicable). Kindly provide the requested information within
30 days as mandated under Section 7(1) of the Act.

Applicant details:
Name: {applicant_name}
Address: {applicant_address}
Contact: {applicant_contact}

Date: {today}

Signature: ___________________
"""

COMPLAINT_TEMPLATE = """To,
{recipient}

Subject: Formal complaint regarding {subject}

Sir/Madam,

I am writing to formally complain about the following matter:

{details}

I request that this matter be resolved within a reasonable time, and I reserve the
right to escalate this complaint to the appropriate forum if not addressed
satisfactorily.

Complainant details:
Name: {applicant_name}
Address: {applicant_address}
Contact: {applicant_contact}

Date: {today}

Signature: ___________________
"""


def _extract_rti_questions(conversation_summary: str) -> list[str]:
    """Uses the LLM to turn a conversation into 2-4 crisp RTI questions."""
    if not llm_client.available():
        return [conversation_summary or "[Describe the specific information you need]"]

    prompt = f"""Based on this conversation summary, write 2-4 crisp, specific
questions suitable for a formal RTI application. Each should be answerable with
a concrete fact or document. Output as a numbered list, nothing else. Write the
questions in English regardless of what language the conversation was in — RTI
applications are filed in English or the state's official language, and English
is the safe universal default.

Conversation summary:
{conversation_summary}"""

    text = llm_client.complete("You draft precise RTI application questions.", prompt, max_tokens=300)
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    return lines if lines else [conversation_summary]


def draft_rti(department: str, conversation_summary: str, applicant: dict) -> str:
    questions = _extract_rti_questions(conversation_summary)
    questions_block = "\n".join(questions)

    return RTI_TEMPLATE.format(
        department=department or "[Name of Department/Public Authority]",
        questions=questions_block,
        applicant_name=applicant.get("name", "[Your Name]"),
        applicant_address=applicant.get("address", "[Your Address]"),
        applicant_contact=applicant.get("contact", "[Your Phone/Email]"),
        today=date.today().strftime("%d-%m-%Y"),
    )


def draft_complaint(recipient: str, subject: str, conversation_summary: str,
                     applicant: dict) -> str:
    if llm_client.available():
        prompt = f"""Based on this conversation summary, write a clear, factual
paragraph (4-6 sentences) describing the complaint for a formal letter, in
English. Be specific about dates/amounts if mentioned. Do not invent facts not
present in the summary.

Conversation summary:
{conversation_summary}"""
        details = llm_client.complete("You draft formal complaint letters.", prompt, max_tokens=300)
    else:
        details = conversation_summary or "[Describe the issue in detail]"

    return COMPLAINT_TEMPLATE.format(
        recipient=recipient or "[Recipient / Organization]",
        subject=subject or "[Brief subject of complaint]",
        details=details,
        applicant_name=applicant.get("name", "[Your Name]"),
        applicant_address=applicant.get("address", "[Your Address]"),
        applicant_contact=applicant.get("contact", "[Your Phone/Email]"),
        today=date.today().strftime("%d-%m-%Y"),
    )
