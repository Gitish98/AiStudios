#!/usr/bin/env python3
"""
AiStudios — AI-assisted film script development tool.
Usage: python aistudios.py <command> [options]
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

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
GENRES_DIR = Path("genres")
PROJECTS_DIR = Path("projects")

client = anthropic.Anthropic()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_prompt(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.txt"
    if path.exists():
        return path.read_text()
    return ""


def load_genre_prompt(genre: str) -> str:
    if not genre:
        return ""
    path = GENRES_DIR / genre.lower() / "prompt.txt"
    if path.exists():
        return path.read_text()
    return ""


def load_genre_beats(genre: str) -> str:
    if not genre:
        return ""
    path = GENRES_DIR / genre.lower() / "beats.txt"
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


def load_project(name: str) -> dict:
    path = PROJECTS_DIR / name / "project.json"
    if not path.exists():
        print(f"Project not found: {name}", file=sys.stderr)
        sys.exit(1)
    return json.loads(path.read_text())


def project_context_prefix(project_data: dict) -> str:
    """Build a context preamble from project.json to prepend to AI prompts."""
    parts = []
    if project_data.get("title"):
        parts.append(f"Project title: {project_data['title']}")
    if project_data.get("genre"):
        parts.append(f"Genre: {project_data['genre']}")
    if project_data.get("logline"):
        parts.append(f"Logline: {project_data['logline']}")
    if project_data.get("author"):
        parts.append(f"Author: {project_data['author']}")
    if not parts:
        return ""
    return "PROJECT CONTEXT:\n" + "\n".join(parts) + "\n\n"


def inject_project(args) -> dict | None:
    """If --project flag is set, load project and return data. Otherwise None."""
    if hasattr(args, "project") and args.project:
        return load_project(args.project)
    return None


# ---------------------------------------------------------------------------
# Existing commands
# ---------------------------------------------------------------------------

def cmd_scene(args):
    """Generate a screenplay scene in Fountain format."""
    system = load_prompt("scene") or (
        "You are an expert screenwriter. Write scenes in proper Fountain format "
        "(plain-text screenplay syntax). Use sluglines, action lines, character cues, "
        "and dialogue. Be cinematic, specific, and economical with words. "
        "Output ONLY the Fountain-formatted scene — no commentary."
    )

    project_data = inject_project(args)
    context = project_context_prefix(project_data) if project_data else ""

    if args.context:
        context += f"\n\nContext / prior story beats:\n{args.context}"

    user = f"{context}Write a scene with this description:\n{args.description}"
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

    project_data = inject_project(args)
    context = project_context_prefix(project_data) if project_data else ""

    user = f"{context}Create a detailed film outline for:\n\nTitle: {args.title}\nConcept: {args.concept}"
    if args.genre:
        user += f"\nGenre: {args.genre}"
        genre_prompt = load_genre_prompt(args.genre)
        if genre_prompt:
            system = system + "\n\n" + genre_prompt
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

    project_data = inject_project(args)
    context = project_context_prefix(project_data) if project_data else ""

    user = f"{context}Develop a character:\n{args.description}"
    if args.role:
        user += f"\nRole in story: {args.role}"
    if args.genre:
        user += f"\nFilm genre: {args.genre}"

    result = ai(system, user, max_tokens=1500)
    print(result)

    if args.name or args.output:
        filename = args.output or f"{(args.name or 'character').lower().replace(' ', '_')}.md"
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
    header += "Contact: \n\n===\n\n"
    path.write_text(header)
    print(f"Created: {path}")


# ---------------------------------------------------------------------------
# New commands
# ---------------------------------------------------------------------------

def cmd_beats(args):
    """Generate a Save the Cat beat sheet for a story."""
    project_data = inject_project(args)

    # Determine title, concept, genre — args override project
    title = args.title
    concept = args.concept
    genre = getattr(args, "genre", None)

    if project_data:
        if not title and project_data.get("title"):
            title = project_data["title"]
        if not concept and project_data.get("logline"):
            concept = project_data["logline"]
        if not genre and project_data.get("genre"):
            genre = project_data["genre"]

    genre_beats = load_genre_beats(genre) if genre else ""
    genre_prompt = load_genre_prompt(genre) if genre else ""

    system = (
        "You are a story development expert specializing in Save the Cat beat sheet methodology. "
        "Generate detailed, story-specific beat sheets that tell the writer exactly what happens "
        "in THEIR story at each beat — not generic descriptions of what a beat is. "
        "Each beat should name what happens in this specific story."
    )
    if genre_prompt:
        system += "\n\n" + genre_prompt
    if genre_beats:
        system += "\n\n" + genre_beats

    context = project_context_prefix(project_data) if project_data else ""

    user = f"""{context}Generate a complete Save the Cat beat sheet for the following:

