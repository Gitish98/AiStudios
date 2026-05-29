# AiStudios

Developing film scripts in the modern world — using AI as a creative collaborator.

AiStudios is a lightweight toolkit for writing, structuring, and producing screenplays with AI assistance. Designed to work from any device, including mobile.

---

## What's here

```
aistudios.py        — Main CLI tool (all AI commands live here)
scripts/            — Your screenplay files (.fountain format)
outlines/           — Story outlines and beat sheets
characters/         — Character profiles
prompts/            — System prompts that shape AI behavior (edit to tune the AI's voice)
genres/             — Genre-specific craft guides and beat patterns
projects/           — Project directories (JSON + scripts + characters + outlines)
requirements.txt    — Python dependencies
```

---

## Setup

```bash
pip install anthropic
export ANTHROPIC_API_KEY=your_key_here
```

That's it. No build step. No server. Just Python.

---

## Commands

### Start a new script

```bash
python aistudios.py new my_film --title "The Long Way Home" --author "Your Name"
```

Creates `scripts/my_film.fountain` with a proper Fountain header.

---

### Generate a scene

```bash
python aistudios.py scene "Two estranged brothers meet at their father's funeral and argue over the will" \
  --characters "DAVID (older, controlled), JAKE (younger, angry)" \
  --tone "tense, restrained" \
  --output my_film.fountain
```

Generates a scene in [Fountain format](https://fountain.io) and optionally appends it to your script.

Use `--project` to automatically inject the project's title, genre, and logline as context:

```bash
python aistudios.py scene "The heist goes wrong" --project my_project
```

---

### Build a story outline

```bash
python aistudios.py outline "The Long Way Home" \
  "A burnt-out ER nurse discovers her recently deceased mother had a secret second family across town" \
  --genre drama \
  --length 100
```

Saves a structured outline to `outlines/`.

---

### Generate a beat sheet

```bash
python aistudios.py beats "The Long Way Home" \
  "A burnt-out ER nurse discovers her deceased mother had a secret second family" \
  --genre drama \
  --output long_way_home_beats.md
```

Generates all 15 Save the Cat beats (Opening Image through Final Image) specific to your story. When `--genre` is provided, genre-specific structural guidance from `genres/<genre>/beats.txt` informs the output.

```bash
# Load title/logline/genre automatically from a project:
python aistudios.py beats "Untitled" "placeholder" --project my_project
```

---

### Script coverage (notes)

```bash
python aistudios.py notes --file scripts/my_film.fountain
```

Generates professional script coverage with an overall grade (Consider / Recommend / Pass), premise notes, structure notes, character notes, dialogue notes, and specific scene feedback.

```bash
# Focus on a specific area:
python aistudios.py notes --file scripts/my_film.fountain --focus "dialogue"

# Cover all scripts in a project:
python aistudios.py notes --project my_project
```

---

### Generate a logline

```bash
python aistudios.py logline \
  "A corporate lawyer discovers she's been defending a client she knows is guilty of her sister's murder" \
  --genre thriller \
  --variations 4
```

Generates N logline variations, each with a brief note on its angle and emphasis. Default is 3 variations.

```bash
# Refine a logline using existing project context:
python aistudios.py logline "current logline draft" --project my_project
```

---

### Develop a character

```bash
python aistudios.py character "A mid-50s ex-cop who became a florist after a shooting incident he won't talk about" \
  --name "Ray Okafor" \
  --role protagonist \
  --genre thriller
```

Saves a full character profile to `characters/`.

---

### Rewrite a scene

```bash
python aistudios.py rewrite --file scripts/scene_01.fountain \
  "Make the dialogue less on-the-nose. The characters should talk around the real issue."
```

---

### Generate dialogue

```bash
python aistudios.py dialogue \
  "A wife tells her husband she's leaving, but they're at a dinner party and can't make a scene" \
  --characters "CLAIRE (composed, decisive), NOEL (oblivious until he isn't)" \
  --subtext "She's been planning this for months. He thinks everything is fine."
```

---

### Project management

Projects collect your title, genre, logline, and script files in one place. Other commands use `--project` to load this context automatically.

**Create a project:**

```bash
python aistudios.py project new my_thriller \
  --title "Glass Jaw" \
  --genre thriller \
  --logline "When a forensic accountant uncovers evidence her firm has been laundering money for a cartel, she has 48 hours to expose them before they expose her." \
  --author "Your Name"
```

Creates `projects/my_thriller/` with `project.json` and subdirectories (`scripts/`, `characters/`, `outlines/`).

**Show a project:**

```bash
python aistudios.py project show my_thriller
```

Displays title, genre, logline, status, and all files in the project.

**List all projects:**

```bash
python aistudios.py project list
```

---

### HTML preview

Generate a self-contained HTML file for reviewing your project:

```bash
# Single project preview (writes to projects/my_thriller/preview.html):
python aistudios.py preview --project my_thriller

# Dashboard of all projects (writes to preview.html):
python aistudios.py preview
```

The HTML is fully self-contained — no external dependencies, no CDN, no server required. Open in any browser, including Safari on iPhone. It includes:

- Logline card
- Beat sheet (parsed from outlines)
- Character cards
- Outline viewer
- Script viewer in screenplay format

---

## Genre templates

The `genres/` directory contains craft guides and Save the Cat beat patterns for each genre. These are automatically loaded when you pass `--genre` to `beats`, `outline`, or `logline`.

Available genres: `thriller`, `drama`, `horror`, `comedy`, `romance`, `sci-fi`, `action`

Each genre has:
- `prompt.txt` — structural conventions, tone, pacing, pitfalls, craft moves
- `beats.txt` — how the 15 Save the Cat beats manifest in this genre specifically

---

## Fountain Format

Scripts are saved in [Fountain](https://fountain.io) — an open plain-text format for screenplays. It's readable as-is and can be converted to industry-standard PDFs using:

- [Fade In](https://www.fadeinpro.com/) (Mac/iOS)
- [Highland 2](https://quoteunquoteapps.com/highland-2/) (Mac/iOS)
- [Afterwriting](https://afterwriting.com/) (free, browser-based)
- [Fountain.io](https://fountain.io/apps) — full list of apps

---

## Customizing AI behavior

The `prompts/` directory contains system prompts that shape how the AI writes. Edit them to change the AI's voice, style, or priorities:

- `prompts/scene.txt` — how scenes are written
- `prompts/outline.txt` — how outlines are structured
- `prompts/character.txt` — how characters are developed

---

## Working from iPhone

This project is designed to be picked up from any device. Via Claude Code:

- Browse and edit files directly in the session
- Run commands in the terminal
- Continue mid-project across sessions

The only external dependency is your Anthropic API key set as an environment variable.
