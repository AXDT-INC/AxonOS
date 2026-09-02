"""Pure routing rules for AxonAI turns."""

VISION_PHRASES = (
    "what do you see", "describe the screen", "what's on screen",
    "what is on screen", "analyze the image", "look at the screen",
    "screen shows", "what's displayed", "current view", "what am i looking at",
    "screen content", "desktop shows", "analyze this visualization",
    "explain this visualization", "describe this plot", "analyze this graph",
    "explain this chart", "what does this chart show", "read this screen",
    "what's open on screen", "what windows are open", "desktop state",
    "help me with what i'm doing", "help me with what is on screen",
)

ROUTE_OVERRIDES = (("/agent", "agent"), ("/chat", "chat"), ("/vision", "vision"))


def needs_screen(user_text):
    """Return whether the user explicitly refers to the current screen."""
    lowered = user_text.lower()
    return any(phrase in lowered for phrase in VISION_PHRASES)


def choose_route(user_text, agentic_enabled=True):
    """Choose exactly one backend, honoring a leading explicit override."""
    stripped = user_text.strip()
    lowered = stripped.lower()
    for prefix, route in ROUTE_OVERRIDES:
        if lowered == prefix or lowered.startswith(prefix + " "):
            return route, stripped[len(prefix):].strip()
    if needs_screen(stripped):
        return "vision", stripped
    return ("agent" if agentic_enabled else "chat"), stripped


def format_agent_request(conversation_history, synced_through, current_request):
    """Bridge direct-chat turns that are not already in the OpenCode session."""
    pending = [
        message
        for message in conversation_history[max(0, synced_through):-1]
        if not message.get("cancelled")
    ]
    if not pending:
        return current_request

    lines = []
    for message in pending:
        role = "User" if message.get("role") == "user" else "Assistant"
        lines.append(f"{role}: {message.get('content', '')}")
    context = "\n".join(lines)
    return (
        "Context from direct AxonOS chat turns that occurred outside this OpenCode "
        f"session:\n\n{context}\n\nCurrent user request:\n{current_request}"
    )