Title: {title or 'Untitled'}
Concept: {concept}"""
    if genre:
        user += f"\nGenre: {genre}"

    user += """

Produce all 15 beats in this exact format for each beat:

## [BEAT NAME] (p. [PAGE RANGE])
[2-4 sentences describing exactly what happens in THIS story at this beat. Be specific — name characters, locations, what is said or done, what changes.]

The 15 beats in order:
1. Opening Image (p. 1)
2. Theme Stated (p. 5)
3. Set-Up (p. 1-10)
4. Catalyst (p. 12)
5. Debate (p. 12-25)
6. Break into Two (p. 25)
7. B Story (p. 30)
8. Fun and Games (p. 30-55)
9. Midpoint (p. 55)
10. Bad Guys Close In (p. 55-75)
11. All Is Lost (p. 75)
12. Dark Night of the Soul (p. 75-85)
13. Break into Three (p. 85)
14. Finale (p. 85-110)
15. Final Image (p. 110)
"""

    result = ai(system, user, max_tokens=3000)
    print(result)

    if args.output or (project_data and args.project):
        if args.output:
            out_path = OUTLINES_DIR / args.output
        else:
            slug = (title or "beats").lower().replace(" ", "_")
            out_path = OUTLINES_DIR / f"{slug}_beats.md"
        out_path.write_text(f"# {title or 'Untitled'} — Beat Sheet\n\n{result}\n")
        print(f"\n[Saved to {out_path}]", file=sys.stderr)


def cmd_notes(args):
    """Generate script coverage / professional feedback on a screenplay."""
    if not args.file and not args.project:
        print("Provide --file <path> or --project <name>", file=sys.stderr)
        sys.exit(1)

    script_text = ""
    title = "Untitled"

    if args.file:
        p = Path(args.file)
        if not p.exists():
            print(f"File not found: {args.file}", file=sys.stderr)
            sys.exit(1)
        script_text = p.read_text()
        title = p.stem.replace("_", " ").title()
    elif args.project:
        project_data = load_project(args.project)
        title = project_data.get("title", args.project)
        project_scripts_dir = PROJECTS_DIR / args.project / "scripts"
        if project_scripts_dir.exists():
            files = sorted(project_scripts_dir.glob("*.fountain"))
            for f in files:
                script_text += f"\n\n--- {f.name} ---\n\n" + f.read_text()
        if not script_text:
            # Fallback: check global scripts dir for matching files
            for f in sorted(SCRIPTS_DIR.glob("*.fountain")):
                script_text += f"\n\n--- {f.name} ---\n\n" + f.read_text()

    if not script_text.strip():
        print("No script content found to evaluate.", file=sys.stderr)
        sys.exit(1)

    system = (
        "You are a professional script reader and story analyst for a major film production company. "
        "Your coverage reports are trusted, actionable, and specific. You give real feedback — "
        "not encouragement, not crushing critique, but the truth a writer needs to improve their work. "
        "You cite specific scenes, specific lines, specific structural moments. "
        "You grade work as: CONSIDER (has real potential, needs significant work), "
        "RECOMMEND (strong work, ready for the next step), or PASS (fundamental issues)."
    )

    user = f"Write full script coverage for the following screenplay:\n\nTitle: {title}\n\n"
    if args.focus:
        user += f"Focus especially on: {args.focus}\n\n"
    user += "SCREENPLAY:\n" + script_text + "\n\n"
    user += """Structure your coverage as follows:

TITLE: [Title]
GRADE: [CONSIDER / RECOMMEND / PASS]
LOGLINE: [One sentence summary of what the script is actually about]

PREMISE NOTES
[2-3 paragraphs on the core concept — is it original? compelling? does the premise generate drama?]

STRUCTURE NOTES
[2-3 paragraphs on three-act structure, pacing, scene order, turning points — what works, what doesn't, what's missing]

CHARACTER NOTES
[2-3 paragraphs on protagonist, antagonist, supporting characters — clarity of want/need, arcs, distinctiveness of voice]

DIALOGUE NOTES
[1-2 paragraphs on dialogue quality, subtext, on-the-nose problems, standout lines or scenes]

