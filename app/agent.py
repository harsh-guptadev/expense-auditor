# ruff: noqa
import re
import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from google.adk.agents import Agent, Context, LlmAgent
from google.adk.apps import App
from google.adk.models import Gemini
from google.genai import types
from google.adk.events import Event, RequestInput
from google.adk.workflow import Workflow, Edge, START, node
from google.adk.tools import McpToolset
from mcp import StdioServerParameters

from app.config import config

logger = logging.getLogger("expense_auditor")

import asyncio

# Define the state schema
async def handle_rate_limit_error(callback_context: Context, llm_request: Any, error: Exception) -> Any:
    error_str = str(error)
    if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str or "quota" in error_str.lower():
        # Parse retry time
        retry_time = 5.0
        match = re.search(r"Please retry in (\d+(?:\.\d+)?)s", error_str)
        if match:
            retry_time = float(match.group(1)) + 0.5
        elif "per minute" in error_str.lower() or "minute" in error_str.lower():
            retry_time = 10.0
            
        logger.warning(f"Rate limit hit! Sleeping for {retry_time}s before retrying...")
        await asyncio.sleep(retry_time)
        
        # Retry model call
        try:
            llm = callback_context.get_invocation_context().agent.canonical_model
            responses_generator = llm.generate_content_async(llm_request, stream=False)
            async for response in responses_generator:
                return response
        except Exception as retry_err:
            return await handle_rate_limit_error(callback_context, llm_request, retry_err)
    
    # Return None so ADK raises the original error if it's not a 429
    return None

class WorkflowState(BaseModel):
    raw_input: str = ""
    expense_data: Dict[str, Any] = Field(default_factory=dict)
    pii_redacted: bool = False
    security_checked: bool = True
    security_violations: List[str] = Field(default_factory=list)
    audit_report: Dict[str, Any] = Field(default_factory=dict)
    audit_status: str = "PENDING"  # APPROVED, REJECTED, NEEDS_HUMAN_REVIEW, SECURITY_REJECTED
    communication_draft: str = ""
    human_remarks: Optional[str] = None
    admin_approved: Optional[bool] = None

# Set up the local MCP Toolset using the system Python and the local mcp_server.py
mcp_toolset = McpToolset(
    connection_params=StdioServerParameters(
        command=sys.executable,
        args=[os.path.join(os.path.dirname(__file__), "mcp_server.py")]
    )
)

# 1. Specialized Policy Auditor Agent
policy_auditor = LlmAgent(
    name="policy_auditor",
    model=Gemini(
        model=config.model,
        retry_options=types.HttpRetryOptions(attempts=5),
    ),
    instruction=(
        "You are a specialized Policy Auditor agent. Your job is to audit expense claims against company policies. "
        "Use the `query_policy_rules` tool to check limits for categories like lodging, food, transport, and entertainment. "
        "Use the `fetch_historical_expenses` tool to retrieve past submissions and check for duplicates or anomalies. "
        "Evaluate the expense claim, flag any policy exceptions, and decide on an audit action (e.g., APPROVED, REJECTED, or NEEDS_HUMAN_REVIEW). "
        "Log your audit decision using the `log_audit_entry` tool. "
        "Respond with a structured JSON object containing keys: 'status' (APPROVED, REJECTED, or NEEDS_HUMAN_REVIEW) and 'reason'."
    ),
    tools=[mcp_toolset],
    on_model_error_callback=handle_rate_limit_error,
)

# 2. Specialized Communication Drafter Agent
communication_drafter = LlmAgent(
    name="communication_drafter",
    model=Gemini(
        model=config.model,
        retry_options=types.HttpRetryOptions(attempts=5),
    ),
    mode="chat",
    instruction=(
        "You are a specialized Communication Drafter agent. Your job is to write a polite and professional email "
        "to the employee informing them of the status of their expense claim submission. "
        "Write a clear, concise email citing specific policy rules if their claim was rejected or flagged."
    ),
    tools=[mcp_toolset],
    on_model_error_callback=handle_rate_limit_error,
)

# 3. Central Coordinator Orchestrator Agent
# Uses policy_auditor as a tool (delegation)
from google.adk.tools import AgentTool
orchestrator = LlmAgent(
    name="orchestrator",
    model=Gemini(
        model=config.model,
        retry_options=types.HttpRetryOptions(attempts=5),
    ),
    instruction=(
        "You are the central coordinator Orchestrator agent for the Expense Auditor system. "
        "Your task is to analyze the expense claim by calling the `policy_auditor` sub-agent as a tool. "
        "Once you receive the auditor's report, inspect the results. "
        "If the auditor flagged the claim as needing manual review, or if you detect that the claim is suspicious or exceeds $1000, "
        "decide that the claim requires manual human intervention and set status to NEEDS_HUMAN_REVIEW. "
        "Otherwise, if it complies with company policies, call the `communication_drafter` sub-agent as a tool "
        "to draft the response email. "
        "Return a final JSON object with keys: 'status' (APPROVED, REJECTED, or NEEDS_HUMAN_REVIEW) and 'audit_report'."
    ),
    tools=[AgentTool(agent=policy_auditor), AgentTool(agent=communication_drafter)],
    on_model_error_callback=handle_rate_limit_error,
)

