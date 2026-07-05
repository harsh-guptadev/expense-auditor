from mcp.server.fastmcp import FastMCP
import json

# Initialize FastMCP Server
mcp = FastMCP("ExpenseAuditorServer")

@mcp.tool()
def query_policy_rules(category: str) -> str:
    """Retrieves corporate travel, entertainment, and general expense policy rules.
    
    Args:
        category: The category of expense (e.g. food, transport, lodging, lodging_limits, general).
    """
    policies = {
        "food": "Daily limit for meals is $75 per person. Alcohol is strictly prohibited on individual meal claims.",
        "transport": "Standard rideshare and taxi fares are allowed. Premium classes (Uber Black, First Class) require prior approval.",
        "lodging": "Hotel limit is $250 per night in standard cities, and $400 per night in high-cost cities (SF, NYC, London).",
        "entertainment": "Entertainment expenses require a clear list of business attendees and business justification. Limit is $150 per event.",
        "general": "All expenses must have valid receipt images. Expenses exceeding $1000 require manual administrative approval."
    }
    return policies.get(category.lower(), f"No specific policy rules found for '{category}'. Standard policy: receipts required, limit $100 per day.")

@mcp.tool()
def fetch_historical_expenses(employee_name: str) -> str:
    """Checks for duplicate or anomalous submissions by retrieving recent expense claims for the employee.
    
    Args:
        employee_name: The name of the employee to check history for.
    """
    # Simulated database of past expense claims
    history = [
        {"claim_id": "EXP-101", "date": "2026-06-15", "amount": 120.00, "category": "food", "status": "APPROVED"},
        {"claim_id": "EXP-105", "date": "2026-06-20", "amount": 80.50, "category": "transport", "status": "APPROVED"},
        {"claim_id": "EXP-112", "date": "2026-07-02", "amount": 250.00, "category": "lodging", "status": "PENDING"}
    ]
    return json.dumps(history)

@mcp.tool()
def log_audit_entry(claim_id: str, action: str, reason: str) -> str:
    """Stores the audit outcome and rationale in the audit logs.
    
    Args:
        claim_id: The ID of the expense claim.
        action: The audit action taken (e.g. APPROVED, REJECTED, NEEDS_REVIEW).
        reason: The reason or policy details behind the action.
    """
    log_entry = {
        "claim_id": claim_id,
        "action": action,
        "reason": reason,
        "logged": True
    }
    return f"Audit log successfully recorded: {json.dumps(log_entry)}"

if __name__ == "__main__":
    mcp.run()
