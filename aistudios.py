#!/usr/bin/env python3
"""
AiStudios — AI-assisted film script development tool.
Usage: python aistudios.py <command> [options]
"""

import argparse
import os
import sys
from pathlib import Path
from datetime import datetime

try:
    import anthropic
except ImportError:
    print("Install the Anthropic SDK: pip install anthropic")
    sys.exit(1)

MODEL = "claude-opus-4-8"
SCRIPTS_DIR = Path("scripts")
OUTLINES_DIR = Path("outlines")
CHARACTERS_DIR = Path("characters")
PROMPTS_DIR = Path("prompts")

client = anthropic.Anthropic()


def load_prompt(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.txt"
    if path.exists():
        return path.read_text()
    return ""


def ai(system: str, user: str, max_tokens: int = 2000) -> str:
    response = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return response.content[0].text


def cmd_scene(args):
    """Generate a screenplay scene in Fountain format."""
    system = load_prompt("scene") or (
        "You are an expert screenwriter. Write scenes in proper Fountain format "
        "(plain-text screenplay syntax). Use sluglines, action lines, character cues, "
        "and dialogue. Be cinematic, specific, and economical with words. "
        "Output ONLY the Fountain-formatted scene — no commentary."
    )

    context = ""
    if args.context:
        context = f"\n\nContext / prior story beats:\n{args.context}"

    user = f"Write a scene with this description:\n{args.description}{context}"
    if args.characters:
        user += f"\n\nCharacters involved: {args.characters}"
    if args.tone:
        user += f"\n\nTone: {args.tone}"

    result = ai(system, user, max_tokens=1500)
    print(result)

    if args.output:
        out_path = SCRIPTS_DIR / args.output
        mode = "a" if out_path.exists() else "w"
        with open(out_path, mode) as f:
            if mode == "a":
                f.write("\n\n")
            f.write(result)
        print(f"\n[Saved to {out_path}]", file=sys.stderr)


def cmd_outline(args):
    """Generate a story outline."""
    system = load_prompt("outline") or (
        "You are a story development expert and screenwriting consultant. "
        "Create clear, structured film outlines that cover premise, characters, "
        "three-act structure, key turning points, and thematic throughline. "
        "Be specific and actionable — the outline should guide writing."
    )

    user = f"Create a detailed film outline for:\n\nTitle: {args.title}\nConcept: {args.concept}"
    if args.genre:
        user += f"\nGenre: {args.genre}"
    if args.length:
        user += f"\nTarget length: {args.length} minutes"

    result = ai(system, user, max_tokens=2500)
    print(result)

    if args.output or args.title:
        filename = args.output or f"{args.title.lower().replace(' ', '_')}_outline.md"
        out_path = OUTLINES_DIR / filename
        out_path.write_text(f"# {args.title} — Outline\n\n{result}\n")
        print(f"\n[Saved to {out_path}]", file=sys.stderr)


def cmd_character(args):
    """Develop a character profile."""
    system = load_prompt("character") or (
        "You are a character development expert. Create rich, psychologically complex "
        "characters for film. Include: name, age, backstory, want (external goal), "
        "need (internal truth), flaw, voice/speech patterns, relationships, and arc. "
        "Make them feel like real people with contradictions."
    )

    user = f"Develop a character:\n{args.description}"
    if args.role:
        user += f"\nRole in story: {args.role}"
    if args.genre:
        user += f"\nFilm genre: {args.genre}"

    result = ai(system, user, max_tokens=1500)
    print(result)

    if args.name or args.output:
        filename = args.output or f"{args.name.lower().replace(' ', '_')}.md"
        out_path = CHARACTERS_DIR / filename
        header = f"# Character: {args.name or 'Unknown'}\n\n"
        out_path.write_text(header + result + "\n")
        print(f"\n[Saved to {out_path}]", file=sys.stderr)


def cmd_rewrite(args):
    """Rewrite or polish an existing scene."""
    if args.file:
        text = Path(args.file).read_text()
    elif args.text:
        text = args.text
    else:
        print("Provide --file or --text", file=sys.stderr)
        sys.exit(1)

    system = (
        "You are an expert script editor. Rewrite the provided scene to improve it "
        "based on the given notes. Maintain the Fountain format. "
        "Output ONLY the rewritten scene — no commentary."
    )

    user = f"Rewrite this scene:\n\n{text}\n\nNotes: {args.notes}"
    result = ai(system, user, max_tokens=1500)
    print(result)

    if args.output:
        out_path = SCRIPTS_DIR / args.output
        out_path.write_text(result)
        print(f"\n[Saved to {out_path}]", file=sys.stderr)


def cmd_dialogue(args):
    """Generate or improve dialogue for a scene moment."""
    system = (
        "You are a dialogue specialist for film. Write sharp, character-specific dialogue "
        "that reveals personality, subtext, and conflict. Avoid on-the-nose exposition. "
        "Output in Fountain format (character cue + dialogue blocks only)."
    )

    user = f"Write dialogue for this moment:\n{args.moment}"
    if args.characters:
        user += f"\n\nCharacters: {args.characters}"
    if args.subtext:
        user += f"\n\nUnderlying subtext/tension: {args.subtext}"

    result = ai(system, user, max_tokens=800)
    print(result)


def cmd_new(args):
    """Create a new empty script file."""
    filename = args.name if args.name.endswith(".fountain") else args.name + ".fountain"
    path = SCRIPTS_DIR / filename
    if path.exists():
        print(f"File already exists: {path}", file=sys.stderr)
        sys.exit(1)

    header = f"Title: {args.title or args.name}\n"
    header += f"Author: {args.author or 'Unknown'}\n"
    header += f"Draft date: {datetime.now().strftime('%B %d, %Y')}\n"
    header += f"Contact: \n\n===\n\n"
    path.write_text(header)
    print(f"Created: {path}")


def main():
    parser = argparse.ArgumentParser(
        prog="aistudios",
        description="AI-assisted film script development"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # scene
    p = sub.add_parser("scene", help="Generate a screenplay scene")
    p.add_argument("description", help="What happens in the scene")
    p.add_argument("--characters", help="Characters in the scene")
    p.add_argument("--tone", help="Tone (e.g. tense, comedic, melancholic)")
    p.add_argument("--context", help="Prior story context")
    p.add_argument("--output", help="Append to this file in scripts/")
    p.set_defaults(func=cmd_scene)

    # outline
    p = sub.add_parser("outline", help="Generate a story outline")
    p.add_argument("title", help="Film title")
    p.add_argument("concept", help="One-paragraph concept/logline")
    p.add_argument("--genre", help="Genre")
    p.add_argument("--length", help="Target runtime in minutes", default="90-100")
    p.add_argument("--output", help="Output filename in outlines/")
    p.set_defaults(func=cmd_outline)

    # character
    p = sub.add_parser("character", help="Develop a character profile")
    p.add_argument("description", help="Brief description of the character")
    p.add_argument("--name", help="Character name")
    p.add_argument("--role", help="Role in story (protagonist, antagonist, etc.)")
    p.add_argument("--genre", help="Film genre")
    p.add_argument("--output", help="Output filename in characters/")
    p.set_defaults(func=cmd_character)

    # rewrite
    p = sub.add_parser("rewrite", help="Rewrite/polish a scene")
    p.add_argument("--file", help="Path to scene file")
    p.add_argument("--text", help="Scene text directly")
    p.add_argument("notes", help="Rewrite notes / direction")
    p.add_argument("--output", help="Save result to scripts/")
    p.set_defaults(func=cmd_rewrite)

    # dialogue
    p = sub.add_parser("dialogue", help="Generate dialogue for a moment")
    p.add_argument("moment", help="Describe the dramatic moment")
    p.add_argument("--characters", help="Characters and their voices")
    p.add_argument("--subtext", help="Underlying subtext or tension")
    p.set_defaults(func=cmd_dialogue)

    # new
    p = sub.add_parser("new", help="Create a new script file")
    p.add_argument("name", help="Script filename (without extension)")
    p.add_argument("--title", help="Full title (defaults to name)")
    p.add_argument("--author", help="Author name")
    p.set_defaults(func=cmd_new)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