# Node 1: Security Scan
@node
async def security_checkpoint(ctx: Context, node_input: Any) -> Event:
    """Scans the expense claim for PII, prompt injection, and restricted items."""
    raw_text = ""
    if isinstance(node_input, dict):
        raw_text = node_input.get("claim_text", "") or node_input.get("text", "") or json.dumps(node_input)
    elif isinstance(node_input, str):
        raw_text = node_input
    else:
        raw_text = str(node_input)

    ctx.state["raw_input"] = raw_text
    ctx.state["security_violations"] = []

    # 1. Prompt Injection Detection
    injection_keywords = ["ignore previous instructions", "system prompt", "you are now", "override policy", "bypass safety"]
    has_injection = False
    for kw in injection_keywords:
        if kw in raw_text.lower():
            ctx.state["security_violations"].append(f"Prompt injection pattern detected: '{kw}'")
            has_injection = True

    # 2. PII Detection & Redaction
    scrubbed_text = raw_text
    cc_regex = r"\b(?:\d[ -]??){13,16}\b"
    ssn_regex = r"\b\d{3}-\d{2}-\d{4}\b"
    found_cc = re.findall(cc_regex, raw_text)
    found_ssn = re.findall(ssn_regex, raw_text)

    if found_cc:
        ctx.state["security_violations"].append("PII violation: Credit Card number detected.")
        scrubbed_text = re.sub(cc_regex, "[REDACTED_CREDIT_CARD]", scrubbed_text)
    if found_ssn:
        ctx.state["security_violations"].append("PII violation: SSN/Tax ID detected.")
        scrubbed_text = re.sub(ssn_regex, "[REDACTED_SSN]", scrubbed_text)

    # 3. Domain Specific Rule: check for restricted items (Casino / Bar tab / Gambling)
    restricted_keywords = ["casino", "gambling", "bar tab", "personal luxury"]
    has_restricted = False
    for rkw in restricted_keywords:
        if rkw in raw_text.lower():
            ctx.state["security_violations"].append(f"Policy violation: restricted expense type '{rkw}'")
            has_restricted = True

    ctx.state["pii_redacted"] = len(found_cc) > 0 or len(found_ssn) > 0
    ctx.state["expense_data"] = {"text": scrubbed_text}

    # Audit logging
    severity = "INFO"
    if ctx.state["security_violations"]:
        severity = "WARNING"
        if has_injection or has_restricted:
            severity = "CRITICAL"

    audit_log = {
        "event": "security_checkpoint_evaluation",
        "severity": severity,
        "violations": ctx.state["security_violations"],
        "pii_redacted": ctx.state["pii_redacted"],
        "timestamp": ctx.timestamp.isoformat() if hasattr(ctx, "timestamp") else "2026-07-06T00:00:00"
    }
    logger.info(f"AUDIT_LOG: {json.dumps(audit_log)}")

    if severity == "CRITICAL":
        ctx.state["security_checked"] = False
        ctx.state["audit_status"] = "SECURITY_REJECTED"
        ctx.route = "SECURITY_EVENT"
        return Event(output={"status": "SECURITY_REJECTED", "details": ctx.state["security_violations"]})

    ctx.state["security_checked"] = True
    ctx.route = "safe"
    return Event(output=scrubbed_text)

# Node 2: Security Event Handler
@node
async def security_event_node(ctx: Context) -> Event:
    """Handles security events by terminating the workflow immediately with a rejection."""
    violations = ctx.state.get("security_violations", [])
    output_message = f"Expense submission rejected due to security or severe policy violations: {', '.join(violations)}."
    ctx.state["communication_draft"] = output_message
    return Event(output={"status": "SECURITY_REJECTED", "draft": output_message})