SPECIFIC SCENE NOTES
[3-5 specific scenes that should be revised, cut, or expanded — with page numbers if available and specific suggestions]

RECOMMENDATION
[1 paragraph: what does the writer need to do next?]
"""

    result = ai(system, user, max_tokens=3500)
    print(result)


def cmd_logline(args):
    """Generate or refine a logline."""
    project_data = inject_project(args)

    genre = getattr(args, "genre", None)
    if project_data and not genre:
        genre = project_data.get("genre")

    genre_prompt = load_genre_prompt(genre) if genre else ""

    system = (
        "You are a development executive and story analyst who specializes in crafting "
        "loglines that sell. A great logline is specific, ironic, emotionally compelling, "
        "and contains an implicit promise of the story to come. "
        'Format: "When [inciting incident], a [protagonist with defining trait] must [goal/action] '
        'before/or [stakes/deadline/consequence]." '
        "Variations should take different angles: different protagonists, different emphasis, different tone."
    )
    if genre_prompt:
        system += "\n\n" + genre_prompt

    context = project_context_prefix(project_data) if project_data else ""
    variations = getattr(args, "variations", 3)

    user = f"""{context}Generate {variations} logline variation(s) for the following concept:

{args.concept}"""
    if genre:
        user += f"\n\nGenre: {genre}"

    user += f"""

For each logline:
1. Write the logline itself (one sentence, using the format above)
2. Write a brief note (1-2 sentences) on what angle or emphasis this version takes

