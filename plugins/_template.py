"""Plugin template. Copy this file, rename it, and drop it into plugins/.

Files starting with an underscore are never loaded, so this template is safe
to keep. The only required pieces are TOOL and run().
"""

TOOL = {
    # snake_case, unique, not the name of a built-in tool.
    "name": "example_echo",
    # The model reads this to decide when to call the tool. Be specific.
    "description": "Repeat back a short phrase exactly as given. Use only when the user asks for an echo test.",
    # Every parameter is required. Types: "str", "int", "float", "bool".
    "parameters": {"phrase": "str"},
}


def run(phrase: str) -> str:
    """Return the text the model will read back. Keep it short."""
    return f"You said: {phrase}"
