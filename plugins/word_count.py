"""Count words, characters, or sentences in text the user provides."""

TOOL = {
    "name": "word_count",
    "description": "Count the words, characters, or sentences in a piece of text. Use when the user asks how long a text is.",
    "parameters": {"text": "str"},
}


def run(text: str) -> str:
    words = len(text.split())
    chars = len(text)
    sentences = sum(text.count(p) for p in ".!?") or (1 if text.strip() else 0)
    return f"That text has {words} words, {chars} characters and about {sentences} sentences."