Number each variation. Make them genuinely different from each other.
"""

    result = ai(system, user, max_tokens=1000)
    print(result)


def cmd_project(args):
    """Project management: new, show, list."""
    subcmd = args.subcommand

    if subcmd == "new":
        _project_new(args)
    elif subcmd == "show":
        _project_show(args)
    elif subcmd == "list":
        _project_list(args)
    else:
        print(f"Unknown subcommand: {subcmd}", file=sys.stderr)
        sys.exit(1)


def _project_new(args):
    name = args.name
    project_dir = PROJECTS_DIR / name
    if project_dir.exists():
        print(f"Project already exists: {project_dir}", file=sys.stderr)
        sys.exit(1)

    # Create directory structure
    for subdir in ["scripts", "characters", "outlines"]:
        (project_dir / subdir).mkdir(parents=True, exist_ok=True)

    project_data = {
        "name": name,
        "title": args.title or name.replace("_", " ").title(),
        "genre": args.genre or "",
        "logline": args.logline or "",
        "author": args.author or "",
        "status": "development",
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
    }

    project_file = project_dir / "project.json"
    project_file.write_text(json.dumps(project_data, indent=2))

    print(f"Created project: {name}")
    print(f"  Directory: {project_dir}")
    print(f"  Title: {project_data['title']}")
    if project_data["genre"]:
        print(f"  Genre: {project_data['genre']}")
    if project_data["logline"]:
        print(f"  Logline: {project_data['logline']}")
    print(f"\nSubdirectories: scripts/, characters/, outlines/")
    print(f"\nEdit project details: {project_file}")


def _project_show(args):
    data = load_project(args.name)
    project_dir = PROJECTS_DIR / args.name

    print(f"\n{'='*60}")
    print(f"  {data.get('title', args.name).upper()}")
    print(f"{'='*60}")
    print(f"  Name:    {data.get('name', args.name)}")
    print(f"  Genre:   {data.get('genre') or '(not set)'}")
    print(f"  Status:  {data.get('status', 'development')}")
    print(f"  Author:  {data.get('author') or '(not set)'}")
    created = data.get("created_at", "")
    if created:
        try:
            dt = datetime.fromisoformat(created)
            created = dt.strftime("%B %d, %Y")
        except ValueError:
            pass
    print(f"  Created: {created or '(unknown)'}")

    logline = data.get("logline", "")
    if logline:
        print(f"\n  LOGLINE")
        print(f"  {logline}")

    print(f"\n  FILES")
    for subdir in ["scripts", "characters", "outlines"]:
        d = project_dir / subdir
        if d.exists():
            files = list(d.iterdir())
            files = [f for f in files if not f.name.startswith(".")]
            if files:
                print(f"  {subdir}/")
                for f in sorted(files):
                    print(f"    {f.name}")
            else:
                print(f"  {subdir}/  (empty)")

    print(f"{'='*60}\n")


def _project_list(args):
    if not PROJECTS_DIR.exists() or not any(PROJECTS_DIR.iterdir()):
        print("No projects found. Create one with: aistudios project new <name>")
        return

    projects = []
    for p in sorted(PROJECTS_DIR.iterdir()):
        if p.is_dir() and (p / "project.json").exists():
            try:
                data = json.loads((p / "project.json").read_text())
                projects.append(data)
            except (json.JSONDecodeError, OSError):
                pass

    if not projects:
        print("No projects found.")
        return

    print(f"\n{'Project':<24} {'Genre':<14} {'Status':<16} {'Title'}")
    print("-" * 70)
    for d in projects:
        name = d.get("name", "?")[:22]
        genre = (d.get("genre") or "-")[:12]
        status = (d.get("status") or "-")[:14]
        title = d.get("title", "")[:30]
        print(f"  {name:<22} {genre:<14} {status:<16} {title}")
    print()


def cmd_preview(args):
    """Generate a self-contained HTML preview of a project or all projects."""
    if args.project:
        _preview_project(args.project, args)
    else:
        _preview_dashboard(args)


def _preview_project(project_name: str, args):
    data = load_project(project_name)
    project_dir = PROJECTS_DIR / project_name

    # Collect characters
    characters = []
    char_dir = project_dir / "characters"
    if char_dir.exists():
        for f in sorted(char_dir.glob("*.md")):
            characters.append({"filename": f.name, "content": f.read_text()})

    # Collect outlines / beats
    outlines = []
    outline_dir = project_dir / "outlines"
    if outline_dir.exists():
        for f in sorted(outline_dir.glob("*.md")):
            outlines.append({"filename": f.name, "content": f.read_text()})
    # Also check global outlines
    title_slug = data.get("title", project_name).lower().replace(" ", "_")
    for f in sorted(OUTLINES_DIR.glob("*.md")):
        if title_slug in f.name.lower() or project_name.lower() in f.name.lower():
            if not any(o["filename"] == f.name for o in outlines):
                outlines.append({"filename": f.name, "content": f.read_text()})

    # Collect scripts
    scripts = []
    script_dir = project_dir / "scripts"
    if script_dir.exists():
        for f in sorted(script_dir.glob("*.fountain")):
            scripts.append({"filename": f.name, "content": f.read_text()})
    # Also check global scripts
    for f in sorted(SCRIPTS_DIR.glob("*.fountain")):
        if title_slug in f.name.lower() or project_name.lower() in f.name.lower():
            if not any(s["filename"] == f.name for s in scripts):
                scripts.append({"filename": f.name, "content": f.read_text()})

    embed_data = {
        "project": data,
        "characters": characters,
        "outlines": outlines,
        "scripts": scripts,
    }

    html = _build_project_html(embed_data)
    out_path = project_dir / "preview.html"
    out_path.write_text(html)
    print(f"Preview written to: {out_path}")


def _preview_dashboard(args):
    projects = []
    if PROJECTS_DIR.exists():
        for p in sorted(PROJECTS_DIR.iterdir()):
            if p.is_dir() and (p / "project.json").exists():
                try:
                    d = json.loads((p / "project.json").read_text())
                    projects.append(d)
                except (json.JSONDecodeError, OSError):
                    pass

    html = _build_dashboard_html(projects)
    out_path = Path("preview.html")
    out_path.write_text(html)
    print(f"Dashboard written to: {out_path}")


def _build_project_html(embed_data: dict) -> str:
    data_json = json.dumps(embed_data, ensure_ascii=False, indent=2)
    project = embed_data["project"]
    title = project.get("title", "Untitled")
    genre = project.get("genre", "")
    status = project.get("status", "development")
    logline = project.get("logline", "")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_esc(title)} — AiStudios</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Helvetica Neue', Arial, sans-serif; background: #f4f4f0; color: #1a1a1a; display: flex; min-height: 100vh; }}

  /* Sidebar */
  #sidebar {{ width: 240px; min-width: 240px; background: #1a1a2e; color: #c8c8d8; display: flex; flex-direction: column; padding: 24px 0; position: sticky; top: 0; height: 100vh; overflow-y: auto; }}
  #sidebar .brand {{ padding: 0 20px 20px; border-bottom: 1px solid #2a2a4a; margin-bottom: 16px; }}
  #sidebar .brand h1 {{ font-size: 13px; font-weight: 700; letter-spacing: 0.1em; text-transform: uppercase; color: #7878a8; margin-bottom: 6px; }}
  #sidebar .brand .project-title {{ font-size: 16px; font-weight: 600; color: #e8e8f0; line-height: 1.3; }}
  .genre-badge {{ display: inline-block; background: #2d2d5e; color: #9898c8; font-size: 11px; font-weight: 600; letter-spacing: 0.08em; text-transform: uppercase; padding: 3px 8px; border-radius: 4px; margin-top: 6px; }}
  .status-badge {{ display: inline-block; background: #1e3a2e; color: #5aaa7a; font-size: 11px; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; padding: 3px 8px; border-radius: 4px; margin-top: 4px; margin-left: 4px; }}
  #sidebar nav {{ padding: 0 12px; flex: 1; }}
  #sidebar nav a {{ display: block; color: #8888aa; text-decoration: none; padding: 9px 12px; border-radius: 6px; font-size: 14px; font-weight: 500; margin-bottom: 2px; transition: background 0.15s, color 0.15s; }}
  #sidebar nav a:hover, #sidebar nav a.active {{ background: #2a2a4e; color: #d8d8f0; }}
  #sidebar nav .section-label {{ font-size: 10px; font-weight: 700; letter-spacing: 0.12em; text-transform: uppercase; color: #4a4a6a; padding: 14px 12px 4px; }}

  /* Content */
  #content {{ flex: 1; padding: 40px 48px; max-width: 900px; }}
  section {{ margin-bottom: 56px; }}
  section h2 {{ font-size: 22px; font-weight: 700; color: #1a1a2e; margin-bottom: 20px; padding-bottom: 10px; border-bottom: 2px solid #e0e0d8; }}

  /* Logline card */
  .logline-card {{ background: #1a1a2e; color: #e8e8f0; border-radius: 12px; padding: 28px 32px; font-size: 17px; line-height: 1.65; font-style: italic; }}
  .logline-card:empty::after {{ content: "No logline set."; color: #5a5a7a; font-style: normal; }}

  /* Beat sheet */
  .beat {{ background: #fff; border-radius: 8px; padding: 18px 22px; margin-bottom: 12px; border-left: 4px solid #1a1a2e; }}
  .beat h3 {{ font-size: 14px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em; color: #1a1a2e; margin-bottom: 6px; }}
  .beat p {{ font-size: 14px; line-height: 1.6; color: #333; }}

  /* Characters */
  .character-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 16px; }}
  .character-card {{ background: #fff; border-radius: 10px; padding: 20px; border-top: 4px solid #1a1a2e; }}
  .character-card h3 {{ font-size: 16px; font-weight: 700; color: #1a1a2e; margin-bottom: 6px; }}
  .character-card .role {{ font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.08em; color: #7878a8; margin-bottom: 12px; }}
  .character-card .char-body {{ font-size: 13px; line-height: 1.55; color: #444; white-space: pre-wrap; }}

  /* Script viewer */
  .script-file {{ margin-bottom: 32px; }}
  .script-file h3 {{ font-size: 14px; font-weight: 600; color: #7878a8; margin-bottom: 12px; text-transform: uppercase; letter-spacing: 0.08em; }}
  .screenplay {{ font-family: 'Courier New', Courier, monospace; font-size: 13px; line-height: 1.7; background: #fff; padding: 40px 48px; border-radius: 8px; white-space: pre-wrap; color: #1a1a1a; }}
  .scene-heading {{ font-weight: bold; text-transform: uppercase; }}
  .character-cue {{ text-align: center; font-weight: bold; text-transform: uppercase; }}
  .parenthetical {{ text-align: center; margin-left: 60px; margin-right: 60px; color: #444; }}
  .dialogue-block {{ margin-left: 80px; margin-right: 80px; }}

  /* Outline */
  .outline-content {{ background: #fff; border-radius: 8px; padding: 28px 32px; font-size: 15px; line-height: 1.7; color: #222; white-space: pre-wrap; }}

  /* Responsive */
  @media (max-width: 700px) {{
    body {{ flex-direction: column; }}
    #sidebar {{ width: 100%; height: auto; position: static; }}
    #content {{ padding: 24px 20px; }}
    .character-grid {{ grid-template-columns: 1fr; }}
    .screenplay {{ padding: 20px; }}
  }}
</style>
</head>
<body>
<div id="sidebar">
  <div class="brand">
    <h1>AiStudios</h1>
    <div class="project-title">{_esc(title)}</div>
    {'<span class="genre-badge">' + _esc(genre) + '</span>' if genre else ''}
    {'<span class="status-badge">' + _esc(status) + '</span>' if status else ''}
  </div>
  <nav>
    <div class="section-label">Sections</div>
    <a href="#logline">Logline</a>
    <a href="#beats">Beat Sheet</a>
    <a href="#characters">Characters</a>
    <a href="#outlines">Outlines</a>
    <a href="#script">Script</a>
  </nav>
</div>
<div id="content">
  <section id="logline">
    <h2>Logline</h2>
    <div class="logline-card">{_esc(logline)}</div>
  </section>

  <section id="beats">
    <h2>Beat Sheet</h2>
    <div id="beats-content"></div>
  </section>

  <section id="characters">
    <h2>Characters</h2>
    <div id="characters-content" class="character-grid"></div>
  </section>

  <section id="outlines">
    <h2>Outlines</h2>
    <div id="outlines-content"></div>
  </section>

  <section id="script">
    <h2>Script</h2>
    <div id="script-content"></div>
  </section>
</div>

<script>
var AISTUDIOS_DATA = {data_json};

function esc(s) {{
  return (s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}}

function renderBeats() {{
  var outlines = AISTUDIOS_DATA.outlines || [];
  var beatsFile = null;
  for (var i = 0; i < outlines.length; i++) {{
    if (outlines[i].filename.toLowerCase().indexOf('beat') !== -1) {{
      beatsFile = outlines[i];
      break;
    }}
  }}
  var el = document.getElementById('beats-content');
  if (!beatsFile) {{
    el.innerHTML = '<p style="color:#888;">No beat sheet found. Run: aistudios beats</p>';
    return;
  }}
  var lines = beatsFile.content.split('\\n');
  var html = '';
  var currentBeat = null;
  var currentText = [];
  for (var i = 0; i < lines.length; i++) {{
    var line = lines[i];
    if (line.match(/^##\\s/)) {{
      if (currentBeat) {{
        html += '<div class="beat"><h3>' + esc(currentBeat) + '</h3><p>' + esc(currentText.join(' ').trim()) + '</p></div>';
      }}
      currentBeat = line.replace(/^##\\s+/, '').trim();
      currentText = [];
    }} else if (currentBeat && line.trim()) {{
      currentText.push(line.trim());
    }}
  }}
  if (currentBeat) {{
    html += '<div class="beat"><h3>' + esc(currentBeat) + '</h3><p>' + esc(currentText.join(' ').trim()) + '</p></div>';
  }}
  el.innerHTML = html || '<p style="color:#888;">Beat sheet found but could not be parsed. View the raw file.</p>';
}}

function renderCharacters() {{
  var chars = AISTUDIOS_DATA.characters || [];
  var el = document.getElementById('characters-content');
  if (!chars.length) {{
    el.innerHTML = '<p style="color:#888;">No character files found. Run: aistudios character</p>';
    return;
  }}
  var html = '';
  for (var i = 0; i < chars.length; i++) {{
    var c = chars[i];
    var lines = c.content.split('\\n');
    var name = lines[0].replace(/^#+\\s*Character:\\s*/i,'').replace(/^#+\\s*/,'').trim() || c.filename;
    var role = '';
    var body = c.content;
    for (var j = 0; j < lines.length; j++) {{
      var l = lines[j].toLowerCase();
      if (l.indexOf('role') !== -1 && lines[j+1]) {{
        role = lines[j+1].trim();
        break;
      }}
    }}
    html += '<div class="character-card"><h3>' + esc(name) + '</h3>';
    if (role) html += '<div class="role">' + esc(role) + '</div>';
    var bodyTrimmed = body.replace(/^#.*\\n/, '').trim().substring(0, 400);
    html += '<div class="char-body">' + esc(bodyTrimmed) + (body.length > 400 ? '\\n\\n[...]' : '') + '</div></div>';
  }}
  el.innerHTML = html;
}}

function renderOutlines() {{
  var outlines = AISTUDIOS_DATA.outlines || [];
  var el = document.getElementById('outlines-content');
  var nonBeats = outlines.filter(function(o) {{ return o.filename.toLowerCase().indexOf('beat') === -1; }});
  if (!nonBeats.length) {{
    el.innerHTML = '<p style="color:#888;">No outline files found. Run: aistudios outline</p>';
    return;
  }}
  var html = '';
  for (var i = 0; i < nonBeats.length; i++) {{
    var o = nonBeats[i];
    html += '<div class="script-file"><h3>' + esc(o.filename) + '</h3>';
    html += '<div class="outline-content">' + esc(o.content) + '</div></div>';
  }}
  el.innerHTML = html;
}}

function renderScripts() {{
  var scripts = AISTUDIOS_DATA.scripts || [];
  var el = document.getElementById('script-content');
  if (!scripts.length) {{
    el.innerHTML = '<p style="color:#888;">No script files found. Run: aistudios scene or aistudios new</p>';
    return;
  }}
  var html = '';
  for (var i = 0; i < scripts.length; i++) {{
    var s = scripts[i];
    html += '<div class="script-file"><h3>' + esc(s.filename) + '</h3>';
    html += '<div class="screenplay">' + esc(s.content) + '</div></div>';
  }}
  el.innerHTML = html;
}}

document.addEventListener('DOMContentLoaded', function() {{
  renderBeats();
  renderCharacters();
  renderOutlines();
  renderScripts();

  // Sidebar active state
  var links = document.querySelectorAll('#sidebar nav a');
  function setActive() {{
    var scrollY = window.scrollY;
    links.forEach(function(link) {{
      var target = document.querySelector(link.getAttribute('href'));
      if (target) {{
        var top = target.offsetTop - 60;
        var bottom = top + target.offsetHeight;
        if (scrollY >= top && scrollY < bottom) {{
          link.classList.add('active');
        }} else {{
          link.classList.remove('active');
        }}
      }}
    }});
  }}
  window.addEventListener('scroll', setActive);
  setActive();
}});
</script>
</body>
</html>"""


