"""Request and response schemas."""

from pydantic import BaseModel, Field


class StaleFinding(BaseModel):
    item: str = Field(description="The exact parameter, command, file, or version at fault")
    problem: str = Field(description="What changed, and in which release if known")
    search_query: str = Field(description="Targeted web search to find what replaced it")


class JudgeVerdict(BaseModel):
    """Structured so the branch is taken in code, never parsed out of free text.

    `findings` is a list rather than one `reason` because a single-reason schema
    let the model stop at the first problem it noticed — a section typically
    rots in several places at once.
    """

    current: bool = Field(description="True only if NOTHING in the excerpt is outdated")
    reason: str = Field(description="One line summarising the verdict overall")
    findings: list[StaleFinding] = Field(
        default_factory=list, description="Every stale item found; empty when current"
    )


class DiagnoseRequest(BaseModel):
    error_log: str = Field(..., min_length=1, examples=["FATAL: sorry, too many clients already"])