# Node 3: Orchestrator Router
@node
async def orchestrator_router(ctx: Context, node_input: Any) -> Event:
    """Parses orchestrator's output and determines the downstream workflow routing."""
    output_text = ""
    if isinstance(node_input, str):
        output_text = node_input
    elif isinstance(node_input, dict):
        output_text = json.dumps(node_input)
    else:
        output_text = str(node_input)

    needs_review = "needs_human_review" in output_text.lower() or "human_review" in output_text.lower() or "review" in output_text.lower()

    try:
        # Strip markdown formatting code blocks if present
        cleaned_text = output_text.strip()
        if cleaned_text.startswith("```"):
            lines = cleaned_text.splitlines()
            if len(lines) > 1 and lines[0].startswith("```"):
                lines = lines[1:]
            if len(lines) > 0 and lines[-1].startswith("```"):
                lines = lines[:-1]
            cleaned_text = "\n".join(lines).strip()

        data = json.loads(cleaned_text)
        if isinstance(data, dict):
            status = data.get("status", "")
            if status == "NEEDS_HUMAN_REVIEW":
                needs_review = True
            ctx.state["audit_status"] = status
            ctx.state["audit_report"] = data.get("audit_report", {})
    except Exception:
        pass

    # Check if amount >= $1000
    raw_input = ctx.state.get("raw_input", "")
    amounts = [float(val) for val in re.findall(r"\$\s*(\d+(?:\.\d{2})?)", raw_input)]
    for amt in amounts:
        if amt >= 1000.0:
            needs_review = True
            ctx.state["audit_status"] = "NEEDS_HUMAN_REVIEW"

    if needs_review:
        ctx.route = "needs_human_review"
        return Event(output={"status": "NEEDS_HUMAN_REVIEW"})
    else:
        ctx.route = "auto_complete"
        return Event(output={"status": "AUTO_COMPLETE"})

# Pydantic model for human review input
class HumanReviewInput(BaseModel):
    approved: bool = Field(description="Whether the expense is approved or not")
    remarks: Optional[str] = Field(default="", description="Administrator remarks explaining the decision")

# Node 4: Human Review Pause (HITL)
@node(rerun_on_resume=True)
async def human_review(ctx: Context) -> Event | RequestInput:
    """Pauses the workflow for manual human verification if policies are violated or thresholds exceeded."""
    res = ctx.resume_inputs.get("admin_review")
    if res is not None:
        if isinstance(res, dict):
            approved = res.get("approved", False)
            remarks = res.get("remarks", "")
        else:
            approved = getattr(res, "approved", False)
            remarks = getattr(res, "remarks", "")

        ctx.state["admin_approved"] = approved
        ctx.state["human_remarks"] = remarks
        ctx.state["audit_status"] = "APPROVED" if approved else "REJECTED"
        return Event(output={"status": ctx.state["audit_status"], "remarks": remarks})

    return RequestInput(
        interrupt_id="admin_review",
        message="Expense exceeds $1000 threshold or requires manual policy review. Please approve/reject.",
        response_schema=HumanReviewInput
    )

# Node 5: Dynamic Communication Drafter Runner
@node(rerun_on_resume=True)
async def communication_drafter_node(ctx: Context) -> Event:
    """Calls the specialized communication_drafter sub-agent to write the final message draft."""
    audit_status = ctx.state.get("audit_status", "PENDING")
    audit_report = ctx.state.get("audit_report", {})
    human_remarks = ctx.state.get("human_remarks", "")

    prompt = f"""
    Draft the final communication email based on:
    - Audit Status: {audit_status}
    - Audit Report: {json.dumps(audit_report)}
    - Human Review Remarks (if any): {human_remarks}
    
    Make sure the response is highly professional and polite. Detail the decision clearly.
    """

    await ctx.run_node(communication_drafter, node_input=prompt)
    
    draft_text = ""
    for event in reversed(ctx.session.events):
        if event.author == "communication_drafter" and event.content:
            parts_text = "".join(p.text for p in event.content.parts if p.text and not p.thought)
            if parts_text.strip():
                draft_text = parts_text.strip()
                break

    ctx.state["communication_draft"] = draft_text
    return Event(output={"status": audit_status, "draft": draft_text})

# Node 6: Final Output Collector
@node
async def final_output(ctx: Context, node_input: Any) -> Event:
    """Terminal node of the workflow. Collects the final output response."""
    status = ctx.state.get("audit_status", "UNKNOWN")
    draft = ctx.state.get("communication_draft", "")

    output_payload = {
        "status": status,
        "draft": draft,
        "pii_redacted": ctx.state.get("pii_redacted", False),
        "security_checked": ctx.state.get("security_checked", False)
    }
    return Event(output=output_payload)

# Build the Workflow Graph
workflow = Workflow(
    name="expense_auditor_workflow",
    description="Secure expense claim auditing and response drafting workflow.",
    state_schema=WorkflowState,
    edges=[
        Edge(from_node=START, to_node=security_checkpoint),
        Edge(from_node=security_checkpoint, to_node=orchestrator, route="safe"),
        Edge(from_node=security_checkpoint, to_node=security_event_node, route="SECURITY_EVENT"),
        Edge(from_node=orchestrator, to_node=orchestrator_router),
        Edge(from_node=orchestrator_router, to_node=human_review, route="needs_human_review"),
        Edge(from_node=orchestrator_router, to_node=communication_drafter_node, route="auto_complete"),
        Edge(from_node=human_review, to_node=communication_drafter_node),
        Edge(from_node=communication_drafter_node, to_node=final_output),
        Edge(from_node=security_event_node, to_node=final_output),
    ],
)

app = App(
    root_agent=workflow,
    name="app",
)