def _build_dashboard_html(projects: list) -> str:
    data_json = json.dumps({"projects": projects}, ensure_ascii=False, indent=2)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AiStudios — Projects Dashboard</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Helvetica Neue', Arial, sans-serif; background: #f4f4f0; color: #1a1a1a; min-height: 100vh; }}
  header {{ background: #1a1a2e; color: #e8e8f0; padding: 32px 48px; }}
  header h1 {{ font-size: 28px; font-weight: 700; letter-spacing: -0.02em; }}
  header p {{ color: #8888aa; margin-top: 6px; font-size: 15px; }}
  main {{ max-width: 1100px; margin: 0 auto; padding: 40px 48px; }}
  main h2 {{ font-size: 18px; font-weight: 700; color: #1a1a2e; margin-bottom: 24px; }}
  .project-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 20px; }}
  .project-card {{ background: #fff; border-radius: 12px; padding: 24px; border-top: 5px solid #1a1a2e; box-shadow: 0 2px 8px rgba(0,0,0,0.06); }}
  .project-card h3 {{ font-size: 18px; font-weight: 700; color: #1a1a2e; margin-bottom: 8px; }}
  .badges {{ display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 14px; }}
  .badge {{ font-size: 11px; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; padding: 3px 8px; border-radius: 4px; }}
  .badge-genre {{ background: #ebe8f8; color: #5a3aaa; }}
  .badge-status {{ background: #e8f4ec; color: #2a6a3a; }}
  .logline {{ font-size: 14px; line-height: 1.6; color: #444; font-style: italic; margin-top: 8px; }}
  .no-logline {{ color: #aaa; font-size: 13px; }}
  .created {{ font-size: 12px; color: #aaa; margin-top: 14px; }}
  .empty-state {{ text-align: center; padding: 80px 40px; color: #888; }}
  .empty-state h3 {{ font-size: 20px; margin-bottom: 10px; color: #555; }}
  @media (max-width: 600px) {{
    header, main {{ padding: 24px 20px; }}
    .project-grid {{ grid-template-columns: 1fr; }}
  }}
</style>
</head>
<body>
<header>
  <h1>AiStudios</h1>
  <p>Film Script Development Toolkit</p>
</header>
<main>
  <h2>Projects</h2>
  <div id="projects-grid" class="project-grid"></div>
</main>

<script>
var DASHBOARD_DATA = {data_json};

function esc(s) {{
  return (s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}}

function formatDate(iso) {{
  if (!iso) return '';
  try {{
    var d = new Date(iso);
    return d.toLocaleDateString('en-US', {{ year: 'numeric', month: 'long', day: 'numeric' }});
  }} catch(e) {{ return iso; }}
}}

document.addEventListener('DOMContentLoaded', function() {{
  var projects = DASHBOARD_DATA.projects || [];
  var grid = document.getElementById('projects-grid');

  if (!projects.length) {{
    grid.innerHTML = '<div class="empty-state"><h3>No projects yet</h3><p>Create one with: python aistudios.py project new &lt;name&gt;</p></div>';
    return;
  }}

  var html = '';
  for (var i = 0; i < projects.length; i++) {{
    var p = projects[i];
    var genre = p.genre || '';
    var status = p.status || 'development';
    var logline = p.logline || '';
    var created = formatDate(p.created_at);
    html += '<div class="project-card">';
    html += '<h3>' + esc(p.title || p.name) + '</h3>';
    html += '<div class="badges">';
    if (genre) html += '<span class="badge badge-genre">' + esc(genre) + '</span>';
    html += '<span class="badge badge-status">' + esc(status) + '</span>';
    html += '</div>';
    if (logline) {{
      html += '<div class="logline">' + esc(logline) + '</div>';
    }} else {{
      html += '<div class="no-logline">No logline yet</div>';
    }}
    if (created) html += '<div class="created">Created ' + esc(created) + '</div>';
    html += '</div>';
  }}
  grid.innerHTML = html;
}});
</script>
</body>
</html>"""


def _esc(s: str) -> str:
    """HTML-escape a string for safe embedding."""
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

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
    p.add_argument("--project", help="Load context from project")
    p.set_defaults(func=cmd_scene)

    # outline
    p = sub.add_parser("outline", help="Generate a story outline")
    p.add_argument("title", help="Film title")
    p.add_argument("concept", help="One-paragraph concept/logline")
    p.add_argument("--genre", help="Genre")
    p.add_argument("--length", help="Target runtime in minutes", default="90-100")
    p.add_argument("--output", help="Output filename in outlines/")
    p.add_argument("--project", help="Load context from project")
    p.set_defaults(func=cmd_outline)

    # character
    p = sub.add_parser("character", help="Develop a character profile")
    p.add_argument("description", help="Brief description of the character")
    p.add_argument("--name", help="Character name")
    p.add_argument("--role", help="Role in story (protagonist, antagonist, etc.)")
    p.add_argument("--genre", help="Film genre")
    p.add_argument("--output", help="Output filename in characters/")
    p.add_argument("--project", help="Load context from project")
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

    # beats
    p = sub.add_parser("beats", help="Generate a Save the Cat beat sheet")
    p.add_argument("title", help="Film title")
    p.add_argument("concept", help="The story concept or logline")
    p.add_argument("--genre", help="Genre (loads genre-specific beat guidance)")
    p.add_argument("--project", help="Load context from project")
    p.add_argument("--output", help="Output filename in outlines/")
    p.set_defaults(func=cmd_beats)

    # notes
    p = sub.add_parser("notes", help="Script coverage / professional feedback")
    p.add_argument("--file", help="Path to a .fountain script file")
    p.add_argument("--project", help="Read all scripts from a project")
    p.add_argument("--focus", help="Focus area: dialogue, pacing, character, structure")
    p.set_defaults(func=cmd_notes)

    # logline
    p = sub.add_parser("logline", help="Generate or refine a logline")
    p.add_argument("concept", help="The rough idea or existing logline to work from")
    p.add_argument("--genre", help="Genre")
    p.add_argument("--project", help="Load context from project")
    p.add_argument("--variations", type=int, default=3, help="Number of variations (default: 3)")
    p.set_defaults(func=cmd_logline)

    # project
    p = sub.add_parser("project", help="Project management (new / show / list)")
    project_sub = p.add_subparsers(dest="subcommand", required=True)

    pn = project_sub.add_parser("new", help="Create a new project")
    pn.add_argument("name", help="Project name (slug, no spaces)")
    pn.add_argument("--title", help="Full project title")
    pn.add_argument("--genre", help="Genre")
    pn.add_argument("--logline", help="Logline")
    pn.add_argument("--author", help="Author name")
    pn.set_defaults(func=cmd_project)

    ps = project_sub.add_parser("show", help="Show project details")
    ps.add_argument("name", help="Project name")
    ps.set_defaults(func=cmd_project)

    pl = project_sub.add_parser("list", help="List all projects")
    pl.set_defaults(func=cmd_project)

    p.set_defaults(func=cmd_project)

    # preview
    p = sub.add_parser("preview", help="Generate self-contained HTML preview")
    p.add_argument("--project", help="Project to preview (omit for all-projects dashboard)")
    p.set_defaults(func=cmd_preview)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
