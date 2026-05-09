"""Meta tools: validate_formula."""
import json
import sys

from ..core.shared_state import mcp


@mcp.tool()
async def validate_formula(formula: str) -> str:
    """Validate an Excel formula for syntax and function name errors.

    Use this tool to check formulas BEFORE using set_cell_formula to catch:
    - Invalid Excel function names (e.g., SUMPMT should be CUMIPMT)
    - Potential undefined named ranges
    - Basic syntax errors (unbalanced parentheses, etc.)

    Args:
        formula: Excel formula string (should start with '=')

    Returns:
        JSON string with validation results:
        {
            "valid": bool,
            "errors": List[str],
            "warnings": List[str],
            "functions_used": List[str],
            "potential_names": List[str]
        }
    """
    try:
        from excel_mcp_server import formula_validator

        result = formula_validator.validate_formula(formula)

        return json.dumps(result, indent=2)

    except Exception as e:
        return json.dumps({
            "valid": False,
            "errors": [f"Validation error: {str(e)}"],
            "warnings": [],
            "functions_used": [],
            "potential_names": []
        }, indent=2)
